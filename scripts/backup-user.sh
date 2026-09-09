#!/usr/bin/env bash
# scripts/backup-user.sh — archive a workspace's home volume (issue #579).
#
# The home PVC is the only thing in a workspace that isn't reproducible: every
# project checkout, ~/.credentials (SSH keys, gh/npm/docker/aws/gcloud logins),
# .claude-memory/memory.db and .claude-tasks/ all live on it. The chart already
# annotates the PVC `helm.sh/resource-policy: keep` so our own tooling can't
# delete it — that does nothing for storage failure, node loss, a cluster
# rebuild or a stray `kubectl delete pvc`. This is the missing half.
#
# Usage:
#   scripts/backup-user.sh <user> [flags]
#     --out PATH          write the archive here (default:
#                         backups/<user>/ws-<user>-home-<UTC-timestamp>.tar.gz)
#     --s3 s3://B/PREFIX  stream straight to an S3-compatible target from
#                         inside the cluster (bytes never touch this machine)
#     --s3-secret NAME    k8s Secret in the workspace namespace holding
#                         AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY /
#                         AWS_DEFAULT_REGION (+ optional AWS_ENDPOINT_URL for
#                         R2/MinIO). Required with --s3.
#     --quiesce           scale the workspace to 0 for the duration, so the
#                         archive is point-in-time consistent (see below), and
#                         scale it back afterwards
#     --encrypt-to ID     pipe the local archive through `gpg --encrypt` for
#                         this recipient; the plaintext archive is never kept
#     --namespace N       workspace namespace (default: auto-detect ws-<user>
#                         then coder)
#     --dry-run           print every action, touch nothing
#
# THE ARCHIVE CONTAINS LIVE CREDENTIALS. It is written 0600 inside a 0700
# directory, and `backups/` is gitignored, but that is the floor and not a
# story: treat the file exactly like the SSH private key that is inside it, and
# prefer --encrypt-to (or an encrypted-at-rest bucket) for anything that leaves
# this machine. docs/BACKUP_RESTORE.md spells out what is and isn't captured.
#
# Consistency: without --quiesce this is a HOT copy of a volume that is being
# written. Ordinary files are fine; a SQLite DB mid-write (memory.db) can be
# captured torn. --quiesce is the honest option and costs the user a restart.
#
# Integrity: the archive is written to a `.part` file, verified end-to-end
# (`gzip -t` + `tar -t`, which only pass on a complete stream with its EOF
# blocks) and only then moved into place with a .sha256 sidecar. A truncated
# transfer therefore leaves NO file that looks like a backup — the same
# fail-loudly-rather-than-silently-partial property as the tar-pipe in
# scripts/migrate-user-namespace.sh, which this borrows its technique from.
#
# Restoring is scripts/restore-user.sh. A backup nobody has restored is not a
# backup — docs/BACKUP_RESTORE.md has the drill.
set -euo pipefail

USER_SLUG=""
NS=""
OUT=""
S3_URI=""
S3_SECRET=""
QUIESCE=0
GPG_RECIPIENT=""
DRY_RUN=0
KUBECTL="${KUBECTL:-kubectl}"
HELPER_IMAGE="${BACKUP_HELPER_IMAGE:-busybox:1.36}"
S3_HELPER_IMAGE="${BACKUP_S3_HELPER_IMAGE:-amazon/aws-cli:2.17.0}"

die() { echo "ERROR: $*" >&2; exit 1; }
run() { if [ "$DRY_RUN" = 1 ]; then echo "DRY-RUN> $*"; else echo "+ $*"; "$@"; fi; }

while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="${2:-}"; shift 2 ;;
    --s3) S3_URI="${2:-}"; shift 2 ;;
    --s3-secret) S3_SECRET="${2:-}"; shift 2 ;;
    --quiesce) QUIESCE=1; shift ;;
    --encrypt-to) GPG_RECIPIENT="${2:-}"; shift 2 ;;
    --namespace) NS="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
    -*) die "unknown flag: $1" ;;
    *) [ -z "$USER_SLUG" ] && USER_SLUG="$1" && shift || die "unexpected arg: $1" ;;
  esac
done

