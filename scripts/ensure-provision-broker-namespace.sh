#!/usr/bin/env bash
# scripts/ensure-provision-broker-namespace.sh — the one operator step the
# provisioning broker's namespace needs (#421).
#
# Usage: ensure-provision-broker-namespace.sh [broker_ns] [control_plane_ns]
#        (defaults: kube-coder-provision, coder — override with
#         provision.broker.namespace / .Values.namespace if you changed them)
#
# WHY THIS IS NOT IN THE CHART. #421 moved the privileged provisioner into its
# own namespace so that the internet-facing controller has no workload-creation
# rights beside it. The chart renders that namespace and everything in it — but
# it cannot render the shared `regcred` image-pull Secret, because it has no
# registry credentials (an operator creates regcred by hand, see scripts/
# setup.sh), and a pod can only pull from a Secret in its OWN namespace. So the
# broker Deployment and the provisioner Jobs both need a copy here.
#
# Run this with your ADMIN kubeconfig — deliberately not something any in-cluster
# identity can do for you. Idempotent: run it as often as you like. Safe to run
# before or after `helm upgrade`; the broker pod sits in ImagePullBackOff until
# the Secret exists and recovers on its own once it does.
set -euo pipefail

NS="${1:-${BROKER_NAMESPACE:-kube-coder-provision}}"
SRC="${2:-${CONTROL_PLANE_NAMESPACE:-coder}}"
REGCRED_NAME="${REGCRED_NAME:-regcred}"

if [ "$NS" = "$SRC" ]; then
  echo "FATAL: broker namespace ('$NS') must not be the control-plane namespace." >&2
  echo "       Co-locating the privileged provisioner with the controller is exactly" >&2
  echo "       the privilege bridge #421 removed. Set provision.broker.namespace." >&2
  exit 1
fi

if ! kubectl get namespace "$NS" >/dev/null 2>&1; then
  echo "FATAL: namespace '$NS' does not exist. The chart renders it —" >&2
  echo "       run 'helm upgrade --install' with provision.enabled=true first," >&2
  echo "       then re-run this script. (Creating it here would fight Helm's" >&2
  echo "       ownership metadata on the next upgrade.)" >&2
  exit 1
fi

if kubectl get secret "$REGCRED_NAME" -n "$NS" >/dev/null 2>&1; then
  echo "==> $REGCRED_NAME already present in $NS"
  exit 0
fi

if ! kubectl get secret "$REGCRED_NAME" -n "$SRC" >/dev/null 2>&1; then
  echo "FATAL: secret '$REGCRED_NAME' not found in '$SRC' to copy from." >&2
  echo "       Create it there first (scripts/setup.sh prints the command)." >&2
  exit 1
fi

echo "==> copying $REGCRED_NAME from $SRC into $NS"
# Strip namespace-bound + server-managed metadata so the object applies cleanly
# into the destination. Same shape as ensure-workspace-namespace.sh.
kubectl get secret "$REGCRED_NAME" -n "$SRC" -o json \
  | python3 -c 'import sys, json; d = json.load(sys.stdin); m = d.get("metadata", {}); [m.pop(k, None) for k in ("namespace", "resourceVersion", "uid", "creationTimestamp", "selfLink", "managedFields", "ownerReferences", "generation")]; d["metadata"] = m; d.pop("status", None); json.dump(d, sys.stdout)' \
  | kubectl apply -n "$NS" -f -
echo "==> done. The broker pod will pull on its next retry."
