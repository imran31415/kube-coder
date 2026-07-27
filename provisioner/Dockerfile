# syntax=docker/dockerfile:1.7
# provisioner/Dockerfile
#
# Dedicated, minimal image for the privileged workspace-provisioner Job
# (charts/workspace-controller/controller.py :: build_job_manifest). Supply-chain
# hardening, security review July 2026, finding 7.
#
# Why a separate image at all: the provisioner Job runs `make deploy` under the
# cluster-privileged `workspace-provisioner` ServiceAccount. It previously reused
# the fat general-workspace image AND installed Helm at runtime with a `curl`
# from get.helm.sh. That runtime download is exactly the kind of moving part a
# privileged path must not have. This image bakes every tool the Job needs —
# helm, kubectl, git, make — each checksum-verified at build time, so:
#   * provisioning performs NO runtime tool downloads (only the approved GitOps
#     git clones the Job itself does), and
#   * the image is published, signed (keyless cosign), SBOM- + provenance-attested
#     by .github/workflows/release.yml and pinned BY DIGEST in the controller
#     chart (provision.image), then verified at admission.
#
# Keep the pins here in lockstep with the constants they mirror in controller.py
# (PROVISION_HELM_VERSION) and devlaptop/Dockerfile (KUBECTL_VERSION) — CI and the
# controller unit tests assert the helm version matches.

# Reuse the same Ubuntu base already pinned + Renovate-tracked for devlaptop, so
# there is one vetted base digest across the repo rather than a second one to
# audit. Renovate bumps the digest here too (pinDigests).
FROM ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90

ENV DEBIAN_FRONTEND=noninteractive

# Base OS packages: apt GPG-verifies every one. `apt-get upgrade` picks up base
# CVE fixes on rebuild (mirrors devlaptop). git+make+bash+coreutils are what
# `make deploy` and the provisioner scripts shell out to; curl+ca-certificates
# are only used at BUILD time to fetch the verified helm/kubectl tarballs below.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    ca-certificates curl git make bash coreutils \
    && rm -rf /var/lib/apt/lists/*

# Helm — baked in, checksum-verified. The published per-release .sha256sum
# (which already carries the `<hash>  <file>` line format) is fetched from the
# same release path and `sha256sum -c`'d before the tarball is unpacked, so a
# tampered/MITM'd helm fails the BUILD (not silently at provision time). Keep
# HELM_VERSION == controller.py PROVISION_HELM_VERSION (asserted in tests).
# renovate: datasource=github-releases depName=helm/helm
ARG HELM_VERSION=v3.14.4
RUN ARCH=$(dpkg --print-architecture) \
    && TARBALL="helm-${HELM_VERSION}-linux-${ARCH}.tar.gz" \
    && curl -fsSL "https://get.helm.sh/${TARBALL}" -o "/tmp/${TARBALL}" \
    && curl -fsSL "https://get.helm.sh/${TARBALL}.sha256sum" -o "/tmp/${TARBALL}.sha256sum" \
    # get.helm.sh's .sha256sum carries the *original* tarball name, so verify from
    # /tmp with the file kept under that exact name (a rename breaks `-c`).
    && (cd /tmp && sha256sum -c "${TARBALL}.sha256sum") \
    && tar -xzf "/tmp/${TARBALL}" -C /tmp \
    && install -m 0755 "/tmp/linux-${ARCH}/helm" /usr/local/bin/helm \
    && rm -rf "/tmp/${TARBALL}" "/tmp/${TARBALL}.sha256sum" "/tmp/linux-${ARCH}" \
    && helm version --short

# kubectl — same pinned version + published-checksum pattern as devlaptop.
# renovate: datasource=github-releases depName=kubernetes/kubernetes
ARG KUBECTL_VERSION=v1.31.4
RUN ARCH=$(dpkg --print-architecture) \
    && curl -fsSLO "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl" \
    && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl.sha256" -o kubectl.sha256 \
    && echo "$(cat kubectl.sha256)  kubectl" | sha256sum -c - \
    && install -m 0755 kubectl /usr/local/bin/kubectl \
    && rm -f kubectl kubectl.sha256 \
    && kubectl version --client=true --output=yaml >/dev/null

# Run as a non-root user. The Job exports HOME=/tmp and clones/deploys entirely
# under /tmp (1777), so an unprivileged uid is sufficient — and it keeps this
# image inside the provisioner ValidatingAdmissionPolicy's non-root expectation.
RUN useradd -u 65532 -m -s /usr/sbin/nologin provisioner
USER 65532
ENV HOME=/tmp
WORKDIR /tmp
