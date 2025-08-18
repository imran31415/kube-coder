# Deployment Guide for Remote Dev Helm Chart

## Prerequisites

Before deploying, ensure you have:
- Kubernetes cluster with NGINX Ingress Controller
- kubectl configured to access your cluster
- Helm 3 installed
- Docker registry (e.g., DigitalOcean Container Registry)
- DNS control for your domain

## Step-by-Step Deployment

### 1. Build and Push the Base Image

```bash
cd devlaptop
docker build -t registry.digitalocean.com/resourceloop/coder:devlaptop-v0.1.0 .
docker push registry.digitalocean.com/resourceloop/coder:devlaptop-v0.1.0
```

### 2. Create Kubernetes Namespace

```bash
kubectl create namespace coder
```

### 3. Create Basic Auth Secret

Generate password and create secret for ingress authentication:

```bash
# Generate password hash
htpasswd -nb admin YOUR_PASSWORD

# Create secret (replace the hash with your output)
kubectl -n coder create secret generic api-basic-auth \
  --from-literal=auth='admin:$apr1$...'
```

### 4. Create Docker Registry Secret

```bash
kubectl -n coder create secret docker-registry regcred \
  --docker-server=registry.digitalocean.com \
  --docker-username=YOUR_DO_TOKEN \
  --docker-password=YOUR_DO_TOKEN \
  --docker-email=your-email@example.com
```

### 5. Configure DNS

Add wildcard or individual A records pointing to your ingress controller:
- `*.dev.scalebase.io` → Your Ingress IP
- OR individual records: `imran.dev.scalebase.io` → Your Ingress IP

### 6. Update values.yaml

Customize the following in `remote-dev/values.yaml`:
- `domain`: Your base domain
- `image.repository`: Your registry path
- `users`: List of developers needing access
- `ingress.tls`: Configure if you have TLS certificates

### 7. Deploy the Helm Chart

```bash
cd remote-dev
helm install remote-dev . -n coder

# Or upgrade if already installed
helm upgrade remote-dev . -n coder
```

### 8. Verify Deployment

```bash
# Check pods are running
kubectl -n coder get pods

# Check ingresses are created
kubectl -n coder get ingress

# Check PVCs are bound
kubectl -n coder get pvc
```

## Access Your Workspace

Navigate to: `http://imran.dev.scalebase.io` (or your configured domain)
Enter basic auth credentials when prompted.

## Troubleshooting

### Pod Not Starting
```bash
kubectl -n coder describe pod ws-imran-xxxxx
kubectl -n coder logs ws-imran-xxxxx
```

### Ingress Not Working
- Verify DNS resolution: `nslookup imran.dev.scalebase.io`
- Check ingress controller: `kubectl -n ingress-nginx get pods`
- Verify ingress: `kubectl -n coder describe ingress ws-imran`

### Build Issues
Check Kaniko job logs:
```bash
kubectl -n coder get jobs
kubectl -n coder logs job/kaniko-imran-xxxxx
```

## Adding New Users

1. Edit `values.yaml` and add user to the `users` list
2. Run: `helm upgrade remote-dev . -n coder`
3. Configure DNS for the new subdomain

## Uninstall

```bash
helm uninstall remote-dev -n coder
kubectl delete namespace coder  # Warning: Deletes all PVCs/data
```