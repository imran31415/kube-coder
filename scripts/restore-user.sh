#!/usr/bin/env bash
# scripts/restore-user.sh — restore a workspace's home volume from a
# scripts/backup-user.sh archive (issue #579).
#
# The inverse of backup-user.sh, and the half that makes the other half real: a
# backup nobody has restored is not a backup. docs/BACKUP_RESTORE.md has the
# drill this script exists to make routine.
#
# Usage:
#   scripts/restore-user.sh <user> <archive> [flags]
#     <archive>           a local .tar.gz (or .tar.gz.gpg), or an s3:// URI
#     --force             restore even though the volume is NOT empty. Without
#                         this, a non-empty /home/dev is refused — the default
#                         assumption is that you are filling a fresh volume,
#                         not overwriting live work.
#     --s3-secret NAME    k8s Secret with AWS_ACCESS_KEY_ID /
#                         AWS_SECRET_ACCESS_KEY / AWS_DEFAULT_REGION (+ optional
#                         AWS_ENDPOINT_URL). Required for an s3:// source.
#     --namespace N       workspace namespace (default: auto-detect ws-<user>
#                         then coder)
#     --dry-run           print every action, touch nothing
#
# What --force actually does: `tar -x` over the existing contents. Files present
# in the archive are overwritten; files on the volume that the archive doesn't
# contain are LEFT IN PLACE. It is a merge, not a replace. If you need a true
# replace, delete the PVC and let the chart recreate it, then restore into the
# empty volume — that path is the one this script's default is built for.
#
# The workspace is scaled to 0 for the duration and scaled back afterwards.
# That is not optional: the pod's entrypoint writes to /home/dev on boot
# (credential symlinks, rc files, the kube-coder clone), so extracting into a
# live volume races it.
#
# Integrity is checked BEFORE the volume is touched: the .sha256 sidecar if
# present, then `gzip -t` + `tar -t`, which only pass on a complete archive.
# A bad archive therefore fails while the old data is still intact.
set -euo pipefail

USER_SLUG=""
ARCHIVE=""
NS=""
FORCE=0
S3_SECRET=""
DRY_RUN=0
KUBECTL="${KUBECTL:-kubectl}"
HELPER_IMAGE="${BACKUP_HELPER_IMAGE:-busybox:1.36}"
S3_HELPER_IMAGE="${BACKUP_S3_HELPER_IMAGE:-amazon/aws-cli:2.17.0}"

die() { echo "ERROR: $*" >&2; exit 1; }
run() { if [ "$DRY_RUN" = 1 ]; then echo "DRY-RUN> $*"; else echo "+ $*"; "$@"; fi; }

while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --s3-secret) S3_SECRET="${2:-}"; shift 2 ;;
    --namespace) NS="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    -*) die "unknown flag: $1" ;;
    *) if [ -z "$USER_SLUG" ]; then USER_SLUG="$1"; elif [ -z "$ARCHIVE" ]; then ARCHIVE="$1"; else die "unexpected arg: $1"; fi; shift ;;
  esac
done

[ -n "$USER_SLUG" ] && [ -n "$ARCHIVE" ] \
  || die "usage: restore-user.sh <user> <archive.tar.gz|s3://bucket/key> [--force] [--s3-secret NAME] [--dry-run]"

FROM_S3=0
case "$ARCHIVE" in s3://*) FROM_S3=1 ;; esac
[ "$FROM_S3" = 0 ] || [ -n "$S3_SECRET" ] || die "an s3:// source needs --s3-secret NAME"

WS="ws-${USER_SLUG}"
PVC="ws-${USER_SLUG}-home"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
HELPER="restore-${USER_SLUG}-${STAMP}"

# --- Preflight on the archive, before anything is scaled or mounted ----------
DECRYPT=0
if [ "$FROM_S3" = 0 ]; then
  [ -f "$ARCHIVE" ] || die "archive not found: $ARCHIVE"
  [ -r "$ARCHIVE" ] || die "archive not readable: $ARCHIVE"
  case "$ARCHIVE" in *.gpg) DECRYPT=1 ;; esac
  if [ -f "${ARCHIVE}.sha256" ]; then
    echo "--- verifying ${ARCHIVE}.sha256 ---"
    if [ "$DRY_RUN" != 1 ]; then
      ( cd "$(dirname "$ARCHIVE")" \
        && { command -v sha256sum >/dev/null 2>&1 && sha256sum -c "$(basename "$ARCHIVE").sha256" \
             || shasum -a 256 -c "$(basename "$ARCHIVE").sha256"; } ) >/dev/null \
        || die "checksum MISMATCH for $ARCHIVE — the archive is corrupt or truncated; nothing was touched"
    fi
  else
    echo "note: no .sha256 sidecar next to $ARCHIVE — falling back to structural verification only"
  fi
  if [ "$DECRYPT" = 1 ]; then
    command -v gpg >/dev/null 2>&1 || die "$ARCHIVE is gpg-encrypted but gpg is not on PATH"
    echo "--- verifying the encrypted archive (decrypt | gzip -t) ---"
    if [ "$DRY_RUN" != 1 ]; then
      gpg --quiet --batch --decrypt "$ARCHIVE" 2>/dev/null | gzip -t \
        || die "could not decrypt+verify $ARCHIVE (wrong key, or corrupt) — nothing was touched"
    fi
  else
    echo "--- verifying the archive (gzip -t + tar -t) ---"
    if [ "$DRY_RUN" != 1 ]; then
      gzip -t "$ARCHIVE" || die "$ARCHIVE failed gzip integrity check — nothing was touched"
      ENTRIES="$(tar -tzf "$ARCHIVE" 2>/dev/null | wc -l | tr -d ' ')"
      [ "${ENTRIES:-0}" -gt 0 ] || die "$ARCHIVE contains no entries — nothing was touched"
      echo "archive looks complete: $ENTRIES entries"
    fi
  fi
