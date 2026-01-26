# New User Provisioning Guide

This guide explains how to provision a new user workspace with OAuth2 authentication in the remote development environment.

## 📋 Prerequisites

Before provisioning a new user, ensure you have:

- ✅ **Kubernetes cluster** with nginx ingress controller
- ✅ **cert-manager** installed for automatic HTTPS certificates
- ✅ **Base infrastructure** deployed (`make deploy-base`)
- ✅ **GitHub OAuth App** configured (see GitHub OAuth Setup section)

## 🆔 Required Information

To provision a new user, you need:

| Required Item | Description | Example |
|---------------|-------------|---------|
| **GitHub Username** | The user's GitHub username (case-sensitive) | `john_doe` |
| **Full Name** | User's display name for git configuration | `"John Doe"` |
| **Email** | User's email for git configuration | `john.doe@company.com` |
| **Subdomain** | Unique subdomain for the user | `john` |
| **Domain** | Your base domain | `dev.company.com` |
| **PVC Size** | Storage size for user workspace | `50Gi` |

## 🔧 Step-by-Step Provisioning

### 1. Create User Directory Structure

```bash
# Create directories for the new user
mkdir -p deployments/john
mkdir -p secrets/john
```

### 2. Create User Values File

Create `deployments/john/values.yaml`:

```yaml
namespace: coder

user:
  name: john
  pvcSize: 50Gi  # Adjust based on user needs
  host: john.dev.company.com
  env:
    - name: GIT_USER_NAME
      value: "John Doe"
    - name: GIT_USER_EMAIL
      value: "john.doe@company.com"

image:
  repository: registry.digitalocean.com/resourceloop/coder
  tag: devlaptop-v1.6.2-browser-stealth
  pullPolicy: Always
  pullSecretName: regcred

ingress:
  className: nginx
  auth:
    type: basic  # Use 'oauth2' for GitHub OAuth authentication
    secretName: john-basic-auth
  tls:
    enabled: true
    secretName: john-dev-company-com-tls
    clusterIssuer: letsencrypt-production

# OAuth2 configuration (when auth.type is 'oauth2')
oauth2:
  # GitHub user authorization - comma-separated list of GitHub usernames
  githubUsers: "john_doe"  # Add authorized GitHub usernames
  # Optional: Restrict to specific GitHub org/team instead of individual users
  # githubOrg: "your-github-org"
  # githubTeam: "your-github-org:your-team"
  
  # These will be overridden by secrets file (gitignored)
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

build:
  mode: buildkit
  kanikoImage: gcr.io/kaniko-project/executor:latest
  pushSecretName: regcred
  defaultDestinationRepo: registry.digitalocean.com/resourceloop/coder

ssh:
  enabled: false
  port: 22
```

### 3. Create OAuth2 Values File (Optional)

If using OAuth2 authentication, create `deployments/john/values-oauth2.yaml`:

```yaml
namespace: coder

user:
  name: john
  pvcSize: 50Gi
  host: john.dev.company.com
  env:
    - name: GIT_USER_NAME
      value: "John Doe"
    - name: GIT_USER_EMAIL
      value: "john.doe@company.com"

image:
  repository: registry.digitalocean.com/resourceloop/coder
  tag: devlaptop-v1.6.2-browser-stealth
  pullPolicy: Always
  pullSecretName: regcred

ingress:
  className: nginx
  auth:
    type: oauth2  # Enable OAuth2 authentication
    secretName: john-basic-auth  # Keep for fallback
  tls:
    enabled: true
    secretName: john-dev-company-com-tls
    clusterIssuer: letsencrypt-production

# OAuth2 configuration 
oauth2:
  # GitHub user authorization - comma-separated list of GitHub usernames
  githubUsers: "john_doe"  # Authorized GitHub usernames
  # Secrets are provided separately in secrets/john/oauth2.yaml
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

build:
  mode: buildkit
  kanikoImage: gcr.io/kaniko-project/executor:latest
  pushSecretName: regcred
  defaultDestinationRepo: registry.digitalocean.com/resourceloop/coder

ssh:
  enabled: false
  port: 22
```

### 4. Create Authentication Secrets

