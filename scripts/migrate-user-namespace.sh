#!/usr/bin/env bash
# scripts/migrate-user-namespace.sh — move an existing workspace from the shared
# `coder` namespace into its own per-user namespace `ws-<user>` (issue #103).
#
# Usage:
#   scripts/migrate-user-namespace.sh <user> [flags]
#     --dry-run         print every action, touch nothing
#     --src-namespace N  source namespace (default: coder)
#     --cutover         after the copy, repoint the user's values.yaml at
#                       ws-<user> and `make deploy` into it (+verify)
#     --decommission    after cutover, delete the OLD coder release + PVC
#                       (destructive; implies --cutover)
#
# Phases (each builds on the previous; pick how far to go):
#   COPY (always): preflight; scale source to 0; create+label ws-<user> ns +
#     copy regcred; create the new ws-<user>-home PVC (same size); tar-pipe the
#     home volume across namespaces (PVCs are namespace-scoped and cannot be
#     moved, so the data is streamed between two helper pods). Fully reversible.
#   CUTOVER (--cutover): set `namespace: ws-<user>` in the user's values.yaml
#     and `make deploy USER=<user>` (installs base-infra into ws-<user>, rolls
#     the pod onto the migrated PVC), then verify /home/dev. The old copy is
#     left scaled-to-0 in the source namespace for rollback.
#   DECOMMISSION (--decommission): reclaim the old copy (helm uninstall + delete
#     the old PVC). This is the only destructive step.
#
# Without --cutover the source is left intact and stopped, so COPY alone is
# safe to run ahead of time and cut over later.
#
# See docs/PER_WORKSPACE_NAMESPACE_MIGRATION.md for the full runbook, and
# `make migrate-user` / `make migrate-all` for the convenience wrappers.
set -euo pipefail

USER_SLUG=""
SRC_NS="coder"
DRY_RUN=0
CUTOVER=0        # after the copy: repoint values.yaml + `make deploy` into ws-<user>
DECOMMISSION=0   # after cutover: delete the OLD coder release + PVC (destructive; implies --cutover)
HELPER_IMAGE="${MIGRATION_HELPER_IMAGE:-busybox:1.36}"

die() { echo "ERROR: $*" >&2; exit 1; }
run() { if [ "$DRY_RUN" = 1 ]; then echo "DRY-RUN> $*"; else echo "+ $*"; "$@"; fi; }

while [ $# -gt 0 ]; do
  case "$1" in
    --src-namespace) SRC_NS="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --cutover) CUTOVER=1; shift ;;
    --decommission) DECOMMISSION=1; CUTOVER=1; shift ;;
    -h|--help) sed -n '2,31p' "$0"; exit 0 ;;
    -*) die "unknown flag: $1" ;;
    *) [ -z "$USER_SLUG" ] && USER_SLUG="$1" && shift || die "unexpected arg: $1" ;;
  esac
done

[ -n "$USER_SLUG" ] || die "usage: migrate-user-namespace.sh <user> [--src-namespace coder] [--cutover] [--decommission] [--dry-run]"

WS="ws-${USER_SLUG}"
DST_NS="ws-${USER_SLUG}"
PVC="ws-${USER_SLUG}-home"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Resolve the user's values.yaml the same way the Makefile does (first match
# wins): committed deployments/, private users-private/, then the GitOps
# checkout .users/. Only needed for --cutover (repoint the namespace field).
resolve_values_file() {
  for d in "$ROOT/deployments/$1" "$ROOT/users-private/$1" "$ROOT/.users/users-private/$1"; do
    [ -f "$d/values.yaml" ] && { echo "$d/values.yaml"; return 0; }
  done
  return 1
}

echo "=== migrate $WS : $SRC_NS -> $DST_NS (dry-run=$DRY_RUN cutover=$CUTOVER decommission=$DECOMMISSION) ==="

