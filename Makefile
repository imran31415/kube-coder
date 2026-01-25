# Makefile for kube-coder
.PHONY: build push deploy-base deploy-imran deploy-gerard deploy-all clean help status logs-imran logs-gerard shell-imran shell-gerard version test-imran test-gerard test-all rollback-imran rollback-gerard

# Variables
REGISTRY := registry.digitalocean.com/resourceloop/coder
IMAGE_NAME := devlaptop
VERSION := v1.6.0
PLATFORM := linux/amd64
NAMESPACE := coder

# Docker image full name
IMAGE := $(REGISTRY):$(IMAGE_NAME)-$(VERSION)

# Default target
.DEFAULT_GOAL := help

help: ## Show this help message
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# =============================================================================
# Docker Image
# =============================================================================

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

clean: ## Clean up local Docker images
	@echo "Cleaning up local images..."
	docker rmi $(IMAGE) || true

# =============================================================================
# Deployment
# =============================================================================

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

# =============================================================================
# Monitoring
# =============================================================================

status: ## Check deployment status
	@echo "=== Helm Releases ==="
	@helm list -n $(NAMESPACE)
	@echo ""
	@echo "=== Pods ==="
	@kubectl get pods -n $(NAMESPACE)
	@echo ""
	@echo "=== Ingresses ==="
	@kubectl get ingress -n $(NAMESPACE) --no-headers | awk '{print $$1, $$3}'

version: ## Show current versions and config
	@echo "Current configuration:"
	@echo "  Registry:  $(REGISTRY)"
	@echo "  Image:     $(IMAGE_NAME)"
	@echo "  Version:   $(VERSION)"
	@echo "  Platform:  $(PLATFORM)"
	@echo "  Namespace: $(NAMESPACE)"
	@echo "  Full tag:  $(IMAGE)"

logs-imran: ## Show logs from Imran's workspace
	kubectl logs -f -n $(NAMESPACE) deployment/ws-imran -c ide

logs-gerard: ## Show logs from Gerard's workspace
	kubectl logs -f -n $(NAMESPACE) deployment/ws-gerard -c ide

test-imran: ## Test Imran's workspace
	@echo "Testing Imran's workspace..."
	@kubectl exec -n $(NAMESPACE) deployment/ws-imran -c ide -- node --version
	@kubectl exec -n $(NAMESPACE) deployment/ws-imran -c ide -- yarn --version
	@kubectl exec -n $(NAMESPACE) deployment/ws-imran -c ide -- gh --version | head -1
	@kubectl exec -n $(NAMESPACE) deployment/ws-imran -c ide -- code-server --version | head -1

test-gerard: ## Test Gerard's workspace
	@echo "Testing Gerard's workspace..."
	@kubectl exec -n $(NAMESPACE) deployment/ws-gerard -c ide -- node --version
	@kubectl exec -n $(NAMESPACE) deployment/ws-gerard -c ide -- yarn --version
	@kubectl exec -n $(NAMESPACE) deployment/ws-gerard -c ide -- gh --version | head -1
	@kubectl exec -n $(NAMESPACE) deployment/ws-gerard -c ide -- code-server --version | head -1

test-all: test-imran test-gerard ## Test all workspaces

# =============================================================================
# Shell Access
# =============================================================================

shell-imran: ## Shell into Imran's workspace
	kubectl exec -it -n $(NAMESPACE) deployment/ws-imran -c ide -- /bin/bash

shell-gerard: ## Shell into Gerard's workspace
	kubectl exec -it -n $(NAMESPACE) deployment/ws-gerard -c ide -- /bin/bash
