#!/usr/bin/env bash
# scripts/doctor.sh — operator preflight for a cloud / multi-tenant ("Option B")
# kube-coder install. Runs BEFORE the first user or workspace exists.
#
# Usage: scripts/doctor.sh          (env: NAMESPACE, DOMAIN, USERS_REPO)
#        NAMESPACE=coder DOMAIN=dev.example.io scripts/doctor.sh
#
# READ-ONLY diagnosis. It never installs, applies, patches, or mutates anything
# — every cluster call is a `get`/`status`/`ls-remote`. It checks the documented
# Option-B prerequisites and reports ALL failures at once (it never stops at the
# first), each with a specific remediation, then exits non-zero if any check
# FAILed. Warnings (e.g. a skipped wildcard-DNS check) never fail the run.
#
# Checks:
#   1. kubectl reachable + cluster server version >= 1.19; helm >= 3.0
#   2. an nginx-ingress controller is present and its external IP/hostname
#      is known and resolvable
#   3. cert-manager present AND at least one ClusterIssuer that actually exists
#   4. wildcard DNS: <nonce>.$DOMAIN resolves to the ingress external IP
#      (the single highest-value check — a broken wildcard silently produces a
#      dead workspace much later). Skipped with a note when DOMAIN is unset.
#   5. a `regcred` image-pull secret is present in the target namespace
#   6. the GitOps config repo is reachable with the credentials in use
#
# Style mirrors scripts/validate-user.sh (that one is per-user, post-scaffold;
# this one is cluster-level, pre-first-user — different scope, so they are kept
# separate). Wired into the Makefile as `make doctor`.

set -uo pipefail

# --- Parameters (env, matching the Makefile's NAMESPACE / DOMAIN naming) ------
# The shared regcred image-pull Secret + control-plane releases live in the
# control-plane namespace, which the Makefile calls NAMESPACE (default `coder`).
NS="${NAMESPACE:-coder}"
# Operator-supplied base domain for the wildcard-DNS check (e.g. dev.example.io).
# Empty => that check is skipped with a note on how to run it.
DOMAIN="${DOMAIN:-}"
# GitOps workspace-config repo (host/path, no scheme). Resolved from KC_USERS_REPO
# / USERS_REPO, or the gitignored controller values, mirroring the Makefile.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USERS_REPO="${USERS_REPO:-${KC_USERS_REPO:-}}"
if [ -z "$USERS_REPO" ]; then
  USERS_REPO="$(awk '/^[[:space:]]*gitops:/{f=1} f&&/repo:/{print $2; exit}' \
    "$ROOT/users-private/_controller/values.yaml" 2>/dev/null || true)"
fi

# --- Pretty output. Plain ASCII markers so this works in any TTY. -------------
PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0
pass() { echo "  [OK]   $*"; PASS_COUNT=$((PASS_COUNT+1)); }
warn() { echo "  [WARN] $*"; WARN_COUNT=$((WARN_COUNT+1)); }
fail() { echo "  [FAIL] $*"; FAIL_COUNT=$((FAIL_COUNT+1)); }
# Indent a remediation hint under the preceding [FAIL]/[WARN] line.
hint() { echo "         -> $*"; }

echo "doctor: kube-coder operator preflight (namespace: $NS${DOMAIN:+, domain: $DOMAIN})"
echo

# Resolve an A record (IPv4) for a name, best-effort across the tools that might
# be installed. Prints one IP per line (may be empty). Never fails the shell.
resolve_ipv4() {
  local name="$1" out=""
  if command -v dig >/dev/null 2>&1; then
    out="$(dig +short A "$name" 2>/dev/null | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$')"
  fi
  if [ -z "$out" ] && command -v host >/dev/null 2>&1; then
    out="$(host -t A "$name" 2>/dev/null | awk '/has address/{print $NF}')"
  fi
  if [ -z "$out" ] && command -v getent >/dev/null 2>&1; then
    out="$(getent ahostsv4 "$name" 2>/dev/null | awk '{print $1}' | sort -u)"
  fi
  echo "$out"
}

# True if a name resolves to anything at all (A record or otherwise).
resolves_at_all() {
  local name="$1"
  getent hosts "$name" >/dev/null 2>&1 && return 0
  command -v host >/dev/null 2>&1 && host "$name" >/dev/null 2>&1 && return 0
  command -v dig  >/dev/null 2>&1 && [ -n "$(dig +short "$name" 2>/dev/null)" ] && return 0
  return 1
}