# 1. Preflight.
kubectl get deployment "$WS" -n "$SRC_NS" >/dev/null 2>&1 \
  || die "workspace '$WS' not found in namespace '$SRC_NS' (already migrated?)"
kubectl get pvc "$PVC" -n "$SRC_NS" >/dev/null 2>&1 \
  || die "home PVC '$PVC' not found in namespace '$SRC_NS'"
if kubectl get deployment "$WS" -n "$DST_NS" >/dev/null 2>&1; then
  die "'$WS' already exists in target namespace '$DST_NS' — resolve by hand"
fi

SIZE="$(kubectl get pvc "$PVC" -n "$SRC_NS" \
  -o jsonpath='{.spec.resources.requests.storage}')"
echo "home volume size: $SIZE"

# 2. Quiesce the source so the volume isn't written during the copy.
echo "--- scaling source workspace to 0 (quiesce home volume) ---"
run kubectl scale deployment "$WS" -n "$SRC_NS" --replicas=0
run kubectl wait --for=delete pod -l "app=$WS" -n "$SRC_NS" --timeout=120s || true

# 3. Target namespace + regcred.
echo "--- ensuring target namespace + regcred ---"
run "$ROOT/scripts/ensure-workspace-namespace.sh" "$DST_NS" "$USER_SLUG" "$SRC_NS"

# 4. New PVC in the target namespace (same size).
echo "--- creating home PVC in $DST_NS ---"
if kubectl get pvc "$PVC" -n "$DST_NS" >/dev/null 2>&1; then
  echo "PVC $PVC already exists in $DST_NS — reusing"
elif [ "$DRY_RUN" = 1 ]; then
  echo "DRY-RUN> kubectl apply -f - (PersistentVolumeClaim $PVC, $SIZE, in $DST_NS)"
else
  # The workspace chart templates this PVC, so the cutover's `helm install`
  # must ADOPT the pre-created one — Helm only does that when these ownership
  # labels/annotations are present (else: "invalid ownership metadata").
  kubectl apply -f - <<YAML
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${PVC}
  namespace: ${DST_NS}
  labels:
    app: ${WS}
    app.kubernetes.io/managed-by: Helm
  annotations:
    meta.helm.sh/release-name: ${USER_SLUG}-workspace
    meta.helm.sh/release-namespace: ${DST_NS}
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: ${SIZE}
YAML
fi

# 5. Copy the home volume across namespaces.
#
# The data streams pod-to-pod over the CLUSTER network (src helper -> dst helper
# via netcat), not through a local `kubectl exec | kubectl exec` pipe. The old
# tar-pipe routed every byte through this laptop's two long-lived exec streams,
# so a single blip between here and the remote API server killed the transfer —
# fine for a 5Gi/20Gi volume, but a 50Gi copy reliably dropped with a websocket
# 1006 close. Here the laptop only creates the pods and polls for completion; the
# bytes never leave the cluster, so the copy no longer depends on local
# connection stability. The dst helper carries the migration label (not the
# workspace app= label), so the workspace NetworkPolicy doesn't select it and
# cross-namespace ingress from the src helper is allowed.
echo "--- copying home volume ($SRC_NS -> $DST_NS, in-cluster pod-to-pod) ---"
SRC_POD="migrate-src-${USER_SLUG}"
DST_POD="migrate-dst-${USER_SLUG}"
COPY_PORT="${MIGRATION_COPY_PORT:-2000}"

# dst: listen once, extract into the volume. The `&&` gates on tar's exit, so the
# pod only reaches Succeeded when extraction actually completed (a truncated
# stream fails tar -> pod Failed, never a silent partial copy).
dst_pod() {
  cat <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: ${DST_POD}
  namespace: ${DST_NS}
  labels: { app: kube-coder-migration }
spec:
  restartPolicy: Never
  containers:
  - name: helper
    image: ${HELPER_IMAGE}
    command: ["sh", "-c", "nc -l -p ${COPY_PORT} | tar -C /home/dev -xf - && echo COPY_RECV_OK"]
    volumeMounts:
    - { name: home, mountPath: /home/dev }
  volumes:
  - name: home
    persistentVolumeClaim: { claimName: ${PVC} }
YAML
}

