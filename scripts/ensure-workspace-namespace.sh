#!/usr/bin/env bash
# scripts/ensure-workspace-namespace.sh — stand up a per-workspace namespace.
#
# Usage: ensure-workspace-namespace.sh <namespace> <user> [regcred_src_namespace]
#
# Under per-workspace namespaces (issue #103) every workspace owns its own
# `ws-<user>` namespace. Before Helm can install the workspace chart there, the
# namespace must exist (labelled so the controller's NetworkPolicy can select
# it) and the cluster-shared `regcred` image-pull Secret must be present — a pod
# can only pull from a Secret in its OWN namespace. This script is idempotent:
# run it as many times as you like.
#
# It does NOT create the kaniko-wrapper ConfigMap — that ships via the
# base-infrastructure chart, which `make deploy` installs into the same
# namespace right after this runs.
set -euo pipefail

NS="${1:?usage: ensure-workspace-namespace.sh <namespace> <user> [regcred_src_namespace]}"
USER_SLUG="${2:?missing <user>}"
REGCRED_SRC="${3:-coder}"
REGCRED_NAME="${REGCRED_NAME:-regcred}"

echo "==> ensuring namespace $NS (user=$USER_SLUG)"
# Create + label in one apply so re-runs converge (labels are what the
# controller's self-serve NetworkPolicy namespaceSelector matches on).
kubectl apply -f - <<YAML
apiVersion: v1
kind: Namespace
metadata:
  name: ${NS}
  labels:
    kube-coder.dev/managed: "true"
    kube-coder.dev/user: ${USER_SLUG}
YAML

# Copy a cluster-shared Secret from the control-plane namespace into the tenant
# namespace, unless it's already there. Strips namespace-bound + server-managed
# metadata so the object applies cleanly into the destination (python3 ships in
# the coder/provisioner image).
copy_secret() {  # name required(1|0)
  local name="$1" required="$2"
  if kubectl get secret "$name" -n "$NS" >/dev/null 2>&1; then
    echo "==> $name already present in $NS"
  elif kubectl get secret "$name" -n "$REGCRED_SRC" >/dev/null 2>&1; then
    echo "==> copying $name from $REGCRED_SRC into $NS"
    kubectl get secret "$name" -n "$REGCRED_SRC" -o json \
      | python3 -c 'import sys, json; d = json.load(sys.stdin); m = d.get("metadata", {}); [m.pop(k, None) for k in ("namespace", "resourceVersion", "uid", "creationTimestamp", "selfLink", "managedFields", "ownerReferences", "generation")]; d["metadata"] = m; d.pop("status", None); json.dump(d, sys.stdout)' \
      | kubectl apply -n "$NS" -f -
  elif [ "$required" = 1 ]; then
    echo "WARNING: secret '$name' not found in '$REGCRED_SRC';" \
         "the workspace pod may fail without it. Create it in $NS by hand." >&2
  fi
}

# Seed the per-workspace self-serve token: HMAC-SHA256 of "kc-self-serve/<user>"
# keyed by the controller's master token (same derivation as controller.py's
# self_serve_token_for — keep in sync). The master never enters the tenant
# namespace, so a token read out of one workspace only authorizes self-serve
# actions on that workspace (security review July 2026, finding 2). Always
# (re)applied — re-running this script migrates a namespace that still holds
# a verbatim pre-derivation copy of the master. Optional: skipped silently
# when self-serve isn't enabled (no master Secret in the source namespace).
seed_self_serve_secret() {
  local name="${SELF_SERVE_SECRET_NAME:-kc-self-serve}"
  if [ "$NS" = "$REGCRED_SRC" ]; then
    # Single-namespace layout: the workspace would mount the controller's own
    # master Secret. Never overwrite it — point the workspace's
    # update.selfServeSecretName at a separate derived Secret instead.
    echo "WARNING: workspace namespace == $REGCRED_SRC; refusing to overwrite" \
         "the master $name Secret with a derived token." >&2
    return 0
  fi
  local master
  master=$(kubectl get secret "$name" -n "$REGCRED_SRC" \
             -o jsonpath='{.data.self-serve-token}' 2>/dev/null | base64 -d) || true
  if [ -z "$master" ]; then
    echo "==> $name absent in $REGCRED_SRC (self-serve disabled); skipping"
    return 0
  fi
  echo "==> seeding per-workspace self-serve token $name into $NS"
  local derived
  derived=$(KC_MASTER="$master" KC_USER="$USER_SLUG" python3 -c 'import hashlib, hmac, os
print(hmac.new(os.environ["KC_MASTER"].encode(),
               ("kc-self-serve/" + os.environ["KC_USER"]).encode(),
               hashlib.sha256).hexdigest())')
  kubectl create secret generic "$name" -n "$NS" \
    --from-literal=self-serve-token="$derived" \
    --dry-run=client -o yaml | kubectl apply -n "$NS" -f -
}

# Image-pull Secret — required for the pod to pull from the private registry.
copy_secret "$REGCRED_NAME" 1
# Self-serve update token: per-workspace derivation of the controller's
# kc-self-serve master (never a verbatim copy). Workspaces whose values set
# update.selfServeSecretName mount it; without a per-namespace Secret the pod
# dies with CreateContainerConfigError.
seed_self_serve_secret
# Shared assistant secret (openrouter key). Provisioned workspaces reference it
# via assistant.openrouter.sharedSecretName (default coder-shared-assistant) as
# a pre-existing shared Secret the chart does NOT create; per-namespace isolation
# (#103) means it must be copied in or the ide container hits
# CreateContainerConfigError. Optional: skip silently if absent.
copy_secret "${ASSISTANT_SHARED_SECRET_NAME:-coder-shared-assistant}" 0
