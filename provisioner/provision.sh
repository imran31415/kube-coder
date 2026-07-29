#!/usr/bin/env bash
# provisioner/provision.sh — entrypoint of the privileged workspace-provisioner
# Job (supply-chain hardening, #422 item 1; unblocks #421).
#
# This script used to be a Python string in the controller
# (controller.py :: PROVISION_JOB_SCRIPT), injected as the Job's `command`.
# That meant whoever could build the Job manifest also chose the code that ran
# under the cluster-privileged `workspace-provisioner` ServiceAccount — so a
# controller compromise was arbitrary code execution at provisioner privilege,
# and no Job template could be called "immutable" while a caller still supplied
# the command. Baking it here inverts that: the image carries the code, the Job
# carries only data (env), and the ValidatingAdmissionPolicy
# (charts/workspace-controller/templates/provisioner-vap.yaml) rejects any Job
# under that SA that tries to override the entrypoint at all.
#
# CONTRACT — the Job passes these as env and nothing else:
#   SLUG           workspace/user slug being provisioned
#   NAMESPACE      control-plane namespace the Job runs in (regcred source)
#   WS_NAMESPACE   the workspace's own per-user namespace, ws-<slug> (#103)
#   CHART_REPO     kube-coder chart repo to clone
#   CHART_REF      immutable ref to clone (validated controller-side: 40-hex
#                  SHA or vX.Y.Z tag, unless ALLOW_MUTABLE_CHART_REF)
#   GITOPS_REPO    users-private GitOps repo host/path
#   GITOPS_BRANCH  branch of that repo
#   GITOPS_TOKEN   token for it (never logged)
#
# Keep this file in lockstep with build_job_manifest() in controller.py: it is
# the *only* consumer of that env, and controller tests assert the two agree.
set -euo pipefail
export HOME=/tmp

# Fail closed with a readable message on a missing input rather than letting
# `set -u` abort mid-clone with a bare "unbound variable". Env is now the whole
# input surface of this privileged Job, so state its shape explicitly.
for var in SLUG NAMESPACE WS_NAMESPACE CHART_REPO CHART_REF GITOPS_REPO GITOPS_BRANCH GITOPS_TOKEN; do
  if [ -z "${!var:-}" ]; then
    echo "FATAL: required env '${var}' is empty or unset — the provisioner Job must supply the full env contract (see provisioner/provision.sh)" >&2
    exit 1
  fi
done

# Supply-chain (finding 7): helm + kubectl + git + make are baked into this
# dedicated, checksum-verified image and it is pinned by digest. This privileged
# path performs NO runtime tool downloads — the only network it does is the
# approved git clones below. Fail closed if a tool is missing (e.g. provision.image
# was pointed at some other image) rather than silently fetching binaries from
# the internet under the cluster-privileged provisioner ServiceAccount.
for tool in helm kubectl git make; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "FATAL: '$tool' not found in provisioner image — provisioning requires the dedicated provisioner image (provisioner/Dockerfile); refusing to download tools at runtime under the provisioner SA" >&2
    exit 1
  }
done

# Provenance: the expected helm version is written into the image at BUILD time
# from provisioner/Dockerfile's HELM_VERSION ARG (the single source of truth —
# the controller no longer carries a second copy to drift from). A baked file,
# not env, deliberately: env is the caller-supplied surface and must not be able
# to rewrite what this Job claims about itself.
helm_expected="$(cat /etc/provisioner/helm-version 2>/dev/null || echo 'unknown')"
helm_actual="$(helm version --short 2>/dev/null || echo '?')"
echo "provisioner: baked-in helm ${helm_actual} (expected ${helm_expected}), kubectl present — no runtime tool download"
case "${helm_actual}" in
  "${helm_expected}"*) ;;
  *) echo "provisioner: WARNING helm ${helm_actual} does not match the version baked into this image (${helm_expected})" >&2 ;;
esac

git clone --depth 1 -b "$CHART_REF" "$CHART_REPO" /tmp/kc
# Provenance: record the exact commit the privileged deploy actually runs from.
echo "provisioner: chart ref ${CHART_REF} resolved to commit $(git -C /tmp/kc rev-parse HEAD)"
git clone --depth 1 -b "$GITOPS_BRANCH" "https://x-access-token:${GITOPS_TOKEN}@${GITOPS_REPO}" /tmp/cfg
mkdir -p /tmp/kc/users-private
cp -r "/tmp/cfg/users-private/${SLUG}" "/tmp/kc/users-private/${SLUG}"
cd /tmp/kc
# Per-workspace namespace (#103): deploy into ws-<slug>, and copy the regcred
# image-pull Secret from the control-plane namespace into it. `make deploy`
# creates+labels the namespace and replicates regcred (see REGCRED_SRC_NAMESPACE).
make deploy USER="${SLUG}" NAMESPACE="${WS_NAMESPACE}" REGCRED_SRC_NAMESPACE="${NAMESPACE}"