# =============================================================================
# 1. kubectl reachable + versions
# =============================================================================
echo "1. CLI tooling + cluster reachability"
KUBECTL_OK=0
if ! command -v kubectl >/dev/null 2>&1; then
  fail "kubectl not on PATH"
  hint "install kubectl: https://kubernetes.io/docs/tasks/tools/"
elif ! kubectl version -o json >/dev/null 2>&1 && ! kubectl cluster-info >/dev/null 2>&1; then
  fail "kubectl is installed but cannot reach a cluster"
  hint "check your kubeconfig / context: kubectl config current-context && kubectl cluster-info"
else
  KUBECTL_OK=1
  # serverVersion.minor can carry a '+' suffix (e.g. \"24+\"); strip non-digits.
  SRV_JSON="$(kubectl version -o json 2>/dev/null)"
  SRV_MAJOR="$(printf '%s' "$SRV_JSON" | awk -F'"' '/"serverVersion"/{f=1} f&&/"major"/{gsub(/[^0-9]/,"",$4); print $4; exit}')"
  SRV_MINOR="$(printf '%s' "$SRV_JSON" | awk -F'"' '/"serverVersion"/{f=1} f&&/"minor"/{gsub(/[^0-9]/,"",$4); print $4; exit}')"
  if [ -z "$SRV_MAJOR" ] || [ -z "$SRV_MINOR" ]; then
    warn "reached the cluster but could not parse its server version — skipping the >=1.19 check"
    hint "check manually: kubectl version -o json | grep -A3 serverVersion"
  elif [ "$SRV_MAJOR" -gt 1 ] || { [ "$SRV_MAJOR" -eq 1 ] && [ "$SRV_MINOR" -ge 19 ]; }; then
    pass "cluster reachable, server version $SRV_MAJOR.$SRV_MINOR (>= 1.19)"
  else
    fail "cluster server version $SRV_MAJOR.$SRV_MINOR is below the required 1.19"
    hint "upgrade the cluster control plane to Kubernetes 1.19 or newer"
  fi
fi

if ! command -v helm >/dev/null 2>&1; then
  fail "helm not on PATH"
  hint "install Helm 3: https://helm.sh/docs/intro/install/"
else
  HELM_VER="$(helm version --short 2>/dev/null | grep -oE 'v?[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
  HELM_MAJOR="$(printf '%s' "$HELM_VER" | sed -E 's/^v?([0-9]+)\..*/\1/')"
  if [ -z "$HELM_MAJOR" ]; then
    warn "helm is installed but its version could not be parsed — skipping the >=3.0 check"
    hint "check manually: helm version --short"
  elif [ "$HELM_MAJOR" -ge 3 ]; then
    pass "helm ${HELM_VER} (>= 3.0)"
  else
    fail "helm ${HELM_VER} is below the required 3.0"
    hint "upgrade Helm to v3: https://helm.sh/docs/intro/install/"
  fi
fi
echo

# =============================================================================
# 2. nginx-ingress controller present + external address known
# =============================================================================
echo "2. nginx-ingress controller"
INGRESS_IP=""      # external IPv4 of the ingress LoadBalancer, when it has one
INGRESS_HOST=""    # external hostname (cloud LB CNAME), when it has one instead
if [ "$KUBECTL_OK" -ne 1 ]; then
  warn "skipping (no reachable cluster)"
