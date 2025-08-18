# Remote Dev Helm Chart Build & Deployment Plan

## Project Overview
Building and deploying a Helm chart that creates isolated dev environments accessible via dev.scalebase.io subdomains.

## Current Status
- ✅ Helm chart structure created with templates for deployment, service, ingress, PVC, RBAC
- ✅ Dockerfile fixed for Claude Code CLI installation (multi-arch support)
- ✅ Kubernetes namespace 'coder' exists
- ✅ Values.yaml configured for dev.scalebase.io domain

## Next Steps (In Order)

### 1. Build and Push Docker Image
```bash
cd devlaptop
docker build --platform linux/amd64,linux/arm64 -t registry.digitalocean.com/resourceloop/coder:devlaptop-v0.1.0 .
docker push registry.digitalocean.com/resourceloop/coder:devlaptop-v0.1.0
```

### 2. Create Required Secrets
```bash
# Basic auth secret for ingress protection
htpasswd -nb admin YOUR_PASSWORD
kubectl -n coder create secret generic api-basic-auth --from-literal=auth='admin:$apr1$...'

# Docker registry credentials
kubectl -n coder create secret docker-registry regcred \
  --docker-server=registry.digitalocean.com \
  --docker-username=YOUR_DO_TOKEN \
  --docker-password=YOUR_DO_TOKEN \
  --docker-email=your-email@example.com
```

### 3. Deploy/Update Helm Chart
```bash
cd remote-dev
helm upgrade --install remote-dev . -n coder
```

### 4. Verify DNS Configuration
Ensure wildcard DNS or specific A records:
- `*.dev.scalebase.io` → Ingress Controller IP
- OR `imran.dev.scalebase.io` → Ingress Controller IP

### 5. Test Deployment
```bash
# Check resources
kubectl -n coder get pods,services,ingress,pvc

# Check logs
kubectl -n coder logs -l app=remote-dev

# Test access
curl -u admin:password http://imran.dev.scalebase.io
```

## Configuration Details

### Current Values (values.yaml)
- **Namespace**: coder
- **Domain**: dev.scalebase.io
- **Image**: registry.digitalocean.com/resourceloop/coder:devlaptop-v0.1.0
- **Users**: imran (50Gi PVC)
- **Resources**: 500m CPU / 1Gi RAM (requests), 4 CPU / 8Gi RAM (limits)
- **Ingress**: nginx with basic auth
- **Build**: Kaniko for in-cluster Docker builds

### Key Features
- Isolated workspaces per user with persistent storage
- Code-server IDE accessible via web browser
- In-cluster Docker builds using Kaniko
- Basic auth protection on ingress
- Claude Code CLI pre-installed in containers

## Troubleshooting Commands

### Pod Issues
```bash
kubectl -n coder describe pod ws-imran-xxxxx
kubectl -n coder logs ws-imran-xxxxx
```

### Ingress Issues
```bash
nslookup imran.dev.scalebase.io
kubectl -n ingress-nginx get pods
kubectl -n coder describe ingress ws-imran
```

### Build Issues
```bash
kubectl -n coder get jobs
kubectl -n coder logs job/kaniko-imran-xxxxx
```

## Adding New Users
1. Edit `remote-dev/values.yaml` and add user to `users` list
2. Run: `helm upgrade remote-dev . -n coder`
3. Configure DNS for new subdomain

## Rollback Plan
```bash
helm rollback remote-dev -n coder
# or
helm uninstall remote-dev -n coder
```

## Security Considerations
- Basic auth protects IDE access
- RBAC configured for workspace isolation
- Docker registry credentials stored as K8s secrets
- Non-root user in containers

---
**Last Updated**: 2025-08-18
**Status**: Ready for Docker build and deployment