# src: connect to the dst pod IP and stream the volume. pipefail makes a tar
# read error (not just nc's exit) fail the pod. A short connect-retry covers the
# race between "dst pod Running" and "nc actually listening".
src_pod() {  # dst_ip
  cat <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: ${SRC_POD}
  namespace: ${SRC_NS}
  labels: { app: kube-coder-migration }
spec:
  restartPolicy: Never
  containers:
  - name: helper
    image: ${HELPER_IMAGE}
    command: ["sh", "-c", "set -o pipefail; i=0; while [ \$i -lt 30 ]; do tar -C /home/dev -cf - . | nc $1 ${COPY_PORT} && exit 0; i=\$((i+1)); echo waiting-for-receiver; sleep 2; done; echo connect-failed >&2; exit 1"]
    volumeMounts:
    - { name: home, mountPath: /home/dev }
  volumes:
  - name: home
    persistentVolumeClaim: { claimName: ${PVC} }
YAML
}

wait_pod_done() {  # name namespace  -> prints phase, returns 0 if Succeeded
  local name="$1" ns="$2" phase=""
  kubectl wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$name" -n "$ns" --timeout=3600s >/dev/null 2>&1 && { echo Succeeded; return 0; }
  phase="$(kubectl get pod "$name" -n "$ns" -o jsonpath='{.status.phase}' 2>/dev/null)"
  echo "${phase:-Unknown}"
  [ "$phase" = "Succeeded" ]
}

if [ "$DRY_RUN" = 1 ]; then
  echo "DRY-RUN> start dst helper $DST_POD in $DST_NS (nc -l -p $COPY_PORT | tar -x)"
  echo "DRY-RUN> start src helper $SRC_POD in $SRC_NS (tar -cf - . | nc <dst-ip> $COPY_PORT)"
  echo "DRY-RUN> wait for both pods to reach Succeeded, then delete them"
else
  dst_pod | kubectl apply -f -
  kubectl wait --for=jsonpath='{.status.phase}'=Running "pod/$DST_POD" -n "$DST_NS" --timeout=120s
  DST_IP="$(kubectl get pod "$DST_POD" -n "$DST_NS" -o jsonpath='{.status.podIP}')"
  [ -n "$DST_IP" ] || die "could not resolve dst helper pod IP"
  echo "receiver ready at $DST_IP:$COPY_PORT — streaming /home/dev (in-cluster; large volumes take a while)..."
  src_pod "$DST_IP" | kubectl apply -f -

  src_phase="$(wait_pod_done "$SRC_POD" "$SRC_NS")"; src_ok=$?
  dst_phase="$(wait_pod_done "$DST_POD" "$DST_NS")"; dst_ok=$?
  if [ "$src_ok" -ne 0 ] || [ "$dst_ok" -ne 0 ]; then
    echo "ERROR: copy failed (src=$src_phase dst=$dst_phase). Logs:" >&2
    kubectl logs "$SRC_POD" -n "$SRC_NS" --tail=20 2>/dev/null | sed 's/^/  src| /' >&2 || true
    kubectl logs "$DST_POD" -n "$DST_NS" --tail=20 2>/dev/null | sed 's/^/  dst| /' >&2 || true
    die "home volume copy did not complete — source left intact, nothing cut over"
  fi
  echo "copy complete (src=$src_phase dst=$dst_phase) — removing helper pods"
  kubectl delete pod "$SRC_POD" -n "$SRC_NS" --ignore-not-found
  kubectl delete pod "$DST_POD" -n "$DST_NS" --ignore-not-found
fi

echo "=== data migrated ($SRC_NS -> $DST_NS) ==="

if [ "$CUTOVER" != 1 ]; then
  cat <<NEXT

