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

# 1. Locate values.yaml + secrets dir.
PUBLIC_DIR="$ROOT/deployments/$NAME"
PRIVATE_DIR="$ROOT/users-private/$NAME"
USER_DIR=""
SECRETS_DIR=""
if [ -f "$PUBLIC_DIR/values.yaml" ]; then
  USER_DIR="$PUBLIC_DIR"
  SECRETS_DIR="$ROOT/secrets/$NAME"
elif [ -f "$PRIVATE_DIR/values.yaml" ]; then
  USER_DIR="$PRIVATE_DIR"
  SECRETS_DIR="$PRIVATE_DIR/secrets"
fi
if [ -z "$USER_DIR" ]; then
  fail "no values.yaml found under deployments/$NAME/ or users-private/$NAME/"
  echo
  echo "Tip: run 'scripts/new-user.sh $NAME' to scaffold one."
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

# 2. Placeholder scan across values + secret files.
PLACEHOLDER_RE='CHANGE ME|PLACEHOLDER-OVERRIDE|__USER__|__COOKIE_SECRET__|__IMAGE_TAG__|__DATE__'
HAS_PLACEHOLDER=0
for f in "$VALUES" "${SECRET_FILES[@]}"; do
  if grep -qE "$PLACEHOLDER_RE" "$f" 2>/dev/null; then
    fail "placeholders remain in $(basename "$(dirname "$f")")/$(basename "$f"):"
    grep -nE "$PLACEHOLDER_RE" "$f" | sed 's/^/         /'
    HAS_PLACEHOLDER=1
  fi
done
[ "$HAS_PLACEHOLDER" -eq 0 ] && pass "no PLACEHOLDER / 'CHANGE ME' strings found"

# 3. Cookie secret length.
COOKIE=$(awk -F'"' '/^[[:space:]]*cookieSecret:/{print $2; exit}' "$VALUES")
if [ -z "$COOKIE" ]; then
  fail "oauth2.cookieSecret missing in $VALUES"
else
  COOKIE_LEN=${#COOKIE}
  # oauth2-proxy: must be EXACTLY 16, 24, or 32 chars (used as raw AES key).
  case "$COOKIE_LEN" in
    16|24|32) pass "cookieSecret present ($COOKIE_LEN chars)" ;;
    *)        fail "cookieSecret is $COOKIE_LEN chars; must be exactly 16/24/32. Regenerate: openssl rand -base64 64 | tr -d '\\n=+/' | head -c 32" ;;
  esac
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
