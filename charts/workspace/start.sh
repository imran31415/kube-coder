#!/bin/bash
# Workspace pod entrypoint. Runs all in-pod services (code-server, ttyd,
# Xvfb/x11vnc/websockify, browser/Claude API server) and supervises them
# in a watchdog loop. Mirrors the inline script that previously lived in
# deployment.yaml — extracted here so it's editable as a real shell script.
set -o pipefail

log_stage() { echo "[stage] $*" >&2; }

# Forward SIGTERM/SIGINT to in-pod services so tmux sessions and the
# browser server flush gracefully on pod terminate.
on_term() {
  log_stage "SIGTERM received, killing tmux server and child services"
  tmux kill-server 2>/dev/null || true
  pkill -TERM -f "code-server" 2>/dev/null || true
  pkill -TERM -f "ttyd" 2>/dev/null || true
  pkill -TERM -f "websockify" 2>/dev/null || true
  pkill -TERM -f "python3 server.py" 2>/dev/null || true
  exit 0
}
trap on_term TERM INT

# ttyd is read-only by default; --writable is what lets clients type into
# the TTY. On the read-only public demo (READONLY_MODE=true) we drop the
# flag so visitors can scroll/copy the scripted simulated-claude.sh output
# but cannot send keystrokes into the pod. Unquoted expansion: empty =>
# zero args, so a normal deploy still launches ttyd --writable.
TTYD_WRITABLE="--writable"
if [ "${READONLY_MODE:-false}" = "true" ]; then
  TTYD_WRITABLE=""
  log_stage "READONLY_MODE — launching ttyd read-only (keystrokes disabled)"
fi

log_stage "preparing home and credential directories"
cd /home/dev
mkdir -p /home/dev/.local/share/code-server/extensions
mkdir -p ~/.config ~/.config/git ~/.config/gh
mkdir -p /home/dev/.credentials/.ssh /home/dev/.credentials/.config/git /home/dev/.credentials/.config/gh
chmod 700 /home/dev/.credentials/.ssh
ln -sf /home/dev/.credentials/.ssh ~/.ssh
touch /home/dev/.credentials/.config/git/config
ln -sf /home/dev/.credentials/.config/git/config ~/.gitconfig
ln -sf /home/dev/.credentials/.config/gh ~/.config/gh

# Persist per-tool API logins across pod restarts (issue #243). The
# interactive terminals (ttyd, code-server, SSH) run with HOME=/home/ubuntu,
# which is ephemeral — so `eas login` (Expo), `npm login`, `docker login`,
# `aws configure`, `gcloud auth` etc. are lost on every restart. Redirect
# those credential files/dirs onto the persistent /home/dev/.credentials
# store, the same way ~/.ssh and ~/.gitconfig are handled above. Dirs and
# single files are treated separately so we never shadow a real login with
# an empty stub, and any pre-existing ephemeral login is migrated onto the
# PVC once (no-clobber) so upgrading an established workspace keeps a session
# that was active before this change shipped.
persist_cred() {                      # $1 = subpath under HOME, $2 = dir|file
  local sub="$1" kind="$2"
  local target="/home/dev/.credentials/$sub"
  if [ "$kind" = dir ]; then
    mkdir -p "$target"; chmod 700 "$target"
  else
    mkdir -p "$(dirname "$target")"; touch "$target"; chmod 600 "$target"
  fi
  for HOME_DIR in /home/ubuntu; do
    [ -d "$HOME_DIR" ] || continue
    local link="$HOME_DIR/$sub"
    if [ -e "$link" ] && [ ! -L "$link" ]; then   # migrate a real login once
      if [ "$kind" = dir ]; then
        cp -an "$link/." "$target/" 2>/dev/null || true; rm -rf "$link"
      else
        cp -an "$link" "$target" 2>/dev/null || true; rm -f "$link"
      fi
    fi
    mkdir -p "$(dirname "$link")"
    ln -sfn "$target" "$link"
    chown -h ubuntu:ubuntu "$link" 2>/dev/null || true
  done
}
persist_cred .expo            dir    # Expo / EAS (eas login, expo login)
persist_cred .docker          dir    # docker login (registry auth)
persist_cred .aws             dir    # aws configure / SSO cache
persist_cred .config/gcloud   dir    # gcloud auth
persist_cred .npmrc           file   # npm / yarn registry tokens
persist_cred .git-credentials file   # git credential.helper store (non-GitHub HTTPS)

# CLAUDE.md is chart-managed (describes the workspace environment to
# Claude). Always overwrite from the configmap so chart updates land
# — previously we preserved any existing file, which meant edits to
# claude-md.txt never reached workspaces with an established PVC.
# User-level notes belong in the persistent-memory subsystem, not
# here.
cp /claude-config/CLAUDE.md /home/dev/CLAUDE.md

# tmux config — chart-managed so dashboard users get the same scrollback
# ergonomics out of the box. mouse on means scroll-wheel in the
# Session/Preview iframe enters tmux copy-mode automatically (no Ctrl+B [
# hotkey needed); 50k line history covers a long Claude session without
# hitting the default 2k limit. Always overwritten on boot so chart
# edits propagate.
# Brace-group of printf lines instead of a heredoc — the surrounding
# YAML block scalar uses 4-space indent which conflicts with
# heredoc terminator placement (`EOF` must be unindented for `<<'EOF'`
# or use `<<-` with tab indent, neither friendly to this file).
{
  printf '%s\n' 'set -g mouse on'
  printf '%s\n' 'set -g history-limit 50000'
  printf '%s\n' '# Wheel events arrive as proper mouse-scroll escapes (xterm.js DEC'
  printf '%s\n' '# 1007 is reset in terminal-entry.sh so alt-screen apps like Claude'
  printf '%s\n' '# pass wheel through instead of converting to arrow keys). On first'
  printf '%s\n' '# wheel-up, enter copy-mode; on wheel-down at the bottom, return to'
  printf '%s\n' '# the live pane. Natural scrollbar UX without the Ctrl+B [ hotkey.'
  printf '%s\n' "bind -T root WheelUpPane   if-shell -F -t = '#{mouse_any_flag}' 'send-keys -M' 'if-shell -F -t = \"#{pane_in_mode}\" \"send-keys -M\" \"copy-mode -et=\"'"
  printf '%s\n' "bind -T root WheelDownPane if-shell -F -t = '#{mouse_any_flag}' 'send-keys -M' 'send-keys -M'"
  printf '%s\n' 'bind -T copy-mode-vi WheelUpPane   send-keys -X -N 3 scroll-up'
  printf '%s\n' 'bind -T copy-mode-vi WheelDownPane send-keys -X -N 3 scroll-down'
} > /home/dev/.tmux.conf
# If a tmux server is already running (e.g. after a pod restart with
# surviving sessions), re-source the config so the live sessions pick
# up the new settings without needing to be killed + relaunched.
tmux source-file /home/dev/.tmux.conf 2>/dev/null || true