[ -n "$USER_SLUG" ] || die "usage: backup-user.sh <user> [--out PATH | --s3 s3://bucket/prefix --s3-secret NAME] [--quiesce] [--encrypt-to ID] [--dry-run]"
[ -z "$S3_URI" ] || [ -n "$S3_SECRET" ] || die "--s3 needs --s3-secret NAME (the helper pod gets its credentials from that Secret)"
[ -z "$S3_URI" ] || [ -z "$OUT" ] || die "--out and --s3 are mutually exclusive (pick one sink)"
[ -z "$S3_URI" ] || [ -z "$GPG_RECIPIENT" ] || die "--encrypt-to applies to the local sink only; for --s3 use the bucket's own encryption"
case "$S3_URI" in ""|s3://*) ;; *) die "--s3 must be an s3:// URI" ;; esac
if [ -n "$GPG_RECIPIENT" ] && [ "$DRY_RUN" != 1 ]; then
  command -v gpg >/dev/null 2>&1 || die "--encrypt-to needs gpg on PATH"
fi

WS="ws-${USER_SLUG}"
PVC="ws-${USER_SLUG}-home"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
HELPER="backup-${USER_SLUG}-${STAMP}"

# Namespace: workspaces live in their own ws-<user> namespace (#103) but the
# pre-migration ones are still in the shared `coder` namespace. Find the PVC
# rather than making the operator remember which.
if [ -z "$NS" ]; then
  for candidate in "ws-${USER_SLUG}" "${NAMESPACE:-coder}"; do
    if "$KUBECTL" get pvc "$PVC" -n "$candidate" >/dev/null 2>&1; then NS="$candidate"; break; fi
  done
  [ -n "$NS" ] || die "home PVC '$PVC' not found in ws-${USER_SLUG} or ${NAMESPACE:-coder} — pass --namespace"
else
  "$KUBECTL" get pvc "$PVC" -n "$NS" >/dev/null 2>&1 || die "home PVC '$PVC' not found in namespace '$NS'"
fi

SIZE="$("$KUBECTL" get pvc "$PVC" -n "$NS" -o jsonpath='{.spec.resources.requests.storage}' 2>/dev/null || true)"
echo "=== backup ${WS} (namespace=$NS pvc=$PVC size=${SIZE:-unknown} quiesce=$QUIESCE dry-run=$DRY_RUN) ==="

# The live workspace pod, if any. Two things depend on it:
#   * --quiesce has to scale the deployment down and back up, and
#   * without --quiesce the helper shares a ReadWriteOnce PVC with the running
#     pod. RWO means single NODE, not single pod, so the helper must be pinned
#     to the workspace pod's node or it sits Pending forever waiting for an
#     attachment it can never get.
WS_NODE=""
WS_REPLICAS=""
if "$KUBECTL" get deployment "$WS" -n "$NS" >/dev/null 2>&1; then
  WS_REPLICAS="$("$KUBECTL" get deployment "$WS" -n "$NS" -o jsonpath='{.spec.replicas}' 2>/dev/null || true)"
  WS_NODE="$("$KUBECTL" get pod -n "$NS" -l "app=$WS" -o jsonpath='{.items[0].spec.nodeName}' 2>/dev/null || true)"
else
  echo "note: no deployment '$WS' in $NS — backing up the volume alone (stopped or partially deleted workspace)"
fi

# Every exit path goes through this: an abandoned helper pod would keep the RWO
# volume attached, and an abandoned scale-to-0 would leave the user's workspace
# down. `PART` is blanked once the archive is verified and moved, so an
# unverified partial is always deleted and a good archive never is.
CLEANUP_DONE=0
cleanup() {
  if [ "$CLEANUP_DONE" = 0 ] && [ "$DRY_RUN" != 1 ]; then
    CLEANUP_DONE=1
    if [ -n "${PART:-}" ]; then rm -f "$PART" || true; fi
    "$KUBECTL" delete pod "$HELPER" -n "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
    if [ "$QUIESCE" = 1 ] && [ -n "$WS_REPLICAS" ] && [ "$WS_REPLICAS" != 0 ]; then
      echo "--- restoring $WS to $WS_REPLICAS replica(s) ---"
      "$KUBECTL" scale deployment "$WS" -n "$NS" --replicas="$WS_REPLICAS" >/dev/null 2>&1 \
        || echo "WARNING: could not scale $WS back up — do it by hand: kubectl scale deploy/$WS -n $NS --replicas=$WS_REPLICAS" >&2
    fi
  fi
  return 0
}
trap cleanup EXIT INT TERM

if [ "$QUIESCE" = 1 ]; then
  if [ -n "$WS_REPLICAS" ]; then
    echo "--- quiescing $WS (scale to 0) ---"
    run "$KUBECTL" scale deployment "$WS" -n "$NS" --replicas=0
    run "$KUBECTL" wait --for=delete pod -l "app=$WS" -n "$NS" --timeout=180s || true
    WS_NODE=""   # nothing holds the volume now; let the scheduler place the helper
  else
    echo "note: --quiesce with no deployment to scale — the volume is already unattached"
  fi
