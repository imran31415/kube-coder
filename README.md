# Kube-Coder


A Helm chart for deploying secure, isolated development workspaces in Kubernetes. Each workspace provides VS Code IDE, terminal access, and remote browser capabilities, protected by GitHub OAuth2 authentication.

A clean user dashboard:
<img width="401" height="809" alt="image" src="https://github.com/user-attachments/assets/93ec2ac0-f75f-4d5e-99e0-069a70ba14c5" />

Access browser/editor from anywhere
<img width="685" height="407" alt="image" src="https://github.com/user-attachments/assets/34f1b356-22be-40da-bc99-c0a8a2a205a2" />

<img width="601" height="173" alt="image" src="https://github.com/user-attachments/assets/4e354a1c-c72f-4617-a95f-ed6273649a56" />

<img width="1279" height="793" alt="image" src="https://github.com/user-attachments/assets/dcf81a8a-da6a-42c9-a738-c331cc8aa36d" />



## Features

### Core Development Environment
- **VS Code IDE** - Browser-based IDE with extensions support
- **Terminal** - Full system access via browser
- **Remote Browser** - Firefox with VNC viewer for testing web apps
- **System Monitoring** - Real-time CPU, memory, disk usage dashboard
- **GitHub Integration** - Easy SSH key and git config setup from dashboard

### Security & Authentication
- **GitHub OAuth2** - Secure authentication with configurable user authorization
- **HTTPS Everywhere** - Let's Encrypt certificates with automatic renewal
- **Isolated Workspaces** - Complete isolation between user environments

### Development Stack
- **Node.js 20 + Yarn** - Latest Node.js with Yarn package manager
- **Container Builds** - Docker-in-Docker with BuildKit support
- **Claude Code CLI** - AI-powered development assistant built-in
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

# Deploy a workspace
make deploy-imran
```

### Access (OAuth2)

- **Dashboard**: `https://username.yourdomain.com/oauth`
- **VS Code**: `https://username.yourdomain.com/oauth/ide`
- **Terminal**: `https://username.yourdomain.com/oauth/terminal`

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

## Project Structure

```
charts/
├── base-infrastructure/    # Shared ConfigMaps
└── workspace/              # Workspace template
    └── templates/
        ├── deployment.yaml
        ├── service.yaml
        ├── ingress.yaml
        ├── ingress-oauth2.yaml
        ├── oauth2-proxy.yaml
        ├── browser-configmap.yaml
        ├── pvc.yaml
        └── serviceaccount.yaml

deployments/
├── imran/values.yaml       # User-specific config
└── gerard/values.yaml
```

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
```

## Security Features

- **GitHub OAuth2** - Secure authentication with user authorization
- **TLS encryption** - All traffic encrypted with Let's Encrypt
- **Workspace isolation** - Users cannot access each other's environments
- **Non-root containers** - All processes run as uid/gid 1000
- **Isolated storage** - Dedicated PVC per user

## License

MIT
