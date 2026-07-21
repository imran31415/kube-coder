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
# Scaffold into the GitOps checkout (.users/, synced via `make users-sync`) — the
# single source of truth for provisioned workspaces. The operator commits+pushes
# it, then `make deploy USER=<name>`. Override USERS_PRIVATE for legacy in-repo
# scaffolding (e.g. the _controller bootstrap config).
USERS_PRIVATE="${USERS_PRIVATE:-$ROOT/.users/users-private}"

if [ $# -lt 1 ] || [ -z "${1:-}" ]; then
  echo "Usage: $0 <username>" >&2
  exit 2
fi

if [ ! -d "$ROOT/.users/.git" ] && [ "$USERS_PRIVATE" = "$ROOT/.users/users-private" ]; then
  echo "ERROR: $ROOT/.users is not a GitOps checkout yet — run 'make users-sync' first" >&2
  echo "       (or set USERS_PRIVATE=<dir> to scaffold elsewhere)." >&2
  exit 1
fi

NAME="$1"
if ! [[ "$NAME" =~ ^[a-z][a-z0-9-]{0,30}[a-z0-9]$ ]] && ! [[ "$NAME" =~ ^[a-z]$ ]]; then
  echo "ERROR: <username> must be lowercase letters/digits/hyphens, 1-32 chars, starts with letter, no trailing hyphen." >&2
  exit 2
fi

USER_DIR="$USERS_PRIVATE/$NAME"
VALUES_OUT="$USER_DIR/values.yaml"
OAUTH_OUT="$USER_DIR/secrets/oauth2.yaml"

# Image tag for new workspaces. Defaults to the current release image
# (devlaptop-v<release>), kept in lockstep with the Makefile VERSION and the
# GitHub release. Each release builds a matching immutable devlaptop-v<X.Y.Z>
# image; new workspaces pin it so they run the released code. Override
# IMAGE_TAG in the environment to pin a workspace to a different build.
IMAGE_TAG="${IMAGE_TAG:-v1.36.0}"

mkdir -p "$USER_DIR/secrets"

# Cookie secret: oauth2-proxy requires the cookie_secret string to be
# *exactly* 16, 24, or 32 characters long (it's used directly as an AES
# key — the chars themselves are the key bytes). Raw `openssl rand -base64
# 32` produces a 44-char b64 string that oauth2-proxy rejects with
# "must be 16, 24, or 32 bytes to create an AES cipher". Strip the b64
# padding/special chars and trim to exactly 32 — gives ~190 bits of
# entropy, well above any sane threshold.
COOKIE_SECRET=$(openssl rand -base64 64 | tr -d '\n=+/' | head -c 32)

# Shared OpenRouter secret name so new workspaces get OpenRouter cluster-wide
# without a per-user key. Operators set KC_SHARED_ASSISTANT_SECRET to the Secret
# they created (key: openrouter-api-key); blank by default keeps the public repo
# free of any deploy-specific secret names.
SHARED_ASSISTANT_SECRET="${KC_SHARED_ASSISTANT_SECRET:-}"

# Shared self-serve-update token Secret name (#147), so new workspaces can
# update themselves from their dashboard. Operators set KC_SELF_SERVE_SECRET to
# the Secret they created (key: self-serve-token); blank by default keeps the
# public repo free of any deploy-specific secret names.
SELF_SERVE_SECRET="${KC_SELF_SERVE_SECRET:-}"

substitute() {
  # Read tmpl from stdin, substitute placeholders, write to stdout.
  sed -e "s|__USER__|$NAME|g" \
      -e "s|__DATE__|$(date -u +%Y-%m-%d)|g" \
      -e "s|__IMAGE_TAG__|$IMAGE_TAG|g" \
      -e "s|__SHARED_ASSISTANT_SECRET__|$SHARED_ASSISTANT_SECRET|g" \
      -e "s|__SELF_SERVE_SECRET__|$SELF_SERVE_SECRET|g" \
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
  substitute < "$TEMPLATE_DIR/oauth2-secrets.yaml.tmpl" > "$OAUTH_OUT"
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

1. Create a GitHub OAuth App (NOT a GitHub App) for this workspace:
     https://github.com/settings/developers → New OAuth App
     Homepage URL:  https://$NAME.dev.scalebase.io
     Callback URL:  https://$NAME.dev.scalebase.io/oauth2/callback
   (its Client ID starts with "Ov…"; a GitHub App "Iv…" id 404s oauth2-proxy.)

2. Edit $USER_DIR/values.yaml — fix every line with "CHANGE ME":
     • user.host               (must resolve to your ingress IP via DNS)
     • user.env GIT_USER_NAME  / GIT_USER_EMAIL
     • oauth2.githubUsers      (comma-separated GH usernames to allow)
     • oauth2.clientId         (from the OAuth app, "Ov…")

3. Edit $USER_DIR/secrets/oauth2.yaml:
     • oauth2.clientSecret     (from the OAuth app)

4. (Optional) Drop additional secrets into $USER_DIR/secrets/:
     • claude.yaml             (API key)
     • github-app.yaml         (private-repo auth)

5. Commit + push the GitOps repo so the config is durable:
     git -C $ROOT/.users add -A && git -C $ROOT/.users commit -m "add $NAME" && git -C $ROOT/.users push

6. Validate, then deploy:
     make validate-user USER=$NAME
     make deploy        USER=$NAME

The GitOps checkout (.users/) and this repo's users-private/ are both
gitignored — none of this lands in the public repo.
EOF
