#!/usr/bin/env bash
# Tests for kc-issue's issue-body lint (#569).
#
# The lint decides whether we spend a worktree and real tokens on an issue, so
# it needs to be exercisable without GitHub, git, or a spawn. `kc-issue.sh lint`
# exists for exactly that, and this is its test suite.
#
# The two properties that actually matter:
#   * it FIRES on a thin body (otherwise it is useless), and
#   * it stays SILENT on a good one (a noisy lint gets ignored, which is worse
#     than no lint at all).
#
# Run:  bash .claude/skills/kc-issue/lint_test.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KC="$HERE/kc-issue.sh"
PASS=0; FAIL=0

# A body that trips nothing — the false-positive control.
GOOD_BODY=$(cat <<'EOF'
## Problem
`charts/workspace/server.py` returns a 500 when the memory store is empty
because `top_for_prompt` assumes at least one row exists in the table.

## Proposal
Guard the empty case and return an empty list instead of indexing blindly.

## Acceptance criteria
- [ ] `/api/memory` returns 200 with `{"memories": []}` on an empty store
- [ ] A unit test covers the empty-store path

## Verification
Run `python3 -m unittest tests.memory_test` and confirm it passes.
EOF
)

ok()   { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

# _lint <body> [labels] -> stderr text on stdout (findings are stderr-only)
_lint() {
  local body="$1" labels="${2:-}"
  KC_ISSUE_LABELS="$labels" bash "$KC" lint - <<<"$body" 2>&1 >/dev/null
}

# assert_fires <name> <code> <body> [labels]
assert_fires() {
  local name="$1" code="$2" body="$3" labels="${4:-}" out
  out=$(_lint "$body" "$labels")
  if grep -q "$code" <<<"$out"; then ok "$name"
  else bad "$name" "expected '$code' in findings, got: ${out:-<none>}"; fi
}

# assert_silent <name> <code> <body> [labels]
assert_silent() {
  local name="$1" code="$2" body="$3" labels="${4:-}" out
  out=$(_lint "$body" "$labels")
  if grep -q "$code" <<<"$out"; then bad "$name" "did NOT expect '$code', got: $out"
  else ok "$name"; fi
}

echo "kc-issue lint tests"

# ── Each signal fires on a body that deserves it ────────────────────────────
echo "- signals fire"
assert_fires "thin body"                thin-body                 "fix it"
assert_fires "no acceptance criteria"   no-acceptance-criteria    "Refactor the thing in charts/workspace/server.py and test it thoroughly please."
assert_fires "no file pointers"         no-file-pointers          "We should rework the whole retrieval flow. Acceptance criteria: it works. Tested by hand."
assert_fires "no verification path"     no-verification           "Change \`charts/workspace/server.py\` per the acceptance criteria listed in the linked design doc above."
assert_fires "unquantified comparative" unquantified-comparative  "Make the dashboard faster. Acceptance criteria in charts/workspace/server.py. Tested manually."
assert_fires "needs-scoping label"      needs-scoping             "$GOOD_BODY" "bug,needs-scoping"

# ── No false positives on a well-formed body ───────────────────────────────
echo "- no false positives on a good body"
for code in thin-body no-acceptance-criteria no-file-pointers no-verification \
            unquantified-comparative needs-scoping; do
  assert_silent "good body: no $code" "$code" "$GOOD_BODY"
done

# ── Signal-specific non-firing ─────────────────────────────────────────────
echo "- signals stay quiet when satisfied"
assert_silent "quantified comparative is fine" unquantified-comparative \
  "Make it faster: cut p95 from 800ms to 200ms. Acceptance criteria above. See charts/workspace/server.py. Tested with a benchmark."
assert_silent "checkbox counts as criteria" no-acceptance-criteria \
  "Do the thing.
- [ ] it works
See charts/workspace/server.py and run the tests."
assert_silent "labels without needs-scoping" needs-scoping "$GOOD_BODY" "bug,enhancement"

# ── Exit codes ─────────────────────────────────────────────────────────────
echo "- exit codes"
bash "$KC" lint - <<<"$GOOD_BODY" >/dev/null 2>&1
[ $? -eq 0 ] && ok "good body exits 0" || bad "good body exits 0"

KC_ISSUE_LABELS="needs-scoping" bash "$KC" lint - <<<"$GOOD_BODY" >/dev/null 2>&1
[ $? -eq 1 ] && ok "blocker exits 1" || bad "blocker exits 1"

bash "$KC" lint - <<<"fix it" >/dev/null 2>&1
[ $? -eq 0 ] && ok "warnings alone still exit 0 (warn, never block)" \
             || bad "warnings alone still exit 0 (warn, never block)"

# ── stdout purity: the caller parses stdout as JSON, so findings must not
#    leak into it. This is the regression that would break the Hypervisor.
echo "- stdout stays machine-readable"
# This half needs no jq and is the property that actually protects the caller:
# the human report must never contaminate the JSON channel.
out=$(bash "$KC" lint - <<<"fix it" 2>/dev/null)
grep -q 'kc-issue: lint:' <<<"$out" && bad "no human text on stdout" "found it: $out" \
                                    || ok "no human text on stdout"
[ -n "$out" ] && ok "stdout is non-empty" || bad "stdout is non-empty"

if command -v jq >/dev/null 2>&1; then
  if jq -e 'type == "array"' <<<"$out" >/dev/null 2>&1; then
    ok "stdout is a JSON array"
  else
    bad "stdout is a JSON array" "got: $out"
  fi
  n=$(jq 'length' <<<"$out" 2>/dev/null)
  [ "${n:-0}" -ge 4 ] && ok "findings present in JSON ($n)" \
                      || bad "findings present in JSON" "got $n"
  # Every finding carries the three fields the skill renders.
  jq -e 'all(.[]; has("severity") and has("code") and has("message"))' \
     <<<"$out" >/dev/null 2>&1 \
    && ok "findings have severity/code/message" \
    || bad "findings have severity/code/message" "got: $out"
else
  echo "  skip jq-shape assertions (jq not installed; present in the pod image)"
fi

# ── Hostile input must not break the script ────────────────────────────────
echo "- hostile input"
out=$(_lint 'body with `backticks`, $(command substitution), "quotes" and émoji 🚀')
[ -n "$out" ] && ok "quotes/substitution/unicode handled" \
              || bad "quotes/substitution/unicode handled" "no output at all"

echo
echo "passed: $PASS  failed: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