else
  echo "note: an s3:// source is verified as it streams (tar fails on a truncated object) rather than up front"
fi

# --- Resolve the target volume ----------------------------------------------
if [ -z "$NS" ]; then
  for candidate in "ws-${USER_SLUG}" "${NAMESPACE:-coder}"; do
    if "$KUBECTL" get pvc "$PVC" -n "$candidate" >/dev/null 2>&1; then NS="$candidate"; break; fi
  done
  [ -n "$NS" ] || die "home PVC '$PVC' not found in ws-${USER_SLUG} or ${NAMESPACE:-coder} — create the workspace first (make deploy USER=$USER_SLUG), or pass --namespace"
else
  "$KUBECTL" get pvc "$PVC" -n "$NS" >/dev/null 2>&1 || die "home PVC '$PVC' not found in namespace '$NS'"
fi

echo "=== restore ${WS} <- ${ARCHIVE} (namespace=$NS pvc=$PVC force=$FORCE dry-run=$DRY_RUN) ==="

WS_REPLICAS=""
if "$KUBECTL" get deployment "$WS" -n "$NS" >/dev/null 2>&1; then
  WS_REPLICAS="$("$KUBECTL" get deployment "$WS" -n "$NS" -o jsonpath='{.spec.replicas}' 2>/dev/null || true)"
fi

CLEANUP_DONE=0
cleanup() {
  if [ "$CLEANUP_DONE" = 0 ] && [ "$DRY_RUN" != 1 ]; then
    CLEANUP_DONE=1
    "$KUBECTL" delete pod "$HELPER" -n "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true
    if [ -n "$WS_REPLICAS" ] && [ "$WS_REPLICAS" != 0 ]; then
      echo "--- bringing $WS back to $WS_REPLICAS replica(s) ---"
      "$KUBECTL" scale deployment "$WS" -n "$NS" --replicas="$WS_REPLICAS" >/dev/null 2>&1 \
        || echo "WARNING: could not scale $WS back up — do it by hand: kubectl scale deploy/$WS -n $NS --replicas=$WS_REPLICAS" >&2
    fi
  fi
  return 0
}
trap cleanup EXIT INT TERM

# The pod's entrypoint writes to /home/dev on boot, so it must be stopped for
# the extraction — and stopping it also releases the ReadWriteOnce volume, so
# the helper is free to land on any node.
if [ -n "$WS_REPLICAS" ]; then
  echo "--- stopping $WS (scale to 0) ---"
  run "$KUBECTL" scale deployment "$WS" -n "$NS" --replicas=0
  run "$KUBECTL" wait --for=delete pod -l "app=$WS" -n "$NS" --timeout=180s || true
else
  echo "note: no running deployment '$WS' in $NS — restoring into the volume directly"
fi

# Read-WRITE mount here (unlike backup, which mounts read-only). aws-cli image
# when the source is S3 so the download streams in-cluster; busybox otherwise.
# app=kube-coder-backup keeps it out of the workspace NetworkPolicies' selectors
# (both select ws-<user>), which is what lets the S3 variant egress.
IMAGE="$HELPER_IMAGE"
[ "$FROM_S3" = 1 ] && IMAGE="$S3_HELPER_IMAGE"
ENVFROM_BLOCK=""
[ -n "$S3_SECRET" ] && ENVFROM_BLOCK="$(printf '    envFrom:\n    - secretRef: { name: %s }' "$S3_SECRET")"

helper_pod() {
  cat <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: ${HELPER}
  namespace: ${NS}
  labels: { app: kube-coder-backup }
spec:
  restartPolicy: Never
  containers:
  - name: helper
    image: ${IMAGE}
    command: ["sh", "-c", "sleep 7200"]
${ENVFROM_BLOCK}
    resources:
      requests: { cpu: 100m, memory: 128Mi }
      limits:   { cpu: "1",  memory: 512Mi }
    volumeMounts:
    - { name: home, mountPath: /home/dev }
  volumes:
  - name: home
    persistentVolumeClaim: { claimName: ${PVC} }
YAML
}

