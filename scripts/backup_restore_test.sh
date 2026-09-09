#!/usr/bin/env bash
# Tests for scripts/backup-user.sh + scripts/restore-user.sh (#579).
#
# Both scripts reach the cluster only through kubectl, so the whole flow is
# exercisable offline by shimming kubectl on PATH and driving each branch from
# env. No cluster, no network, seconds.
#
# The properties that actually matter here — the ones that decide whether a
# backup is trustworthy rather than merely present:
#   * a partial/truncated stream leaves NO file that looks like a backup,
#   * the archive is 0600 with a verifiable .sha256 sidecar,
#   * backup mounts the home volume READ-ONLY (a backup cannot damage the data
#     it protects) and pins itself to the live pod's node (ReadWriteOnce),
#   * --quiesce scales the workspace down AND back up, on success and failure,
#   * restore verifies the archive BEFORE touching the volume, refuses a
#     non-empty volume without --force, and never reports success unless the
#     remote tar did,
#   * --dry-run issues no mutating kubectl verb at all.
#
# Run:  bash scripts/backup_restore_test.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP="$HERE/backup-user.sh"
RESTORE="$HERE/restore-user.sh"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
SHIMDIR="$WORK/bin"; mkdir -p "$SHIMDIR"
export PATH="$SHIMDIR:$PATH"

# --- fixture volume + archive ------------------------------------------------
FIX="$WORK/fixture"
mkdir -p "$FIX/.credentials/.ssh" "$FIX/proj"
echo "PRIVATE KEY" > "$FIX/.credentials/.ssh/id_ed25519"
echo "hello"       > "$FIX/proj/main.go"
tar -C "$FIX" -czf "$WORK/fixture.tar.gz" .

# --- kubectl shim ------------------------------------------------------------
# Behaviour comes from KC_* env vars; every call is logged, and every MUTATING
# verb is logged separately so a --dry-run test can assert none happened.
cat > "$SHIMDIR/kubectl" <<'SHIM'
#!/usr/bin/env bash
args="$*"
echo "kubectl $args" >> "$KC_KUBECTL_LOG"
case "${1:-}" in
  apply|scale|delete|exec|patch|create|replace|cp)
    echo "kubectl $args" >> "$KC_MUTATION_LOG" ;;
esac
ns=""; prev=""
for a in "$@"; do [ "$prev" = "-n" ] && ns="$a"; prev="$a"; done
last="${!#}"
case "$args" in
  "get pvc "*-o\ jsonpath*) echo "${KC_PVC_SIZE:-50Gi}"; exit 0 ;;
  "get pvc "*)
    [ "$ns" = "${KC_PVC_NS:-}" ] && exit 0 || exit 1 ;;
  "get deployment "*-o\ jsonpath*)
    [ -n "${KC_HAS_DEPLOY:-}" ] || exit 1
    echo "${KC_REPLICAS:-1}"; exit 0 ;;
  "get deployment "*)
    [ -n "${KC_HAS_DEPLOY:-}" ] && exit 0 || exit 1 ;;
  "get pod "*nodeName*) echo "${KC_NODE:-}"; exit 0 ;;
  "get pod "*phase*) echo "${KC_POD_PHASE:-Failed}"; exit 0 ;;
  "apply -f -"*) cat > "$KC_APPLIED"; exit 0 ;;
  wait*) [ -n "${KC_WAIT_FAIL:-}" ] && exit 1; exit 0 ;;
  scale*) exit 0 ;;
  delete*) exit 0 ;;
  describe*) echo "  (shimmed describe)"; exit 0 ;;
  logs*) echo "${KC_POD_LOGS:-}"; exit 0 ;;
  exec*)
    case "$last" in
      *-czf\ -*)  # backup: stream the volume out
        if [ -n "${KC_STREAM_FAIL:-}" ]; then echo "tar: read error" >&2; exit 2; fi
        if [ -n "${KC_TRUNCATE:-}" ]; then head -c 120 "$KC_FIXTURE_TAR"; exit 0; fi
        cat "$KC_FIXTURE_TAR"; exit 0 ;;
      *ls\ -A*) printf '%s' "${KC_LS_OUTPUT:-}"; [ -n "${KC_LS_OUTPUT:-}" ] && echo ""; exit 0 ;;
      *aws\ s3\ cp*)  # restore from S3: the download happens inside the pod.
        # Checked BEFORE the plain-extract pattern — this command contains a
        # `tar -xzf -` too, on the far side of the pipe.
        [ -n "${KC_EXTRACT_FAIL:-}" ] && { echo "download failed" >&2; exit 2; }
        echo RESTORE_OK; exit 0 ;;
      *-xzf\ -*)  # restore: extract into a local dir instead of a volume
        if [ -n "${KC_EXTRACT_FAIL:-}" ]; then cat >/dev/null; echo "tar: unexpected EOF" >&2; exit 2; fi
        mkdir -p "$KC_EXTRACT_DIR"
        tar -C "$KC_EXTRACT_DIR" -xzf - || { echo "tar failed" >&2; exit 2; }
        echo RESTORE_OK; exit 0 ;;
    esac
    exit 0 ;;