# Ensure the upstream kube-coder source is available at /home/dev/kube-coder
# so Claude can introspect the workspace's own infrastructure and contribute
# changes back via fork+PR (see CLAUDE.md "kube-coder source" section).
# Also keeps /home/dev/kube-coder/docs current so the in-app Docs site
# picks up new pages without a manual sync. Idempotent — clones if missing,
# ff-only-pulls if present (skipped silently when there are local commits).
# Backgrounded so a slow/flaky network never blocks pod readiness.
# Runs in READONLY_MODE too — the readonly gate is for *runtime* mutations
# from HTTP clients, not boot-time setup. The public demo needs the docs
# tree at /home/dev/kube-coder/docs/ to populate the in-app /docs route.
(
  if [ ! -d /home/dev/kube-coder/.git ]; then
    log_stage "cloning kube-coder source into /home/dev/kube-coder"
    git clone https://github.com/imran31415/kube-coder.git /home/dev/kube-coder \
      && log_stage "kube-coder source clone complete" \
      || log_stage "WARNING: kube-coder clone failed (network?); will retry on next pod start"
  else
    log_stage "refreshing kube-coder source (git pull --ff-only)"
    ( cd /home/dev/kube-coder && git pull --ff-only origin main >/dev/null 2>&1 ) \
      && log_stage "kube-coder source refreshed" \
      || log_stage "NOTE: git pull skipped (local commits or offline); existing clone retained"
  fi
) &

# Render an OpenCode config file whenever at least one OpenCode-backed
# provider has been configured (OpenRouter and/or Fallback). Claude
# itself needs no on-disk config — it reads ANTHROPIC_API_KEY from env
# or OAuths interactively. The config is rewritten on every pod start
# so Helm value changes flow through cleanly.
if [ -n "$OPENROUTER_API_KEY" ] || [ -n "$DEEPSEEK_API_KEY" ] || [ -n "$KC_FALLBACK_BASE_URL" ]; then
  log_stage "writing OpenCode config (openrouter=$([ -n "$OPENROUTER_API_KEY" ] && echo on || echo off) deepseek=$([ -n "$DEEPSEEK_API_KEY" ] && echo on || echo off) fallback=$([ -n "$KC_FALLBACK_BASE_URL" ] && echo on || echo off))"
  mkdir -p $HOME/.config/opencode
  OPENROUTER_MODEL_DEFAULT="${KC_OPENROUTER_MODEL:-anthropic/claude-sonnet-4}"
  FALLBACK_MODEL_DEFAULT="${KC_FALLBACK_MODEL:-anthropic/claude-sonnet-4}"
  FALLBACK_PROVIDER_ID="${KC_FALLBACK_PROVIDER_ID:-kube-coder-fallback}"
  FALLBACK_PROVIDER_NAME="${KC_FALLBACK_PROVIDER_NAME:-Kube-Coder Fallback}"
  # NOTE: heredoc delimiter is single-quoted ('PY') so bash performs NO
  # expansion on the body. Critical because (a) the literal token
  # {env:VAR} must reach opencode unchanged, and (b) earlier comments
  # containing backticks were being command-substituted by bash and
  # corrupting the python stdin.
  python3 - <<'PY' > $HOME/.config/opencode/opencode.json
import json, os
cfg = {"$schema": "https://opencode.ai/config.json"}
provider = {}
# OpenRouter and DeepSeek are first-class OpenCode providers -- the CLI
# auto-discovers them from $OPENROUTER_API_KEY and $DEEPSEEK_API_KEY in
# the pod env. We deliberately do NOT emit stub blocks for them: any
# custom provider listed alongside an incomplete first-class entry
# (no npm field) makes opencodes config loader silently drop the rest,
# including our fallback. Their models still surface to
# /api/claude/assistants via KC_OPENROUTER_MODEL / KC_DEEPSEEK_MODEL.
if os.environ.get("KC_FALLBACK_BASE_URL"):
    options = {"baseURL": os.environ["KC_FALLBACK_BASE_URL"]}
    # Only declare apiKey when one is actually present. OpenCode
    # silently disables the provider if the env-ref resolves to empty,
    # which made --model <id>/<model> fall back to OpenRouter without a
    # visible error. Open Ollama endpoints don't need a key at all.
    if os.environ.get("KC_FALLBACK_API_KEY"):
        options["apiKey"] = "{env:KC_FALLBACK_API_KEY}"
    provider[os.environ.get("KC_FALLBACK_PROVIDER_ID", "kube-coder-fallback")] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": os.environ.get("KC_FALLBACK_PROVIDER_NAME", "Kube-Coder Fallback"),
        "options": options,
        # tool_call: true is required — OpenCode silently disables tool
        # advertising for custom-provider models that don't declare it,
        # which makes the model say "there is no function to run X" and
        # hallucinate webfetch-style replies instead of using bash/edit.
        "models": {os.environ.get("KC_FALLBACK_MODEL", "anthropic/claude-sonnet-4"): {"tool_call": True}},
    }
if provider:
    cfg["provider"] = provider
# Register the same stdio MCP servers Claude and Ante get so OpenCode can
# also initiate + track sub-agents (agent-orchestrator) and share the
# persistent memory. `mcp` is an independent top-level key — it does not
# interact with the provider-loader fragility noted above.
cfg["mcp"] = {
    "agent-orchestrator": {
        "type": "local",
        "command": ["python3", "/tmp/browser/mcp_agent_orchestrator.py"],
        "enabled": True,
    },
    "memory": {
        "type": "local",
        "command": ["python3", "/home/dev/.claude-memory/mcp_memory.py"],
        "enabled": True,
    },
    "dashboard": {
        "type": "local",
        "command": ["python3", "/tmp/browser/mcp_dashboard.py"],
        "enabled": True,
    },
}
print(json.dumps(cfg, indent=2))
PY
fi

