# devlaptop/Dockerfile
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends     ca-certificates curl gnupg git openssh-server sudo jq unzip zip less vim nano tmux     build-essential pkg-config make     nodejs npm python3 python3-pip openjdk-17-jdk     golang-go   && rm -rf /var/lib/apt/lists/*

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

# Non-root dev user
RUN useradd -m -s /bin/bash dev && echo "dev ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers
USER dev
WORKDIR /home/dev

# Note: docker builds use the mounted 'docker-build' wrapper that spawns a Kaniko Job.
