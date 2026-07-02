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

# Image-pull Secret — required for the pod to pull from the private registry.
copy_secret "$REGCRED_NAME" 1
# Self-serve update token (controller's kc-self-serve). Workspaces whose values
# set update.selfServeSecretName mount it; without a per-namespace copy the pod
# dies with CreateContainerConfigError. Optional: skip silently if absent.
copy_secret "${SELF_SERVE_SECRET_NAME:-kc-self-serve}" 0
# Shared assistant secret (openrouter key). Provisioned workspaces reference it
# via assistant.openrouter.sharedSecretName (default coder-shared-assistant) as
# a pre-existing shared Secret the chart does NOT create; per-namespace isolation
# (#103) means it must be copied in or the ide container hits
# CreateContainerConfigError. Optional: skip silently if absent.
copy_secret "${ASSISTANT_SHARED_SECRET_NAME:-coder-shared-assistant}" 0
