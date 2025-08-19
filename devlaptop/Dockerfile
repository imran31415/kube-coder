# devlaptop/Dockerfile
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg git openssh-server sudo jq unzip zip less vim nano tmux \
    build-essential pkg-config make \
    nodejs npm python3 python3-pip openjdk-17-jdk \
    golang-go \
    apt-transport-https lsb-release \
    mysql-client \
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
RUN npm install -g @anthropic-ai/claude-code

# Install ttyd for mobile-friendly terminal access
RUN apt-get update && apt-get install -y wget \
    && ARCH=$(dpkg --print-architecture) \
    && if [ "$ARCH" = "amd64" ]; then TTYD_ARCH="x86_64"; elif [ "$ARCH" = "arm64" ]; then TTYD_ARCH="aarch64"; else TTYD_ARCH="$ARCH"; fi \
    && wget -O /usr/local/bin/ttyd "https://github.com/tsl0922/ttyd/releases/latest/download/ttyd.${TTYD_ARCH}" \
    && chmod +x /usr/local/bin/ttyd \
    && rm -rf /var/lib/apt/lists/*

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Non-root dev user
RUN useradd -m -s /bin/bash dev && echo "dev ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers
# Also give ubuntu user (created by code-server) passwordless sudo
RUN echo "ubuntu ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers
USER dev
WORKDIR /home/dev

# Note: docker builds use the mounted 'docker-build' wrapper that spawns a Kaniko Job.