if [ -n "$GITHUB_APP_ID" ]; then
  log_stage "minting initial GitHub App token and starting refresh daemon"
  python3 /github-app/github-app-token.py --once
  # shellcheck disable=SC1091
  source /home/dev/.credentials/.github-env
  # NOTE: the --once run above already wrote /home/dev/.profile.d-github-env as a
  # mode-guarded hook (exports the App token only in "app" mode). Do NOT clobber
  # it with a raw copy of .github-env, or a "personal" login would be shadowed
  # again (issue #256).

  # Make GH_TOKEN/GITHUB_TOKEN visible to EVERY shell flavour in the pod.
  # The container runs with allowPrivilegeEscalation:false (see the
  # securityContext in deployment.yaml), so `sudo` cannot escalate and any
  # write under /etc/* fails silently. We therefore wire only user-writable
  # locations (uid 1000) — no sudo, nothing under /etc:
  #   1. BASH_ENV=/home/dev/.profile.d-github-env (set in deployment.yaml)
  #                          → non-interactive bash: `bash -c …`, hooks,
  #                            the gh/git credential helper — the path that
  #                            actually breaks when this is missing
  #   2. ~/.bashrc           → interactive non-login bash
  #   3. ~/.profile          → login shells
  # All three read /home/dev/.profile.d-github-env, which the refresh daemon
  # rewrites in place, so new shells pick up rotated tokens without restart.
  GH_SRC_LINE='[ -f /home/dev/.profile.d-github-env ] && . /home/dev/.profile.d-github-env'
  for rc in /home/dev/.bashrc /home/dev/.profile; do
    touch "$rc"
    if ! grep -qF '/home/dev/.profile.d-github-env' "$rc"; then
      printf '\n# github-app token (rewritten in place by the refresh daemon)\n%s\n' "$GH_SRC_LINE" >> "$rc"
    fi
  done

  python3 /github-app/github-app-token.py --daemon &

  # gh CLI wrapper: always read the live token the refresh daemon keeps fresh.
  # BASH_ENV + the ~/.profile hook above re-export GH_TOKEN for NEW shells, but a
  # long-lived process (notably the Claude harness) freezes whatever GH_TOKEN it
  # captured at startup, and `gh` prefers that stale env var -> 401 "Bad
  # credentials" ~an hour into a session. `git push` is immune because it uses
  # the file-based credential helper; this shim gives `gh` the same file-backed
  # guarantee by re-reading the token on every invocation. The real binary is
  # pinned to an absolute path so re-runs can't recurse back into this shim.
  GH_SHIM_DIR="$HOME/.local/bin"
  if mkdir -p "$GH_SHIM_DIR" 2>/dev/null; then
    cat > "$GH_SHIM_DIR/gh" <<'GHSHIM'
#!/usr/bin/env bash
# Respect the workspace GitHub auth mode (issue #256).
#   app       force the fresh App installation token so long-lived processes
#             (e.g. the Claude harness) never go stale.
#   personal  defer to the user's own login (~/.config/gh/hosts.yml): strip an
#             INHERITED App token so gh reads hosts.yml. A user's own personal
#             PAT in GH_TOKEN is preserved (only the App token is stripped).
_mode="$(cat /home/dev/.credentials/.github-auth-mode 2>/dev/null || echo app)"
_tokfile=/home/dev/.credentials/.github-token
if [ "$_mode" = personal ]; then
  if [ -r "$_tokfile" ] && [ "$GH_TOKEN" = "$(cat "$_tokfile")" ]; then
    unset GH_TOKEN GITHUB_TOKEN
  fi
elif [ -r "$_tokfile" ]; then
  GH_TOKEN="$(cat "$_tokfile")"
  GITHUB_TOKEN="$GH_TOKEN"
  export GH_TOKEN GITHUB_TOKEN
fi
exec /usr/bin/gh "$@"
GHSHIM
    chmod +x "$GH_SHIM_DIR/gh"
  fi
fi

{{- if .Values.codeServer.enabled }}
log_stage "seeding default code-server settings (first run only)"
CS_USER_DIR=/home/dev/.local/share/code-server/User
mkdir -p "$CS_USER_DIR"
if [ ! -f "$CS_USER_DIR/settings.json" ]; then
  cat > "$CS_USER_DIR/settings.json" <<'JSON'
{
  "workbench.colorTheme": "Default Dark Modern",
  "workbench.preferredDarkColorTheme": "Default Dark Modern",
  "window.autoDetectColorScheme": false,
  "terminal.integrated.scrollback": 2000,
  "files.watcherExclude": {
    "**/node_modules/**": true,
    "**/.git/**": true,
    "**/dist/**": true,
    "**/build/**": true,
    "**/.next/**": true,
    "**/target/**": true
  }
}
JSON
fi

log_stage "starting code-server"
NODE_OPTIONS="--max-old-space-size=1536" \
  code-server --bind-addr 0.0.0.0:8080 --auth none \
  --user-data-dir /home/dev/.local/share/code-server \
  --extensions-dir /home/dev/.local/share/code-server/extensions \
  /home/dev > /tmp/code-server.log 2>&1 &
{{- else }}
log_stage "code-server disabled (codeServer.enabled=false); skipping launch"
{{- end }}

log_stage "starting ttyd"
# scrollback=10000   — give xterm.js a 10k-line buffer + show the native
#                       scrollbar on the iframe edge. User can drag to
#                       scroll regardless of alt-screen mode (Claude TUI).
# disableLeaveAlert  — suppress "Are you sure you want to leave?" prompt
#                       when navigating away from the iframe.
ttyd --port 7681 --interface 0.0.0.0 $TTYD_WRITABLE \
  --client-option disableLeaveAlert=true \
  --client-option scrollback=10000 \
  -w /home/dev \
  /terminal-scripts/terminal-entry.sh > /tmp/ttyd.log 2>&1 &

{{- if .Values.browser.enabled }}
log_stage "starting Xvfb / fluxbox / x11vnc"
export DISPLAY=:99
rm -f /tmp/.X99-lock
# The initial -screen geometry sets the MAX framebuffer Xvfb can ever
# render; xrandr can switch to smaller modes within this allocation but
# not larger. We size the framebuffer to fit a 1080p landscape monitor
# AND a tall mobile portrait (e.g. iPhone 15 Pro Max 430x932 zoomed),
# then immediately switch to 1280x720 as the visible default.
Xvfb :99 -screen 0 1920x1280x24 +extension RANDR &
sleep 3
DISPLAY=:99 setxkbmap us
DISPLAY=:99 fluxbox &
sleep 1

