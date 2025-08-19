# Makefile for remote-dev-helm (New Architecture)
.PHONY: build push deploy-base deploy-imran deploy-gerard deploy-all clean help status logs shell version

# Variables
REGISTRY := registry.digitalocean.com/resourceloop/coder
IMAGE_NAME := devlaptop
VERSION := v1.5.0
PLATFORM := linux/amd64
NAMESPACE := coder

# Docker image full name
IMAGE := $(REGISTRY):$(IMAGE_NAME)-$(VERSION)

help: ## Show this help message
	@echo 'Usage: make [target]'
	@echo ''
	@echo 'Targets:'
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-20s %s\\n", $$1, $$2}' $(MAKEFILE_LIST)

build: ## Build Docker image for amd64 architecture
	@echo "Building $(IMAGE) for $(PLATFORM)..."
	docker buildx build \
		--platform $(PLATFORM) \
		-t $(IMAGE) \
		-f devlaptop/Dockerfile \
		.

push: build ## Build and push Docker image
	@echo "Pushing $(IMAGE)..."
	docker buildx build \
		--platform $(PLATFORM) \
		-t $(IMAGE) \
		-f devlaptop/Dockerfile \
		--push \
		.

deploy-base: ## Deploy base infrastructure
	@echo "Deploying base infrastructure..."
	helm upgrade base-infrastructure ./charts/base-infrastructure \
		--namespace $(NAMESPACE) \
		--install \
		--wait

deploy-imran: ## Deploy Imran's workspace
	@echo "Deploying Imran's workspace..."
	helm upgrade imran-workspace ./charts/workspace \
		-f ./deployments/imran/values.yaml \
		--namespace $(NAMESPACE) \
		--install \
		--wait

deploy-gerard: ## Deploy Gerard's workspace
	@echo "Deploying Gerard's workspace..."
	helm upgrade gerard-workspace ./charts/workspace \
		-f ./deployments/gerard/values.yaml \
		--namespace $(NAMESPACE) \
		--install \
		--wait

deploy-all: deploy-base deploy-imran deploy-gerard ## Deploy all components

rollback-imran: ## Rollback Imran's workspace
	@echo "Rolling back Imran's workspace..."
	helm rollback imran-workspace --namespace $(NAMESPACE)

rollback-gerard: ## Rollback Gerard's workspace  
	@echo "Rolling back Gerard's workspace..."
	helm rollback gerard-workspace --namespace $(NAMESPACE)

clean: ## Clean up local Docker images
	@echo "Cleaning up local images..."
	docker rmi $(IMAGE) || true

status: ## Check deployment status
	@echo "Checking deployment status..."
	@echo "=== Base Infrastructure ==="
	helm status base-infrastructure -n $(NAMESPACE) || echo "Not deployed"
	@echo ""
	@echo "=== Workspaces ==="
	helm list -n $(NAMESPACE)
	@echo ""
	kubectl get pods -n $(NAMESPACE)
	@echo ""
	kubectl get ingress -n $(NAMESPACE)

logs-imran: ## Show logs from Imran's workspace pod
	@echo "Showing logs from Imran's workspace..."
	kubectl logs -f -n $(NAMESPACE) deployment/ws-imran

logs-gerard: ## Show logs from Gerard's workspace pod
	@echo "Showing logs from Gerard's workspace..."
	kubectl logs -f -n $(NAMESPACE) deployment/ws-gerard

shell-imran: ## Get shell access to Imran's workspace pod
	@echo "Getting shell access to Imran's workspace..."
	kubectl exec -it -n $(NAMESPACE) deployment/ws-imran -- /bin/bash

shell-gerard: ## Get shell access to Gerard's workspace pod
	@echo "Getting shell access to Gerard's workspace..."
	kubectl exec -it -n $(NAMESPACE) deployment/ws-gerard -- /bin/bash

version: ## Show current versions
	@echo "Current configuration:"
	@echo "  Registry: $(REGISTRY)"
	@echo "  Image: $(IMAGE_NAME)"
	@echo "  Version: $(VERSION)"
	@echo "  Platform: $(PLATFORM)"
	@echo "  Namespace: $(NAMESPACE)"

test-imran: ## Test Imran's workspace (Node version and yarn)
	@echo "Testing Imran's workspace..."
	@kubectl exec -n $(NAMESPACE) deployment/ws-imran -- node --version
	@kubectl exec -n $(NAMESPACE) deployment/ws-imran -- yarn --version
	@kubectl exec -n $(NAMESPACE) deployment/ws-imran -- pwd

test-gerard: ## Test Gerard's workspace (Node version and yarn)  
	@echo "Testing Gerard's workspace..."
	@kubectl exec -n $(NAMESPACE) deployment/ws-gerard -- node --version
	@kubectl exec -n $(NAMESPACE) deployment/ws-gerard -- yarn --version
	@kubectl exec -n $(NAMESPACE) deployment/ws-gerard -- pwd

test-all: test-imran test-gerard ## Test both workspaces