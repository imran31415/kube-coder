# Remote Dev Helm

A production-ready Helm chart for deploying secure, isolated development workspaces in Kubernetes. Each workspace provides a complete development environment with VS Code IDE, terminal access, and remote browser capabilities, all protected by GitHub OAuth2 authentication.

<img width="2720" height="1796" alt="image" src="https://github.com/user-attachments/assets/72ca8635-80c0-4ae2-b9ee-14ed918185eb" />

## ✨ Features

### Core Development Environment
- 💻 **VS Code IDE** - Full-featured browser-based IDE with extensions support
- ⚡ **Terminal Access** - Browser-based terminal with full system access
- 🌐 **Remote Browser** - Firefox browser with VNC viewer for testing web applications
- 🎨 **Modern Control Panel** - Beautiful, mobile-responsive dashboard to access all services

### Security & Authentication  
- 🔐 **GitHub OAuth2** - Secure authentication with configurable user authorization
- 🔒 **HTTPS Everywhere** - Let's Encrypt certificates with automatic renewal
- 🛡️ **Isolated Workspaces** - Complete isolation between user environments
- 👥 **Multi-User Support** - Independent workspaces with separate authentication

### Development Stack
- 🔧 **Node.js 20 + Yarn** - Latest Node.js with Yarn package manager
- 🐳 **Container Builds** - Docker-in-Docker with BuildKit support
- 🤖 **Claude Code CLI** - AI-powered development assistant built-in
- 💾 **Persistent Storage** - Dedicated storage that survives redeploys and restarts

## 🏗️ Architecture

### New Modular Design

```
┌─────────────────────────────────────────────────┐
│             Base Infrastructure                  │
│  • Shared ConfigMaps (kaniko-wrapper, etc.)     │
│  • Common build tools and utilities             │
└─────────────────────────────────────────────────┘
                        │
            ┌───────────┴───────────┐
            │                       │
┌───────────▼──────────┐  ┌─────────▼──────────────┐
│   Imran Workspace    │  │   Gerard Workspace     │
│  (imran-workspace)   │  │  (gerard-workspace)    │
│  • Independent Helm  │  │  • Independent Helm    │
│  • Own PVC & secrets │  │  • Own PVC & secrets   │
│  • Dedicated ingress │  │  • Dedicated ingress   │
└──────────────────────┘  └────────────────────────┘
```

### Charts Structure
```
charts/
├── base-infrastructure/     # Shared resources
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
│       └── configmaps.yaml
└── workspace/              # Individual workspace template
    ├── Chart.yaml
    ├── values.yaml
    └── templates/
        ├── deployment.yaml
        ├── service.yaml
        ├── ingress.yaml
        ├── pvc.yaml
        └── serviceaccount.yaml
```

## 🚀 Quick Start

### Prerequisites
- Kubernetes cluster (1.19+)
- Helm 3.0+
- nginx ingress controller
- cert-manager for automatic HTTPS
- GitHub OAuth App (for OAuth2 authentication)

### 1. Setup Infrastructure

```bash
# Clone the repository
git clone <your-repo-url>
cd remote-dev-helm

# Create namespace
kubectl create namespace coder

# Create registry secret
kubectl create secret docker-registry regcred \
  --docker-server=registry.digitalocean.com \
  --docker-username=your-username \
  --docker-password=your-token \
  -n coder

# Deploy base infrastructure
make deploy-base
```

### 2. Setup Authentication

