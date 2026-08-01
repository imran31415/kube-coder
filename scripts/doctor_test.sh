#!/usr/bin/env bash
# Tests for scripts/doctor.sh — the operator preflight (#580).
#
# doctor.sh only talks to the outside world through a handful of CLIs
# (kubectl, helm, dig/host/getent, git), so its logic is fully exercisable
# offline by shimming those on PATH and driving each check's inputs from env.
#
# The properties that actually matter here:
#   * it reports ALL failures in one run (never stops at the first),
#   * it exits non-zero iff at least one check FAILed (warnings never fail),
#   * the wildcard-DNS check compares against the ingress IP — matching passes,
#     a wrong/absent record fails (the single highest-value check), and
#   * it is READ-ONLY: it must never invoke a mutating kubectl verb.
#
# Run:  bash scripts/doctor_test.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCTOR="$HERE/doctor.sh"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

# --- PATH shims --------------------------------------------------------------
# A temp bin/ shadowing the real kubectl/helm/dig/host/getent/git. Each shim
# reads its behavior from KC_* env vars the individual tests set. kubectl also
# appends any mutating verb to $KC_MUTATION_LOG so we can assert read-only.
SHIMDIR="$(mktemp -d)"
export KC_MUTATION_LOG="$SHIMDIR/mutations.log"
trap 'rm -rf "$SHIMDIR"' EXIT

cat > "$SHIMDIR/kubectl" <<'SHIM'
#!/usr/bin/env bash
verb="${1:-}"
case "$verb" in
  apply|create|patch|delete|replace|edit|scale|annotate|label|rollout|set|cp|exec|drain|cordon|uncordon)
    echo "kubectl $*" >> "$KC_MUTATION_LOG"; exit 1 ;;
esac
args="$*"
case "$args" in
  "version -o json"*)
    [ -n "${KC_KUBECTL_OK:-}" ] || exit 1
    printf '{\n "serverVersion": {\n  "major": "%s",\n  "minor": "%s"\n }\n}\n' \
      "${KC_K8S_MAJOR:-1}" "${KC_K8S_MINOR:-27}"; exit 0 ;;
  "cluster-info"*) [ -n "${KC_KUBECTL_OK:-}" ] && exit 0 || exit 1 ;;
esac
[ -n "${KC_KUBECTL_OK:-}" ] || exit 1
case "$args" in
  *loadBalancer.ingress*ip*)       printf '%s' "${KC_INGRESS_IP:-}";   exit 0 ;;
  *loadBalancer.ingress*hostname*) printf '%s' "${KC_INGRESS_HOST:-}"; exit 0 ;;
  *"get svc -A"*ingress-nginx*)
    [ -n "${KC_INGRESS_PRESENT:-}" ] && { printf '%s\n' "${KC_INGRESS_SVC:-ingress-nginx/ingress-nginx-controller}"; exit 0; }
    exit 0 ;;  # empty result, but the command itself succeeds
  *"get svc -n ingress-nginx ingress-nginx-controller"*)
    [ -n "${KC_INGRESS_PRESENT:-}" ] && exit 0 || exit 1 ;;
  *"get crd clusterissuers"*)      [ -n "${KC_CM_CRD:-}" ] && exit 0 || exit 1 ;;
  *"get deploy"*cert-manager*"-o name"*)
    [ -n "${KC_CM_DEPLOY:-}" ] && { echo "deployment.apps/cert-manager"; exit 0; }; exit 0 ;;
  *"get deploy"*cert-manager*)     [ -n "${KC_CM_DEPLOY:-}" ] && exit 0 || exit 0 ;;
  *"get clusterissuers"*jsonpath*) printf '%s' "${KC_ISSUERS:-}"; [ -n "${KC_ISSUERS:-}" ] && echo; exit 0 ;;
  *"get secret regcred"*jsonpath*) printf '%s' "${KC_REGCRED_TYPE:-}"; exit 0 ;;
  *"get secret regcred"*)          [ -n "${KC_REGCRED:-}" ] && exit 0 || exit 1 ;;
  *"get ns "*)                     [ -n "${KC_NS_EXISTS:-}" ] && exit 0 || exit 1 ;;
  *"get serviceaccount default"*)  [ -n "${KC_NS_EXISTS:-}" ] && exit 0 || exit 1 ;;
esac
exit 0
SHIM

cat > "$SHIMDIR/helm" <<'SHIM'
#!/usr/bin/env bash
[ "${1:-}" = "version" ] && { printf '%s\n' "${KC_HELM_VER:-v3.14.4+gtest}"; exit 0; }
exit 0
SHIM

