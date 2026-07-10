# Makefile for kube-coder
.PHONY: build push deploy-base deploy-all clean help status version deploy logs shell test rollback delete-user migrate-user migrate-all migrate-status new-user validate-user require-user release users-sync dashboard-web dashboard-web-install dashboard-web-test dashboard-web-clean python-tests python-coverage dashboard-web-coverage coverage test-coverage local local-up local-build local-secret local-deploy local-forward local-info local-down mobile-install mobile-typecheck mobile-web mobile-export-web mobile-screenshots mobile-build mobile-build-ios mobile-build-android mobile-submit-ios mobile-clean

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

# Resolve a user's config across roots, first match wins: legacy ./deployments,
# this repo's ./users-private, then ./.users (a checkout of the GitOps repo via
# `make users-sync`). The GitOps checkout is the unifying store — once a user is
# migrated there and removed from ./users-private, the same `make` targets keep
# working against the synced copy.
user_dir = $(firstword $(wildcard ./deployments/$(1) ./users-private/$(1) ./.users/users-private/$(1)))
values_file = $(call user_dir,$(1))/values.yaml
secrets_dir = $(firstword $(wildcard ./secrets/$(1) ./users-private/$(1)/secrets ./.users/users-private/$(1)/secrets))
# Build a `-f path` arg for every *.yaml inside the resolved secrets dir.
secret_flags = $(foreach f,$(wildcard $(call secrets_dir,$(1))/*.yaml),-f $(f))

# Resolve the image ref (repository:tag) a user's pod actually runs, read
# straight from their values.yaml `image:` block. `make ship USER=<x>` builds
# and pushes THIS tag, so the pushed image and the helm deploy can never
# diverge. Previously `ship` built the global $(VERSION) tag below, which
# matched almost no deployed workspace — a silent "configmap shipped but the
# image didn't" footgun, since the rolled pod just re-pulled its existing,
# unchanged tag. Empty when a workspace pins no image.tag in its values.
user_image = $(shell awk '/^image:/{i=1;next} i&&/^[^[:space:]]/{exit} i&&/^[[:space:]]*repository:[[:space:]]*/{r=$$2} i&&/^[[:space:]]*tag:[[:space:]]*/{t=$$2} END{if(r&&t)print r":"t}' $(call values_file,$(1)))

# Namespace a user's workspace lives in. Single source of truth is the
# `namespace:` field in their values.yaml (set to ws-<user> by the scaffold and
# the controller); falls back to the ws-<user> convention when the file is
# absent (e.g. orphan cleanup). This is what lets each tenant own an isolated
# namespace (#103) while the same targets keep working. The control-plane
# NAMESPACE below stays for cluster-shared releases (base infra, controller).
ws_namespace = $(shell ns=$$(awk '/^namespace:/{print $$2; exit}' $(call values_file,$(1)) 2>/dev/null); echo "$${ns:-ws-$(1)}")

# Variables
REGISTRY := registry.digitalocean.com/resourceloop/coder
IMAGE_NAME := devlaptop
# Fallback tag for user-less `make build` / `make push` / `make clean` only.
# `make ship USER=<x>` does NOT use this — it derives the tag from that user's
# values.yaml via $(user_image) so build and deploy stay in lockstep.
VERSION := v1.13.0
PLATFORM := linux/amd64
# Control-plane namespace: the shared base-infrastructure + workspace-controller
# releases live here. Per-#103 individual workspaces live in their OWN ws-<user>
# namespace (see ws_namespace above), NOT here.
NAMESPACE := coder
# Namespace the shared regcred image-pull Secret is copied FROM when standing up
# a new per-workspace namespace (#103). Override if your registry secret lives
# elsewhere.
REGCRED_SRC_NAMESPACE ?= $(NAMESPACE)

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

# Per-user workspaces use the generic targets (USER=<name>):
#   make deploy/logs/shell/test/rollback USER=<name>
# Tracked example values live in deployments/example-user/ (oauth2) and
# deployments/example-basic-auth/ (basic auth). Real configs belong under
# users-private/<name>/ (gitignored) — scaffold with `make new-user USER=<name>`.
deploy-all: deploy-base ## Deploy base infrastructure (per-user: make deploy USER=<name>)

# =============================================================================
# Monitoring
# =============================================================================

status: ## Check deployment status
	@echo "=== Helm Releases (control-plane: $(NAMESPACE)) ==="
	@helm list -n $(NAMESPACE)
	@echo ""
	@echo "=== Workspace namespaces (#103) ==="
	@kubectl get ns -l kube-coder.dev/managed=true 2>/dev/null || echo "(none labelled kube-coder.dev/managed)"
	@echo ""
	@echo "=== Workspace pods (all per-user namespaces) ==="
	@kubectl get pods -A 2>/dev/null | awk 'NR==1 || $$1 ~ /^ws-/'
	@echo ""
	@echo "=== Workspace ingresses ==="
	@kubectl get ingress -A --no-headers 2>/dev/null | awk '$$1 ~ /^ws-/ {print $$1, $$2, $$4}'

version: ## Show current versions and config
	@echo "Current configuration:"
	@echo "  Registry:  $(REGISTRY)"
	@echo "  Image:     $(IMAGE_NAME)"
	@echo "  Version:   $(VERSION)"
	@echo "  Platform:  $(PLATFORM)"
	@echo "  Namespace: $(NAMESPACE)"
	@echo "  Full tag:  $(IMAGE)"

# Per-user logs / test / shell are the generic targets:
#   make logs USER=<name>   make test USER=<name>   make shell USER=<name>

# =============================================================================
# Generic per-user targets (USER=<name>)
# =============================================================================

# Internal: fail fast with a helpful message if USER isn't set.
require-user:
	@if [ -z "$(USER)" ]; then echo "ERROR: pass USER=<name> (e.g. make deploy USER=chase)"; exit 1; fi
	@if [ -z "$(call user_dir,$(USER))" ]; then \
	  echo "ERROR: no values.yaml for '$(USER)' under deployments/, users-private/, or .users/ (run 'make users-sync' to fetch GitOps config)"; exit 1; fi

new-user: ## Scaffold a private workspace (USER=<name>); generates values.yaml + cookieSecret + checklist
	@if [ -z "$(USER)" ]; then echo "ERROR: pass USER=<name> (e.g. make new-user USER=chase)"; exit 1; fi
	@./scripts/new-user.sh $(USER)

# GitOps workspace-config repo (host/path, no scheme) — the unifying store the
# controller's provisioner also pushes to. Kept out of this public Makefile:
# resolved from the gitignored controller values, or override with KC_USERS_REPO.
USERS_DIR := .users
USERS_REPO ?= $(KC_USERS_REPO)
ifeq ($(USERS_REPO),)
USERS_REPO := $(shell awk '/^[[:space:]]*gitops:/{f=1} f&&/repo:/{print $$2; exit}' users-private/_controller/values.yaml 2>/dev/null)
endif

users-sync: ## Clone/pull the GitOps workspace-config repo into .users/ (set KC_USERS_REPO or _controller provision.gitops.repo)
	@if [ -z "$(USERS_REPO)" ]; then \
	  echo "ERROR: GitOps repo unknown. Set KC_USERS_REPO=github.com/<org>/<repo>.git,"; \
	  echo "       or provision.gitops.repo in users-private/_controller/values.yaml."; exit 1; fi
	@if [ -d $(USERS_DIR)/.git ]; then \
	  echo "Pulling latest workspace config into $(USERS_DIR)/ ..."; \
	  git -C $(USERS_DIR) pull --ff-only; \
	else \
	  echo "Cloning $(USERS_REPO) into $(USERS_DIR)/ ..."; \
	  git clone https://$(USERS_REPO) $(USERS_DIR); \
	fi

validate-user: ## Pre-deploy sanity check (USER=<name>); placeholders, DNS, cluster prereqs
	@if [ -z "$(USER)" ]; then echo "ERROR: pass USER=<name> (e.g. make validate-user USER=chase)"; exit 1; fi
	@./scripts/validate-user.sh $(USER)

deploy: require-user validate-user ## Deploy any user's workspace (USER=<name>); auto-finds values.yaml + secrets
	@echo "Deploying $(USER)'s workspace into namespace $(call ws_namespace,$(USER)) from $(call values_file,$(USER))..."
	@# Per-#103: create+label the tenant namespace and seed the shared prereqs
	@# (regcred image-pull Secret, then the base-infra kaniko-wrapper ConfigMap
	@# the pod mounts) BEFORE the workspace chart lands.
	@./scripts/ensure-workspace-namespace.sh "$(call ws_namespace,$(USER))" "$(USER)" "$(REGCRED_SRC_NAMESPACE)"
	helm upgrade base-infrastructure ./charts/base-infrastructure \
		--namespace $(call ws_namespace,$(USER)) \
		--set namespace=$(call ws_namespace,$(USER)) \
		--install
	helm upgrade $(USER)-workspace ./charts/workspace \
		-f $(call values_file,$(USER)) \
		$(call secret_flags,$(USER)) \
		--namespace $(call ws_namespace,$(USER)) \
		--install \
		--wait \
		--timeout 8m

logs: require-user ## Tail logs for any user's workspace (USER=<name>)
	kubectl logs -f -n $(call ws_namespace,$(USER)) deployment/ws-$(USER) -c ide

shell: require-user ## Shell into any user's workspace (USER=<name>)
	kubectl exec -it -n $(call ws_namespace,$(USER)) deployment/ws-$(USER) -c ide -- /bin/bash

test: require-user ## Sanity-test any user's workspace (USER=<name>)
	@echo "Testing $(USER)'s workspace..."
	@kubectl exec -n $(call ws_namespace,$(USER)) deployment/ws-$(USER) -c ide -- node --version
	@kubectl exec -n $(call ws_namespace,$(USER)) deployment/ws-$(USER) -c ide -- yarn --version
	@kubectl exec -n $(call ws_namespace,$(USER)) deployment/ws-$(USER) -c ide -- gh --version | head -1
	@kubectl exec -n $(call ws_namespace,$(USER)) deployment/ws-$(USER) -c ide -- code-server --version | head -1
	@kubectl exec -n $(call ws_namespace,$(USER)) deployment/ws-$(USER) -c ide -- ante --version | head -1

rollback: require-user ## Rollback any user's workspace (USER=<name>)
	helm rollback $(USER)-workspace --namespace $(call ws_namespace,$(USER))

# Permanently delete a workspace AND its home volume. Operates purely on
# cluster resources by name ($(USER)-workspace / ws-$(USER)-home), so it also
# cleans up orphans whose users-private/<name>/ dir is already gone — that's
# why it deliberately does NOT depend on require-user (no values dir needed).
# Guard: you must retype the workspace name at the prompt before anything is
# touched. Skips local config + the GitHub OAuth app (remove those by hand).
delete-user: ## Delete a workspace + its PVC/DATA (USER=<name>); retype the name to confirm
	@if [ -z "$(USER)" ] || [ "$(origin USER)" = "environment" ]; then \
	  echo "ERROR: pass USER=<name> explicitly on the command line (e.g. make delete-user USER=oldname)."; \
	  echo "       (\$$USER is also your shell login name, so it is ignored here to avoid deleting the wrong workspace.)"; \
	  exit 1; \
	fi
	@echo "WARNING: permanently deletes workspace '$(USER)' from namespace '$(call ws_namespace,$(USER))':"
	@echo "  helm release : $(USER)-workspace"
	@echo "  PVC + DATA   : ws-$(USER)-home   (IRREVERSIBLE — the home volume is destroyed)"
	@echo "  namespace    : $(call ws_namespace,$(USER))   (deleted last — removes any leftover objects)"
	@echo "  + all pods / services / ingress / configmaps / secrets in that namespace"
	@printf "Type the workspace name '%s' to confirm: " "$(USER)"
	@read confirm; \
	if [ "$$confirm" != "$(USER)" ]; then echo "Aborted — input did not match '$(USER)'."; exit 1; fi; \
	ns=$(call ws_namespace,$(USER)); \
	echo "==> helm uninstall $(USER)-workspace"; \
	helm uninstall $(USER)-workspace --namespace $$ns || true; \
	echo "==> deleting PVC ws-$(USER)-home (and its underlying volume)"; \
	kubectl delete pvc ws-$(USER)-home --namespace $$ns --ignore-not-found; \
	if [ "$$ns" != "$(NAMESPACE)" ]; then \
	  echo "==> deleting the per-workspace namespace $$ns (removes all remaining objects)"; \
	  kubectl delete namespace $$ns --ignore-not-found; \
	else \
	  echo "==> deleting leftover secrets (TLS + basic-auth, if present)"; \
	  kubectl delete secret $(USER)-dev-scalebase-io-tls $(USER)-basic-auth --namespace $$ns --ignore-not-found; \
	fi; \
	echo "Done. '$(USER)' removed from the cluster."; \
	echo "NOTE: users-private/$(USER)/ (local config) and the GitHub OAuth app are untouched — delete those manually if desired."

# =============================================================================
# Per-workspace namespace migration (#103)
# =============================================================================
# Move existing workspaces out of the shared control-plane namespace into their
# own ws-<user> namespace. Wraps scripts/migrate-user-namespace.sh.
#
#   make migrate-user USER=<name>              # copy the home volume only (safe, reversible)
#   make migrate-user USER=<name> CUTOVER=1    # + repoint values.yaml & deploy into ws-<name>
#   make migrate-user USER=<name> DECOMMISSION=1  # + delete the old copy (destructive)
#   make migrate-user USER=<name> DRY_RUN=1    # print every action, touch nothing
#   make migrate-all [CUTOVER=1] [DECOMMISSION=1] [SRC=coder]  # every workspace in SRC
#
# Migrate-all discovers every ws-<user> Deployment currently in SRC (default
# $(NAMESPACE)) and runs migrate-user for each. Start with a DRY_RUN=1 pass.
SRC ?= $(NAMESPACE)
MIGRATE_FLAGS = --src-namespace $(SRC) \
	$(if $(filter 1 true yes,$(DRY_RUN)),--dry-run,) \
	$(if $(filter 1 true yes,$(CUTOVER)),--cutover,) \
	$(if $(filter 1 true yes,$(DECOMMISSION)),--decommission,)

migrate-user: require-user ## Migrate one workspace to its own namespace (USER=<name> [CUTOVER=1] [DECOMMISSION=1] [DRY_RUN=1])
	@./scripts/migrate-user-namespace.sh "$(USER)" $(MIGRATE_FLAGS)

migrate-status: ## Show migration progress: which workspaces are in their own namespace vs still in SRC (default $(NAMESPACE))
	@echo "=== workspace namespace migration status (source: $(SRC)) ==="
	@kubectl get deploy -A -o jsonpath='{range .items[*]}{.metadata.namespace}{" "}{.metadata.name}{"\n"}{end}' 2>/dev/null \
	  | awk -v src='$(SRC)' 'BEGIN{printf "  %-22s %-24s %s\n","USER","NAMESPACE","STATUS"} \
	      $$2 ~ /^ws-/ { user=substr($$2,4); ns=$$1; total++; \
	        if(ns==$$2){st="migrated"; mig++} \
	        else if(ns==src){st="PENDING (shared "src")"; pend++} \
	        else {st="other"; oth++} \
	        printf "  %-22s %-24s %s\n", user, ns, st } \
	      END{ if(total==0) print "  (no ws-* workspaces found)"; \
	           else printf "\n  %d migrated / %d pending / %d total\n", mig, pend, total }'

migrate-all: ## Migrate every workspace in SRC (default $(NAMESPACE)) to its own namespace ([CUTOVER=1] [DECOMMISSION=1] [DRY_RUN=1] [SRC=coder])
	@users="$$(kubectl get deploy -n $(SRC) -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null | sed -n 's/^ws-//p')"; \
	if [ -z "$$users" ]; then echo "No ws-* workspaces found in namespace '$(SRC)'."; exit 0; fi; \
	echo "Workspaces to migrate from '$(SRC)':"; echo "$$users" | sed 's/^/  - /'; \
	for u in $$users; do \
	  echo ""; echo "########## migrate $$u ##########"; \
	  ./scripts/migrate-user-namespace.sh "$$u" $(MIGRATE_FLAGS) || { echo "migrate $$u FAILED — stopping."; exit 1; }; \
	done; \
	echo ""; echo "migrate-all complete."

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
	cd $(WEB_DIR) && yarn test --reporter=verbose

dashboard-web-clean: ## Remove SPA build artifacts
	rm -rf $(WEB_DIR)/dist $(WEB_DIR)/node_modules

# =============================================================================
# Mobile app (mobile/  — Expo / React Native, built & shipped via EAS)
# =============================================================================

MOBILE_DIR := mobile

mobile-install: ## Install mobile app deps (Expo SDK, npm)
	cd $(MOBILE_DIR) && npm install

mobile-typecheck: mobile-install ## Type-check the mobile app (tsc --noEmit)
	cd $(MOBILE_DIR) && npm run typecheck

mobile-web: mobile-install ## Run the app in a browser (react-native-web)
	cd $(MOBILE_DIR) && npm run web

mobile-ios: mobile-install ## Run the app in the iOS Simulator via Expo Go
	cd $(MOBILE_DIR) && npm run ios

mobile-android: mobile-install ## Run the app in the Android emulator via Expo Go
	cd $(MOBILE_DIR) && npm run android

# Expose a running workspace's Bearer API (BrowserHandler on 6080) to localhost
# so the mobile app can connect without a public host — the path for a local
# minikube workspace, or any cluster, when you don't want to use the public DNS.
mobile-forward: ## Port-forward a workspace's API (6080) to localhost for the app (USER=<name>)
	@if [ -z "$(USER)" ]; then echo "ERROR: pass USER=<name> (e.g. make mobile-forward USER=imran)"; exit 1; fi
	@echo "Forwarding http://localhost:6080 -> ws-$(USER) (namespace $(call ws_namespace,$(USER)), context $$(kubectl config current-context))"
	@echo "  App host:  http://localhost:6080 (iOS simulator)  |  http://<your-Mac-LAN-IP>:6080 (physical device / Android emulator)"
	@echo "  API token: kubectl -n $(call ws_namespace,$(USER)) exec deploy/ws-$(USER) -c ide -- cat /home/dev/.claude-tasks/.api-token"
	kubectl -n $(call ws_namespace,$(USER)) port-forward svc/ws-$(USER) 6080:6080

mobile-export-web: mobile-install ## Export the demo/mock web build → mobile/dist
	cd $(MOBILE_DIR) && npm run export:web

mobile-screenshots: mobile-install ## Capture store-sized screenshots → ios-assets/ + android-assets/
	cd $(MOBILE_DIR) && npx playwright install chromium >/dev/null 2>&1 || true
	cd $(MOBILE_DIR) && npm run screenshots
	@echo ""
	@echo "Screenshots written to ios-assets/ and android-assets/"

# Cloud builds via EAS. Requires `eas login` once (or EXPO_TOKEN in CI) and an
# EAS project (`eas init`, or EAS_PROJECT_ID + EAS_OWNER env vars). iOS is built
# on EAS macOS workers, so no local Mac is needed to produce the .ipa.
mobile-build: mobile-typecheck ## EAS production build for iOS + Android
	cd $(MOBILE_DIR) && npx eas-cli build --profile production --platform all

mobile-build-ios: mobile-typecheck ## EAS production build, iOS only (.ipa)
	cd $(MOBILE_DIR) && npx eas-cli build --profile production --platform ios

mobile-build-android: mobile-typecheck ## EAS production build, Android only (.aab)
	cd $(MOBILE_DIR) && npx eas-cli build --profile production --platform android

mobile-submit-ios: ## Upload the latest iOS production build to App Store Connect
	cd $(MOBILE_DIR) && npx eas-cli submit --profile production --platform ios

mobile-clean: ## Remove mobile build artifacts and deps
	rm -rf $(MOBILE_DIR)/dist $(MOBILE_DIR)/node_modules $(MOBILE_DIR)/.expo

python-tests: ## Run server.py unit + integration tests
	cd charts/workspace && python3 -m unittest discover -s tests -p '*_test.py' -v

python-coverage: ## Run Python tests with coverage report
	cd charts/workspace && coverage run -m unittest discover -s tests -p '*_test.py' -v && coverage report && coverage html

dashboard-web-coverage: dashboard-web-install ## Run SPA tests with coverage report
	cd $(WEB_DIR) && yarn test:coverage

test-coverage: ## Run tests with terminal coverage summary
	@echo "=== Running Frontend Tests with Coverage ==="
	@cd $(WEB_DIR) && yarn test:coverage 2>&1 | tail -20
	@echo ""
	@echo "=== Running Backend Tests with Coverage ==="
	@cd charts/workspace && coverage run -m unittest discover -s tests -p '*_test.py' -v > /dev/null 2>&1 && coverage report

coverage: ## Run all tests with comprehensive coverage reports and HTML output
	@./scripts/coverage-report.sh

test-all-units: dashboard-web-test python-tests controller-web-test controller-python-tests ## Run dashboard SPA + server.py + controller SPA + controller.py tests

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
#
# The image tag is taken from the USER's values.yaml (via $(user_image)), NOT
# the global $(VERSION) — so `make push` builds exactly the tag the helm
# deploy references and the pod re-pulls. Building the wrong tag used to leave
# the image silently stale while only the configmap updated.
# Usage:  make ship USER=<name>
ship: require-user validate-user ## Full deploy: build+push the user's image tag, helm upgrade, force-roll the pod (USER=<name>)
	@img="$(call user_image,$(USER))"; \
	if [ -z "$$img" ]; then \
	  echo "ERROR: no image.repository/tag found in $(call values_file,$(USER))."; \
	  echo "       Set them there, or run 'make push' + 'make deploy USER=$(USER)' manually."; \
	  exit 1; \
	fi; \
	echo "==> Shipping $(USER): building + pushing $$img (from $(call values_file,$(USER)))"; \
	$(MAKE) --no-print-directory push IMAGE="$$img"
	@$(MAKE) --no-print-directory deploy USER=$(USER)
	@echo "Forcing rollout restart so $(USER)'s pod re-pulls the image..."
	kubectl rollout restart deployment/ws-$(USER) -n $(call ws_namespace,$(USER))
	kubectl rollout status deployment/ws-$(USER) -n $(call ws_namespace,$(USER)) --timeout=180s

# Stop / start a workspace pod without touching the helm release. PVC + secrets +
# ingress + cookieSecret all stay; just the pod is gone. Reversible.
stop: require-user ## Scale a user's pod to 0 — turns off the workspace, preserves data (USER=<name>)
	@echo "Scaling ws-$(USER) to 0 replicas (workspace is being turned off)..."
	kubectl scale deployment/ws-$(USER) -n $(call ws_namespace,$(USER)) --replicas=0
	kubectl get deployment/ws-$(USER) -n $(call ws_namespace,$(USER))

start: require-user ## Scale a user's pod back to 1 — turns on a previously stopped workspace (USER=<name>)
	@echo "Scaling ws-$(USER) back to 1 replica..."
	kubectl scale deployment/ws-$(USER) -n $(call ws_namespace,$(USER)) --replicas=1
	kubectl rollout status deployment/ws-$(USER) -n $(call ws_namespace,$(USER)) --timeout=180s

# Just the configmap path — refreshes server.py + dashboard.html in the pod
# without rebuilding the Docker image. Faster than `make ship` for backend
# changes; doesn't pick up SPA/Dockerfile changes (use `ship` for those).
ship-config: require-user deploy ## Helm upgrade only — for server.py / configmap changes (USER=<name>)
	@echo "Forcing rollout in case the configmap checksum didn't change..."
	kubectl rollout restart deployment/ws-$(USER) -n $(call ws_namespace,$(USER))
	kubectl rollout status deployment/ws-$(USER) -n $(call ws_namespace,$(USER)) --timeout=180s

# Cut a release end-to-end: bump versions, build+push the matching
# devlaptop-<VERSION> image, commit, tag, and (after a y/N confirm) push +
# publish the GitHub release. VERSION must be passed on the command line
# (e.g. make release VERSION=v1.3.0); the bare 'v' is optional. Optional
# NOTES=<file> supplies release notes (otherwise GitHub auto-generates them).
# Does NOT redeploy workspaces — see the follow-up command it prints.
release: ## Cut a release (VERSION=x.y.z [NOTES=path]); bumps versions, builds+pushes image, tags, publishes
	@if [ "$(origin VERSION)" != "command line" ]; then \
	  echo "ERROR: pass VERSION=<x.y.z> on the command line (e.g. make release VERSION=v1.3.0)."; \
	  echo "       (VERSION has a default in this Makefile, so it must be set explicitly to release.)"; \
	  exit 1; \
	fi
	@./scripts/release.sh "$(VERSION)" "$(NOTES)"

# =============================================================================
# workspace-controller (charts/workspace-controller/) — admin console that
# lists every workspace in the namespace and starts/stops them. Deployed ONCE
# per namespace (not per user). Reuses the coder image (python3 + kubectl);
# controller.py and the built SPA both ship via ConfigMap, so the whole app
# uses the fast config-only path — no second image build.
#
# One-time prerequisites (mirror new-user.sh; cert-manager won't issue TLS
# until DNS resolves and the OAuth callback must match the host exactly):
#   - a DNS host for the console (controller.host)
#   - a DEDICATED GitHub OAuth App, callback https://<host>/oauth2/callback
#   - a 32-char cookieSecret: openssl rand -base64 64 | tr -d '\n=+/' | head -c 32
#   - oauth2.githubUsers — the admin allowlist; THIS is the access gate
# Put these in users-private/_controller/values.yaml (gitignored); any
# users-private/_controller/secrets/*.yaml are merged in too.
# =============================================================================
.PHONY: controller-web controller-web-install controller-web-test controller-python-tests controller-tests deploy-controller ship-controller-config controller-dev require-controller

WC_DIR := charts/workspace-controller
WC_WEB_DIR := $(WC_DIR)/web
CONTROLLER_DIR := users-private/_controller
controller_secret_flags = $(foreach f,$(wildcard $(CONTROLLER_DIR)/secrets/*.yaml),-f $(f))

controller-web-install: ## Install controller SPA deps (yarn, node 20)
	cd $(WC_WEB_DIR) && yarn install

controller-web-test: controller-web-install ## Run controller SPA unit tests (Vitest)
	cd $(WC_WEB_DIR) && yarn test --reporter=verbose

controller-python-tests: ## Run controller.py unit tests (stdlib unittest, no cluster)
	cd $(WC_DIR) && python3 -m unittest discover -s tests -p '*_test.py' -v

controller-tests: controller-web-test controller-python-tests ## Run both controller test suites

# vite-plugin-singlefile emits one self-contained index.html; copy it into the
# chart (web-dist/) where Helm's .Files.Get can read it for the ConfigMap. The
# size guard catches the day the bundle outgrows the 1 MiB ConfigMap limit.
controller-web: controller-web-install ## Build controller SPA → chart web-dist/ (single inlined index.html)
	cd $(WC_WEB_DIR) && yarn build
	rm -rf $(WC_DIR)/web-dist && mkdir -p $(WC_DIR)/web-dist
	cp $(WC_WEB_DIR)/dist/index.html $(WC_DIR)/web-dist/index.html
	@bytes=$$(wc -c < $(WC_DIR)/web-dist/index.html); \
	echo "Built controller SPA: $$bytes bytes (ConfigMap limit ~1048576)"; \
	if [ $$bytes -gt 1000000 ]; then \
	  echo "WARNING: SPA is near the 1 MiB ConfigMap limit — switch to image-baked delivery."; fi

require-controller:
	@if [ ! -f $(CONTROLLER_DIR)/values.yaml ]; then \
	  echo "ERROR: missing $(CONTROLLER_DIR)/values.yaml — set controller.host, oauth2.{cookieSecret,clientId,clientSecret,githubUsers}. Schema: $(WC_DIR)/values.yaml"; exit 1; fi

deploy-controller: require-controller ## Helm upgrade the workspace-controller release
	@echo "Deploying workspace-controller from $(CONTROLLER_DIR)/values.yaml..."
	helm upgrade workspace-controller ./$(WC_DIR) \
		-f $(CONTROLLER_DIR)/values.yaml \
		$(controller_secret_flags) \
		--set controller.image.tag=$(IMAGE_NAME)-$(VERSION) \
		--namespace $(NAMESPACE) \
		--install \
		--wait \
		--timeout 5m

# Primary deploy path: rebuild SPA, helm upgrade, force-roll the pod.
ship-controller-config: controller-web deploy-controller ## Build SPA + deploy + roll the controller pod
	@echo "Forcing rollout in case the configmap checksum didn't change..."
	kubectl rollout restart deployment/workspace-controller -n $(NAMESPACE)
	kubectl rollout status deployment/workspace-controller -n $(NAMESPACE) --timeout=180s

# Local dev: run controller.py against your kubeconfig. Listing is read-only
# and safe against the real cluster. Auth via a dev bearer token — in the
# browser set localStorage['kc.devToken']='devtoken' (or curl -H 'Authorization:
# Bearer devtoken'). Run `yarn --cwd $(WC_WEB_DIR) dev` in another shell for the UI.
controller-dev: ## Run controller.py locally (KUBECONFIG listing; dev bearer token)
	CONTROLLER_DEV_TOKEN=devtoken \
	CONTROLLER_DIST_DIR=$(PWD)/$(WC_DIR)/web-dist \
	NAMESPACE=$(NAMESPACE) \
	TRUSTED_PROXY=false \
	python3 $(WC_DIR)/controller.py

# =============================================================================
# Local development — run kube-coder on a local single-node cluster (minikube)
# =============================================================================
# No cloud dependencies: a locally-built image loaded into minikube, plain HTTP,
# http basic auth, and the cluster-default storage class. Every command targets
# the minikube context EXPLICITLY (--context / --kube-context) so these never
# touch a real/remote cluster. Full guide: docs/local-development.md
#
#   make local          # one-shot: start cluster, build+load image, deploy
#   make local-forward   # port-forward the ingress to localhost:8080 (blocking)
#   make local-info      # print the /etc/hosts line, URL, and credentials
#   make local-down      # remove the workspace (DELETE=1 also deletes the cluster)

LOCAL_PROFILE     := kube-coder
LOCAL_IMAGE       := kube-coder:local
LOCAL_VALUES      := deployments/local/values.yaml
LOCAL_HOST        := kube-coder.local
LOCAL_AUTH_SECRET := kube-coder-basic-auth
LOCAL_AUTH_USER   := admin
LOCAL_AUTH_PASS   := admin
LOCAL_RELEASE     := local-workspace
# The local workspace lives in its own namespace too (#103), matching the
# `namespace:` field in deployments/local/values.yaml.
LOCAL_NS          := ws-local
# Host arch -> docker platform. Native arm64 on Apple Silicon avoids emulation.
LOCAL_ARCH        := $(shell uname -m | sed 's/x86_64/amd64/; s/aarch64/arm64/')
# Bind every kubectl/helm call to the minikube context, never the current one.
LOCAL_KUBECTL     := kubectl --context $(LOCAL_PROFILE)
LOCAL_HELM        := helm --kube-context $(LOCAL_PROFILE)

local: local-up local-build local-secret local-deploy local-info ## Local one-shot: start minikube, build+load image, deploy, print access info

local-up: ## Start the local minikube cluster + enable the nginx ingress addon
	@command -v minikube >/dev/null || { echo "ERROR: minikube not found. Install it: 'brew install minikube' (macOS) or https://minikube.sigs.k8s.io/docs/start/"; exit 1; }
	minikube start -p $(LOCAL_PROFILE) --driver=docker --cpus=4 --memory=6g
	minikube addons enable ingress -p $(LOCAL_PROFILE)
	@echo "Waiting for the ingress controller to be ready..."
	$(LOCAL_KUBECTL) -n ingress-nginx wait --for=condition=ready pod \
		-l app.kubernetes.io/component=controller --timeout=180s

local-build: ## Build the workspace image for your host arch directly inside minikube
	@echo "Building $(LOCAL_IMAGE) for linux/$(LOCAL_ARCH) inside minikube..."
	# Build INSIDE the minikube node rather than host `docker buildx ... --load`.
	# On Docker Desktop the docker-container buildx driver reliably hangs the
	# CLI *after* the image is written (the same post-build hang
	# scripts/buildx-push.sh wraps for `--push`) — `--load` and
	# `-o type=docker,dest=…` both wedge `make`. `minikube image build` uses the
	# node's own builder: no host buildx, no hang, and no separate image-load
	# copy (the image lands straight in the cluster). Honors .dockerignore.
	minikube image build -t $(LOCAL_IMAGE) -f devlaptop/Dockerfile -p $(LOCAL_PROFILE) .

local-secret: ## Create the namespace + basic-auth secret (admin/admin) in the local cluster
	$(LOCAL_KUBECTL) create namespace $(LOCAL_NS) --dry-run=client -o yaml | $(LOCAL_KUBECTL) apply -f -
	@printf '%s:%s\n' "$(LOCAL_AUTH_USER)" "$$(openssl passwd -apr1 $(LOCAL_AUTH_PASS))" > /tmp/kc-local-htpasswd
	$(LOCAL_KUBECTL) -n $(LOCAL_NS) create secret generic $(LOCAL_AUTH_SECRET) \
		--from-file=auth=/tmp/kc-local-htpasswd --dry-run=client -o yaml | $(LOCAL_KUBECTL) apply -f -
	@rm -f /tmp/kc-local-htpasswd
	@echo "basic-auth secret '$(LOCAL_AUTH_SECRET)' ready (user: $(LOCAL_AUTH_USER) / pass: $(LOCAL_AUTH_PASS))"

local-deploy: ## Deploy base-infrastructure + the workspace to the local cluster
	$(LOCAL_HELM) upgrade base-infrastructure ./charts/base-infrastructure \
		--namespace $(LOCAL_NS) --set namespace=$(LOCAL_NS) --install --wait --timeout 3m
	$(LOCAL_HELM) upgrade $(LOCAL_RELEASE) ./charts/workspace \
		-f $(LOCAL_VALUES) --namespace $(LOCAL_NS) --install --wait --timeout 5m
	# Force a rollout so a freshly `local-build`-loaded image is picked up:
	# the tag (kube-coder:local) doesn't change, so helm sees no diff and the
	# pod would otherwise keep the old image even though minikube reloaded it.
	$(LOCAL_KUBECTL) -n $(LOCAL_NS) rollout restart deployment/ws-local
	$(LOCAL_KUBECTL) -n $(LOCAL_NS) rollout status deployment/ws-local --timeout=180s

local-forward: ## Port-forward the local ingress controller to localhost:8080 (blocking; Ctrl-C to stop)
	@echo "Forwarding http://$(LOCAL_HOST):8080 -> ingress-nginx. Ensure /etc/hosts maps $(LOCAL_HOST) -> 127.0.0.1 (see 'make local-info')."
	$(LOCAL_KUBECTL) -n ingress-nginx port-forward svc/ingress-nginx-controller 8080:80

local-info: ## Print local access details (/etc/hosts line, URL, credentials)
	@echo ""
	@echo "=== kube-coder local access ==="
	@echo "1. Map the host once (needs sudo):"
	@echo "     echo '127.0.0.1  $(LOCAL_HOST)' | sudo tee -a /etc/hosts"
	@echo "2. Forward the ingress (keep this running in a terminal):"
	@echo "     make local-forward"
	@echo "3. Open:        http://$(LOCAL_HOST):8080/"
	@echo "4. Basic auth:  $(LOCAL_AUTH_USER) / $(LOCAL_AUTH_PASS)"
	@echo ""
	@echo "Logs:  $(LOCAL_KUBECTL) -n $(LOCAL_NS) logs -f deploy/ws-local -c ide"
	@echo "Shell: $(LOCAL_KUBECTL) -n $(LOCAL_NS) exec -it deploy/ws-local -c ide -- bash"

local-down: ## Remove the local workspace (add DELETE=1 to also delete the minikube cluster)
	-$(LOCAL_HELM) uninstall $(LOCAL_RELEASE) -n $(LOCAL_NS)
	-$(LOCAL_HELM) uninstall base-infrastructure -n $(LOCAL_NS)
	@if [ "$(DELETE)" = "1" ]; then echo "Deleting minikube profile $(LOCAL_PROFILE)..."; minikube delete -p $(LOCAL_PROFILE); else echo "Cluster kept. Run 'make local-down DELETE=1' to delete the minikube profile."; fi