# Register the common screen sizes noVNC clients may request via the
# ExtendedDesktopSize protocol (resize=remote). Without these, x11vnc's
# -xrandr handler has no matching mode to switch to and silently keeps
# the old size. We use synthetic modelines (Xvfb ignores real pixel
# timings) instead of `cvt`, which isn't shipped in x11-xserver-utils.
# The output name comes from xrandr itself ("screen" on Xvfb 21.x).
if command -v xrandr >/dev/null 2>&1; then
  OUTPUT_NAME=$(DISPLAY=:99 xrandr 2>/dev/null | awk '/ connected/{print $1; exit}')
  OUTPUT_NAME=${OUTPUT_NAME:-screen}
  register_mode() {
    local W=$1 H=$2 NAME="${1}x${2}" CLK
    CLK=$(( W * H / 16384 + 25 ))
    DISPLAY=:99 xrandr --newmode "$NAME" "$CLK" \
      "$W" "$((W+16))" "$((W+32))" "$((W+64))" \
      "$H" "$((H+1))"  "$((H+3))"  "$((H+10))" \
      -HSync -VSync 2>/dev/null || true
    DISPLAY=:99 xrandr --addmode "$OUTPUT_NAME" "$NAME" 2>/dev/null || true
  }
  for mode in \
      "1280 720" "1920 1080" "1366 768" "1024 768" \
      "390 844" "844 390" "393 852" "852 393" "430 932" "932 430" \
      "768 1024" "1024 1366" "414 896" "896 414"; do
    # shellcheck disable=SC2086
    register_mode $mode
  done
  DISPLAY=:99 xrandr --output "$OUTPUT_NAME" --mode 1280x720 2>/dev/null || true
fi

# -xrandr resize lets remote clients trigger a framebuffer resize via
# the ExtendedDesktopSize message — x11vnc applies it through xrandr,
# so mobile devices get a portrait framebuffer instead of a letterbox.
#
# We deliberately do NOT enable -ncache here: while client-side pixel
# caching reduces bandwidth on window motion, x11vnc implements it by
# placing the cache *below* the visible framebuffer, doubling the
# reported canvas height. Combined with noVNC's resize=scale (used by
# the dashboard's Preview pane) this would scale the cache area into
# the iframe and shrink the visible desktop to a tiny strip at the top.
x11vnc -display :99 -nopw -listen localhost -xkb -forever -shared -repeat \
  -cursor -cursorpos -24to32 -nobell -noipv6 \
  -xrandr resize &
sleep 2

log_stage "waiting for VNC port 5900"
for i in {1..30}; do
  netstat -tln 2>/dev/null | grep -q :5900 && break
  sleep 1
done

log_stage "starting websockify (noVNC from image at /usr/share/novnc)"
pkill -f websockify || true
sleep 1
websockify --web=/usr/share/novnc --heartbeat=30 6081 localhost:5900 \
  > /tmp/websockify.log 2>&1 &
sleep 2
{{- else }}
log_stage "browser/VNC stack disabled (browser.enabled=false); skipping Xvfb/fluxbox/x11vnc/websockify"
{{- end }}

log_stage "preparing persistent memory subsystem"
# Persist Claude Code's own file-based auto-memory across pod restarts
# by symlinking ~/.claude (ephemeral in /home/ubuntu or wherever $HOME
# points) to a PVC-backed directory under /home/dev/.claude. Without
# this, every restart wipes ~/.claude/projects/*/memory/*.md and the
# auto-sync prune step would soft-delete every imported entry.
TARGET=/home/dev/.claude
mkdir -p "$TARGET"
chmod 700 "$TARGET"
# If a previous deploy mis-symlinked $TARGET to itself, repair it: a
# self-link is detectable as `readlink == basename` and yields the
# "too many levels of symbolic links" error on any access.
if [ -L "$TARGET" ]; then
  LINK_DEST=$(readlink "$TARGET")
  if [ "$LINK_DEST" = "$TARGET" ] || [ "$LINK_DEST" = ".claude" ]; then
    rm -f "$TARGET"
    mkdir -p "$TARGET"
    chmod 700 "$TARGET"
  fi
fi
# Symlink every other plausible $HOME/.claude → the PVC target so that
# whichever user actually runs `claude`, its file-based auto-memory
# writes land on the PVC.
for HOME_DIR in /home/ubuntu; do
  [ -d "$HOME_DIR" ] || continue
  LINK="$HOME_DIR/.claude"
  # Don't try to symlink the target to itself.
  [ "$LINK" = "$TARGET" ] && continue
  # Already a correct symlink? leave it.
  if [ -L "$LINK" ] && [ "$(readlink "$LINK")" = "$TARGET" ]; then
    continue
  fi
  # Existing directory (or stale symlink): merge contents into the
  # PVC target (no-clobber preserves PVC as canonical), then replace.
  if [ -d "$LINK" ] && [ ! -L "$LINK" ]; then
    cp -an "$LINK"/. "$TARGET"/ 2>/dev/null || true
    rm -rf "$LINK"
  elif [ -e "$LINK" ] || [ -L "$LINK" ]; then
    rm -f "$LINK"
  fi
  ln -sfn "$TARGET" "$LINK" 2>/dev/null || true
done

# Persist Codex's OAuth credentials + sessions across pod restarts. Codex stores
# everything (auth.json from `codex login`, config.toml, saved sessions) under
# $CODEX_HOME (default ~/.codex), which on the ephemeral home is wiped every
# restart — losing the login. Point ~/.codex at the PVC (same pattern as
# ~/.claude above) so the ChatGPT login survives and `codex exec` (the
# Hypervisor driver) can resume sessions.
CODEX_TARGET=/home/dev/.codex
mkdir -p "$CODEX_TARGET"
chmod 700 "$CODEX_TARGET"
if [ -L "$CODEX_TARGET" ]; then
  CD=$(readlink "$CODEX_TARGET")
  if [ "$CD" = "$CODEX_TARGET" ] || [ "$CD" = ".codex" ]; then
    rm -f "$CODEX_TARGET"; mkdir -p "$CODEX_TARGET"; chmod 700 "$CODEX_TARGET"
  fi
