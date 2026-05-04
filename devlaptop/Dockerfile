# devlaptop/Dockerfile
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg git openssh-server sudo jq unzip zip less vim nano tmux \
    build-essential pkg-config make \
    python3 python3-pip openjdk-17-jdk \
    golang-go \
    apt-transport-https lsb-release \
    mysql-client \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20.x via NodeSource
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g yarn \
    && rm -rf /var/lib/apt/lists/*

# Install Docker CLI and Docker Compose
RUN curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null \
    && apt-get update \
    && apt-get install -y docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

# Install standalone docker-compose for compatibility
RUN ARCH=$(dpkg --print-architecture) \
    && if [ "$ARCH" = "amd64" ]; then COMPOSE_ARCH="x86_64"; elif [ "$ARCH" = "arm64" ]; then COMPOSE_ARCH="aarch64"; else COMPOSE_ARCH="$ARCH"; fi \
    && curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${COMPOSE_ARCH}" -o /usr/local/bin/docker-compose \
    && chmod +x /usr/local/bin/docker-compose

# Install kubectl
RUN curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/$(dpkg --print-architecture)/kubectl" \
  && chmod +x kubectl \
  && mv kubectl /usr/local/bin/

# code-server (VS Code in the browser, MIT-licensed)
# https://github.com/coder/code-server
RUN ARCH=$(dpkg --print-architecture) \
  && curl -fsSL "https://github.com/coder/code-server/releases/download/v4.95.3/code-server_4.95.3_${ARCH}.deb" -o /tmp/cs.deb \
  && apt-get update && apt-get install -y /tmp/cs.deb && rm /tmp/cs.deb

# Claude Code CLI (terminal assistant). Bring your own API key (or Bedrock/Vertex).
# https://docs.anthropic.com/en/docs/claude-code/setup
# Version is pinned for reproducible builds. To bump: update CLAUDE_CODE_VERSION,
# rebuild the image (`make push`), and roll out the new image tag via Helm.
ARG CLAUDE_CODE_VERSION=2.1.126
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}

# Install ttyd for mobile-friendly terminal access.
# Pinned to a known-good version so transient 502s from GitHub's release CDN
# don't break the build (we hit them repeatedly while bumping to v1.7.1).
# To bump: update TTYD_VERSION, rebuild image.
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
    fluxbox xterm \
    libgtk-3-0 libdbus-glib-1-2 libxt6 libwayland-client0 \
    && rm -rf /var/lib/apt/lists/* \
    && wget -O /tmp/firefox.tar.bz2 "https://download.mozilla.org/?product=firefox-latest&os=linux64&lang=en-US" \
    && file /tmp/firefox.tar.bz2 \
    && tar -xf /tmp/firefox.tar.bz2 -C /opt/ \
    && ln -s /opt/firefox/firefox /usr/local/bin/firefox \
    && rm /tmp/firefox.tar.bz2

# Create a simple script to start the browser service
RUN mkdir -p /opt/browser && \
    cat > /opt/browser/start-browser.sh << 'EOF'
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
EOF

RUN chmod +x /opt/browser/start-browser.sh

# Non-root dev user
RUN useradd -m -s /bin/bash dev && echo "dev ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers
# Also give ubuntu user (created by code-server) passwordless sudo
RUN echo "ubuntu ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers
USER dev
WORKDIR /home/dev

# Ensure shell always starts in /home/dev
RUN echo 'cd /home/dev' >> ~/.bashrc

# Note: docker builds use the mounted 'docker-build' wrapper that spawns a Kaniko Job.