#### Option A: GitHub OAuth2 (Recommended)
```bash
# 1. Create GitHub OAuth App at https://github.com/settings/developers
#    - Authorization callback URL: https://username.yourdomain.com/oauth2/callback
#    - Note the Client ID and Client Secret

# 2. Configure authorized users in deployments/imran/values.yaml
oauth2:
  githubUsers: "username1,username2"  # Comma-separated GitHub usernames

# 3. Create OAuth secrets file (gitignored)
mkdir -p secrets/imran
cat > secrets/imran/oauth2.yaml << EOF
oauth2:
  cookieSecret: "$(openssl rand -base64 32)"
  clientId: "your_github_oauth_app_client_id"
  clientSecret: "your_github_oauth_app_client_secret"
EOF

# 4. Deploy with OAuth2
helm upgrade imran-workspace charts/workspace/ \
  -f deployments/imran/values-oauth2.yaml \
  -f secrets/imran/oauth2.yaml \
  --namespace coder --install
```

#### Option B: Basic Auth (Legacy)
```bash
# Create basic auth for users
htpasswd -c auth admin
kubectl create secret generic api-basic-auth --from-file=auth -n coder

# Deploy with basic auth
make deploy-imran
```

### 3. Access Your Workspace

#### With OAuth2:
- **Control Panel**: `https://username.yourdomain.com/oauth` - Modern dashboard with service selection
- **VS Code IDE**: `https://username.yourdomain.com/oauth/ide` - Full-featured code editor  
- **Terminal**: `https://username.yourdomain.com/oauth/terminal` - Browser-based terminal
- **Remote Browser**: Launch from control panel - Firefox with VNC viewer

#### With Basic Auth:
- **VS Code IDE**: `https://username.yourdomain.com/` - Main IDE interface
- **Terminal**: `https://username.yourdomain.com/terminal` - Browser-based terminal
- **Browser Controls**: `https://username.yourdomain.com/browser` - Remote browser interface

```bash
# Check deployment status
make status
kubectl get pods -n coder
```

## 🛠️ Pre-installed Stack

Each workspace includes:

| Category | Tools/Versions |
|----------|----------------|
| **Runtime** | Node.js 20.19.4, Python 3.12, Go 1.22 |
| **Package Managers** | Yarn 1.22.22, npm, pip |
| **Development** | VS Code Server, ttyd terminal |
| **Build Tools** | Docker CLI, docker-compose, make, gcc |
| **Cloud Tools** | kubectl, GitHub CLI |
| **Version Control** | Git |
| **AI Assistant** | Claude Code CLI |
| **Utilities** | curl, jq, tmux, vim, nano |

## 📋 Management Commands

### Build & Deploy
```bash
make help              # Show all available commands
make build            # Build Docker image
make push             # Build and push image
make deploy-all       # Deploy everything
```

### Individual User Management  
```bash
# Imran's workspace
make deploy-imran     # Deploy/update Imran
make rollback-imran   # Rollback Imran
make shell-imran      # Shell into Imran's pod
make logs-imran       # View Imran's logs
make test-imran       # Test Imran's setup

# Gerard's workspace  
make deploy-gerard    # Deploy/update Gerard
make rollback-gerard  # Rollback Gerard
make shell-gerard     # Shell into Gerard's pod
make logs-gerard      # View Gerard's logs
make test-gerard      # Test Gerard's setup
```

### Monitoring
```bash
make status           # Overall deployment status
make version          # Show versions and config
kubectl top pods -n coder  # Resource usage
```

## 👥 Adding New Users

For detailed user provisioning instructions, see **[NEW_USER_PROVISIONING.md](./NEW_USER_PROVISIONING.md)**.

### Quick Start
```bash
# Automated provisioning (recommended)
./scripts/provision-user.sh john john_doe "John Doe" john.doe@company.com dev.company.com

# Manual provisioning
mkdir deployments/john
cp templates/user-values-template.yaml deployments/john/values.yaml
# Edit the values file with user details
make deploy-john
```

### Required Information
- **GitHub username** (for OAuth2 authentication)
- **Full name** and **email** (for git configuration)  
- **Subdomain** (e.g., `john` for `john.dev.company.com`)
- **Storage size** (default: 50Gi)

### Access URLs
- **Basic Auth**: `https://username.yourdomain.com/`
- **OAuth2**: `https://username.yourdomain.com/oauth` (recommended)

