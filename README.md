# Kube-Coder

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Helm](https://img.shields.io/badge/Helm-3.0%2B-blue?logo=helm)](https://helm.sh)
[![Kubernetes](https://img.shields.io/badge/Kubernetes-1.19%2B-326CE5?logo=kubernetes&logoColor=white)](https://kubernetes.io)
[![CI](https://github.com/imran31415/kube-coder/actions/workflows/ci.yml/badge.svg)](https://github.com/imran31415/kube-coder/actions/workflows/ci.yml)

A Helm chart for deploying secure, isolated development workspaces in Kubernetes. Each workspace provides VS Code IDE, terminal access, remote browser capabilities, and AI-powered Claude Code integration with remote task management -- all protected by GitHub OAuth2 authentication.

<table>
  <tr>
    <td align="center" width="25%">
      <img src="https://github.com/user-attachments/assets/93ec2ac0-f75f-4d5e-99e0-069a70ba14c5" width="250" alt="Dashboard" /><br/>
      <strong>Dashboard</strong><br/>
      <sub>Clean workspace hub with system metrics, service health, and GitHub config</sub>
    </td>
    <td align="center" width="25%">
      <img src="https://github.com/user-attachments/assets/34f1b356-22be-40da-bc99-c0a8a2a205a2" width="250" alt="Remote Browser" /><br/>
      <strong>VS Code IDE</strong><br/>
      <sub>Full-featured browser IDE with extensions and terminal access</sub>
    </td>
    <td align="center" width="25%">
      <img src="https://github.com/user-attachments/assets/dcf81a8a-da6a-42c9-a738-c331cc8aa36d" width="250" alt="VS Code IDE" /><br/>
      <strong>Remote Browser</strong><br/>
      <sub>Full Firefox browser via VNC — test web apps from anywhere</sub>
    </td>
    <td align="center" width="25%">
      <img width="707" height="690" alt="TTYD with claude" src="https://github.com/user-attachments/assets/7c3aa084-c52f-4b17-b3b9-059390eeddf1" />
      <strong>TTYD terminal interface accessible from browser</strong><br/>
      <sub>Claude built in with environment awareness </sub>
    </td>

  </tr>
</table>


## Features

### Core Development Environment
- **VS Code IDE** - Browser-based IDE with extensions support
- **Terminal** - Full system access via browser (ttyd)
- **Remote Browser** - Firefox with VNC viewer for testing web apps
- **System Monitoring** - Real-time CPU, memory, disk usage dashboard
- **GitHub Integration** - Easy SSH key and git config setup from dashboard

### Claude Code Integration
- **Claude Code CLI** - AI-powered development assistant built-in
- **Claude Task API** - Launch and manage Claude tasks remotely via REST API
- **Claude Tasks Dashboard** - Monitor running tasks, view status, one-click attach to interactive sessions
- **Remote Task Skill** - `/remote-task` Claude Code skill to manage tasks from your local terminal
- **Interactive Sessions** - Attach to any running Claude session to approve permissions, provide input, or observe progress

### Security & Authentication
- **GitHub OAuth2** - Secure authentication with configurable user authorization
- **GitHub App Auth** - Automatic private repo access via GitHub App installation tokens (auto-refreshed every 50 minutes)
- **HTTPS Everywhere** - Let's Encrypt certificates with automatic renewal
- **Isolated Workspaces** - Complete isolation between user environments

### Development Stack
- **Node.js 20 + Yarn** - Latest Node.js with Yarn package manager
- **Container Builds** - Docker-in-Docker with BuildKit support
- **Persistent Storage** - Dedicated storage that survives restarts

## Architecture

```
┌─────────────────────────────────────────────────┐
│             Base Infrastructure                  │
│  • Shared ConfigMaps (kaniko-wrapper, etc.)     │
└─────────────────────────────────────────────────┘
                        │
            ┌───────────┴───────────┐
            │                       │
┌───────────▼──────────┐  ┌─────────▼──────────────┐
│   Imran Workspace    │  │   Gerard Workspace     │
│  • Independent Helm  │  │  • Independent Helm    │
│  • Own PVC & secrets │  │  • Own PVC & secrets   │
│  • Dedicated ingress │  │  • Dedicated ingress   │
│  • Claude Task API   │  │  • Claude Task API     │
└──────────────────────┘  └────────────────────────┘
```

## Quick Start

### Prerequisites
- Kubernetes cluster (1.19+)
- Helm 3.0+
- nginx ingress controller
- cert-manager for HTTPS
- GitHub OAuth App

### Deploy

```bash
# Create namespace
kubectl create namespace coder

# Deploy base infrastructure
make deploy-base

# Deploy a workspace (auto-includes secrets if present)
make deploy-imran
```

### Access (OAuth2)

- **Dashboard**: `https://username.yourdomain.com/oauth`
- **VS Code**: `https://username.yourdomain.com/oauth/ide`
- **Terminal**: `https://username.yourdomain.com/oauth/terminal`

## Claude Task API

Each workspace exposes a REST API for remotely launching and managing Claude Code tasks. Tasks run as interactive tmux sessions that users can attach to for approving permissions and providing input.

See [docs/claude-task-api.md](./docs/claude-task-api.md) for full API documentation.

### Quick Example

```bash
# Create a task
curl -X POST https://imran.dev.archon.cx/api/claude/tasks \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Analyze the codebase and create a CLAUDE.md"}'

# Check status
curl https://imran.dev.archon.cx/api/claude/tasks \
  -H "Authorization: Bearer $TOKEN"
```

### `/remote-task` Skill

When working in this repo with Claude Code, use the `/remote-task` skill:

```bash
/remote-task analyze the codebase and create a CLAUDE.md   # Launch task
/remote-task status                                         # List all tasks
/remote-task output <TASK_ID>                              # View output
/remote-task attach <TASK_ID>                              # Attach info
/remote-task kill <TASK_ID>                                # Kill task
```

## Pre-installed Stack

| Category | Tools |
|----------|-------|
| Runtime | Node.js 20.19.4, Python 3.12, Go 1.22 |
| Package Managers | Yarn 1.22.22, npm, pip |
| Build Tools | Docker CLI, docker-compose, make, gcc |
| Cloud Tools | kubectl, GitHub CLI |
| AI Assistant | Claude Code CLI |
| Utilities | curl, jq, tmux, vim, nano |

## Commands

```bash
make help             # Show all commands
make deploy-imran     # Deploy Imran's workspace
make deploy-gerard    # Deploy Gerard's workspace
make deploy-all       # Deploy everything
make status           # Check deployment status
make test-imran       # Test workspace setup
make shell-imran      # Shell into workspace
make logs-imran       # View logs
make rollback-imran   # Rollback workspace
make version          # Show versions and config
```

## Adding New Users

See [NEW_USER_PROVISIONING.md](./NEW_USER_PROVISIONING.md) for details.

```bash
# Automated
./scripts/provision-user.sh john john_doe "John Doe" john@company.com dev.company.com

# Manual
mkdir deployments/john
cp templates/user-values-template.yaml deployments/john/values.yaml
# Edit values, then deploy
```

## Configuration Reference

### Workspace Values
```yaml
# deployments/username/values.yaml
namespace: coder

user:
  name: username
  pvcSize: 50Gi
  host: username.dev.yourdomain.com
  env:
    - name: GIT_USER_NAME
      value: "User Name"
    - name: GIT_USER_EMAIL
      value: "user@domain.com"

image:
  repository: registry.digitalocean.com/resourceloop/coder
  tag: devlaptop-v1.6.2-browser-stealth
  pullPolicy: Always

oauth2:
  githubUsers: "user1,user2"  # Authorized GitHub usernames

resources:
  requests:
    cpu: "2"
    memory: 3Gi
  limits:
    cpu: "3"
    memory: 5Gi
```

### Secrets (gitignored)

```yaml
# secrets/username/claude.yaml — Anthropic API key (optional)
claude:
  apiKey: "sk-ant-api03-..."

# secrets/username/github-app.yaml — GitHub App credentials (optional)
github:
  app:
    appId: "1234567"
    installationId: "12345678"
    privateKey: |
      -----BEGIN RSA PRIVATE KEY-----
      ...
      -----END RSA PRIVATE KEY-----
```

## Project Structure

```
charts/
├── base-infrastructure/       # Shared ConfigMaps
└── workspace/                 # Workspace template
    ├── dashboard.html         # Dashboard UI with Claude Tasks section
    ├── server.py              # Python HTTP server (dashboard, APIs, task management)
    └── templates/
        ├── deployment.yaml
        ├── service.yaml
        ├── ingress.yaml
        ├── ingress-oauth2.yaml
        ├── ingress-claude-api.yaml
        ├── oauth2-proxy.yaml
        ├── browser-configmap.yaml
        ├── claude-configmap.yaml
        ├── claude-secret.yaml
        ├── terminal-entry-configmap.yaml
        ├── github-app-secret.yaml
        ├── github-app-token-refresh.yaml
        ├── pvc.yaml
        └── serviceaccount.yaml

deployments/
├── imran/values.yaml          # User-specific config
└── gerard/values.yaml

secrets/                       # Gitignored
├── imran/
│   ├── claude.yaml            # Anthropic API key
│   └── github-app.yaml        # GitHub App credentials
└── gerard/

.claude/
└── skills/
    └── remote-task/SKILL.md   # /remote-task skill for managing remote Claude tasks

docs/
├── claude-task-api.md         # Full Claude Task API documentation
└── ...
```

## Documentation

- [Claude Task API](./docs/claude-task-api.md) - REST API for remote Claude task management
- [Browser Architecture](./BROWSER_ARCHITECTURE.md) - Remote browser VNC architecture
- [New User Provisioning](./NEW_USER_PROVISIONING.md) - Adding new workspace users

## Troubleshooting

```bash
# Check pods
kubectl get pods -n coder

# Check logs
make logs-imran

# Test workspace
make test-imran

# Shell access
make shell-imran

# Certificate issues
kubectl get certificate -n coder

# Check Claude task sessions
kubectl exec -n coder <pod> -c ide -- tmux list-sessions
```

## Security Features

- **GitHub OAuth2** - Secure authentication with user authorization
- **GitHub App tokens** - Short-lived installation tokens for private repo access (no long-lived PATs)
- **TLS encryption** - All traffic encrypted with Let's Encrypt
- **Workspace isolation** - Users cannot access each other's environments
- **Non-root containers** - All processes run as uid/gid 1000
- **Isolated storage** - Dedicated PVC per user
- **Interactive permissions** - Claude Code runs with standard permission mode; users approve file writes

## License

MIT
