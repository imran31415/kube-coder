# Remote Dev Helm

A production-ready Helm chart architecture for deploying multi-user remote development workspaces with VS Code, Node.js 20, Yarn, and secure container builds.

<img width="2720" height="1796" alt="image" src="https://github.com/user-attachments/assets/72ca8635-80c0-4ae2-b9ee-14ed918185eb" />

## ✨ Features

- 🚀 **VS Code in Browser** - Full IDE with extensions support
- 🔧 **Node.js 20 + Yarn** - Latest Node.js with Yarn package manager
- 🤖 **Claude Code CLI** - AI-powered development assistant  
- 🔒 **Secure Access** - HTTPS with Let's Encrypt + basic auth
- 👥 **Multi-User** - Independent workspaces per user
- 💾 **Persistent Storage** - Dedicated storage that survives redeploys
- 🐳 **Container Builds** - Docker-in-Docker with BuildKit support
- ⚡ **Independent Management** - Deploy/update users separately
- 🌐 **Terminal Access** - Browser-based terminal with ttyd

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

### 2. Deploy User Workspaces

```bash
# Create basic auth for users
htpasswd -c auth admin
kubectl create secret generic api-basic-auth --from-file=auth -n coder

# Deploy Imran's workspace
make deploy-imran

# Deploy Gerard's workspace (with separate auth)
htpasswd -c gerard-auth admin
kubectl create secret generic gerard-basic-auth --from-file=auth=gerard-auth -n coder
make deploy-gerard
```

### 3. Test Everything Works

```bash
# Test both workspaces
make test-all

# Check deployment status
make status
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

1. **Create user values file:**
```bash
mkdir deployments/newuser
cp deployments/imran/values.yaml deployments/newuser/values.yaml
```

2. **Update configuration:**
```yaml
# deployments/newuser/values.yaml
user:
  name: newuser
  host: newuser.dev.yourdomain.com
  env:
    - name: GIT_USER_NAME
      value: "New User"
    - name: GIT_USER_EMAIL
      value: "newuser@yourdomain.com"
```

3. **Add Makefile targets:**
```makefile
deploy-newuser: ## Deploy newuser's workspace
	helm upgrade newuser-workspace ./charts/workspace \
		-f ./deployments/newuser/values.yaml \
		--namespace $(NAMESPACE) --install --wait
```

4. **Deploy:**
```bash
make deploy-newuser
```

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
  tag: devlaptop-v1.5.0
  pullPolicy: Always

resources:
  requests:
    cpu: 200m
    memory: 512Mi  
  limits:
    cpu: "2"
    memory: 4Gi

ingress:
  auth:
    secretName: username-basic-auth  # User-specific auth
  tls:
    secretName: username-dev-yourdomain-com-tls
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

- ✅ **TLS encryption** for all traffic
- ✅ **Per-user authentication** with basic auth
- ✅ **RBAC isolation** between users  
- ✅ **Non-root containers** (uid/gid 1000)
- ✅ **Private registry** authentication
- ✅ **Isolated storage** per user
- ✅ **Recreate deployment** strategy prevents resource conflicts

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