#### Option A: Basic Authentication
```bash
# Create basic auth credentials
htpasswd -c john-auth admin
kubectl create secret generic john-basic-auth --from-file=auth=john-auth -n coder

# Clean up temporary file
rm john-auth
```

#### Option B: OAuth2 Authentication (Recommended)
```bash
# Create OAuth2 secrets file (this file should be gitignored)
cat > secrets/john/oauth2.yaml << EOF
oauth2:
  cookieSecret: "$(openssl rand -base64 32)"
  clientId: "your_github_oauth_app_client_id"
  clientSecret: "your_github_oauth_app_client_secret"
EOF

# Make sure this file is not committed to git
echo "secrets/john/oauth2.yaml" >> .gitignore
```

### 5. Configure DNS

Set up DNS record for the new user:
```bash
# Example: Create A record pointing to your ingress IP
john.dev.company.com  →  YOUR_INGRESS_IP
```

### 6. Add Makefile Targets (Optional)

Add to your `Makefile`:

```makefile
deploy-john: ## Deploy john's workspace
	helm upgrade john-workspace ./charts/workspace \
		-f ./deployments/john/values.yaml \
		--namespace $(NAMESPACE) --install --wait

deploy-john-oauth2: ## Deploy john's workspace with OAuth2
	helm upgrade john-workspace ./charts/workspace \
		-f ./deployments/john/values-oauth2.yaml \
		-f ./secrets/john/oauth2.yaml \
		--namespace $(NAMESPACE) --install --wait

shell-john: ## Shell into john's pod
	kubectl exec -it -n $(NAMESPACE) deployment/ws-john -c ide -- /bin/bash

logs-john: ## View john's logs
	kubectl logs -f -n $(NAMESPACE) deployment/ws-john -c ide

rollback-john: ## Rollback john's deployment
	helm rollback john-workspace --namespace $(NAMESPACE)

test-john: ## Test john's workspace
	@kubectl exec -n $(NAMESPACE) deployment/ws-john -c ide -- node --version
	@kubectl exec -n $(NAMESPACE) deployment/ws-john -c ide -- yarn --version
	@kubectl exec -n $(NAMESPACE) deployment/ws-john -c ide -- gh --version | head -1
	@kubectl exec -n $(NAMESPACE) deployment/ws-john -c ide -- code-server --version | head -1
```

## 🚀 Deployment Commands

### Basic Authentication Deployment
```bash
# Deploy with basic authentication
make deploy-john

# Or manually:
helm upgrade john-workspace charts/workspace/ \
  -f deployments/john/values.yaml \
  --namespace coder --install
```

### OAuth2 Authentication Deployment
```bash
# Deploy with OAuth2 authentication
make deploy-john-oauth2

# Or manually:
helm upgrade john-workspace charts/workspace/ \
  -f deployments/john/values-oauth2.yaml \
  -f secrets/john/oauth2.yaml \
  --namespace coder --install
```

## 🔐 GitHub OAuth Setup

If using OAuth2 authentication, you need to configure GitHub OAuth App:

### 1. Create GitHub OAuth App
1. Go to GitHub Settings → Developer settings → OAuth Apps
2. Click "New OAuth App"
3. Fill in the details:
   - **Application name**: `john-workspace` (or descriptive name)
   - **Homepage URL**: `https://john.dev.company.com`
   - **Authorization callback URL**: `https://john.dev.company.com/oauth2/callback`
4. Note the **Client ID** and **Client Secret**

### 2. Update OAuth2 Secrets
Update `secrets/john/oauth2.yaml` with the GitHub OAuth credentials:
```yaml
oauth2:
  cookieSecret: "GENERATED_COOKIE_SECRET"
  clientId: "your_github_oauth_app_client_id"
  clientSecret: "your_github_oauth_app_client_secret"
```

## 🧪 Testing the Deployment

### 1. Check Pod Status
```bash
kubectl get pods -n coder -l app=ws-john
```

### 2. Check Ingress
```bash
kubectl get ingress -n coder | grep john
```

### 3. Check Certificates
```bash
kubectl get certificate -n coder | grep john
```

### 4. Access the Workspace

