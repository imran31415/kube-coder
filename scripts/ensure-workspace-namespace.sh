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

# Copy the image-pull Secret from the control-plane namespace into the tenant
# namespace, unless it's already there. Strip namespace-bound metadata so the
# object applies cleanly into the destination.
if kubectl get secret "$REGCRED_NAME" -n "$NS" >/dev/null 2>&1; then
  echo "==> $REGCRED_NAME already present in $NS"
elif kubectl get secret "$REGCRED_NAME" -n "$REGCRED_SRC" >/dev/null 2>&1; then
  echo "==> copying $REGCRED_NAME from $REGCRED_SRC into $NS"
  # Strip namespace-bound + server-managed metadata so the object applies cleanly
  # into the destination namespace (python3 ships in the coder/provisioner image).
  kubectl get secret "$REGCRED_NAME" -n "$REGCRED_SRC" -o json \
    | python3 -c 'import sys, json; d = json.load(sys.stdin); m = d.get("metadata", {}); [m.pop(k, None) for k in ("namespace", "resourceVersion", "uid", "creationTimestamp", "selfLink", "managedFields", "ownerReferences", "generation")]; d["metadata"] = m; d.pop("status", None); json.dump(d, sys.stdout)' \
    | kubectl apply -n "$NS" -f -
else
  echo "WARNING: image-pull secret '$REGCRED_NAME' not found in '$REGCRED_SRC';" \
       "the workspace pod may fail to pull its image. Create it in $NS by hand." >&2
fi
