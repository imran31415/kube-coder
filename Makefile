# Makefile for kube-coder
.PHONY: build push deploy-base deploy-imran deploy-gerard deploy-all clean help status logs-imran logs-gerard shell-imran shell-gerard version test-imran test-gerard test-all rollback-imran rollback-gerard deploy logs shell test rollback new-user validate-user require-user dashboard-web dashboard-web-install dashboard-web-test dashboard-web-clean python-tests

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

build: ## Build Docker image for amd64 architecture (loads into local Docker)
	@echo "Building $(IMAGE) for $(PLATFORM)..."
	docker buildx build \
		--platform $(PLATFORM) \
		-t $(IMAGE) \
		-f devlaptop/Dockerfile \
		--load \
		.

# `push` is *standalone* — it builds + pushes in a single buildx invocation.
# We don't depend on `build` because that target hangs forever without an
# output flag (no --load / --push). Buildkit's layer cache makes this a no-op
# if `build` was just run.
push: ## Build and push Docker image (single buildx invocation; cache shared with `build`)
	@echo "Building + pushing $(IMAGE)..."
	# scripts/buildx-push.sh wraps the raw `docker buildx build --push`
	# call so the recurring post-push CLI hang can't block `make ship`.
	# The wrapper watches buildx output for `pushing manifest … done`
	# (the registry-confirmed success line), gives buildx 10s grace,
	# then SIGTERMs the CLI if still alive. --provenance=false +
	# --sbom=false + BUILDX_NO_DEFAULT_ATTESTATIONS=1 reduce the hang
	# frequency but don't eliminate it on Docker Desktop + the
	# docker-container driver, so the safety-kill is what actually
	# makes `make ship` deterministic. See the script header for the
	# full diagnosis.
	./scripts/buildx-push.sh "$(IMAGE)" devlaptop/Dockerfile "$(PLATFORM)"

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

# =============================================================================
# Dashboard SPA (charts/workspace/web/)
# =============================================================================

WEB_DIR := charts/workspace/web

dashboard-web-install: ## Install SPA deps (yarn 1.22.x, node 20)
	cd $(WEB_DIR) && yarn install

dashboard-web: dashboard-web-install ## Build the new dashboard SPA → charts/workspace/web/dist
	cd $(WEB_DIR) && yarn build
	@echo ""
	@echo "Built dashboard SPA at $(WEB_DIR)/dist"
	@echo "To preview locally: DASHBOARD_DIST_DIR=$(PWD)/$(WEB_DIR)/dist python3 charts/workspace/server.py"

dashboard-web-test: dashboard-web-install ## Run SPA unit tests (Vitest)
	cd $(WEB_DIR) && yarn test

dashboard-web-clean: ## Remove SPA build artifacts
	rm -rf $(WEB_DIR)/dist $(WEB_DIR)/node_modules

python-tests: ## Run server.py unit + integration tests
	cd charts/workspace && python3 -m unittest discover -s tests -p '*_test.py' -v

test-all-units: dashboard-web-test python-tests ## Run SPA (Vitest) + server.py (unittest) tests

# Full end-to-end deploy: image + chart + rolled pod.
# Two delivery paths feed a pod and we need BOTH for any change:
#   1. Docker image  (Dockerfile changes, SPA bundle in /opt/dashboard-dist,
#                     Claude/OpenCode versions, base apt packages, …)
#      → make push  (single buildx --push invocation)
#   2. ConfigMap     (server.py, dashboard.html, claude-md.txt, harness.py,
#                     workspace-entrypoint, …)
#      → make deploy  (helm upgrade — the configmap checksum is annotated on
#                      the deployment, so any change auto-rolls the pod)
# We also force a rollout restart at the end: when only the image changed
# (configmap unchanged), helm sees no diff and the pod wouldn't otherwise
# restart, even with imagePullPolicy: Always.
# Usage:  make ship USER=<name>
ship: require-user push deploy ## Full deploy: build+push image, helm upgrade, force-roll the pod (USER=<name>)
	@echo "Forcing rollout restart so $(USER)'s pod re-pulls the image..."
	kubectl rollout restart deployment/ws-$(USER) -n $(NAMESPACE)
	kubectl rollout status deployment/ws-$(USER) -n $(NAMESPACE) --timeout=180s

# Stop / start a workspace pod without touching the helm release. PVC + secrets +
# ingress + cookieSecret all stay; just the pod is gone. Reversible.
stop: require-user ## Scale a user's pod to 0 — turns off the workspace, preserves data (USER=<name>)
	@echo "Scaling ws-$(USER) to 0 replicas (workspace is being turned off)..."
	kubectl scale deployment/ws-$(USER) -n $(NAMESPACE) --replicas=0
	kubectl get deployment/ws-$(USER) -n $(NAMESPACE)

start: require-user ## Scale a user's pod back to 1 — turns on a previously stopped workspace (USER=<name>)
	@echo "Scaling ws-$(USER) back to 1 replica..."
	kubectl scale deployment/ws-$(USER) -n $(NAMESPACE) --replicas=1
	kubectl rollout status deployment/ws-$(USER) -n $(NAMESPACE) --timeout=180s

# Just the configmap path — refreshes server.py + dashboard.html in the pod
# without rebuilding the Docker image. Faster than `make ship` for backend
# changes; doesn't pick up SPA/Dockerfile changes (use `ship` for those).
ship-config: require-user deploy ## Helm upgrade only — for server.py / configmap changes (USER=<name>)
	@echo "Forcing rollout in case the configmap checksum didn't change..."
	kubectl rollout restart deployment/ws-$(USER) -n $(NAMESPACE)
	kubectl rollout status deployment/ws-$(USER) -n $(NAMESPACE) --timeout=180s