#### Basic Authentication:
- **Dashboard**: `https://john.dev.company.com/`
- **VS Code IDE**: `https://john.dev.company.com/vscode`
- **Terminal**: `https://john.dev.company.com/terminal`
- **Browser**: `https://john.dev.company.com/browser`

#### OAuth2 Authentication (Recommended):
- **Dashboard**: `https://john.dev.company.com/oauth/` — includes system metrics, GitHub configuration, and service links
- **VS Code IDE**: `https://john.dev.company.com/oauth/vscode/`
- **Terminal**: `https://john.dev.company.com/oauth/terminal`
- **Browser Controls**: Click Browser card from dashboard

## 🔧 User Management Commands

```bash
# Check user's deployment status
helm status john-workspace -n coder

# View user's logs
kubectl logs -f -n coder deployment/ws-john -c ide

# Shell into user's workspace
kubectl exec -it -n coder deployment/ws-john -c ide -- /bin/bash

# Restart user's workspace
kubectl delete pod -n coder -l app=ws-john

# Update user's configuration
helm upgrade john-workspace charts/workspace/ -f deployments/john/values.yaml --namespace coder

# Remove user's workspace
helm uninstall john-workspace -n coder
kubectl delete pvc ws-john-home -n coder  # WARNING: Deletes user data
```

## 📊 Resource Planning

### Default Resource Allocation
- **CPU Request**: 2 cores
- **Memory Request**: 3Gi
- **CPU Limit**: 3 cores  
- **Memory Limit**: 5Gi
- **Storage**: 50Gi (configurable)

### Scaling Considerations
- **Light users**: 1 CPU, 2Gi memory, 20Gi storage
- **Heavy users**: 4 CPU, 8Gi memory, 100Gi storage
- **Team leads**: 6 CPU, 12Gi memory, 200Gi storage

## 🔍 Troubleshooting

### Common Issues

1. **Pod stuck in Pending**
   ```bash
   kubectl describe pod -n coder -l app=ws-john
   # Check for resource constraints or PVC issues
   ```

2. **Certificate not ready**
   ```bash
   kubectl describe certificate john-dev-company-com-tls -n coder
   # Check DNS and cert-manager logs
   ```

3. **OAuth2 authentication failures**
   ```bash
   kubectl logs -n coder -l app=oauth2-proxy-john
   # Check GitHub OAuth app configuration
   ```

4. **Storage issues**
   ```bash
   kubectl get pvc -n coder | grep john
   kubectl describe pvc ws-john-home -n coder
   ```

### Health Checks

```bash
# Overall workspace health
kubectl get pods,pvc,ingress,certificate -n coder | grep john

# Service connectivity
kubectl exec -n coder deployment/ws-john -c ide -- curl -sI http://localhost:8080
kubectl exec -n coder deployment/ws-john -c ide -- curl -sI http://localhost:7681
kubectl exec -n coder deployment/ws-john -c ide -- curl -sI http://localhost:6080
```

## 🔄 User Lifecycle

### Onboarding Checklist
- [ ] Gather user requirements (GitHub username, email, resource needs)
- [ ] Create user values files
- [ ] Set up DNS records
- [ ] Configure GitHub OAuth (if using OAuth2)
- [ ] Deploy workspace
- [ ] Test all endpoints
- [ ] Provide user with access URLs and credentials

### Offboarding Checklist
- [ ] Backup user data if needed
- [ ] Remove workspace deployment: `helm uninstall john-workspace -n coder`
- [ ] Delete persistent data: `kubectl delete pvc ws-john-home -n coder`
- [ ] Remove DNS records
- [ ] Clean up OAuth2 secrets (if used)
- [ ] Archive user configuration files

## 📚 Additional Resources

- [Main README](./README.md) - Overview and general setup
- [Helm Documentation](https://helm.sh/docs/)
- [Kubernetes Documentation](https://kubernetes.io/docs/)
- [OAuth2 Proxy Documentation](https://oauth2-proxy.github.io/oauth2-proxy/)
- [cert-manager Documentation](https://cert-manager.io/docs/)

---

For questions or issues with user provisioning, please refer to the troubleshooting section or open an issue in the repository.