# Makefile for remote-dev-helm
.PHONY: build push deploy clean help

# Variables
REGISTRY := registry.digitalocean.com/resourceloop/coder
IMAGE_NAME := devlaptop
VERSION := v1.2.0
PLATFORM := linux/amd64
NAMESPACE := coder
RELEASE_NAME := imran-workspace

# Docker image full name
IMAGE := $(REGISTRY):$(IMAGE_NAME)-$(VERSION)

help: ## Show this help message
	@echo 'Usage: make [target]'
	@echo ''
	@echo 'Targets:'
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-15s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

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

deploy: ## Deploy to Kubernetes using Helm
	@echo "Deploying $(RELEASE_NAME) with image $(IMAGE)..."
	@sed -i.bak 's|tag: devlaptop-.*|tag: $(IMAGE_NAME)-$(VERSION)|g' imran-values.yaml
	helm upgrade $(RELEASE_NAME) ./remote-dev \
		-f imran-values.yaml \
		--namespace $(NAMESPACE) \
		--wait \
		--timeout=300s

rollback: ## Rollback to previous deployment
	@echo "Rolling back $(RELEASE_NAME)..."
	helm rollback $(RELEASE_NAME) --namespace $(NAMESPACE)

clean: ## Clean up local Docker images
	@echo "Cleaning up local images..."
	docker rmi $(IMAGE) || true

status: ## Check deployment status
	@echo "Checking deployment status..."
	kubectl get pods -n $(NAMESPACE)
	@echo ""
	kubectl get deployment -n $(NAMESPACE)

logs: ## Show logs from the workspace pod
	@echo "Showing logs from workspace pod..."
	kubectl logs -f -n $(NAMESPACE) deployment/ws-imran

shell: ## Get shell access to the workspace pod
	@echo "Getting shell access to workspace pod..."
	kubectl exec -it -n $(NAMESPACE) deployment/ws-imran -- /bin/bash

# Force recreation of pods due to PVC single-attach issue
force-deploy: ## Force recreation of deployment (for PVC issues)
	@echo "Force recreating deployment..."
	kubectl patch deployment ws-imran -n $(NAMESPACE) -p '{"spec":{"strategy":{"type":"Recreate"}}}'
	kubectl delete pod -n $(NAMESPACE) -l app=ws-imran --force --grace-period=0
	$(MAKE) deploy

version: ## Show current versions
	@echo "Current configuration:"
	@echo "  Registry: $(REGISTRY)"
	@echo "  Image: $(IMAGE_NAME)"
	@echo "  Version: $(VERSION)"
	@echo "  Platform: $(PLATFORM)"
	@echo "  Namespace: $(NAMESPACE)"
	@echo "  Release: $(RELEASE_NAME)"