# dig is the authoritative resolver in the shim world; host/getent report nothing
# so resolution deterministically flows through dig.
cat > "$SHIMDIR/dig" <<'SHIM'
#!/usr/bin/env bash
name="${!#}"   # last arg is the queried name
case "$name" in
  *"${KC_TEST_DOMAIN:-__no_such_domain__}") printf '%s\n' ${KC_WILDCARD_IPS:-} ;;
esac
exit 0
SHIM
cat > "$SHIMDIR/host"   <<'SHIM'
#!/usr/bin/env bash
exit 1
SHIM
cat > "$SHIMDIR/getent" <<'SHIM'
#!/usr/bin/env bash
exit 2
SHIM

cat > "$SHIMDIR/git" <<'SHIM'
#!/usr/bin/env bash
[ "${1:-}" = "ls-remote" ] && { [ -n "${KC_GIT_OK:-}" ] && exit 0 || exit 128; }
exit 0
SHIM
chmod +x "$SHIMDIR"/*

# run_doctor: run doctor.sh with the shims first on PATH. Env for the scenario is
# passed by the caller via the environment. Captures combined output + rc.
run_doctor() {
  DOCTOR_OUT="$(PATH="$SHIMDIR:$PATH" bash "$DOCTOR" 2>&1)"; DOCTOR_RC=$?
}

# A fully-healthy environment. Individual tests override single vars to break one
# check at a time. `env -i`-style: we export everything the shims read.
healthy_env() {
  export KC_KUBECTL_OK=1 KC_K8S_MAJOR=1 KC_K8S_MINOR=27 KC_HELM_VER="v3.14.4+gtest"
  export KC_INGRESS_PRESENT=1 KC_INGRESS_SVC="ingress-nginx/ingress-nginx-controller"
  export KC_INGRESS_IP="203.0.113.10" KC_INGRESS_HOST=""
  export KC_CM_CRD=1 KC_CM_DEPLOY=1 KC_ISSUERS="letsencrypt-production"
  export KC_REGCRED=1 KC_REGCRED_TYPE="kubernetes.io/dockerconfigjson"
  export KC_NS_EXISTS=1
  export NAMESPACE="coder" DOMAIN="" USERS_REPO=""
  export KC_TEST_DOMAIN="dev.example.io" KC_WILDCARD_IPS="203.0.113.10" KC_GIT_OK=1
  : > "$KC_MUTATION_LOG"
}

assert_fail_contains() { # <name> <substr> ; inspects the last run_doctor output
  local name="$1" sub="$2"
  if grep -q '\[FAIL\]' <<<"$DOCTOR_OUT" && grep -qi "$sub" <<<"$DOCTOR_OUT"; then
    ok "$name"
  else
    bad "$name" "expected a FAIL mentioning '$sub'; got: $DOCTOR_OUT"
  fi
}

# Scenarios run in the current shell (not a subshell) so ok/bad update the
# tallies. healthy_env re-exports every shim var on each call, so a scenario's
# overrides never leak into the next one.
echo "doctor tests"

# ── 1. Healthy cluster: all pass, exit 0, no [FAIL] ─────────────────────────
echo "- all-healthy"
healthy_env; run_doctor
grep -q '\[FAIL\]' <<<"$DOCTOR_OUT" && bad "no FAIL on healthy env" "$DOCTOR_OUT" || ok "no FAIL on healthy env"
[ "$DOCTOR_RC" -eq 0 ] && ok "healthy env exits 0" || bad "healthy env exits 0" "rc=$DOCTOR_RC"

# ── 2. Read-only: a healthy run touches no mutating verb ────────────────────
echo "- read-only"
healthy_env; run_doctor
if [ -s "$KC_MUTATION_LOG" ]; then bad "no mutating kubectl calls" "$(cat "$KC_MUTATION_LOG")"
else ok "no mutating kubectl calls"; fi

# ── 3. Reports ALL failures in one run (never stops at the first) ───────────
echo "- reports all failures at once"
healthy_env
KC_K8S_MINOR=15            # too-old cluster  -> FAIL
KC_INGRESS_PRESENT=""      # no ingress       -> FAIL
KC_ISSUERS=""              # no ClusterIssuer -> FAIL
KC_REGCRED=""              # no regcred       -> FAIL
run_doctor
n=$(grep -c '\[FAIL\]' <<<"$DOCTOR_OUT")
[ "$n" -ge 4 ] && ok "all four failures reported ($n)" || bad "all four failures reported" "got $n: $DOCTOR_OUT"
[ "$DOCTOR_RC" -ne 0 ] && ok "any failure => non-zero exit" || bad "any failure => non-zero exit" "rc=$DOCTOR_RC"

# ── 4. Individual check failures each FAIL ──────────────────────────────────
echo "- individual checks fail on bad input"
healthy_env; KC_K8S_MINOR=15; run_doctor;         assert_fail_contains "old k8s version"     "below the required 1.19"
healthy_env; KC_HELM_VER="v2.17.0"; run_doctor;   assert_fail_contains "old helm"            "below the required 3.0"
healthy_env; KC_INGRESS_PRESENT=""; run_doctor;   assert_fail_contains "no ingress"          "no nginx-ingress controller"
healthy_env; KC_CM_CRD=""; run_doctor;            assert_fail_contains "no cert-manager"     "cert-manager is not installed"
healthy_env; KC_ISSUERS=""; run_doctor;           assert_fail_contains "no ClusterIssuer"    "no ClusterIssuer exists"
healthy_env; KC_REGCRED=""; run_doctor;           assert_fail_contains "no regcred"          "regcred' not found"
healthy_env; KC_KUBECTL_OK=""; run_doctor;        assert_fail_contains "unreachable cluster" "cannot reach a cluster"

# ── 5. Wildcard DNS — the highest-value check ───────────────────────────────
echo "- wildcard DNS vs ingress IP"
healthy_env; DOMAIN="dev.example.io"; KC_WILDCARD_IPS="203.0.113.10"   # == ingress IP
run_doctor
grep -Eq '\[OK\].*wildcard DNS resolves to the ingress IP' <<<"$DOCTOR_OUT" \
  && ok "matching wildcard passes" || bad "matching wildcard passes" "$DOCTOR_OUT"

healthy_env; DOMAIN="dev.example.io"; KC_WILDCARD_IPS="198.51.100.99"  # != ingress IP
run_doctor
grep -Eq '\[FAIL\].*WRONG address' <<<"$DOCTOR_OUT" \
  && ok "mismatched wildcard fails" || bad "mismatched wildcard fails" "$DOCTOR_OUT"
[ "$DOCTOR_RC" -ne 0 ] && ok "mismatched wildcard => non-zero" || bad "mismatched wildcard => non-zero" "rc=$DOCTOR_RC"

healthy_env; DOMAIN="dev.example.io"; KC_WILDCARD_IPS=""               # no record
run_doctor
grep -Eq '\[FAIL\].*wildcard DNS does not resolve' <<<"$DOCTOR_OUT" \
  && ok "absent wildcard fails" || bad "absent wildcard fails" "$DOCTOR_OUT"

healthy_env   # DOMAIN unset -> skipped as a WARN, must NOT fail the run
run_doctor
grep -Eq '\[WARN\].*no DOMAIN supplied' <<<"$DOCTOR_OUT" \
  && ok "no DOMAIN => wildcard skipped (warn)" || bad "no DOMAIN => wildcard skipped (warn)" "$DOCTOR_OUT"
[ "$DOCTOR_RC" -eq 0 ] && ok "skipped wildcard does not fail the run" || bad "skipped wildcard does not fail the run" "rc=$DOCTOR_RC"

# ── 6. Warnings alone never fail the run ────────────────────────────────────
echo "- warnings never fail the run"
healthy_env   # DOMAIN + USERS_REPO unset => two WARN skips, zero FAIL
run_doctor
w=$(grep -c '\[WARN\]' <<<"$DOCTOR_OUT")
[ "$w" -ge 2 ] && ok "warnings present ($w)" || bad "warnings present" "got $w"
[ "$DOCTOR_RC" -eq 0 ] && ok "warnings-only exits 0" || bad "warnings-only exits 0" "rc=$DOCTOR_RC"

# ── 7. GitOps repo reachability ─────────────────────────────────────────────
echo "- gitops repo reachability"
healthy_env; USERS_REPO="github.com/acme/kube-coder-users.git"; KC_GIT_OK=1
run_doctor
grep -Eq '\[OK\].*GitOps repo reachable' <<<"$DOCTOR_OUT" \
  && ok "reachable repo passes" || bad "reachable repo passes" "$DOCTOR_OUT"

healthy_env; USERS_REPO="github.com/acme/kube-coder-users.git"; KC_GIT_OK=""
run_doctor
grep -Eq '\[FAIL\].*GitOps repo not reachable' <<<"$DOCTOR_OUT" \
  && ok "unreachable repo fails" || bad "unreachable repo fails" "$DOCTOR_OUT"

echo
echo "passed: $PASS  failed: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