fi
for HOME_DIR in /home/ubuntu; do
  [ -d "$HOME_DIR" ] || continue
  LINK="$HOME_DIR/.codex"
  [ "$LINK" = "$CODEX_TARGET" ] && continue
  if [ -L "$LINK" ] && [ "$(readlink "$LINK")" = "$CODEX_TARGET" ]; then
    continue
  fi
  if [ -d "$LINK" ] && [ ! -L "$LINK" ]; then
    cp -an "$LINK"/. "$CODEX_TARGET"/ 2>/dev/null || true
    rm -rf "$LINK"
  elif [ -e "$LINK" ] || [ -L "$LINK" ]; then
    rm -f "$LINK"
  fi
  ln -sfn "$CODEX_TARGET" "$LINK" 2>/dev/null || true
done

# Persist Ante's config across pod restarts and keep its binary on the PVC.
# Ante stores everything (settings, sessions, versions.json) under ~/.ante,
# which on the ephemeral /home/ubuntu home was wiped every restart. We point
# ~/.ante at the PVC (same pattern as ~/.claude above) and seed the binary at
# the PVC path /home/dev/.ante/bin/ante — which /usr/local/bin/ante is a
# build-time symlink to. The image is the source of truth for the version
# (bump ANTE_VERSION + rebuild); we refresh the PVC copy from /opt/ante/ante
# below so a rebuild propagates and nothing strands the pod on an old binary.
ANTE_TARGET=/home/dev/.ante
mkdir -p "$ANTE_TARGET/bin"
if [ -L "$ANTE_TARGET" ]; then
  AD=$(readlink "$ANTE_TARGET")
  if [ "$AD" = "$ANTE_TARGET" ] || [ "$AD" = ".ante" ]; then
    rm -f "$ANTE_TARGET"; mkdir -p "$ANTE_TARGET/bin"
  fi
fi
for HOME_DIR in /home/ubuntu; do
  [ -d "$HOME_DIR" ] || continue
  LINK="$HOME_DIR/.ante"
  [ "$LINK" = "$ANTE_TARGET" ] && continue
  if [ -L "$LINK" ] && [ "$(readlink "$LINK")" = "$ANTE_TARGET" ]; then
    continue
  fi
  # Merge any existing ~/.ante (incl. a binary the user already updated to)
  # into the PVC — cp -an keeps the PVC copy canonical — then replace with a
  # symlink.
  if [ -d "$LINK" ] && [ ! -L "$LINK" ]; then
    cp -an "$LINK"/. "$ANTE_TARGET"/ 2>/dev/null || true
    rm -rf "$LINK"
  elif [ -e "$LINK" ] || [ -L "$LINK" ]; then
    rm -f "$LINK"
  fi
  ln -sfn "$ANTE_TARGET" "$LINK" 2>/dev/null || true
done
# Seed/refresh the PVC binary from the image's copy (/opt/ante/ante). The image
# is the source of truth, so overwrite whenever the PVC copy is missing or
# differs — this propagates a manual ANTE_VERSION bump on rebuild and stops a
# stale binary from stranding the pod. /usr/local/bin/ante is a build-time
# symlink to this path, so no runtime root (sudo is blocked) is needed.
if [ -f /opt/ante/ante ]; then
  if [ ! -e "$ANTE_TARGET/bin/ante" ] || ! cmp -s /opt/ante/ante "$ANTE_TARGET/bin/ante"; then
    install -m 0755 /opt/ante/ante "$ANTE_TARGET/bin/ante" 2>/dev/null \
      || cp -p /opt/ante/ante "$ANTE_TARGET/bin/ante" 2>/dev/null || true
  fi
fi

# Antigravity CLI (agy): seed the binary + persist its OAuth login across pod
# restarts. The binary is seeded from the image's /opt/antigravity/agy to the
# PVC path /home/dev/.antigravity/bin/agy that /usr/local/bin/agy (a build-time
# symlink) points at — same idiom as ~/.ante above. agy stores its login + config
# under ~/.gemini, so — like ~/.claude — point that at a PVC-backed dir so a
# one-time `agy` login survives restarts.
ANTIGRAVITY_TARGET=/home/dev/.antigravity
mkdir -p "$ANTIGRAVITY_TARGET/bin"
if [ -f /opt/antigravity/agy ]; then
  if [ ! -e "$ANTIGRAVITY_TARGET/bin/agy" ] || ! cmp -s /opt/antigravity/agy "$ANTIGRAVITY_TARGET/bin/agy"; then
    install -m 0755 /opt/antigravity/agy "$ANTIGRAVITY_TARGET/bin/agy" 2>/dev/null \
      || cp -p /opt/antigravity/agy "$ANTIGRAVITY_TARGET/bin/agy" 2>/dev/null || true
  fi
fi
GEMINI_TARGET=/home/dev/.gemini
mkdir -p "$GEMINI_TARGET"
for HOME_DIR in /home/ubuntu; do
  [ -d "$HOME_DIR" ] || continue
  LINK="$HOME_DIR/.gemini"
  [ "$LINK" = "$GEMINI_TARGET" ] && continue
  if [ -L "$LINK" ] && [ "$(readlink "$LINK")" = "$GEMINI_TARGET" ]; then
    continue
  fi
  if [ -d "$LINK" ] && [ ! -L "$LINK" ]; then
    cp -an "$LINK"/. "$GEMINI_TARGET"/ 2>/dev/null || true
    rm -rf "$LINK"
  elif [ -e "$LINK" ] || [ -L "$LINK" ]; then
    rm -f "$LINK"
  fi
  ln -sfn "$GEMINI_TARGET" "$LINK" 2>/dev/null || true
done

# Persist LibreFang's home across pod restarts — same pattern as ~/.ante
# above. LibreFang keeps everything (config.toml, agents, sessions, the
# registry cache, and the binary at bin/librefang) under ~/.librefang, so
# one PVC symlink covers data + binary. /usr/local/bin/librefang is a
# build-time symlink to the PVC path; we seed/refresh it from the image's
# copy (/opt/librefang/librefang) below so a LIBREFANG_VERSION bump
# propagates on rebuild.
LIBREFANG_TARGET=/home/dev/.librefang
mkdir -p "$LIBREFANG_TARGET/bin"
if [ -L "$LIBREFANG_TARGET" ]; then
  LFD=$(readlink "$LIBREFANG_TARGET")
  if [ "$LFD" = "$LIBREFANG_TARGET" ] || [ "$LFD" = ".librefang" ]; then
    rm -f "$LIBREFANG_TARGET"; mkdir -p "$LIBREFANG_TARGET/bin"
  fi