## 🔧 Configuration Reference

### Workspace Values
```yaml
# deployments/username/values.yaml
namespace: coder

user:
  name: username
  pvcSize: 50Gi  # Persistent storage size
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

ingress:
  className: nginx
  auth:
    type: basic  # or 'oauth2' for GitHub OAuth
    secretName: username-basic-auth
  tls:
    enabled: true
    secretName: username-dev-yourdomain-com-tls
    clusterIssuer: letsencrypt-production

# OAuth2 configuration (when auth.type is 'oauth2')
oauth2:
  githubUsers: "user1,user2"  # Authorized GitHub usernames
  # Secrets provided separately in secrets/username/oauth2.yaml
  cookieSecret: "PLACEHOLDER-OVERRIDE-WITH-SECRETS-FILE"
  clientId: "PLACEHOLDER-OVERRIDE-WITH-SECRETS-FILE"
  clientSecret: "PLACEHOLDER-OVERRIDE-WITH-SECRETS-FILE"

resources:
  requests:
    cpu: "2"
    memory: 3Gi
  limits:
    cpu: "3"
    memory: 5Gi
```

## 🐳 Container Builds

Use Docker-in-Docker for secure builds:

```bash
# In workspace terminal
docker build -t myapp:latest .
docker push myregistry.com/myapp:latest

# Or use the kaniko wrapper
docker-build -t myregistry.com/myapp:latest .
```

## 🔍 Troubleshooting

### Check Individual Workspace
```bash
kubectl get pods -n coder -l app=ws-username
kubectl describe pod ws-username-xxxxx -n coder
kubectl logs ws-username-xxxxx -c ide -n coder
```

### Certificate Issues
```bash
kubectl get certificate -n coder
kubectl describe certificate username-dev-yourdomain-com-tls -n coder
```

### Persistent Storage
```bash
kubectl get pvc -n coder
kubectl describe pvc ws-username-home -n coder
```

### Node/Yarn Issues
```bash
# Test in workspace
make shell-username
node --version    # Should show v20.19.4
yarn --version    # Should show 1.22.22
pwd              # Should be /home/dev
```

## 🛡️ Security Features

- 🔐 **GitHub OAuth2 Authentication** - Secure, modern authentication with user authorization
- ✅ **TLS encryption** for all traffic with Let's Encrypt certificates
- 🛡️ **Complete workspace isolation** - Users cannot access each other's environments
- ✅ **Non-root containers** - All processes run as uid/gid 1000 for security
- 🔒 **Private registry authentication** - Secure container image pulling
- 💾 **Isolated storage** - Dedicated PVC per user with persistent data
- ⚡ **Recreate deployment strategy** - Prevents resource conflicts and ensures clean restarts
- 🌐 **Protected endpoints** - All services (IDE, Terminal, Browser) behind authentication

## 🔄 Architecture Benefits

### Independent Management
- **Per-user deployments** - Update one user without affecting others
- **Isolated rollbacks** - Rollback individual workspaces
- **Resource isolation** - No shared state conflicts
- **Scalable onboarding** - Add users by copying values files

### Operational Excellence
- **No duplicate pods** - Recreate strategy eliminates PVC conflicts
- **Persistent workspaces** - State survives pod restarts/upgrades
- **Working directory** - Always starts in `/home/dev`
- **Modern stack** - Node.js 20, latest yarn, updated tools

## 🗑️ Removal

### Remove Individual User
```bash
helm uninstall username-workspace -n coder
kubectl delete pvc ws-username-home -n coder  # WARNING: Deletes user data
```

### Complete Uninstall
```bash
helm uninstall imran-workspace gerard-workspace base-infrastructure -n coder
kubectl delete namespace coder  # WARNING: Deletes all data
```

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Test with `make test-all`
4. Submit a pull request

## 📄 License

MIT License - see [LICENSE](LICENSE) file for details.

---

⭐ **Star this repo** if you find it useful!