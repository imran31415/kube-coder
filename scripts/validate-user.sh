#!/usr/bin/env bash
# scripts/validate-user.sh — pre-deploy sanity check for a workspace.
#
# Usage: scripts/validate-user.sh <username>
#
# Verifies that:
#   • A values.yaml exists under deployments/<name>/ or users-private/<name>/.
#   • No "CHANGE ME" / placeholder strings remain in the merged values
#     (values.yaml + every *.yaml in the matching secrets dir).
#   • cookieSecret is the right length for oauth2-proxy.
#   • DNS for user.host resolves (best-effort — does not assert IP target).
#   • Cluster prereqs are present (base-infrastructure release + regcred
#     image-pull secret in the workspace namespace).
#
# Exits non-zero on any failure with a clear message. Used by
# `make deploy USER=<name>` as a pre-step.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ $# -lt 1 ] || [ -z "${1:-}" ]; then
  echo "Usage: $0 <username>" >&2
  exit 2
fi
NAME="$1"

# Pretty output. Use plain ASCII markers so this works in any TTY.
PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0
pass() { echo "  [OK]   $*"; PASS_COUNT=$((PASS_COUNT+1)); }
warn() { echo "  [WARN] $*"; WARN_COUNT=$((WARN_COUNT+1)); }
fail() { echo "  [FAIL] $*"; FAIL_COUNT=$((FAIL_COUNT+1)); }

echo "validate-user: $NAME"

# 1. Locate values.yaml + secrets dir. Resolve across the same roots, in the
# same order, as the Makefile: legacy deployments/, this repo's users-private/,
# then the GitOps checkout .users/ (synced via `make users-sync`) — the unifying
# store that holds every provisioned workspace.
PUBLIC_DIR="$ROOT/deployments/$NAME"
PRIVATE_DIR="$ROOT/users-private/$NAME"
GITOPS_DIR="$ROOT/.users/users-private/$NAME"
USER_DIR=""
SECRETS_DIR=""
if [ -f "$PUBLIC_DIR/values.yaml" ]; then
  USER_DIR="$PUBLIC_DIR"
  SECRETS_DIR="$ROOT/secrets/$NAME"
elif [ -f "$PRIVATE_DIR/values.yaml" ]; then
  USER_DIR="$PRIVATE_DIR"
  SECRETS_DIR="$PRIVATE_DIR/secrets"
elif [ -f "$GITOPS_DIR/values.yaml" ]; then
  USER_DIR="$GITOPS_DIR"
  SECRETS_DIR="$GITOPS_DIR/secrets"
fi
if [ -z "$USER_DIR" ]; then
  fail "no values.yaml found under deployments/$NAME/, users-private/$NAME/, or .users/users-private/$NAME/"
  echo
  echo "Tip: run 'make users-sync' to fetch GitOps config, or 'scripts/new-user.sh $NAME' to scaffold one."
  exit 1
fi
pass "values dir: $USER_DIR"

VALUES="$USER_DIR/values.yaml"
SECRET_FILES=()
if [ -d "$SECRETS_DIR" ]; then
  while IFS= read -r f; do
    SECRET_FILES+=("$f")
  done < <(find "$SECRETS_DIR" -maxdepth 1 -name '*.yaml' -type f 2>/dev/null)
