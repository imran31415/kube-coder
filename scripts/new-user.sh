#!/usr/bin/env bash
# scripts/new-user.sh — scaffold a new private workspace under users-private/.
#
# Usage: scripts/new-user.sh <username>
#   ./scripts/new-user.sh chase
#
# What it does (idempotent — running twice is safe; existing files are
# preserved):
#   1. Creates users-private/<name>/secrets/.
#   2. Writes users-private/<name>/values.yaml from the template, with
#      __USER__ / __DATE__ / __IMAGE_TAG__ substituted and a fresh
#      cookieSecret pre-generated.
#   3. Writes users-private/<name>/secrets/oauth2.yaml from the template.
#   4. Prints a setup checklist with the exact fields the operator must edit.
#
# Run `make validate-user USER=<name>` afterwards to confirm everything is
# ready, then `make deploy USER=<name>` to roll out.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE_DIR="$ROOT/scripts/user-template"
USERS_PRIVATE="$ROOT/users-private"

if [ $# -lt 1 ] || [ -z "${1:-}" ]; then
  echo "Usage: $0 <username>" >&2
  exit 2
fi

NAME="$1"
if ! [[ "$NAME" =~ ^[a-z][a-z0-9-]{0,30}[a-z0-9]$ ]] && ! [[ "$NAME" =~ ^[a-z]$ ]]; then
  echo "ERROR: <username> must be lowercase letters/digits/hyphens, 1-32 chars, starts with letter, no trailing hyphen." >&2
  exit 2
fi

USER_DIR="$USERS_PRIVATE/$NAME"
VALUES_OUT="$USER_DIR/values.yaml"
OAUTH_OUT="$USER_DIR/secrets/oauth2.yaml"

# Resolve current image tag from the Makefile so the new user inherits the
# same image their cluster is running.
IMAGE_TAG=$(awk -F':= *' '/^VERSION/{print $2; exit}' "$ROOT/Makefile" | tr -d '[:space:]')
IMAGE_TAG="${IMAGE_TAG:-v1.8.0}"

mkdir -p "$USER_DIR/secrets"

# Cookie secret: oauth2-proxy requires the cookie_secret string to be
# *exactly* 16, 24, or 32 characters long (it's used directly as an AES
# key — the chars themselves are the key bytes). Raw `openssl rand -base64
# 32` produces a 44-char b64 string that oauth2-proxy rejects with
# "must be 16, 24, or 32 bytes to create an AES cipher". Strip the b64
# padding/special chars and trim to exactly 32 — gives ~190 bits of
# entropy, well above any sane threshold.
COOKIE_SECRET=$(openssl rand -base64 64 | tr -d '\n=+/' | head -c 32)

substitute() {
  # Read tmpl from stdin, substitute placeholders, write to stdout.
  sed -e "s|__USER__|$NAME|g" \
      -e "s|__DATE__|$(date -u +%Y-%m-%d)|g" \
      -e "s|__IMAGE_TAG__|$IMAGE_TAG|g" \
      -e "s|__COOKIE_SECRET__|$COOKIE_SECRET|g"
}

if [ -f "$VALUES_OUT" ]; then
  echo "[new-user] $VALUES_OUT already exists — leaving it alone."
else
  substitute < "$TEMPLATE_DIR/values.yaml.tmpl" > "$VALUES_OUT"
  echo "[new-user] wrote $VALUES_OUT"
fi

if [ -f "$OAUTH_OUT" ]; then
  echo "[new-user] $OAUTH_OUT already exists — leaving it alone."
else
  substitute < "$TEMPLATE_DIR/secrets/oauth2.yaml.tmpl" > "$OAUTH_OUT"
  echo "[new-user] wrote $OAUTH_OUT"
fi

cat <<EOF

──────────────────────────────────────────────────────────────────────
Scaffold complete for: $NAME
Files written:
  $VALUES_OUT
  $OAUTH_OUT
──────────────────────────────────────────────────────────────────────

NEXT STEPS

1. Create a GitHub OAuth app for this workspace:
     https://github.com/settings/developers → New OAuth App
     Homepage URL:  https://$NAME.dev.scalebase.io
     Callback URL:  https://$NAME.dev.scalebase.io/oauth2/callback

2. Edit users-private/$NAME/values.yaml — fix every line with "CHANGE ME":
     • user.host               (must resolve to your ingress IP via DNS)
     • user.env GIT_USER_NAME  / GIT_USER_EMAIL
     • oauth2.githubUsers      (comma-separated GH usernames to allow)
     • oauth2.clientId         (from the OAuth app)

3. Edit users-private/$NAME/secrets/oauth2.yaml:
     • oauth2.clientSecret     (from the OAuth app)

4. (Optional) Drop additional secrets into users-private/$NAME/secrets/:
     • claude.yaml             (API key)
     • github-app.yaml         (private-repo auth)

5. Validate, then deploy:
     make validate-user USER=$NAME
     make deploy        USER=$NAME

Everything under users-private/ is gitignored — none of this lands in
your public repo.
EOF