fi

# The helper mounts the home volume READ-ONLY: a backup must not be able to
# write to the data it is protecting, even if this script is wrong. It carries
# app=kube-coder-backup rather than the workspace's app= label, so neither the
# ingress NetworkPolicy nor the egress one (both select ws-<user>) applies —
# the S3 variant needs internet egress. Explicit small requests keep it inside
# the namespace ResourceQuota next to a running workspace.
NODE_LINE=""
[ -n "$WS_NODE" ] && NODE_LINE="  nodeName: ${WS_NODE}"
ENVFROM_BLOCK=""
[ -n "$S3_SECRET" ] && ENVFROM_BLOCK="$(printf '    envFrom:\n    - secretRef: { name: %s }' "$S3_SECRET")"

helper_pod() {  # image, command
  cat <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: ${HELPER}
  namespace: ${NS}
  labels: { app: kube-coder-backup }
spec:
  restartPolicy: Never
${NODE_LINE}
  containers:
  - name: helper
    image: ${1}
    command: ["sh", "-c", "${2}"]
${ENVFROM_BLOCK}
    resources:
      requests: { cpu: 100m, memory: 128Mi }
      limits:   { cpu: "1",  memory: 512Mi }
    volumeMounts:
    - { name: home, mountPath: /home/dev, readOnly: true }
  volumes:
  - name: home
    persistentVolumeClaim: { claimName: ${PVC}, readOnly: true }
YAML
}

# ---------------------------------------------------------------- S3 sink ----
if [ -n "$S3_URI" ]; then
  DEST="${S3_URI%/}/${PVC}-${STAMP}.tar.gz"
  echo "--- streaming $PVC -> $DEST (in-cluster; bytes never touch this machine) ---"
  # pipefail is what makes this fail loudly: without it a tar read error is
  # masked by a successful `aws s3 cp` of a truncated stream, i.e. exactly the
  # silent partial backup this whole script exists to avoid.
  CMD="set -o pipefail; tar -C /home/dev -czf - . | aws s3 cp \${AWS_ENDPOINT_URL:+--endpoint-url \$AWS_ENDPOINT_URL} - ${DEST} && echo BACKUP_SENT_OK"
  if [ "$DRY_RUN" = 1 ]; then
    echo "DRY-RUN> apply helper pod $HELPER in $NS ($S3_HELPER_IMAGE): tar -czf - . | aws s3 cp - $DEST"
    echo "DRY-RUN> wait for $HELPER to reach Succeeded, then delete it"
    exit 0
  fi
  helper_pod "$S3_HELPER_IMAGE" "$CMD" | "$KUBECTL" apply -f -
  if ! "$KUBECTL" wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$HELPER" -n "$NS" --timeout=7200s >/dev/null 2>&1; then
    phase="$("$KUBECTL" get pod "$HELPER" -n "$NS" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
    "$KUBECTL" logs "$HELPER" -n "$NS" --tail=30 2>/dev/null | sed 's/^/  helper| /' >&2 || true
    die "backup pod did not complete (phase=${phase:-Unknown}) — nothing was uploaded, or the upload is incomplete: check $DEST"
  fi
  "$KUBECTL" logs "$HELPER" -n "$NS" 2>/dev/null | grep -q BACKUP_SENT_OK \
    || die "backup pod Succeeded but never printed BACKUP_SENT_OK — treat $DEST as suspect"
  echo "=== backup complete: $DEST ==="
  exit 0
fi

# -------------------------------------------------------------- local sink ---
if [ -z "$OUT" ]; then
  OUT="${ROOT}/backups/${USER_SLUG}/${PVC}-${STAMP}.tar.gz"
fi
OUT_DIR="$(dirname "$OUT")"
PART="${OUT}.part"

if [ "$DRY_RUN" = 1 ]; then
  echo "DRY-RUN> mkdir -p -m 700 $OUT_DIR   (0700 only if it does not exist yet)"
  echo "DRY-RUN> apply helper pod $HELPER in $NS ($HELPER_IMAGE, home mounted read-only${WS_NODE:+, pinned to node $WS_NODE})"
  echo "DRY-RUN> kubectl exec $HELPER -- tar -C /home/dev -czf - .   > $PART"
  echo "DRY-RUN> verify the stream (gzip -t + tar -t), write ${OUT}.sha256, chmod 600, mv -> $OUT"
  [ -n "$GPG_RECIPIENT" ] && echo "DRY-RUN> gpg --encrypt --recipient $GPG_RECIPIENT $OUT (and shred the plaintext)"
  echo "DRY-RUN> delete helper pod $HELPER"
  exit 0
