# Kube Coder

A production-ready Helm chart for deploying multi-user remote development workspaces with VS Code, Claude Code CLI, and secure container builds.

<!-- SCREENSHOT PLACEHOLDER: VS Code web interface -->
<img width="2720" height="1796" alt="image" src="https://github.com/user-attachments/assets/72ca8635-80c0-4ae2-b9ee-14ed918185eb" />


## ✨ Features

- 🚀 **VS Code in Browser** - Full IDE with extensions support
- 🤖 **Claude Code CLI** - AI-powered development assistant  
- 🔒 **Secure Access** - HTTPS with Let's Encrypt + authentication
- 👥 **Multi-User** - Isolated workspaces for teams
- 💾 **Persistent Storage** - Each user gets dedicated storage
- 🐳 **Container Builds** - Secure in-cluster builds with Kaniko
- ⚡ **Ready-to-Use** - Pre-configured development stack

## 🚀 Quick Start

### Prerequisites
- Kubernetes cluster (1.19+)
- Helm 3.0+
- nginx ingress controller
- cert-manager for automatic HTTPS

### 1. Install the Chart

```bash
# Clone the repository
git clone https://github.com/imran31415/kube-coder.git
cd kube-coder

# Create namespace
kubectl create namespace coder

# Create secrets (replace with your values)
kubectl create secret docker-registry regcred \
  --docker-server=your-registry.com \
  --docker-username=your-username \
  --docker-password=your-password \
  -n coder

# Create basic auth secret
htpasswd -c auth admin
kubectl create secret generic api-basic-auth --from-file=auth -n coder

# Install with custom values
helm install remote-dev ./remote-dev -f examples/values-single-user.yaml -n coder
```

### 2. Configure Your Domain

Update the values file with your domain:

```yaml
users:
  - name: alice
    host: alice.dev.yourdomain.com
    pvcSize: 50Gi
```

### 3. Access Your Workspace

Visit `https://alice.dev.yourdomain.com` and login with your credentials.

## 📋 Example Configurations

### Single User
```bash
helm install my-workspace ./remote-dev -f examples/values-single-user.yaml -n coder
```

### Team Setup
```bash
helm install team-workspace ./remote-dev -f examples/values-team.yaml -n coder
```

### Development (No TLS)
```bash
helm install dev-workspace ./remote-dev -f examples/values-no-tls.yaml -n coder
```


## 🛠️ Pre-installed Tools

Each workspace includes:

| Category | Tools |
|----------|-------|
| **Languages** | Node.js, Python 3, Go, Java 17 |
| **Build Tools** | npm, pip, make, gcc |
| **Version Control** | Git |
| **AI Assistant** | Claude Code CLI |
| **Utilities** | curl, wget, jq, tmux |


## 🐳 Container Builds

Use the built-in `docker-build` command for secure container builds:

```bash
# In your workspace terminal
docker-build -t myregistry.com/myapp:latest .
```


## 🔧 Configuration Reference

### Image Settings
```yaml
image:
  repository: your-registry/coder
  tag: latest
  pullSecretName: regcred
```

### User Configuration
```yaml
users:
  - name: username
    pvcSize: 50Gi
    host: username.dev.yourdomain.com
    env:
      - name: GIT_USER_NAME
        value: "Your Name"
```

### TLS/Security
```yaml
ingress:
  tls:
    enabled: true
    clusterIssuer: letsencrypt-production
  auth:
    type: basic
    secretName: api-basic-auth
```

## 📊 Managing Users

### Add a User
1. Add to `values.yaml`
2. Run: `helm upgrade remote-dev ./remote-dev -n coder`
3. Configure DNS for new subdomain

### Remove a User
1. Remove from `values.yaml`  
2. Run: `helm upgrade remote-dev ./remote-dev -n coder`
3. Manually delete PVC if needed: `kubectl delete pvc ws-username-home -n coder`


## 🔍 Troubleshooting

### Check Pod Status
```bash
kubectl get pods -n coder
kubectl describe pod ws-username-xxxxx -n coder
```

### Certificate Issues
```bash
kubectl get certificate -n coder
kubectl describe certificate your-tls-secret -n coder
```

### Storage Issues
```bash
kubectl get pvc -n coder
kubectl describe pvc ws-username-home -n coder
```

## 🛡️ Security Features

- ✅ TLS encryption for all traffic
- ✅ Basic authentication protection
- ✅ RBAC isolation between users
- ✅ Non-root containers
- ✅ Private registry authentication
- ✅ Isolated persistent storage


## 📈 Monitoring

```bash
# Resource usage
kubectl top pods -n coder

# Storage usage  
kubectl get pvc -n coder

# Active workspaces
kubectl get pods -l app!=kaniko-wrapper -n coder
```

## 🗑️ Uninstall

```bash
# Remove the deployment
helm uninstall remote-dev -n coder

# WARNING: This deletes all user data
kubectl delete namespace coder
```

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## 📄 License

MIT License - see [LICENSE](LICENSE) file for details.

## 🆘 Support

- 🐛 **Issues**: [GitHub Issues](https://github.com/imran31415/kube-coder/issues)
- 💬 **Discussions**: [GitHub Discussions](https://github.com/imran31415/kube-coder/discussions)
- 📧 **Email**: Support via GitHub issues preferred

---

⭐ **Star this repo** if you find it useful!