else
  # Find the controller Service (type LoadBalancer) by the canonical ingress-nginx
  # labels, across every namespace it might have been installed into.
  ING_SVC="$(kubectl get svc -A \
    -l app.kubernetes.io/name=ingress-nginx,app.kubernetes.io/component=controller \
    -o jsonpath='{range .items[?(@.spec.type=="LoadBalancer")]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}' 2>/dev/null | head -1)"
  # Fall back to the well-known default name if the labels differ.
  if [ -z "$ING_SVC" ] && kubectl get svc -n ingress-nginx ingress-nginx-controller >/dev/null 2>&1; then
    ING_SVC="ingress-nginx/ingress-nginx-controller"
  fi
  if [ -z "$ING_SVC" ]; then
    fail "no nginx-ingress controller LoadBalancer Service found"
    hint "install it, e.g.: helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx && helm install ingress-nginx ingress-nginx/ingress-nginx -n ingress-nginx --create-namespace"
  else
    ISNS="${ING_SVC%%/*}"; ISNAME="${ING_SVC##*/}"
    INGRESS_IP="$(kubectl get svc -n "$ISNS" "$ISNAME" -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null)"
    INGRESS_HOST="$(kubectl get svc -n "$ISNS" "$ISNAME" -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null)"
    if [ -n "$INGRESS_IP" ]; then
      pass "ingress controller found: $ING_SVC (external IP $INGRESS_IP)"
    elif [ -n "$INGRESS_HOST" ]; then
      # A cloud LB hostname (e.g. AWS ELB). Resolve it so downstream checks and
      # the operator both have a concrete target IP.
      pass "ingress controller found: $ING_SVC (external hostname $INGRESS_HOST)"
      if resolves_at_all "$INGRESS_HOST"; then
        pass "ingress hostname resolves: $INGRESS_HOST"
        INGRESS_IP="$(resolve_ipv4 "$INGRESS_HOST" | head -1)"
      else
        fail "ingress hostname does not resolve yet: $INGRESS_HOST"
        hint "wait for the cloud load balancer's DNS to propagate, then re-run"
      fi
    else
      fail "ingress controller $ING_SVC has no external IP/hostname yet (LoadBalancer pending)"
      hint "check your cloud LoadBalancer provisioning: kubectl get svc -n $ISNS $ISNAME -w"
    fi
  fi
fi
echo

# =============================================================================
# 3. cert-manager present + at least one ClusterIssuer that exists
# =============================================================================
echo "3. cert-manager + ClusterIssuer"
if [ "$KUBECTL_OK" -ne 1 ]; then
  warn "skipping (no reachable cluster)"
else
  # The ClusterIssuer CRD's presence is the reliable signal cert-manager's CRDs
  # are installed; then confirm the controller Deployment is actually running.
  if ! kubectl get crd clusterissuers.cert-manager.io >/dev/null 2>&1; then
    fail "cert-manager is not installed (no clusterissuers.cert-manager.io CRD)"
    hint "install it: helm repo add jetstack https://charts.jetstack.io && helm install cert-manager jetstack/cert-manager -n cert-manager --create-namespace --set crds.enabled=true"
  else
    if kubectl get deploy -l app.kubernetes.io/name=cert-manager -A >/dev/null 2>&1 \
       && [ -n "$(kubectl get deploy -l app.kubernetes.io/name=cert-manager -A -o name 2>/dev/null)" ]; then
      pass "cert-manager is installed"
    else
      warn "cert-manager CRDs exist but its controller Deployment was not found"
      hint "confirm the controller is running: kubectl get pods -n cert-manager"
    fi
    # At least one ClusterIssuer must actually exist — the workspace ingresses
    # annotate cert-manager.io/cluster-issuer: letsencrypt-production by default.
    ISSUERS="$(kubectl get clusterissuers.cert-manager.io -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null)"
    if [ -n "$ISSUERS" ]; then
      pass "ClusterIssuer(s) present: $(echo "$ISSUERS" | paste -sd, - )"
    else
      fail "cert-manager is installed but no ClusterIssuer exists"
      hint "create one (e.g. letsencrypt-production) — see docs/deploy-on-kubernetes.md; without it workspace TLS never issues"
    fi
  fi
fi
echo

# =============================================================================
# 4. Wildcard DNS — <nonce>.$DOMAIN resolves to the ingress external IP.
#    Highest-value check: a broken wildcard silently yields a dead workspace
#    much later, not here.
# =============================================================================
echo "4. wildcard DNS (*.\$DOMAIN -> ingress)"
if [ -z "$DOMAIN" ]; then
  warn "skipped — no DOMAIN supplied"
  hint "run with your base domain to check it: DOMAIN=dev.example.io make doctor"