if [ "$DRY_RUN" = 1 ]; then
  echo "DRY-RUN> apply helper pod $HELPER in $NS ($IMAGE, /home/dev mounted read-write)"
  echo "DRY-RUN> kubectl exec $HELPER -- ls -A /home/dev   (refuse if non-empty and --force absent)"
  if [ "$FROM_S3" = 1 ]; then
    echo "DRY-RUN> kubectl exec $HELPER -- sh -c 'aws s3 cp $ARCHIVE - | tar -C /home/dev -xzf -'"
  elif [ "$DECRYPT" = 1 ]; then
    echo "DRY-RUN> gpg --decrypt $ARCHIVE | kubectl exec -i $HELPER -- tar -C /home/dev -xzf -"
  else
    echo "DRY-RUN> kubectl exec -i $HELPER -- tar -C /home/dev -xzf - < $ARCHIVE"
  fi
  echo "DRY-RUN> verify RESTORE_OK, delete helper pod, scale $WS back to ${WS_REPLICAS:-0}"
  exit 0
fi

echo "--- starting helper pod $HELPER ---"
helper_pod | "$KUBECTL" apply -f -
"$KUBECTL" wait --for=jsonpath='{.status.phase}'=Running "pod/$HELPER" -n "$NS" --timeout=180s >/dev/null 2>&1 \
  || { "$KUBECTL" describe pod "$HELPER" -n "$NS" 2>/dev/null | tail -20 >&2 || true
       die "helper pod never became Running — nothing was written"; }

EXISTING="$("$KUBECTL" exec "$HELPER" -n "$NS" -- sh -c 'ls -A /home/dev 2>/dev/null | head -20' || true)"
if [ -n "$EXISTING" ]; then
  if [ "$FORCE" != 1 ]; then
    echo "the volume is NOT empty — top level:" >&2
    echo "$EXISTING" | sed 's/^/  /' >&2
    die "refusing to restore over a non-empty /home/dev. Re-run with --force to extract over it (archive files win, extra files stay), or restore into a fresh PVC. Nothing was written."
  fi
  echo "WARNING: /home/dev is not empty and --force was given — extracting over the existing contents." >&2
fi

echo "--- extracting into /home/dev ---"
# The `&& echo RESTORE_OK` gates on tar's own exit status, so a truncated
# stream can't look like a successful restore. Same gate as the tar-pipe in
# scripts/migrate-user-namespace.sh.
if [ "$FROM_S3" = 1 ]; then
  OUTPUT="$("$KUBECTL" exec "$HELPER" -n "$NS" -- sh -c \
    "set -o pipefail; aws s3 cp \${AWS_ENDPOINT_URL:+--endpoint-url \$AWS_ENDPOINT_URL} ${ARCHIVE} - | tar -C /home/dev -xzf - && echo RESTORE_OK" 2>&1)" || true
elif [ "$DECRYPT" = 1 ]; then
  OUTPUT="$(gpg --quiet --batch --decrypt "$ARCHIVE" \
    | "$KUBECTL" exec -i "$HELPER" -n "$NS" -- sh -c 'tar -C /home/dev -xzf - && echo RESTORE_OK' 2>&1)" || true
else
  OUTPUT="$("$KUBECTL" exec -i "$HELPER" -n "$NS" -- sh -c 'tar -C /home/dev -xzf - && echo RESTORE_OK' 2>&1 < "$ARCHIVE")" || true
fi

case "$OUTPUT" in
  *RESTORE_OK*) : ;;
  *) echo "$OUTPUT" | tail -20 | sed 's/^/  helper| /' >&2
     die "extraction did not report success — /home/dev may be PARTIALLY written. Do not start the workspace on it: wipe the volume and restore again." ;;
esac

TOP="$("$KUBECTL" exec "$HELPER" -n "$NS" -- sh -c 'ls -A /home/dev 2>/dev/null | head -15' || true)"
echo "--- restored, top level of /home/dev: ---"
echo "$TOP" | sed 's/^/  /'

cleanup

cat <<DONE
=== restore complete ===
  volume:  $PVC (namespace $NS)
  from:    $ARCHIVE
  next:    the workspace is scaling back up; give it a minute, then check
             kubectl -n $NS exec deploy/$WS -c ide -- ls -la /home/dev
           and confirm the things that actually matter came back:
             ~/.credentials/.ssh, .claude-memory/memory.db, your git clones.
  note:    a restored workspace keeps the credentials that were in the archive.
           If it was taken before a credential rotation, re-login is expected.
DONE
