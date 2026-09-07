#!/bin/sh
# devlaptop/retry_test.sh — offline tests for devlaptop/retry.sh.
#
# The wrapper guards every registry install in devlaptop/Dockerfile, so a
# regression here is only visible as a mysterious image build. Nothing below
# touches the network: the "flaky command" is a shell script over a counter
# file. RETRY_DELAY=0 keeps the suite instant.
set -eu

RETRY="$(cd "$(dirname "$0")" && pwd)/retry.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
export RETRY_DELAY=0

fails=0
check() {
    if [ "$2" = "$3" ]; then
        echo "ok   - $1"
    else
        echo "FAIL - $1: expected '$3', got '$2'" >&2
        fails=$((fails + 1))
    fi
}

# A command that fails its first $1 invocations, then succeeds. Counts every
# call in $work/count so the tests can assert how many attempts really ran.
make_flaky() {
    : > "$work/count"
    cat > "$work/flaky" <<SH
#!/bin/sh
echo x >> "$work/count"
[ "\$(wc -l < "$work/count")" -gt $1 ] || exit 7
SH
    chmod +x "$work/flaky"
}

attempts() { wc -l < "$work/count" | tr -d ' '; }

# 1. Succeeds first time: no retry, no wasted attempt.
make_flaky 0
status=0; "$RETRY" "$work/flaky" || status=$?
check "passes through success" "$status" "0"
check "runs the command exactly once" "$(attempts)" "1"

# 2. The case this exists for: fails, then the registry comes back.
make_flaky 2
status=0; "$RETRY" "$work/flaky" || status=$?
check "recovers from a transient failure" "$status" "0"
check "retries until it succeeds" "$(attempts)" "3"

# 3. A genuinely bad pin must still fail the build, with the real exit code.
make_flaky 99
status=0; RETRY_ATTEMPTS=3 "$RETRY" "$work/flaky" 2>/dev/null || status=$?
check "gives up on a permanent failure" "$status" "7"
check "honours RETRY_ATTEMPTS" "$(attempts)" "3"

# 4. Arguments reach the command untouched, including embedded spaces.
status=0; out="$("$RETRY" printf '%s|%s' 'a b' 'c')" || status=$?
check "forwards arguments verbatim" "$out" "a b|c"

if [ "$fails" -ne 0 ]; then
    echo "$fails check(s) failed" >&2
    exit 1
fi
echo "all retry.sh checks passed"
