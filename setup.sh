#!/bin/bash

# Remote Dev Helm Chart Setup Script
# This script helps you set up the remote development environment

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default values
NAMESPACE="coder"
REGISTRY=""
USERNAME=""
PASSWORD=""
EMAIL=""
DOMAIN=""
AUTH_PASSWORD=""

print_banner() {
    echo -e "${BLUE}"
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║                 Remote Dev Helm Chart Setup                 ║"
    echo "║                                                              ║"
    echo "║    Multi-user VS Code workspaces with Claude Code CLI       ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

check_prerequisites() {
    log_info "Checking prerequisites..."
    
    # Check kubectl
    if ! command -v kubectl &> /dev/null; then
        log_error "kubectl not found. Please install kubectl."
        exit 1
    fi
    
    # Check helm
    if ! command -v helm &> /dev/null; then
        log_error "helm not found. Please install Helm 3."
        exit 1
    fi
    
    # Check docker
    if ! command -v docker &> /dev/null; then
        log_error "docker not found. Please install Docker."
        exit 1
    fi
    
    # Check cluster connectivity
    if ! kubectl cluster-info &> /dev/null; then
        log_error "Cannot connect to Kubernetes cluster. Please check your kubeconfig."
        exit 1
    fi
    
    log_success "All prerequisites met!"
}

gather_config() {
    log_info "Gathering configuration..."
    
    echo
    read -p "Docker registry URL (e.g., registry.digitalocean.com/myteam): " REGISTRY
    read -p "Registry username: " USERNAME
    read -s -p "Registry password: " PASSWORD
    echo
    read -p "Registry email: " EMAIL
    read -p "Domain for workspaces (e.g., dev.mycompany.com): " DOMAIN
    read -s -p "Basic auth password for workspace access: " AUTH_PASSWORD
    echo
    
    if [[ -z "$REGISTRY" || -z "$USERNAME" || -z "$PASSWORD" || -z "$DOMAIN" || -z "$AUTH_PASSWORD" ]]; then
        log_error "All fields are required!"
        exit 1
    fi
}

create_namespace() {
    log_info "Creating namespace: $NAMESPACE"
    kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
    log_success "Namespace created/updated"
}

create_secrets() {
    log_info "Creating registry secret..."
    kubectl -n "$NAMESPACE" create secret docker-registry regcred \\
        --docker-server="$REGISTRY" \\
        --docker-username="$USERNAME" \\
        --docker-password="$PASSWORD" \\
        --docker-email="$EMAIL" \\
        --dry-run=client -o yaml | kubectl apply -f -
    
    log_info "Creating basic auth secret..."
    htpasswd -bc /tmp/auth admin "$AUTH_PASSWORD" || {
        log_error "htpasswd command failed. Please install apache2-utils."
        exit 1
    }
    kubectl -n "$NAMESPACE" create secret generic api-basic-auth --from-file=auth=/tmp/auth --dry-run=client -o yaml | kubectl apply -f -
    rm -f /tmp/auth
    
    log_success "Secrets created"
}

build_image() {
    log_info "Building and pushing base image..."
    
    if [[ ! -d "devlaptop" ]]; then
        log_error "devlaptop directory not found. Please run this script from the repository root."
        exit 1
    fi
    
    IMAGE_NAME="$REGISTRY/coder:devlaptop-v0.1.0"
    
    log_info "Building image: $IMAGE_NAME"
    docker build -t "$IMAGE_NAME" devlaptop/
    
    log_info "Pushing image..."
    docker push "$IMAGE_NAME"
    
    log_success "Image built and pushed: $IMAGE_NAME"
}

create_values_file() {
    log_info "Creating values.yaml..."
    
    cat > custom-values.yaml << EOF
namespace: $NAMESPACE

image:
  repository: $REGISTRY/coder
  tag: devlaptop-v0.1.0
  pullSecretName: regcred

ingress:
  tls:
    enabled: true
    clusterIssuer: letsencrypt-production
  auth:
    type: basic
    secretName: api-basic-auth

domain: $DOMAIN

build:
  pushSecretName: regcred
  defaultDestinationRepo: $REGISTRY/builds

users:
  - name: developer
    pvcSize: 50Gi
    host: developer.$DOMAIN
    env:
      - name: GIT_USER_NAME
        value: "Developer"
      - name: GIT_USER_EMAIL
        value: "developer@$DOMAIN"
EOF
    
    log_success "Values file created: custom-values.yaml"
}

deploy_chart() {
    log_info "Deploying Helm chart..."
    
    if [[ ! -d "remote-dev" ]]; then
        log_error "remote-dev directory not found. Please run this script from the repository root."
        exit 1
    fi
    
    helm upgrade --install remote-dev ./remote-dev -n "$NAMESPACE" -f custom-values.yaml
    
    log_success "Chart deployed!"
}

show_dns_instructions() {
    log_warning "DNS Configuration Required!"
    echo
    echo "Please add the following DNS record to your domain:"
    echo
    echo "Type: A"
    echo "Name: *.$DOMAIN (wildcard)"
    echo "Value: [Your Ingress Controller IP]"
    echo
    echo "To find your ingress IP, run:"
    echo "kubectl get svc -n ingress-nginx"
    echo
    echo "Once DNS is configured, access your workspace at:"
    echo "https://developer.$DOMAIN"
    echo "Username: admin"
    echo "Password: [the password you entered]"
}

wait_for_deployment() {
    log_info "Waiting for deployment to be ready..."
    kubectl -n "$NAMESPACE" wait --for=condition=available --timeout=300s deployment/ws-developer || {
        log_warning "Deployment didn't become ready within 5 minutes. Check logs with:"
        echo "kubectl -n $NAMESPACE get pods"
        echo "kubectl -n $NAMESPACE logs -l app=ws-developer"
    }
}

main() {
    print_banner
    check_prerequisites
    gather_config
    create_namespace
    create_secrets
    build_image
    create_values_file
    deploy_chart
    wait_for_deployment
    show_dns_instructions
    
    echo
    log_success "Setup complete! Your remote development environment is ready."
    echo
    echo "Next steps:"
    echo "1. Configure DNS as shown above"
    echo "2. Access your workspace at https://developer.$DOMAIN"
    echo "3. Add more users by editing custom-values.yaml and running:"
    echo "   helm upgrade remote-dev ./remote-dev -n $NAMESPACE -f custom-values.yaml"
}

# Handle interruption
trap 'log_error "Setup interrupted by user"; exit 1' INT

main "$@"