fi
for HOME_DIR in /home/ubuntu; do
  [ -d "$HOME_DIR" ] || continue
  LINK="$HOME_DIR/.librefang"
  [ "$LINK" = "$LIBREFANG_TARGET" ] && continue
  if [ -L "$LINK" ] && [ "$(readlink "$LINK")" = "$LIBREFANG_TARGET" ]; then
    continue
  fi
  # Merge any existing ~/.librefang into the PVC (cp -an keeps the PVC copy
  # canonical), then replace with a symlink.
  if [ -d "$LINK" ] && [ ! -L "$LINK" ]; then
    cp -an "$LINK"/. "$LIBREFANG_TARGET"/ 2>/dev/null || true
    rm -rf "$LINK"
  elif [ -e "$LINK" ] || [ -L "$LINK" ]; then
    rm -f "$LINK"
  fi
  ln -sfn "$LIBREFANG_TARGET" "$LINK" 2>/dev/null || true
done
if [ -f /opt/librefang/librefang ]; then
  if [ ! -e "$LIBREFANG_TARGET/bin/librefang" ] || ! cmp -s /opt/librefang/librefang "$LIBREFANG_TARGET/bin/librefang"; then
    install -m 0755 /opt/librefang/librefang "$LIBREFANG_TARGET/bin/librefang" 2>/dev/null \
      || cp -p /opt/librefang/librefang "$LIBREFANG_TARGET/bin/librefang" 2>/dev/null || true
  fi
fi
# One-time non-interactive init so the first `librefang chat` inside a task
# tmux doesn't block on the interactive setup wizard. Idempotent: skipped
# once config.toml exists. Provider keys are read from the environment at
# chat time (ANTHROPIC_API_KEY, OPENROUTER_API_KEY, …), so the --quick
# defaults are enough here.
if [ -x "$LIBREFANG_TARGET/bin/librefang" ] && [ ! -f "$LIBREFANG_TARGET/config.toml" ]; then
  HOME=/home/dev "$LIBREFANG_TARGET/bin/librefang" init --quick >/dev/null 2>&1 || true
