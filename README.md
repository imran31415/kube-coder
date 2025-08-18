# Remote Dev Helm Chart

A production-ready Helm chart for deploying multi-user remote development workspaces with VS Code (code-server), Claude Code CLI, and containerized builds via Kaniko.

## Features

🚀 **Full-Featured IDE**: VS Code running in the browser with extensions support  
🔒 **Secure Access**: HTTPS with automatic Let's Encrypt certificates and basic authentication  
👥 **Multi-User**: Isolated workspaces for multiple developers  
💾 **Persistent Storage**: Each user gets dedicated persistent storage  
🐳 **Container Builds**: In-cluster Docker builds using Kaniko (no Docker daemon required)  
🤖 **AI Assistant**: Pre-installed Claude Code CLI for AI-powered development assistance  
🛠️ **Pre-configured Tools**: Node.js, Python, Go, Java, Git, and more out of the box  
⚡ **Scalable**: Resource limits and requests configurable per workspace  

## Quick Start

### Prerequisites

- Kubernetes cluster (1.19+)
- Helm 3.0+
- Ingress controller (nginx recommended)
- cert-manager (for automatic HTTPS certificates)
- Docker registry access

### 1. Clone the Repository

```bash
git clone https://github.com/your-org/remote-dev-helm.git
cd remote-dev-helm
```

### 2. Build and Push the Base Image

```bash
cd devlaptop
docker build --platform linux/amd64,linux/arm64 -t YOUR_REGISTRY/coder:devlaptop-v0.1.0 .
docker push YOUR_REGISTRY/coder:devlaptop-v0.1.0
```

### 3. Create Kubernetes Namespace

```bash
kubectl create namespace coder
```

### 4. Create Required Secrets

#### Docker Registry Secret
```bash
kubectl -n coder create secret docker-registry regcred \\
  --docker-server=YOUR_REGISTRY \\
  --docker-username=YOUR_USERNAME \\
  --docker-password=YOUR_PASSWORD \\
  --docker-email=your-email@example.com
```

#### Basic Auth Secret
```bash
htpasswd -nb admin YOUR_PASSWORD | kubectl -n coder create secret generic api-basic-auth --from-file=auth=/dev/stdin
```

### 5. Configure DNS

Add DNS records pointing to your ingress controller:
- `*.dev.yourdomain.com` → Your Ingress IP (wildcard recommended)
- OR individual records: `alice.dev.yourdomain.com` → Your Ingress IP

### 6. Customize Configuration

Edit `remote-dev/values.yaml`:

```yaml
# Update these values for your environment
domain: dev.yourdomain.com
image:
  repository: YOUR_REGISTRY/coder
  tag: devlaptop-v0.1.0
  pullSecretName: regcred

ingress:
  tls:
    enabled: true
    clusterIssuer: letsencrypt-production

users:
  - name: alice
    pvcSize: 50Gi
    host: alice.dev.yourdomain.com
    env:
      - name: GIT_USER_NAME
        value: "Alice Developer"
      - name: GIT_USER_EMAIL
        value: "alice@yourdomain.com"
```

### 7. Deploy the Chart

```bash
cd remote-dev
helm install remote-dev . -n coder
```

### 8. Access Your Workspace

Navigate to `https://alice.dev.yourdomain.com` and log in with your basic auth credentials.

## Configuration

### Core Configuration

| Parameter | Description | Default |
|-----------|-------------|---------|
| `namespace` | Kubernetes namespace | `coder` |
| `domain` | Base domain for workspaces | `dev.example.com` |
| `image.repository` | Container image repository | `registry.digitalocean.com/resourceloop/coder` |
| `image.tag` | Container image tag | `devlaptop-v0.1.0` |
| `image.pullSecretName` | Docker registry secret name | `regcred` |

### TLS/HTTPS Configuration

| Parameter | Description | Default |
|-----------|-------------|---------|
| `ingress.tls.enabled` | Enable HTTPS | `true` |
| `ingress.tls.secretName` | TLS secret name | `dev-tls-secret` |
| `ingress.tls.clusterIssuer` | cert-manager cluster issuer | `letsencrypt-production` |

### Authentication

| Parameter | Description | Default |
|-----------|-------------|---------|
| `ingress.auth.type` | Auth type (`basic` or `none`) | `basic` |
| `ingress.auth.secretName` | Basic auth secret name | `api-basic-auth` |

### User Workspaces

Each user workspace is defined in the `users` array:

```yaml
users:
  - name: username           # Workspace identifier
    pvcSize: 50Gi           # Persistent storage size
    host: user.domain.com   # Full hostname
    env:                    # Environment variables
      - name: GIT_USER_NAME
        value: "Full Name"
```

### Resource Configuration

| Parameter | Description | Default |
|-----------|-------------|---------|
| `resources.requests.cpu` | CPU request per workspace | `500m` |
| `resources.requests.memory` | Memory request per workspace | `1Gi` |
| `resources.limits.cpu` | CPU limit per workspace | `4` |
| `resources.limits.memory` | Memory limit per workspace | `8Gi` |