fi

# 0700 applies only to a directory we create. `--out /tmp/x.tar.gz` must not
# silently re-permission the operator's existing directory — but it is worth
# saying out loud when the archive is about to land somewhere others can read.
if [ ! -d "$OUT_DIR" ]; then
  mkdir -p "$OUT_DIR"
  chmod 700 "$OUT_DIR" 2>/dev/null || true
else
  dir_mode="$(stat -c %a "$OUT_DIR" 2>/dev/null || stat -f %Lp "$OUT_DIR" 2>/dev/null || echo "")"
  case "$dir_mode" in
    ""|*00) : ;;
    *) echo "NOTE: $OUT_DIR is mode $dir_mode — the archive itself is 0600, but the directory is readable by others." >&2 ;;
  esac
fi
rm -f "$PART"

echo "--- starting helper pod $HELPER${WS_NODE:+ (pinned to node $WS_NODE — RWO volume shared with the live pod)} ---"
helper_pod "$HELPER_IMAGE" "sleep 7200" | "$KUBECTL" apply -f -
"$KUBECTL" wait --for=jsonpath='{.status.phase}'=Running "pod/$HELPER" -n "$NS" --timeout=180s >/dev/null 2>&1 \
  || { "$KUBECTL" describe pod "$HELPER" -n "$NS" 2>/dev/null | tail -20 >&2 || true
       die "helper pod never became Running (a Pending pod on a ReadWriteOnce volume usually means it landed on a different node than the workspace — retry with --quiesce)"; }

echo "--- streaming /home/dev -> $OUT ---"
# Exit status comes back from the remote tar, so a read error inside the volume
# fails here. The stream still crosses this machine's exec connection, which is
# the one thing the in-cluster migrate path deliberately avoids; the verify pass
# below is what catches a dropped websocket, and --s3 is the answer for volumes
# big enough that the drop is likely.
set +e
"$KUBECTL" exec "$HELPER" -n "$NS" -- sh -c 'tar -C /home/dev -czf - .' > "$PART"
STREAM_RC=$?
set -e
[ "$STREAM_RC" = 0 ] || die "stream failed (kubectl exec exit $STREAM_RC) — partial file removed, no backup written"

echo "--- verifying the archive ---"
gzip -t "$PART" 2>/dev/null || die "archive failed gzip integrity check (truncated transfer?) — partial file removed"
ENTRIES="$(tar -tzf "$PART" 2>/dev/null | wc -l | tr -d ' ')"
[ "${ENTRIES:-0}" -gt 0 ] || die "archive contains no entries — partial file removed"
BYTES="$(wc -c < "$PART" | tr -d ' ')"

chmod 600 "$PART"
mv "$PART" "$OUT"
PART=""   # adopted; the cleanup trap must not delete it now
( cd "$OUT_DIR" && { command -v sha256sum >/dev/null 2>&1 && sha256sum "$(basename "$OUT")" || shasum -a 256 "$(basename "$OUT")"; } ) > "${OUT}.sha256"
chmod 600 "${OUT}.sha256"

if [ -n "$GPG_RECIPIENT" ]; then
  echo "--- encrypting to $GPG_RECIPIENT ---"
  gpg --yes --batch --encrypt --recipient "$GPG_RECIPIENT" --output "${OUT}.gpg" "$OUT" \
    || die "gpg failed — the PLAINTEXT archive is still at $OUT; encrypt or remove it by hand"
  chmod 600 "${OUT}.gpg"
  rm -f "$OUT" "${OUT}.sha256"
  ( cd "$OUT_DIR" && { command -v sha256sum >/dev/null 2>&1 && sha256sum "$(basename "$OUT").gpg" || shasum -a 256 "$(basename "$OUT").gpg"; } ) > "${OUT}.gpg.sha256"
  chmod 600 "${OUT}.gpg.sha256"
  OUT="${OUT}.gpg"
fi

cat <<DONE
=== backup complete ===
  archive:  $OUT   ($BYTES bytes, $ENTRIES entries, mode 0600)
  checksum: ${OUT}.sha256
  contents: everything under /home/dev — INCLUDING ~/.credentials (SSH keys,
            gh/npm/docker/aws/gcloud logins), .claude-memory/memory.db and
            .claude-tasks/. Guard it like the private key inside it.
  restore:  scripts/restore-user.sh $USER_SLUG $OUT
  drill it: docs/BACKUP_RESTORE.md — an untested backup is not a backup.
DONE