esac
exit 0
SHIM
chmod +x "$SHIMDIR/kubectl"

reset_env() {
  export KC_KUBECTL_LOG="$WORK/kubectl.log"
  export KC_MUTATION_LOG="$WORK/mutations.log"
  export KC_APPLIED="$WORK/applied.yaml"
  export KC_FIXTURE_TAR="$WORK/fixture.tar.gz"
  export KC_EXTRACT_DIR="$WORK/extracted"
  : > "$KC_KUBECTL_LOG"; : > "$KC_MUTATION_LOG"; : > "$KC_APPLIED"
  rm -rf "$KC_EXTRACT_DIR"
  export KC_PVC_NS="ws-alice" KC_HAS_DEPLOY=1 KC_REPLICAS=1 KC_NODE="node-7"
  unset KC_WAIT_FAIL KC_TRUNCATE KC_STREAM_FAIL KC_EXTRACT_FAIL KC_LS_OUTPUT KC_POD_LOGS KC_POD_PHASE
  export KC_POD_PHASE=Succeeded
}
# grep -c already prints 0 when it matches nothing (and exits 1) — swallow the
# status rather than appending a second count.
mutations() { grep -cE 'kubectl (apply|scale|delete|exec|patch|create)' "$KC_MUTATION_LOG" 2>/dev/null || true; }

echo "=== backup-user.sh ==="

# 1. Unknown workspace: refuse, and touch nothing.
reset_env; KC_PVC_NS="ws-bob"
out="$("$BACKUP" alice --out "$WORK/a.tar.gz" 2>&1)"; rc=$?
if [ "$rc" != 0 ] && printf '%s' "$out" | grep -q -- "--namespace" && [ "$(mutations)" = 0 ]; then
  ok "missing PVC in either namespace -> non-zero, no mutating call"
else bad "missing PVC" "rc=$rc mutations=$(mutations) out=$out"; fi

# 2. Happy path: verified 0600 archive + checksum sidecar, content intact.
reset_env
out="$("$BACKUP" alice --out "$WORK/good.tar.gz" 2>&1)"; rc=$?
if [ "$rc" = 0 ] && [ -f "$WORK/good.tar.gz" ]; then
  mode="$(stat -c %a "$WORK/good.tar.gz" 2>/dev/null || stat -f %Lp "$WORK/good.tar.gz")"
  tar -tzf "$WORK/good.tar.gz" 2>/dev/null | grep -q '.credentials/.ssh/id_ed25519' && content=1 || content=0
  ( cd "$WORK" && sha256sum -c good.tar.gz.sha256 >/dev/null 2>&1 ) && sums=1 || sums=0
  [ -f "$WORK/good.tar.gz.part" ] && leftover=1 || leftover=0
  if [ "$mode" = 600 ] && [ "$content" = 1 ] && [ "$sums" = 1 ] && [ "$leftover" = 0 ]; then
    ok "archive written 0600, content intact, .sha256 verifies, no .part left"
  else bad "archive properties" "mode=$mode content=$content sha256ok=$sums leftover=$leftover"; fi
else bad "happy-path backup" "rc=$rc out=$out"; fi

# 3. The property the whole script exists for: a truncated stream must not
#    leave anything that looks like a backup.
reset_env; export KC_TRUNCATE=1
out="$("$BACKUP" alice --out "$WORK/trunc.tar.gz" 2>&1)"; rc=$?
if [ "$rc" != 0 ] && [ ! -e "$WORK/trunc.tar.gz" ] && [ ! -e "$WORK/trunc.tar.gz.part" ]; then
  ok "truncated stream -> non-zero, no archive and no .part left behind"
