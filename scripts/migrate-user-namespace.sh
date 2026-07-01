#!/usr/bin/env bash
# scripts/migrate-user-namespace.sh — move an existing workspace from the shared
# `coder` namespace into its own per-user namespace `ws-<user>` (issue #103).
#
# Usage:
#   scripts/migrate-user-namespace.sh <user> [--src-namespace coder] [--dry-run] [--keep-old]
#
# What it does (idempotent where it can be):
#   1. Preflight — confirm the workspace exists in the source namespace and the
#      target namespace does not already hold a conflicting workspace.
#   2. Scale the source workspace to 0 so its home PVC is quiescent.
#   3. Create + label the ws-<user> namespace and copy the regcred image-pull
#      Secret into it (via ensure-workspace-namespace.sh).
#   4. Create a new home PVC (ws-<user>-home) in the target namespace, same size.
#   5. Copy the home volume across namespaces with a tar-pipe between two helper
#      pods — PVCs are namespace-scoped and cannot be moved, so the data is
#      streamed, not remounted.
#   6. Print the remaining cutover steps (deploy into the new namespace, verify,
#      then delete the old objects) — those are left to you to run deliberately.
#
# This script NEVER deletes the source workspace or its PVC. Cutover + cleanup
# are manual on purpose: verify the new workspace first, then remove the old.
#
# See docs/PER_WORKSPACE_NAMESPACE_MIGRATION.md for the full runbook.
set -euo pipefail

USER_SLUG=""
SRC_NS="coder"
DRY_RUN=0
HELPER_IMAGE="${MIGRATION_HELPER_IMAGE:-busybox:1.36}"

die() { echo "ERROR: $*" >&2; exit 1; }
run() { if [ "$DRY_RUN" = 1 ]; then echo "DRY-RUN> $*"; else echo "+ $*"; "$@"; fi; }

while [ $# -gt 0 ]; do
  case "$1" in
    --src-namespace) SRC_NS="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    -*) die "unknown flag: $1" ;;
    *) [ -z "$USER_SLUG" ] && USER_SLUG="$1" && shift || die "unexpected arg: $1" ;;
  esac
done

[ -n "$USER_SLUG" ] || die "usage: migrate-user-namespace.sh <user> [--src-namespace coder] [--dry-run]"

WS="ws-${USER_SLUG}"
DST_NS="ws-${USER_SLUG}"
PVC="ws-${USER_SLUG}-home"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== migrate $WS : $SRC_NS -> $DST_NS (dry-run=$DRY_RUN) ==="

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
  kubectl apply -f - <<YAML
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${PVC}
  namespace: ${DST_NS}
  labels:
    app: ${WS}
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: ${SIZE}
YAML
fi

# 5. Copy the home volume across namespaces via a tar-pipe between helper pods.
echo "--- copying home volume ($SRC_NS -> $DST_NS) ---"
SRC_POD="migrate-src-${USER_SLUG}"
DST_POD="migrate-dst-${USER_SLUG}"
helper_pod() {  # name namespace
  cat <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: $1
  namespace: $2
  labels: { app: kube-coder-migration }
spec:
  restartPolicy: Never
  containers:
  - name: helper
    image: ${HELPER_IMAGE}
    command: ["sh", "-c", "sleep 3600"]
    volumeMounts:
    - { name: home, mountPath: /home/dev }
  volumes:
  - name: home
    persistentVolumeClaim: { claimName: ${PVC} }
YAML
}

if [ "$DRY_RUN" = 1 ]; then
  echo "DRY-RUN> start helper pod $SRC_POD in $SRC_NS + $DST_POD in $DST_NS"
  echo "DRY-RUN> kubectl exec $SRC_POD -- tar -C /home/dev -cf - . | kubectl exec -i $DST_POD -- tar -C /home/dev -xf -"
else
  helper_pod "$SRC_POD" "$SRC_NS" | kubectl apply -f -
  helper_pod "$DST_POD" "$DST_NS" | kubectl apply -f -
  kubectl wait --for=condition=ready pod "$SRC_POD" -n "$SRC_NS" --timeout=120s
  kubectl wait --for=condition=ready pod "$DST_POD" -n "$DST_NS" --timeout=120s
  echo "streaming /home/dev (this can take a while for large volumes)..."
  kubectl exec -n "$SRC_NS" "$SRC_POD" -- tar -C /home/dev -cf - . \
    | kubectl exec -i -n "$DST_NS" "$DST_POD" -- tar -C /home/dev -xf -
  echo "copy complete — removing helper pods"
  kubectl delete pod "$SRC_POD" -n "$SRC_NS" --ignore-not-found
  kubectl delete pod "$DST_POD" -n "$DST_NS" --ignore-not-found
fi

cat <<NEXT

=== data migrated. Remaining cutover steps (run deliberately) ===
  1. Point the workspace config at the new namespace and deploy:
       # in the user's values.yaml (GitOps repo / users-private/<user>):
       namespace: ${DST_NS}
       make deploy USER=${USER_SLUG}
     (make deploy installs base-infrastructure into ${DST_NS} and rolls the pod
      onto the migrated PVC.)
  2. Verify the workspace is healthy in ${DST_NS} and the home dir is intact:
       kubectl -n ${DST_NS} exec deploy/${WS} -c ide -- ls -la /home/dev
  3. Once satisfied, remove the OLD namespace copy:
       helm uninstall ${USER_SLUG}-workspace -n ${SRC_NS} || true
       kubectl delete pvc ${PVC} -n ${SRC_NS}
       # (leave shared ${SRC_NS} objects — controller, base-infra — in place)
NEXT