fi
# Normalize the LibreFang config so the dashboard's `librefang chat` sessions
# actually work. Runs every boot (idempotent) so it also corrects configs that
# were already persisted on the PVC, not just freshly-init'd ones.
#
#   1. Auth gate. `init --quick` seeds default dashboard credentials
#      (dashboard_user/pass = "librefang"), which auto-enable bearer auth on the
#      daemon's API. But `librefang chat` presents no token, so the chat
#      WebSocket is rejected and the REPL shows "No active connection". The
#      daemon binds 127.0.0.1 only (network_enabled = false) behind
#      oauth2-proxy, so this in-pod auth adds no security — blank the triggers.
#
#   2. Provider. `init --quick` picks the provider from whichever key it finds
#      first, so a workspace that also has ANTHROPIC_API_KEY lands on Anthropic
#      even when its funded/working key is OpenRouter (the same key Ante and the
#      OpenCode assistant use). When OPENROUTER_API_KEY is present, pin
#      [default_model] to OpenRouter. Model is overridable via KC_LIBREFANG_MODEL.
LF_CFG="$LIBREFANG_TARGET/config.toml"
if [ -f "$LF_CFG" ]; then
  sed -i -E 's/^(api_key|dashboard_user|dashboard_pass|dashboard_pass_hash)[[:space:]]*=.*/\1 = ""/' "$LF_CFG" || true
  if [ -n "$OPENROUTER_API_KEY" ]; then
    LF_MODEL="openrouter/${KC_LIBREFANG_MODEL:-deepseek/deepseek-v3.2}"
    awk -v model="$LF_MODEL" '
      /^\[default_model\]/{inblk=1}
      /^\[/ && $0 !~ /^\[default_model\]/{inblk=0}
      inblk && /^provider[[:space:]]*=/{print "provider = \"openrouter\""; next}
      inblk && /^model[[:space:]]*=/{print "model = \"" model "\""; next}
      inblk && /^api_key_env[[:space:]]*=/{print "api_key_env = \"OPENROUTER_API_KEY\""; next}
      {print}
    ' "$LF_CFG" > "$LF_CFG.kc" && mv "$LF_CFG.kc" "$LF_CFG" || rm -f "$LF_CFG.kc"

    # The agent the dashboard launches (KC_LIBREFANG_AGENT, default "coder")
    # ships a template that breaks against OpenRouter in two ways:
    #   - model = "default" (inherit [default_model]). LibreFang's *streaming*
    #     chat path — the one the dashboard REPL uses — sends that literal
    #     "default" to the provider instead of resolving it, so OpenRouter 400s
    #     ("default is not a valid model ID") and the REPL shows no reply. The
    #     non-streaming /message path resolves it, which is why headless works
    #     but interactive chat doesn't.
    #   - api_key_env hardcoded to GEMINI_API_KEY / GROQ_API_KEY (unset here),
    #     which surfaces as "LLM provider authentication failed".
    # Pin just this agent's model + key env to explicit values. Scoped to the
    # dashboard agent only — we don't touch the user's other agents (hands,
    # multi-agent setups), whose providers may differ.
    LF_AGENT="${KC_LIBREFANG_AGENT:-coder}"
    for _af in "$LIBREFANG_TARGET"/workspaces/agents/"$LF_AGENT"/agent.toml \
               "$LIBREFANG_TARGET"/registry/agents/"$LF_AGENT"/agent.toml; do
      [ -f "$_af" ] || continue
      sed -i -E "s#^provider = \"default\"#provider = \"openrouter\"#; \
                 s#^model = \"(default|openrouter/.*)\"#model = \"$LF_MODEL\"#; \
                 s#^api_key_env = \".*\"#api_key_env = \"OPENROUTER_API_KEY\"#" "$_af" 2>/dev/null || true
    done
  fi
fi
# Pre-warm the daemon at boot. Otherwise it only starts lazily when the first
# `librefang chat` opens, and a cold start (daemon launch + model-catalog sync
# of ~57 files) can outlast the per-chat bootstrap's 10s poll window — so the
# first message after opening a fresh session appears to get no reply until it
# warms up. Backgrounded + idempotent (no-op if already running).
if [ -x "$LIBREFANG_TARGET/bin/librefang" ]; then
  HOME=/home/dev "$LIBREFANG_TARGET/bin/librefang" status -q >/dev/null 2>&1 \
    || HOME=/home/dev "$LIBREFANG_TARGET/bin/librefang" start >/dev/null 2>&1 &
fi

# The memory subsystem ships its Python package as flat configmap keys
# (configmap keys cannot contain "/"). Unpack them into a real `memory/`
# tree next to server.py so `from memory.manager import ...` resolves.
mkdir -p /tmp/browser/memory /home/dev/.claude-memory/backups
chmod 700 /home/dev/.claude-memory
# Materialize package files (rename flat keys → real layout).
install -m 0644 /browser-config/memory__init__.py /tmp/browser/memory/__init__.py
install -m 0644 /browser-config/memory_store.py    /tmp/browser/memory/store.py
install -m 0644 /browser-config/memory_manager.py  /tmp/browser/memory/manager.py
install -m 0644 /browser-config/memory_sync.py     /tmp/browser/memory/sync.py
install -m 0644 /browser-config/memory_embeddings.py        /tmp/browser/memory/embeddings.py
install -m 0644 /browser-config/memory_embeddings_worker.py /tmp/browser/memory/embeddings_worker.py

# The skills subsystem (multi-harness SKILL.md surface, issue #187) ships
# the same way: flat configmap keys reassembled into a real package tree.
mkdir -p /tmp/browser/skills/providers
install -m 0644 /browser-config/skills__init__.py            /tmp/browser/skills/__init__.py
install -m 0644 /browser-config/skills_model.py              /tmp/browser/skills/model.py
install -m 0644 /browser-config/skills_parser.py             /tmp/browser/skills/parser.py
install -m 0644 /browser-config/skills_sync.py               /tmp/browser/skills/sync.py
install -m 0644 /browser-config/skills_providers__init__.py  /tmp/browser/skills/providers/__init__.py
install -m 0644 /browser-config/skills_providers_claude.py   /tmp/browser/skills/providers/claude.py
install -m 0644 /browser-config/skills_providers_opencode.py /tmp/browser/skills/providers/opencode.py
install -m 0644 /browser-config/skills_providers_ante.py     /tmp/browser/skills/providers/ante.py
install -m 0644 /browser-config/skills_providers_antigravity.py /tmp/browser/skills/providers/antigravity.py
# Seed the per-user MCP server + user-prompt-submit hook next to the
# SQLite file so claude config points at PVC-backed paths that survive
# configmap rotations.
install -m 0755 /browser-config/mcp_memory.py          /home/dev/.claude-memory/mcp_memory.py
install -m 0755 /browser-config/memory_inject_hook.py  /home/dev/.claude-memory/memory_inject_hook.py
# The MCP server imports the same memory.* package; expose it alongside.
rm -rf /home/dev/.claude-memory/memory
mkdir -p /home/dev/.claude-memory/memory
install -m 0644 /tmp/browser/memory/__init__.py /home/dev/.claude-memory/memory/__init__.py
install -m 0644 /tmp/browser/memory/store.py    /home/dev/.claude-memory/memory/store.py
install -m 0644 /tmp/browser/memory/manager.py  /home/dev/.claude-memory/memory/manager.py
install -m 0644 /tmp/browser/memory/sync.py     /home/dev/.claude-memory/memory/sync.py
install -m 0644 /tmp/browser/memory/embeddings.py        /home/dev/.claude-memory/memory/embeddings.py
install -m 0644 /tmp/browser/memory/embeddings_worker.py /home/dev/.claude-memory/memory/embeddings_worker.py
# Register the MCP server in the user's claude config (idempotent merge).
python3 /browser-config/seed_claude_config.py || \
  log_stage "WARNING: seed_claude_config.py failed (memory MCP not registered)"

# Seed Ante's config so a spawned/selected Ante agent gets the SAME stdio
# MCP servers Claude does — the agent-orchestrator (so Ante can itself
# spawn + track sub-agents, making the task flow bidirectional) and the
# shared persistent memory. Idempotent merge: only the kube-coder-managed
# mcp_servers keys are touched, user settings are preserved.
# has_completed_onboarding is set so headless `ante -p` never stalls on a
# first-run onboarding prompt. Ante reads ~/.ante/settings.json (see
# docs.antigma.ai → Storage / MCP Servers).
log_stage "seeding Ante MCP config (~/.ante/settings.json)"
python3 - <<'PY' || log_stage "WARNING: Ante config seed failed (Ante MCP not registered)"
import json, os
ante_dir = os.path.expanduser('~/.ante')
os.makedirs(ante_dir, exist_ok=True)
path = os.path.join(ante_dir, 'settings.json')
try:
    with open(path) as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        cfg = {}
except (OSError, ValueError):
    cfg = {}
servers = cfg.get('mcp_servers')
if not isinstance(servers, dict):
    servers = {}
servers['agent-orchestrator'] = {
    'command': 'python3',
    'args': ['/tmp/browser/mcp_agent_orchestrator.py'],
}
servers['memory'] = {
    'command': 'python3',
    'args': ['/home/dev/.claude-memory/mcp_memory.py'],
}
# Dashboard tools — the same workspace read/action surface Claude gets, so a
# selected/spawned Ante agent (e.g. in the Hypervisor chat) can inspect metrics
# and tasks and take curated UI actions. See mcp_dashboard.py.
servers['dashboard'] = {
    'command': 'python3',
    'args': ['/tmp/browser/mcp_dashboard.py'],
}
cfg['mcp_servers'] = servers
cfg.setdefault('has_completed_onboarding', True)
# Default Ante to OpenRouter when that key is configured — Ante reuses the
# same OPENROUTER_API_KEY as OpenCode, and 'openrouter' is a built-in Ante
# provider (no catalog.json needed). Without a default, headless `ante -p`
# would auto-detect and could land on a provider with no credentials.
#
# Model: pin a cheap-but-capable default (DeepSeek v3.2 via OpenRouter,
# ~$0.23/$0.34 per 1M) — Ante is frequently a background sub-agent, so the
# expensive Sonnet default isn't warranted. Decoupled from OpenCode's
# KC_OPENROUTER_MODEL; override per-workspace with KC_ANTE_MODEL. Written
# explicitly (not setdefault) so the managed default also lands on existing
# PVCs whose settings.json still carries an older model.
if os.environ.get('OPENROUTER_API_KEY'):
    cfg['provider'] = 'openrouter'
    cfg['model'] = os.environ.get('KC_ANTE_MODEL', 'deepseek/deepseek-v3.2')
tmp = path + '.tmp'
with open(tmp, 'w') as f:
    json.dump(cfg, f, indent=2)
os.replace(tmp, path)
print('[seed_ante_config] wrote', path)
PY

# Seed Codex's MCP servers so a selected/spawned Codex agent gets the SAME
# stdio MCP surface as Claude/Ante — agent-orchestrator (spawn + track
# sub-agents), shared persistent memory, and the dashboard tools (metrics/
# tasks/UI actions in the Hypervisor). Codex reads MCP servers from
# $CODEX_HOME/config.toml; `codex mcp add` merges each block in idempotently
# (overwrites the named server, preserves everything else incl. auth.json), so
# a re-run on an established PVC is safe. Best-effort + gated on the binary so
# older images / a failed add never stall boot. Independent of `codex login`
# (MCP config isn't auth). CODEX_HOME is pinned to the PVC path the persistence
# block above manages, so it lands regardless of this script's own $HOME.
if command -v codex >/dev/null 2>&1; then
  log_stage "seeding Codex MCP config (~/.codex/config.toml)"
  for _mcp in \
    "agent-orchestrator /tmp/browser/mcp_agent_orchestrator.py" \
    "memory /home/dev/.claude-memory/mcp_memory.py" \
    "dashboard /tmp/browser/mcp_dashboard.py"; do
    set -- $_mcp   # $1=server name, $2=script path
    CODEX_HOME=/home/dev/.codex codex mcp add "$1" -- python3 "$2" >/dev/null 2>&1 \
      || log_stage "WARNING: codex mcp add $1 failed (Codex MCP $1 not registered)"
  done
fi

log_stage "starting browser/Claude API server on :6080"
mkdir -p /tmp/browser
# Copy Python sources (these need a pod restart to take effect).
# Skip the flat memory_* / memory__init__.py keys — reassembled above.
for f in /browser-config/*; do
  base=$(basename "$f")
  case "$base" in
    memory_*|memory__init__.py) continue ;;
    skills_*|skills__init__.py) continue ;;
  esac
  cp "$f" /tmp/browser/
done
# Symlink HTML/CSS/JS from the separate (non-checksummed) browser-html
# ConfigMap so dashboard edits + helm-upgrade refresh without a pod
# restart. The kubelet syncs the new ConfigMap to /browser-html
# atomically; the symlinks here dereference to the fresh content on
# the next HTTP request.
for f in /browser-html/*; do
  ln -sf "$f" "/tmp/browser/$(basename "$f")"
done
cd /tmp/browser
# Public-demo seed. Idempotent (no-ops once stores have data) and gated
# on READONLY_MODE so it can never pollute a real workspace. Runs before
# server.py binds so visitors never see an empty Memory / Build page.
if [ "${READONLY_MODE:-false}" = "true" ]; then
  log_stage "READONLY_MODE — running seed_demo.py to populate sample data"
  READONLY_MODE=true python3 /tmp/browser/seed_demo.py \
    2>&1 | sed 's/^/[seed_demo] /' || log_stage "WARNING: seed_demo.py exited non-zero"
fi
python3 server.py &

log_stage "all services launched, entering supervision loop"
tick=0
while true; do
  sleep 30
  tick=$((tick + 1))

  # Every 5 min: snapshot top RSS processes for post-incident diagnosis.
  # Rotates at ~1MB to keep PVC use bounded across long-lived pods.
  if [ $((tick % 10)) -eq 0 ]; then
    {
      echo "=== $(date -u +%FT%TZ) memory snapshot ==="
      ps -eo pid,ppid,rss,comm,args --sort=-rss 2>/dev/null | head -15
      echo "--- cgroup memory.current ---"
      cat /sys/fs/cgroup/memory.current 2>/dev/null \
        || cat /sys/fs/cgroup/memory/memory.usage_in_bytes 2>/dev/null \
        || echo "n/a"
      echo
    } >> /tmp/memory.log 2>&1
    if [ "$(stat -c %s /tmp/memory.log 2>/dev/null || echo 0)" -gt 1048576 ]; then
      tail -c 524288 /tmp/memory.log > /tmp/memory.log.tmp && mv /tmp/memory.log.tmp /tmp/memory.log
    fi
  fi

  # Every 5 min: reap leaked Playwright MCP browsers. The reaper (a) kills
  # crash-orphans (Firefox/chromium reparented to PID 1 — closing the gap
  # the old chromium-only awk had against the Firefox we actually ship) and
  # (b) sweeps Playwright browser trees that have burned ~no CPU for several
  # consecutive sweeps (idle ~30 min), which is the real leak: an idle
  # browser parked under a live MCP node for days holding ~1.3 GB. A browser
  # being actively driven bursts CPU on every tool call and is never killed;
  # the MCP relaunches a fresh browser on the next browser tool call. See
  # https://github.com/imran31415/kube-coder/issues/143. State persists in
  # /tmp across sweeps; decision logic is unit-tested in
  # tests/playwright_reaper_test.py.
  if [ $((tick % 10)) -eq 0 ]; then
    if [ -f /tmp/browser/playwright_reaper.py ]; then
      python3 /tmp/browser/playwright_reaper.py 2>&1 \
        | while read -r line; do log_stage "$line"; done
    fi
  fi

  if ! pgrep -f "python3 server.py" > /dev/null; then
    log_stage "restarting browser server"
    ( cd /tmp/browser && python3 server.py & )
  fi
  {{- if .Values.codeServer.enabled }}
  if ! netstat -tln 2>/dev/null | grep -q :8080; then
    log_stage "restarting code-server (killing stale processes first)"
    pkill -f "code-server" 2>/dev/null || true
    sleep 1
    NODE_OPTIONS="--max-old-space-size=1536" \
      code-server --bind-addr 0.0.0.0:8080 --auth none \
      --user-data-dir /home/dev/.local/share/code-server \
      --extensions-dir /home/dev/.local/share/code-server/extensions \
      /home/dev >> /tmp/code-server.log 2>&1 &
  fi
  {{- end }}
  if ! netstat -tln 2>/dev/null | grep -q :7681; then
    log_stage "restarting ttyd"
    ttyd --port 7681 --interface 0.0.0.0 $TTYD_WRITABLE \
      --client-option disableLeaveAlert=true \
      --client-option scrollback=10000 \
      -w /home/dev \
      /terminal-scripts/terminal-entry.sh >> /tmp/ttyd.log 2>&1 &
  fi
  {{- if .Values.browser.enabled }}
  if ! netstat -tln 2>/dev/null | grep -q :6081; then
    log_stage "restarting websockify"
    pkill -f websockify || true
    sleep 1
    websockify --web=/usr/share/novnc --heartbeat=30 6081 localhost:5900 \
      >> /tmp/websockify.log 2>&1 &
  fi
  {{- end }}
done