else bad "truncated stream" "rc=$rc exists=$([ -e "$WORK/trunc.tar.gz" ] && echo y || echo n) part=$([ -e "$WORK/trunc.tar.gz.part" ] && echo y || echo n)"; fi

# 4. A failing remote tar is a failure, not an empty backup.
reset_env; export KC_STREAM_FAIL=1
out="$("$BACKUP" alice --out "$WORK/fail.tar.gz" 2>&1)"; rc=$?
if [ "$rc" != 0 ] && [ ! -e "$WORK/fail.tar.gz" ]; then ok "remote tar error -> non-zero, no archive"
else bad "remote tar error" "rc=$rc"; fi

# 5. The helper must not be able to write to the volume it is reading.
reset_env
"$BACKUP" alice --out "$WORK/ro.tar.gz" >/dev/null 2>&1
ro_mount="$(grep -c 'mountPath: /home/dev, readOnly: true' "$KC_APPLIED")"
ro_claim="$(grep -c 'claimName: ws-alice-home, readOnly: true' "$KC_APPLIED")"
if [ "$ro_mount" -ge 1 ] && [ "$ro_claim" -ge 1 ]; then ok "helper mounts the home volume read-only (mount + claim)"
else bad "read-only mount" "mount=$ro_mount claim=$ro_claim
$(cat "$KC_APPLIED")"; fi

# 6. ReadWriteOnce = one NODE: sharing with the live pod requires pinning.
reset_env
"$BACKUP" alice --out "$WORK/pin.tar.gz" >/dev/null 2>&1
if grep -q 'nodeName: node-7' "$KC_APPLIED"; then ok "hot backup pins the helper to the live pod's node (RWO)"
else bad "node pinning" "$(cat "$KC_APPLIED")"; fi

# 7. --quiesce: scale to 0, and back to the ORIGINAL replica count; and with
#    nothing holding the volume the helper must not be pinned.
reset_env; KC_REPLICAS=2
out="$("$BACKUP" alice --out "$WORK/q.tar.gz" --quiesce 2>&1)"; rc=$?
down="$(grep -c 'scale deployment ws-alice -n ws-alice --replicas=0' "$KC_KUBECTL_LOG")"
up="$(grep -c 'scale deployment ws-alice -n ws-alice --replicas=2' "$KC_KUBECTL_LOG")"
if [ "$rc" = 0 ] && [ "$down" -ge 1 ] && [ "$up" -ge 1 ] && ! grep -q nodeName "$KC_APPLIED"; then
  ok "--quiesce scales to 0 then back to 2, and leaves the helper unpinned"
else bad "--quiesce" "rc=$rc down=$down up=$up pinned=$(grep -c nodeName "$KC_APPLIED")"; fi

# 8. A failed backup must still bring the workspace back up.
reset_env; KC_REPLICAS=3; export KC_STREAM_FAIL=1
"$BACKUP" alice --out "$WORK/qf.tar.gz" --quiesce >/dev/null 2>&1
if grep -q 'scale deployment ws-alice -n ws-alice --replicas=3' "$KC_KUBECTL_LOG"; then
  ok "--quiesce restores the replica count even when the backup fails"
else bad "quiesce scale-back on failure" "$(cat "$KC_KUBECTL_LOG")"; fi

# 9. Helper pod is cleaned up (an orphan keeps the RWO volume attached).
reset_env
"$BACKUP" alice --out "$WORK/cleanup.tar.gz" >/dev/null 2>&1
if grep -qE 'delete pod backup-alice-' "$KC_KUBECTL_LOG"; then ok "helper pod is deleted afterwards"
else bad "helper cleanup" "$(cat "$KC_KUBECTL_LOG")"; fi

# 10. --dry-run is genuinely read-only, and shows the default sink.
reset_env
out="$("$BACKUP" alice --dry-run 2>&1)"; rc=$?
if [ "$rc" = 0 ] && [ "$(mutations)" = 0 ] && printf '%s' "$out" | grep -q 'backups/alice/ws-alice-home-'; then
  ok "--dry-run mutates nothing and names the default backups/<user>/ sink"
