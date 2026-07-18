# syntax=docker/dockerfile:1.7
# devlaptop/Dockerfile
#
# Multi-stage:
#   1. `dashboard-builder` — builds the next-generation dashboard SPA
#      (charts/workspace/web/) with Vite. Heavy dev deps (~200MB) stay in
#      this stage and never reach the final image.
#   2. The main Ubuntu stage that runs in the workspace pod. It copies the
#      built dist/ into /opt/dashboard-dist/, which server.py serves at /next.

FROM node:20-bookworm-slim AS dashboard-builder
WORKDIR /web
# Copy package manifests first so dep-install layer is cacheable.
COPY charts/workspace/web/package.json ./package.json
COPY charts/workspace/web/yarn.lock* ./
RUN if [ -f yarn.lock ]; then yarn install --frozen-lockfile; else yarn install; fi
COPY charts/workspace/web/ ./
RUN yarn build

FROM ubuntu:24.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive
# `apt-get upgrade` patches base-image OS packages that ship with known CVEs
# (e.g. libgnutls30t64, libgcrypt20) and auto-picks-up future base CVE fixes on
# rebuild. See github.com/imran31415/kube-coder/issues/75.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg git openssh-server sudo jq unzip zip less vim nano tmux \
    build-essential pkg-config make \
    python3 python3-pip openjdk-17-jdk \
    golang-go \
    apt-transport-https lsb-release \
    mysql-client \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20.x via NodeSource
