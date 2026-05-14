# Makefile for kube-coder
.PHONY: build push deploy-base deploy-imran deploy-gerard deploy-all clean help status logs-imran logs-gerard shell-imran shell-gerard version test-imran test-gerard test-all rollback-imran rollback-gerard deploy logs shell test rollback new-user validate-user require-user

# =============================================================================
# Generic per-user helpers
# =============================================================================
# Resolve a user's values directory in this order:
#   1. deployments/<user>/         (public, committed)
#   2. users-private/<user>/       (gitignored, private)
# Same lookup applies to its secrets dir. All *.yaml files in the secrets
# dir (claude.yaml, github-app.yaml, oauth2.yaml, …) are auto-included.
#
# Usage:
#   make deploy   USER=chase
#   make logs     USER=chase
#   make shell    USER=chase
#   make test     USER=chase
#   make rollback USER=chase

user_dir = $(firstword $(wildcard ./deployments/$(1) ./users-private/$(1)))
values_file = $(call user_dir,$(1))/values.yaml
secrets_dir = $(firstword $(wildcard ./secrets/$(1) ./users-private/$(1)/secrets))
# Build a `-f path` arg for every *.yaml inside the resolved secrets dir.
secret_flags = $(foreach f,$(wildcard $(call secrets_dir,$(1))/*.yaml),-f $(f))

# Variables
REGISTRY := registry.digitalocean.com/resourceloop/coder
IMAGE_NAME := devlaptop
VERSION := v1.10.0-vnc-resize
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

deploy-imran: ## Deploy Imran's workspace (delegates to generic target so all secrets/*.yaml are picked up)
	@$(MAKE) --no-print-directory deploy USER=imran

deploy-gerard: ## Deploy Gerard's workspace (delegates to generic target so all secrets/*.yaml are picked up)
	@$(MAKE) --no-print-directory deploy USER=gerard

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

# =============================================================================
# Generic per-user targets (USER=<name>)
# =============================================================================

# Internal: fail fast with a helpful message if USER isn't set.
require-user:
	@if [ -z "$(USER)" ]; then echo "ERROR: pass USER=<name> (e.g. make deploy USER=chase)"; exit 1; fi
	@if [ -z "$(call user_dir,$(USER))" ]; then \
	  echo "ERROR: no values.yaml for '$(USER)' under deployments/ or users-private/"; exit 1; fi

new-user: ## Scaffold a private workspace (USER=<name>); generates values.yaml + cookieSecret + checklist
	@if [ -z "$(USER)" ]; then echo "ERROR: pass USER=<name> (e.g. make new-user USER=chase)"; exit 1; fi
	@./scripts/new-user.sh $(USER)

validate-user: ## Pre-deploy sanity check (USER=<name>); placeholders, DNS, cluster prereqs
	@if [ -z "$(USER)" ]; then echo "ERROR: pass USER=<name> (e.g. make validate-user USER=chase)"; exit 1; fi
	@./scripts/validate-user.sh $(USER)

deploy: require-user validate-user ## Deploy any user's workspace (USER=<name>); auto-finds values.yaml + secrets
	@echo "Deploying $(USER)'s workspace from $(call values_file,$(USER))..."
	helm upgrade $(USER)-workspace ./charts/workspace \
		-f $(call values_file,$(USER)) \
		$(call secret_flags,$(USER)) \
		--namespace $(NAMESPACE) \
		--install \
		--wait \
		--timeout 8m

logs: require-user ## Tail logs for any user's workspace (USER=<name>)
	kubectl logs -f -n $(NAMESPACE) deployment/ws-$(USER) -c ide

shell: require-user ## Shell into any user's workspace (USER=<name>)
	kubectl exec -it -n $(NAMESPACE) deployment/ws-$(USER) -c ide -- /bin/bash

test: require-user ## Sanity-test any user's workspace (USER=<name>)
	@echo "Testing $(USER)'s workspace..."
	@kubectl exec -n $(NAMESPACE) deployment/ws-$(USER) -c ide -- node --version
	@kubectl exec -n $(NAMESPACE) deployment/ws-$(USER) -c ide -- yarn --version
	@kubectl exec -n $(NAMESPACE) deployment/ws-$(USER) -c ide -- gh --version | head -1
	@kubectl exec -n $(NAMESPACE) deployment/ws-$(USER) -c ide -- code-server --version | head -1

rollback: require-user ## Rollback any user's workspace (USER=<name>)
	helm rollback $(USER)-workspace --namespace $(NAMESPACE)