fi
if [ ${#SECRET_FILES[@]} -gt 0 ]; then
  pass "secrets dir: $SECRETS_DIR (${#SECRET_FILES[@]} *.yaml file(s))"
else
  warn "no secrets/*.yaml files under $SECRETS_DIR (deploy will use values.yaml as-is)"
fi

# 2. Placeholder + cookieSecret scan, performed on the MERGED values.
#    We use `helm template` against the workspace chart with the same -f
#    list `make deploy` would use. This is the only way to honor secrets/*
#    overrides — scanning each file separately gives false positives when
#    a placeholder in values.yaml is intentionally overridden by a secret.
CHART="$ROOT/charts/workspace"
PLACEHOLDER_RE='CHANGE ME|PLACEHOLDER-OVERRIDE|__USER__|__COOKIE_SECRET__|__IMAGE_TAG__|__DATE__'
if [ ! -d "$CHART" ]; then
  fail "workspace chart missing at $CHART"
else
  HELM_F=(-f "$VALUES")
  # Workspaces without any secrets/*.yaml (e.g. the sentinel `locked`
  # workspace) leave SECRET_FILES empty; under `set -u`, expanding an
  # empty array with [@] raises an error, so guard with :-.
  for f in "${SECRET_FILES[@]:-}"; do
    [ -n "$f" ] && HELM_F+=(-f "$f")
  done
  RENDER_OUT=$(helm template validate-preview "$CHART" "${HELM_F[@]}" 2>&1)
  RENDER_RC=$?
  if [ "$RENDER_RC" -ne 0 ]; then
    fail "helm template failed — chart will not render with current values:"
    echo "$RENDER_OUT" | sed 's/^/         /' | head -30
  else
    if echo "$RENDER_OUT" | grep -qE "$PLACEHOLDER_RE"; then
      fail "placeholders survived secrets merge (will land in cluster):"
      echo "$RENDER_OUT" | grep -nE "$PLACEHOLDER_RE" | sed 's/^/         /'
    else
      pass "no PLACEHOLDER / 'CHANGE ME' strings in merged output"
    fi

    # cookieSecret length check on merged value. Find `cookie-secret:` in the
    # rendered Secret manifest. oauth2-proxy accepts raw 16/24/32 bytes or
    # their urlsafe base64-encoded forms (24/32/44 chars).
    COOKIE=$(echo "$RENDER_OUT" \
      | awk -F'"' '/^[[:space:]]*cookie-secret:[[:space:]]*"/{print $2; exit}')
    if [ -z "$COOKIE" ]; then
      warn "could not extract cookie-secret from rendered chart — skipping length check"
    else
      COOKIE_LEN=${#COOKIE}
      case "$COOKIE_LEN" in
        16|24|32|44) pass "cookieSecret present ($COOKIE_LEN chars, merged)" ;;
        *) fail "cookieSecret is $COOKIE_LEN chars; oauth2-proxy needs 16/24/32 (raw) or 24/32/44 (base64). Regenerate: openssl rand -base64 32" ;;
      esac
    fi
  fi
fi

# 4. DNS for user.host (best-effort; doesn't assert IP match).
HOST=$(awk -F'[ "#]+' '/^[[:space:]]*host:[[:space:]]/{print $3; exit}' "$VALUES")
if [ -z "$HOST" ]; then
  warn "could not parse user.host out of $VALUES"
else
  if getent hosts "$HOST" >/dev/null 2>&1 || host "$HOST" >/dev/null 2>&1 || dig +short "$HOST" 2>/dev/null | grep -q .; then
    pass "DNS resolves: $HOST"
  else
    warn "DNS does not resolve for $HOST yet — make sure an A/CNAME points at your nginx ingress IP"
  fi
fi

# 5. Cluster prereqs (only checked if kubectl is reachable).
if command -v kubectl >/dev/null 2>&1; then
  NS=$(awk '/^namespace:/{print $2; exit}' "$VALUES")
  NS=${NS:-coder}
  if kubectl get ns "$NS" >/dev/null 2>&1; then
    pass "namespace exists: $NS"
  elif kubectl get serviceaccount default -n "$NS" >/dev/null 2>&1; then
    # `get namespaces` is cluster-scoped; under namespaced RBAC (e.g. the
    # provisioner Job's Role) it's forbidden even when the namespace exists.
    # Fall back to a namespaced probe — every namespace has a `default` SA.
    pass "namespace exists: $NS (verified via namespaced probe)"
  else
    fail "namespace '$NS' does not exist — kubectl create namespace $NS"
  fi
  if kubectl get secret regcred -n "$NS" >/dev/null 2>&1; then
    pass "image pull secret 'regcred' present in $NS"
  else
    warn "image pull secret 'regcred' missing in $NS — workspace may fail to pull the image"
  fi
  if command -v helm >/dev/null 2>&1; then
    if helm status base-infrastructure -n "$NS" >/dev/null 2>&1; then
      pass "base-infrastructure helm release is deployed"
    else
      warn "base-infrastructure release missing — run 'make deploy-base' first"
    fi
  fi
else
  warn "kubectl not on PATH — skipping cluster checks"
fi

echo
echo "Summary: $PASS_COUNT ok, $WARN_COUNT warn, $FAIL_COUNT fail"
if [ "$FAIL_COUNT" -gt 0 ]; then
  echo "validate-user: FAILED — fix the items above before deploying."
  exit 1
fi
echo "validate-user: OK"