# npm bundled with Node 20 (10.8.x) vendors old tar/minimatch/glob/cross-spawn
# that Trivy flags (HIGH). Upgrade the global npm first so it ships patched
# vendored deps (npm 11.x: tar>=7.5, minimatch>=10.2, glob>=13, cross-spawn
# dropped). Pinned for reproducible builds — bump NPM_VERSION + rebuild to
# update. See github.com/imran31415/kube-coder/issues/69.
# renovate: datasource=npm depName=npm
ARG NPM_VERSION=11.18.0
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g npm@${NPM_VERSION} \
    && npm install -g yarn \
    && rm -rf /var/lib/apt/lists/*

# Install Docker CLI and Docker Compose
RUN curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null \
    && apt-get update \
    && apt-get install -y docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

# Install standalone docker-compose for compatibility.
# Pinned for reproducible builds — Renovate bumps COMPOSE_VERSION weekly
# (github-releases datasource: docker/compose). To bump manually: edit + rebuild.
# renovate: datasource=github-releases depName=docker/compose
ARG COMPOSE_VERSION=v5.3.1
RUN ARCH=$(dpkg --print-architecture) \
    && if [ "$ARCH" = "amd64" ]; then COMPOSE_ARCH="x86_64"; elif [ "$ARCH" = "arm64" ]; then COMPOSE_ARCH="aarch64"; else COMPOSE_ARCH="$ARCH"; fi \
    && curl -L "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${COMPOSE_ARCH}" -o /usr/local/bin/docker-compose \
    && chmod +x /usr/local/bin/docker-compose

# Install kubectl
RUN curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/$(dpkg --print-architecture)/kubectl" \
  && chmod +x kubectl \
  && mv kubectl /usr/local/bin/

# code-server (VS Code in the browser, MIT-licensed)
# https://github.com/coder/code-server
# Pinned for reproducible builds. Bumped 4.125.0 -> 4.128.0 to ship patched
# vendored deps Trivy flagged: undici 7.24.4 -> 7.28.0 (HIGH) + tar 7.5.13 ->
# 7.5.16 (MEDIUM) + ws. Note: the deps bundled under lib/vscode/ (undici, ws,
# tar) track the upstream VS Code build code-server vendors and only move when
# code-server rebases onto a newer VS Code — so bumping code-server is the lever.
# renovate: datasource=github-releases depName=coder/code-server
ARG CODE_SERVER_VERSION=4.128.0
RUN ARCH=$(dpkg --print-architecture) \
  && curl -fsSL "https://github.com/coder/code-server/releases/download/v${CODE_SERVER_VERSION}/code-server_${CODE_SERVER_VERSION}_${ARCH}.deb" -o /tmp/cs.deb \
  && apt-get update && apt-get install -y /tmp/cs.deb && rm /tmp/cs.deb

# Claude Code CLI (terminal assistant). Bring your own API key (or Bedrock/Vertex).
# https://docs.anthropic.com/en/docs/claude-code/setup
# Version is pinned for reproducible builds. To bump: update CLAUDE_CODE_VERSION,
# rebuild the image (`make push`), and roll out the new image tag via Helm.
# renovate: datasource=npm depName=@anthropic-ai/claude-code
ARG CLAUDE_CODE_VERSION=2.1.211
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}

# OpenCode CLI — alternative AI coding assistant. Selected per-workspace via
# the chart's `assistant.provider` value (defaults to claude). Supports
# OpenRouter directly and any OpenAI-compatible base URL (e.g. a self-hosted
# proxy on a Digital Ocean droplet) via on-disk config written at pod start.
# https://opencode.ai
# renovate: datasource=npm depName=opencode-ai
ARG OPENCODE_VERSION=1.18.3
RUN npm install -g opencode-ai@${OPENCODE_VERSION}

# Codex CLI — OpenAI's terminal-native coding agent. Surfaces in the dashboard
# as the `codex` assistant and drives the Hypervisor via `codex exec --json`.
# Auth is ChatGPT OAuth (no API key) — the user runs `codex login` once in the
# pod; credentials + sessions live under $CODEX_HOME (~/.codex), which start.sh
# persists on the PVC (same pattern as ~/.claude). npm-global install, so no
# PVC-binary symlink is needed (unlike agy/ante). https://github.com/openai/codex
# renovate: datasource=npm depName=@openai/codex
ARG CODEX_VERSION=0.144.5
RUN npm install -g @openai/codex@${CODEX_VERSION}

# Antigravity CLI (agy) — Google's terminal-native coding agent. Selected
# per-task in the dashboard (the `antigravity` assistant) and spawnable via the
# agent-orchestrator. Auth is OAuth Google Sign-In (no API key) — the user runs
# `agy` once in the pod to log in. Same PVC-persistence pattern as Ante below:
# we install the binary to /opt/antigravity/agy and make /usr/local/bin/agy a
# *symlink* to the PVC-backed path /home/dev/.antigravity/bin/agy that start.sh
# seeds on boot — a writable, persistent location for the binary across pod
# restarts (the pod blocks runtime sudo). The official installer (curl|bash,
# --dir to place the binary) is the only distribution channel, same mechanism
# as Ante/LibreFang. https://antigravity.google
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash -s -- --dir /opt/antigravity \
    && test -x /opt/antigravity/agy \
    && ln -sfn /home/dev/.antigravity/bin/agy /usr/local/bin/agy

# Ante CLI — terminal-native AI coding agent from Antigma Labs.
# Distributed as a single self-contained binary. install.sh installs it to
# $HOME/.ante/bin (/root/.ante/bin in this root build stage). We keep the real
# binary at /opt/ante/ante and make /usr/local/bin/ante a *symlink* to the
# PVC-backed path /home/dev/.ante/bin/ante. start.sh seeds that PVC path from
# /opt/ante/ante on boot — this gives a writable, persistent binary location
# (the pod blocks runtime sudo, so we can't repoint /usr/local/bin at runtime),
# while the image stays the source of truth: bump ANTE_VERSION + rebuild to
# update. Pinned to a concrete release manifest (the `stable` channel floats)
# for reproducible builds. Ante ships no GitHub releases / OCI tag, so Renovate
# can't auto-track it — this is a documented manual-bump exception (see
# docs/SUPPLY_CHAIN.md); check https://download.ante.run/channels/stable/manifest.json
# for the current version.
# https://docs.antigma.ai
ARG ANTE_VERSION=v0.preview.60
RUN curl -fsSL https://ante.run/install.sh | bash -s -- "https://download.ante.run/releases/${ANTE_VERSION}/manifest.json" \
    && mkdir -p /opt/ante \
    && install -m 0755 "$HOME/.ante/bin/ante" /opt/ante/ante \
    && test -x /opt/ante/ante \
    && ln -sfn /home/dev/.ante/bin/ante /usr/local/bin/ante

# LibreFang CLI — open-source agent OS (https://librefang.ai); surfaces in
# the dashboard/orchestrator as the `librefang` assistant. Same persistence
# pattern as Ante above: the real binary lives at /opt/librefang/librefang
# and /usr/local/bin/librefang is a *symlink* to the PVC-backed path
# /home/dev/.librefang/bin/librefang, which start.sh seeds from /opt on
# boot. ~/.librefang also holds LibreFang's own config/agents/sessions, so
# the one PVC symlink persists data + binary together.
#
# Pull the release tarball straight from GitHub Releases instead of piping
# librefang.ai/install.sh: that host 403s GitHub Actions runner IPs, which
# broke the CI image build (and cascaded into the Trivy SARIF upload). The
# GitHub release CDN is reachable from CI — same as ttyd below. Prefer the
# static musl build, fall back to glibc. The installer pulls this exact
# tarball (librefang/librefang releases); we just skip its WAF-gated front
# door. Bump LIBREFANG_VERSION (a release tag) + rebuild to update.
# renovate: datasource=github-releases depName=librefang/librefang
ARG LIBREFANG_VERSION=v2026.7.11
RUN ARCH=$(dpkg --print-architecture) \
    && case "$ARCH" in \
         amd64) LF_ARCH=x86_64 ;; \
         arm64) LF_ARCH=aarch64 ;; \
         *) LF_ARCH="$ARCH" ;; \
       esac \
    && BASE="https://github.com/librefang/librefang/releases/download/${LIBREFANG_VERSION}" \
    && mkdir -p /opt/librefang \
    && ( curl -fL --retry 5 --retry-delay 5 --retry-connrefused \
           "${BASE}/librefang-${LF_ARCH}-unknown-linux-musl.tar.gz" -o /tmp/librefang.tar.gz \
         || curl -fL --retry 5 --retry-delay 5 --retry-connrefused \
           "${BASE}/librefang-${LF_ARCH}-unknown-linux-gnu.tar.gz" -o /tmp/librefang.tar.gz ) \
    && tar -xzf /tmp/librefang.tar.gz -C /opt/librefang \
    && rm /tmp/librefang.tar.gz \
    && chmod +x /opt/librefang/librefang* \
    && test -x /opt/librefang/librefang \
    && ln -sfn /home/dev/.librefang/bin/librefang /usr/local/bin/librefang

# Install ttyd for mobile-friendly terminal access.
# Pinned to a known-good version so transient 502s from GitHub's release CDN
# don't break the build (we hit them repeatedly while bumping to v1.7.1).
# To bump: update TTYD_VERSION, rebuild image.
# renovate: datasource=github-releases depName=tsl0922/ttyd
ARG TTYD_VERSION=1.7.7
RUN apt-get update && apt-get install -y wget \
    && ARCH=$(dpkg --print-architecture) \
    && if [ "$ARCH" = "amd64" ]; then TTYD_ARCH="x86_64"; elif [ "$ARCH" = "arm64" ]; then TTYD_ARCH="aarch64"; else TTYD_ARCH="$ARCH"; fi \
    && wget --tries=5 --waitretry=10 --retry-connrefused \
       --retry-on-http-error=429,500,502,503,504 \
       -O /usr/local/bin/ttyd \
       "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.${TTYD_ARCH}" \
    && chmod +x /usr/local/bin/ttyd \
    && rm -rf /var/lib/apt/lists/*

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# sqlite-vec loadable extension — enables vector search inside SQLite for the
# persistent-memory subsystem (Phase 2 activates this). Shipped as a single
# .so so the dashboard + MCP memory server can `conn.load_extension('vec0')`.
# renovate: datasource=github-releases depName=asg017/sqlite-vec
ARG SQLITE_VEC_VERSION=0.1.9
RUN ARCH=$(dpkg --print-architecture) \
 && case "$ARCH" in amd64) VEC_ARCH=x86_64 ;; arm64) VEC_ARCH=aarch64 ;; *) VEC_ARCH=$ARCH ;; esac \
 && mkdir -p /usr/local/lib/sqlite-vec \
 && curl -fsSL "https://github.com/asg017/sqlite-vec/releases/download/v${SQLITE_VEC_VERSION}/sqlite-vec-${SQLITE_VEC_VERSION}-loadable-linux-${VEC_ARCH}.tar.gz" \
    | tar -xz -C /usr/local/lib/sqlite-vec/ \
 && ln -sf /usr/local/lib/sqlite-vec/vec0.so /usr/local/lib/vec0.so

# Python deps for memory-system embeddings (Voyage AI / OpenAI) and HTTP
# retries. Installed eagerly so Phase 2 activation needs only a value flip.
# --break-system-packages is required on Ubuntu 24.04 (PEP 668).
RUN pip3 install --break-system-packages --no-cache-dir \
    voyageai==0.4.0 \
    httpx==0.28.1

# Install browser with noVNC for web access.
# `file` lets the post-download tarball check below work.
# fluxbox is the X11 window manager driving the VNC viewer (without it the
# remote desktop is just a black Xvfb canvas). The libgtk-3 / libdbus-glib /
# libxt / libwayland packages are runtime deps for modern Firefox — without
# them `firefox --version` fails with "libgtk-3.so.0: cannot open shared
# object file". This used to be supplied by the layered v1.x.x-browser-stealth
# image (browser-dockerfile/Dockerfile); folding it into the base avoids the
# two-image pattern.
RUN apt-get update && apt-get install -y \
    xvfb x11vnc novnc websockify wget file \
    fluxbox xterm xz-utils \
    x11-xserver-utils \
    libgtk-3-0 libdbus-glib-1-2 libxt6 libwayland-client0 \
    && rm -rf /var/lib/apt/lists/*

# Firefox pinned to an explicit release (the `firefox-latest` redirect floats).
# Renovate does not have a Mozilla datasource, so this is a documented
# manual-bump exception (see docs/SUPPLY_CHAIN.md): current version at
# https://product-details.mozilla.org/1.0/firefox_versions.json (LATEST_FIREFOX_VERSION).
ARG FIREFOX_VERSION=152.0.6
RUN wget -O /tmp/firefox.tar.xz "https://download-installer.cdn.mozilla.net/pub/firefox/releases/${FIREFOX_VERSION}/linux-x86_64/en-US/firefox-${FIREFOX_VERSION}.tar.xz" \
    && file /tmp/firefox.tar.xz \
    && tar -xf /tmp/firefox.tar.xz -C /opt/ \
    && ln -s /opt/firefox/firefox /usr/local/bin/firefox \
    && rm /tmp/firefox.tar.xz

# Create a simple script to start the browser service.
# Uses buildkit heredoc syntax (`RUN <<OUTER`) so the inner shell heredoc
# isn't mistaken for new Dockerfile instructions during parse.
RUN <<OUTER
mkdir -p /opt/browser
cat > /opt/browser/start-browser.sh <<'SCRIPT'
#!/bin/bash
# Start Xvfb
Xvfb :99 -screen 0 1280x720x24 &
export DISPLAY=:99
sleep 2

# Start x11vnc (no password for simplicity within the pod)
x11vnc -display :99 -nopw -listen localhost -xkb -ncache 10 -forever &
sleep 2

# Start Firefox
firefox &

# Start websockify to serve noVNC
websockify --web=/usr/share/novnc/ 6080 localhost:5900
SCRIPT
chmod +x /opt/browser/start-browser.sh
OUTER

# Next-generation dashboard SPA (Vite + Preact). Built in the
# dashboard-builder stage above; server.py serves it at /next during the
# migration and at / once Phase 6 cuts over.
COPY --from=dashboard-builder /web/dist /opt/dashboard-dist

# Non-root dev user
RUN useradd -m -s /bin/bash dev && echo "dev ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers
# Also give ubuntu user (created by code-server) passwordless sudo
RUN echo "ubuntu ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers
USER dev
WORKDIR /home/dev

# Ensure shell always starts in /home/dev
RUN echo 'cd /home/dev' >> ~/.bashrc

# Note: docker builds use the mounted 'docker-build' wrapper that spawns a Kaniko Job.