Copy done (source left intact + stopped, so this is fully reversible).
Finish the cutover when ready — re-run with --cutover to automate it, or by hand:
  1. Set 'namespace: ${DST_NS}' in the user's values.yaml, then:
       make deploy USER=${USER_SLUG}
  2. Verify: kubectl -n ${DST_NS} exec deploy/${WS} -c ide -- ls -la /home/dev
  3. Decommission the old copy (or re-run with --decommission):
       helm uninstall ${USER_SLUG}-workspace -n ${SRC_NS} || true
       kubectl delete pvc ${PVC} -n ${SRC_NS}
NEXT
  exit 0
fi

# --- Cutover: repoint the config at ws-<user> and deploy into it -------------
echo "--- cutover: repointing config + deploying into $DST_NS ---"
VALUES="$(resolve_values_file "$USER_SLUG" || true)"
if [ -n "$VALUES" ]; then
  cur_ns="$(awk '/^namespace:/{print $2; exit}' "$VALUES")"
  if [ "$cur_ns" = "$DST_NS" ]; then
    echo "values.yaml already targets $DST_NS ($VALUES)"
  else
    echo "updating namespace: $cur_ns -> $DST_NS in $VALUES"
    # sed -i needs an explicit (empty-suffix) backup arg on BSD/macOS; use a
    # real .bak suffix + rm so the same line runs on GNU sed too.
    run sed -i.bak "s|^namespace:.*|namespace: ${DST_NS}|" "$VALUES"
    run rm -f "${VALUES}.bak"
    echo "NOTE: commit this values.yaml change to the GitOps repo so the next reconcile is a no-op."
  fi
else
  echo "WARNING: no values.yaml found for '$USER_SLUG' under deployments/, users-private/, or .users/." >&2
  echo "         'make deploy' will fall back to the ws-<user> convention, but nothing will be committed to GitOps." >&2
fi

# The old release's ingresses still claim the workspace hostname from SRC_NS.
# ingress-nginx rejects a same-host/path rule from a second namespace (older
# wins), so the new ingress would never receive traffic — and cert-manager
# couldn't solve the ACME challenge for the new namespace-scoped TLS secret.
# Delete them before deploying; rollback recreates them via a deploy with
# values.yaml pointed back at SRC_NS.
echo "--- removing old ingresses in $SRC_NS (they hold the hostname) ---"
# Two label families: the workspace's own ingresses (app=ws-<user>) and the
# oauth2-proxy one (app=oauth2-proxy-<user>) rendered for oauth2 workspaces.
run kubectl delete ingress -n "$SRC_NS" -l "app in ($WS, oauth2-proxy-$USER_SLUG)" --ignore-not-found

run make -C "$ROOT" deploy USER="$USER_SLUG"

if [ "$DRY_RUN" != 1 ]; then
  echo "--- verify: home dir in $DST_NS ---"
  kubectl -n "$DST_NS" exec "deploy/${WS}" -c ide -- ls -la /home/dev | head -20 || \
    echo "WARNING: could not list /home/dev yet — pod may still be starting; check manually." >&2
fi

if [ "$DECOMMISSION" != 1 ]; then
  cat <<NEXT

Cutover complete — ${WS} is now running in ${DST_NS} on the migrated volume.
The OLD copy in ${SRC_NS} is still present (scaled to 0) for rollback. Once
you're satisfied, reclaim it (or re-run with --decommission):
  helm uninstall ${USER_SLUG}-workspace -n ${SRC_NS} || true
  kubectl delete pvc ${PVC} -n ${SRC_NS}
NEXT
  exit 0
fi

# --- Decommission: reclaim the old coder copy (destructive) ------------------
echo "--- decommission: removing old copy in $SRC_NS ---"
run helm uninstall "${USER_SLUG}-workspace" -n "$SRC_NS" || true
run kubectl delete pvc "$PVC" -n "$SRC_NS" --ignore-not-found
echo "=== ${USER_SLUG} fully migrated to ${DST_NS} and decommissioned from ${SRC_NS} ==="
