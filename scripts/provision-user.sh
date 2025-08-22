#!/bin/bash

# User Provisioning Script
# Usage: ./scripts/provision-user.sh <username> <github_username> <full_name> <email> <domain>

set -e

# Check arguments
if [ $# -ne 5 ]; then
    echo "Usage: $0 <username> <github_username> <full_name> <email> <domain>"
    echo "Example: $0 john john_doe 'John Doe' john.doe@company.com dev.company.com"
    exit 1
fi

USERNAME="$1"
GITHUB_USERNAME="$2"
FULL_NAME="$3"
EMAIL="$4"
DOMAIN="$5"

echo "🚀 Provisioning user: $USERNAME"
echo "   GitHub: $GITHUB_USERNAME"
echo "   Email: $EMAIL"
echo "   Host: $USERNAME.$DOMAIN"

# Create directories
echo "📁 Creating directories..."
mkdir -p "deployments/$USERNAME"
mkdir -p "secrets/$USERNAME"

# Copy and customize values template
echo "📝 Creating user values file..."
cp templates/user-values-template.yaml "deployments/$USERNAME/values.yaml"

# Replace placeholders in values file
sed -i.bak "s/USERNAME_HERE/$USERNAME/g" "deployments/$USERNAME/values.yaml"
sed -i.bak "s/GITHUB_USERNAME_HERE/$GITHUB_USERNAME/g" "deployments/$USERNAME/values.yaml"
sed -i.bak "s/FULL_NAME_HERE/$FULL_NAME/g" "deployments/$USERNAME/values.yaml"
sed -i.bak "s/EMAIL_HERE/$EMAIL/g" "deployments/$USERNAME/values.yaml"
sed -i.bak "s/dev.company.com/$DOMAIN/g" "deployments/$USERNAME/values.yaml"

# Remove backup file
rm "deployments/$USERNAME/values.yaml.bak"

# Create OAuth2 values file
echo "📝 Creating OAuth2 values file..."
cp "deployments/$USERNAME/values.yaml" "deployments/$USERNAME/values-oauth2.yaml"
sed -i.bak 's/type: basic/type: oauth2/' "deployments/$USERNAME/values-oauth2.yaml"
rm "deployments/$USERNAME/values-oauth2.yaml.bak"

# Create OAuth2 secrets template
echo "🔐 Creating OAuth2 secrets template..."
cat > "secrets/$USERNAME/oauth2.yaml" << EOF
# OAuth2 secrets for $USERNAME
# IMPORTANT: Add this file to .gitignore
oauth2:
  cookieSecret: "$(openssl rand -base64 32)"
  clientId: "YOUR_GITHUB_OAUTH_APP_CLIENT_ID"
  clientSecret: "YOUR_GITHUB_OAUTH_APP_CLIENT_SECRET"
EOF

# Add to gitignore
echo "🔒 Adding secrets to .gitignore..."
echo "secrets/$USERNAME/oauth2.yaml" >> .gitignore

# Create basic auth secret
echo "🔐 Creating basic auth secret..."
read -s -p "Enter password for basic auth: " PASSWORD
echo
htpasswd -bc "/tmp/$USERNAME-auth" admin "$PASSWORD"
kubectl create secret generic "$USERNAME-basic-auth" --from-file=auth="/tmp/$USERNAME-auth" -n coder 2>/dev/null || echo "Secret already exists"
rm "/tmp/$USERNAME-auth"

# Add Makefile targets
echo "📋 Adding Makefile targets..."
cat >> Makefile << EOF

# $USERNAME workspace targets
deploy-$USERNAME: ## Deploy $USERNAME's workspace
	helm upgrade $USERNAME-workspace ./charts/workspace \\
		-f ./deployments/$USERNAME/values.yaml \\
		--namespace \$(NAMESPACE) --install --wait

deploy-$USERNAME-oauth2: ## Deploy $USERNAME's workspace with OAuth2
	helm upgrade $USERNAME-workspace ./charts/workspace \\
		-f ./deployments/$USERNAME/values-oauth2.yaml \\
		-f ./secrets/$USERNAME/oauth2.yaml \\
		--namespace \$(NAMESPACE) --install --wait

shell-$USERNAME: ## Shell into $USERNAME's pod
	kubectl exec -it -n \$(NAMESPACE) \$\$(kubectl get pods -n \$(NAMESPACE) -l app=ws-$USERNAME -o jsonpath='{.items[0].metadata.name}') -- bash

logs-$USERNAME: ## View $USERNAME's logs
	kubectl logs -n \$(NAMESPACE) -l app=ws-$USERNAME -f

status-$USERNAME: ## Check $USERNAME's deployment status
	helm status $USERNAME-workspace -n \$(NAMESPACE)
	kubectl get pods -n \$(NAMESPACE) -l app=ws-$USERNAME
EOF

echo "✅ User provisioning completed!"
echo ""
echo "📋 Next steps:"
echo "1. Set up DNS: $USERNAME.$DOMAIN → YOUR_INGRESS_IP"
echo "2. For OAuth2: Update secrets/$USERNAME/oauth2.yaml with GitHub OAuth credentials"
echo "3. Deploy workspace:"
echo "   Basic auth: make deploy-$USERNAME"
echo "   OAuth2:     make deploy-$USERNAME-oauth2"
echo ""
echo "📁 Files created:"
echo "   - deployments/$USERNAME/values.yaml"
echo "   - deployments/$USERNAME/values-oauth2.yaml"  
echo "   - secrets/$USERNAME/oauth2.yaml"
echo ""
echo "🔗 Access URLs (after deployment):"
echo "   Basic auth: https://$USERNAME.$DOMAIN/"
echo "   OAuth2:     https://$USERNAME.$DOMAIN/oauth"