else bad "--dry-run" "rc=$rc mutations=$(mutations) out=$out"; fi

# 11. Flag validation that would otherwise surface as a broken pod.
reset_env
out="$("$BACKUP" alice --s3 s3://b/p 2>&1)"; rc=$?
[ "$rc" != 0 ] && printf '%s' "$out" | grep -q -- "--s3-secret" && a=1 || a=0
out="$("$BACKUP" alice --s3 s3://b/p --s3-secret s --out "$WORK/x" 2>&1)"; rc=$?
[ "$rc" != 0 ] && b=1 || b=0
out="$("$BACKUP" alice --s3 not-a-uri --s3-secret s 2>&1)"; rc=$?
[ "$rc" != 0 ] && c=1 || c=0
if [ "$a$b$c" = 111 ]; then ok "--s3 requires --s3-secret, an s3:// URI, and rejects --out"
else bad "s3 flag validation" "needs-secret=$a excl-out=$b uri-check=$c"; fi

# 12. S3 sink: in-cluster pod, gated on the success marker.
reset_env; export KC_POD_LOGS="BACKUP_SENT_OK"
out="$("$BACKUP" alice --s3 s3://bucket/prefix --s3-secret aws-backup 2>&1)"; rc=$?
sec="$(grep -c 'secretRef: { name: aws-backup }' "$KC_APPLIED")"
if [ "$rc" = 0 ] && [ "$sec" -ge 1 ] && grep -q 'aws s3 cp' "$KC_APPLIED"; then
  ok "--s3 runs an in-cluster pod with the credential Secret and the aws sink"
else bad "s3 sink" "rc=$rc secretRef=$sec out=$out"; fi

reset_env; export KC_POD_LOGS="something else"
out="$("$BACKUP" alice --s3 s3://bucket/prefix --s3-secret aws-backup 2>&1)"; rc=$?
if [ "$rc" != 0 ] && printf '%s' "$out" | grep -qi suspect; then
  ok "--s3 pod that never prints the success marker is reported as suspect"
else bad "s3 marker gate" "rc=$rc out=$out"; fi

echo "=== restore-user.sh ==="

# 13. A missing archive fails before the workspace is touched.
reset_env
out="$("$RESTORE" alice "$WORK/nope.tar.gz" 2>&1)"; rc=$?
if [ "$rc" != 0 ] && [ "$(mutations)" = 0 ]; then ok "missing archive -> non-zero before anything is scaled"
else bad "missing archive" "rc=$rc mutations=$(mutations) out=$out"; fi

# 14. Checksum mismatch is caught while the old data is still intact.
reset_env
cp "$WORK/fixture.tar.gz" "$WORK/bad.tar.gz"
echo "0000000000000000000000000000000000000000000000000000000000000000  bad.tar.gz" > "$WORK/bad.tar.gz.sha256"
out="$("$RESTORE" alice "$WORK/bad.tar.gz" 2>&1)"; rc=$?
if [ "$rc" != 0 ] && printf '%s' "$out" | grep -qi mismatch && [ "$(mutations)" = 0 ]; then
  ok "checksum mismatch -> refused before the volume is touched"
else bad "checksum mismatch" "rc=$rc mutations=$(mutations) out=$out"; fi

# 15. A corrupt archive with no sidecar is caught structurally.
reset_env
head -c 200 "$WORK/fixture.tar.gz" > "$WORK/corrupt.tar.gz"
out="$("$RESTORE" alice "$WORK/corrupt.tar.gz" 2>&1)"; rc=$?
if [ "$rc" != 0 ] && [ "$(mutations)" = 0 ]; then ok "truncated archive -> refused up front (gzip -t)"
else bad "corrupt archive" "rc=$rc mutations=$(mutations)"; fi

# 16. The default: never silently overwrite live work.
reset_env; export KC_LS_OUTPUT="proj
.credentials"
out="$("$RESTORE" alice "$WORK/fixture.tar.gz" 2>&1)"; rc=$?
if [ "$rc" != 0 ] && printf '%s' "$out" | grep -q -- "--force" && [ ! -d "$KC_EXTRACT_DIR" ]; then
  ok "non-empty volume without --force -> refused, nothing extracted"
else bad "non-empty guard" "rc=$rc extracted=$([ -d "$KC_EXTRACT_DIR" ] && echo y || echo n) out=$out"; fi