### Advanced Configuration

| Parameter | Description | Default |
|-----------|-------------|---------|
| `advanced.storageClass` | Storage class for PVCs | `""` (cluster default) |
| `advanced.nodeSelector` | Node selector for pods | `{}` |
| `advanced.tolerations` | Pod tolerations | `[]` |
| `advanced.affinity` | Pod affinity rules | `{}` |

## Examples

### Minimal Configuration

```yaml
domain: dev.mycompany.com
image:
  repository: myregistry.com/coder
  pullSecretName: my-registry-secret

users:
  - name: john
    pvcSize: 30Gi
    host: john.dev.mycompany.com
```

### Production Configuration with Multiple Users

```yaml
domain: dev.mycompany.com
image:
  repository: myregistry.com/coder
  tag: v1.0.0
  pullSecretName: registry-credentials

ingress:
  tls:
    enabled: true
    clusterIssuer: letsencrypt-prod

resources:
  requests:
    cpu: "1"
    memory: "2Gi"
  limits:
    cpu: "8"
    memory: "16Gi"

users:
  - name: alice
    pvcSize: 100Gi
    host: alice.dev.mycompany.com
    env:
      - name: GIT_USER_NAME
        value: "Alice Smith"
      - name: GIT_USER_EMAIL
        value: "alice@mycompany.com"
      - name: NODE_ENV
        value: "development"
        
  - name: bob
    pvcSize: 50Gi
    host: bob.dev.mycompany.com
    env:
      - name: GIT_USER_NAME
        value: "Bob Johnson"
      - name: GIT_USER_EMAIL
        value: "bob@mycompany.com"

advanced:
  storageClass: "fast-ssd"
  nodeSelector:
    workload-type: "development"
```

### Disable TLS (Development)

```yaml
domain: dev.local
ingress:
  tls:
    enabled: false
  auth:
    type: none  # No authentication

users:
  - name: dev
    pvcSize: 20Gi
    host: dev.local
```

## Pre-installed Tools

Each workspace comes with:

- **Languages**: Node.js, Python 3, Go, Java 17
- **Build Tools**: npm, pip, make, gcc, pkg-config
- **Version Control**: Git
- **Editors**: vim, nano
- **Utilities**: curl, wget, jq, unzip, tmux
- **AI Assistant**: Claude Code CLI

## Container Builds

Workspaces include a `docker-build` wrapper that uses Kaniko for secure, in-cluster container builds:

```bash
# Build and push to registry
docker-build -t myregistry.com/myapp:latest .
```

## Troubleshooting

### Pod Not Starting

```bash
kubectl -n coder get pods
kubectl -n coder describe pod ws-username-xxxxx
kubectl -n coder logs ws-username-xxxxx
```

### Certificate Issues

```bash
kubectl -n coder get certificate
kubectl -n coder describe certificate dev-tls-secret
kubectl -n coder get challenge
```

### Ingress Not Working

```bash
kubectl -n coder get ingress
kubectl -n coder describe ingress ws-username
nslookup username.dev.yourdomain.com
```

### Storage Issues

```bash
kubectl -n coder get pvc
kubectl -n coder describe pvc ws-username-home
```

## Adding New Users

1. Add user to `values.yaml`:
```yaml
users:
  - name: newuser
    pvcSize: 50Gi
    host: newuser.dev.yourdomain.com
    env: []
```

2. Update deployment:
```bash
helm upgrade remote-dev . -n coder
```

3. Configure DNS for new subdomain

## Removing Users

1. Remove user from `values.yaml`
2. Update deployment: `helm upgrade remote-dev . -n coder`
3. **Warning**: PVCs are not automatically deleted to prevent data loss

To manually remove user data:
```bash
kubectl -n coder delete pvc ws-username-home
```

## Security Considerations

- Basic authentication protects workspace access
- Each user has an isolated workspace with separate storage
- RBAC configured for workspace isolation
- Non-root containers with security contexts
- TLS encryption for all traffic
- Private registry authentication required

## Monitoring

Monitor workspace usage:

```bash
# Resource usage
kubectl -n coder top pods

# Storage usage
kubectl -n coder get pvc

# Active users
kubectl -n coder get pods -l app!=kaniko-wrapper
```

## Upgrading

To upgrade the chart:

```bash
helm upgrade remote-dev . -n coder
```

For major version upgrades, review the changelog and backup user data.

## Uninstalling

```bash
helm uninstall remote-dev -n coder

# WARNING: This deletes all user data
kubectl delete namespace coder
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Support

- 📧 Email: support@yourcompany.com
- 💬 Slack: #dev-tools
- 🐛 Issues: [GitHub Issues](https://github.com/your-org/remote-dev-helm/issues)
- 📖 Docs: [Documentation](https://docs.yourcompany.com/remote-dev)