else
  NONCE="kc-doctor-$(date +%s)-$$"
  PROBE="${NONCE}.${DOMAIN}"
  WILDCARD_IPS="$(resolve_ipv4 "$PROBE")"
  if [ -z "$WILDCARD_IPS" ]; then
    fail "wildcard DNS does not resolve: $PROBE returned no A record"
    hint "add a wildcard record: *.$DOMAIN  ->  ${INGRESS_IP:-<your ingress external IP>}"
  elif [ -z "$INGRESS_IP" ]; then
    # We got an answer but have no ingress IP to compare against (check 2 failed
    # or the LB only exposes a hostname we could not resolve).
    warn "wildcard $PROBE resolves to $(echo "$WILDCARD_IPS" | paste -sd, - ) but the ingress external IP is unknown — cannot confirm it points at the ingress"
    hint "fix check 2 (ingress external address) then re-run to verify the wildcard target"
  elif echo "$WILDCARD_IPS" | grep -qxF "$INGRESS_IP"; then
    pass "wildcard DNS resolves to the ingress IP: $PROBE -> $INGRESS_IP"
  else
    fail "wildcard DNS points at the WRONG address: $PROBE -> $(echo "$WILDCARD_IPS" | paste -sd, - ), but the ingress external IP is $INGRESS_IP"
    hint "repoint the wildcard record: *.$DOMAIN  ->  $INGRESS_IP"
  fi
fi
echo

# =============================================================================
# 5. regcred image-pull secret present in the target namespace
# =============================================================================
echo "5. regcred image-pull secret (namespace: $NS)"
if [ "$KUBECTL_OK" -ne 1 ]; then
  warn "skipping (no reachable cluster)"
elif ! kubectl get ns "$NS" >/dev/null 2>&1 && ! kubectl get serviceaccount default -n "$NS" >/dev/null 2>&1; then
  fail "target namespace '$NS' does not exist"
  hint "create it (kubectl create namespace $NS) or pass the right one: NAMESPACE=<ns> make doctor"
elif kubectl get secret regcred -n "$NS" >/dev/null 2>&1; then
  TYPE="$(kubectl get secret regcred -n "$NS" -o jsonpath='{.type}' 2>/dev/null)"
  if [ "$TYPE" = "kubernetes.io/dockerconfigjson" ]; then
    pass "image-pull secret 'regcred' present in $NS (kubernetes.io/dockerconfigjson)"
  else
    fail "secret 'regcred' in $NS is of type '$TYPE', not kubernetes.io/dockerconfigjson"
    hint "recreate it: kubectl create secret docker-registry regcred --docker-server=... --docker-username=... --docker-password=... -n $NS"
  fi
else
  fail "image-pull secret 'regcred' not found in namespace $NS"
  hint "create it: kubectl create secret docker-registry regcred --docker-server=<registry> --docker-username=<user> --docker-password=<token> -n $NS"
fi
echo

# =============================================================================
# 6. GitOps config repo reachable with the credentials in use
# =============================================================================
echo "6. GitOps config repo"
if [ -z "$USERS_REPO" ]; then
  warn "skipped — GitOps repo unknown"
  hint "set it to check reachability: USERS_REPO=github.com/<org>/<repo>.git make doctor  (or provision.gitops.repo in users-private/_controller/values.yaml)"
elif ! command -v git >/dev/null 2>&1; then
  warn "git not on PATH — cannot check GitOps repo reachability"
  hint "install git to verify $USERS_REPO"
else
  # Strip any scheme the operator may have included, then probe over https with
  # whatever credential helper / token git is configured to use. Read-only.
  REPO_NOSCHEME="${USERS_REPO#https://}"; REPO_NOSCHEME="${REPO_NOSCHEME#http://}"
  if GIT_TERMINAL_PROMPT=0 git ls-remote "https://${REPO_NOSCHEME}" >/dev/null 2>&1; then
    pass "GitOps repo reachable with current credentials: $REPO_NOSCHEME"
  else
    fail "GitOps repo not reachable with current credentials: $REPO_NOSCHEME"
    hint "authenticate git for it (e.g. 'gh auth login' or a PAT credential helper); confirm manually: git ls-remote https://$REPO_NOSCHEME"
  fi
fi
echo

# =============================================================================
# Summary
# =============================================================================
echo "Summary: $PASS_COUNT ok, $WARN_COUNT warn, $FAIL_COUNT fail"
if [ "$FAIL_COUNT" -gt 0 ]; then
  echo "doctor: FAILED — fix the items above before onboarding the first workspace."
  exit 1
fi
if [ "$WARN_COUNT" -gt 0 ]; then
  echo "doctor: OK (with warnings — review the notes above)"
else
  echo "doctor: OK — cloud prerequisites look good."
fi
exit 0