# 17. ...and it must still bring the workspace back up after refusing.
if grep -q 'scale deployment ws-alice -n ws-alice --replicas=1' "$KC_KUBECTL_LOG"; then
  ok "refusing to restore still scales the workspace back up"
else bad "scale-back after refusal" "$(cat "$KC_KUBECTL_LOG")"; fi

# 18. --force extracts over a non-empty volume.
reset_env; export KC_LS_OUTPUT="proj"
out="$("$RESTORE" alice "$WORK/fixture.tar.gz" --force 2>&1)"; rc=$?
if [ "$rc" = 0 ] && [ -f "$KC_EXTRACT_DIR/.credentials/.ssh/id_ed25519" ]; then
  ok "--force extracts over a non-empty volume"
else bad "--force" "rc=$rc out=$out"; fi

# 19. Empty volume happy path, with the replica count preserved.
reset_env; KC_REPLICAS=2
out="$("$RESTORE" alice "$WORK/fixture.tar.gz" 2>&1)"; rc=$?
down="$(grep -c 'replicas=0' "$KC_KUBECTL_LOG")"; up="$(grep -c 'replicas=2' "$KC_KUBECTL_LOG")"
if [ "$rc" = 0 ] && [ -f "$KC_EXTRACT_DIR/proj/main.go" ] && [ "$down" -ge 1 ] && [ "$up" -ge 1 ]; then
  ok "empty volume -> extracted, workspace stopped then returned to 2 replicas"
else bad "restore happy path" "rc=$rc down=$down up=$up out=$out"; fi

# 20. Never claim success the remote tar didn't report.
reset_env; export KC_EXTRACT_FAIL=1
out="$("$RESTORE" alice "$WORK/fixture.tar.gz" 2>&1)"; rc=$?
if [ "$rc" != 0 ] && printf '%s' "$out" | grep -qi 'PARTIALLY'; then
  ok "failed extraction -> non-zero and an explicit partial-write warning"
else bad "extraction failure" "rc=$rc out=$out"; fi

# 21. --dry-run is read-only here too.
reset_env
out="$("$RESTORE" alice "$WORK/fixture.tar.gz" --dry-run 2>&1)"; rc=$?
if [ "$rc" = 0 ] && [ "$(mutations)" = 0 ]; then ok "restore --dry-run mutates nothing"
else bad "restore --dry-run" "rc=$rc mutations=$(mutations)"; fi

# 22. s3:// source needs its credential Secret, and restores in-cluster.
reset_env
out="$("$RESTORE" alice s3://bucket/key.tar.gz 2>&1)"; rc=$?
[ "$rc" != 0 ] && printf '%s' "$out" | grep -q -- "--s3-secret" && a=1 || a=0
reset_env
out="$("$RESTORE" alice s3://bucket/key.tar.gz --s3-secret aws-backup 2>&1)"; rc=$?
[ "$rc" = 0 ] && grep -q 'secretRef: { name: aws-backup }' "$KC_APPLIED" && b=1 || b=0
if [ "$a$b" = 11 ]; then ok "s3:// source requires --s3-secret and streams in-cluster"
else bad "s3 restore" "needs-secret=$a happy=$b rc=$rc out=$out"; fi

echo "=== round trip ==="

# 24. The drill itself, in miniature: back the volume up with backup-user.sh,
#     restore THAT archive with restore-user.sh, and require the tree to come
#     back byte-identical. This is the only test that proves the two scripts
#     agree about the format rather than each being self-consistent.
reset_env
"$BACKUP" alice --out "$WORK/rt.tar.gz" >/dev/null 2>&1; brc=$?
reset_env
"$RESTORE" alice "$WORK/rt.tar.gz" >/dev/null 2>&1; rrc=$?
if [ "$brc" = 0 ] && [ "$rrc" = 0 ] && diff -r "$FIX" "$KC_EXTRACT_DIR" >/dev/null 2>&1; then
  ok "backup -> restore round trip reproduces the volume byte-identically"
else bad "round trip" "backup rc=$brc restore rc=$rrc diff:
$(diff -r "$FIX" "$KC_EXTRACT_DIR" 2>&1 | head -10)"; fi

echo ""
printf 'passed %d, failed %d\n' "$PASS" "$FAIL"
[ "$FAIL" = 0 ] || exit 1
