# syntax=docker/dockerfile:1.26@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
# provisioner/Dockerfile
#
# Dedicated, minimal image for the privileged workspace-provisioner Job
# (charts/workspace-controller/controller.py :: build_job_manifest). Supply-chain
# hardening, security review July 2026, finding 7.
#
# It also carries the Job's CODE: provision.sh is baked in as the ENTRYPOINT
# (#422 item 1). The controller no longer injects a `command` — the Job supplies
# only env — so the manifest carries data, not code, and admission
# (provisioner-vap.yaml) can reject any Job under the provisioner SA that tries
# to override the entrypoint. That is the precondition for the #421 broker to
# stamp Jobs from a genuinely immutable template.
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
# HELM_VERSION below is now the SINGLE source of truth for the helm the
# privileged path runs: it is baked into /etc/provisioner/helm-version at build
# time and read back by provision.sh, so there is no second copy in controller.py
# to drift from (the old PROVISION_HELM_VERSION constant is gone — #422). Keep
# KUBECTL_VERSION in lockstep with devlaptop/Dockerfile as before.

# Reuse the same Ubuntu base already pinned + Renovate-tracked for devlaptop, so
# there is one vetted base digest across the repo rather than a second one to
# audit. Renovate bumps the digest here too (pinDigests).
FROM ubuntu:24.04@sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea

ENV DEBIAN_FRONTEND=noninteractive

# Base OS packages: apt GPG-verifies every one. `apt-get upgrade` picks up base
# CVE fixes on rebuild (mirrors devlaptop). git+make+bash+coreutils are what
# `make deploy` and the provisioner scripts shell out to; python3 is a RUNTIME
# dep too — scripts/ensure-workspace-namespace.sh uses it to strip server-managed
# metadata off the copied regcred Secret and to derive the self-serve token HMAC
# (json/hashlib/hmac, stdlib only). Before #422 the Job ran under the controller
# image, which has python3; the dedicated provisioner image must ship it itself
# or `make deploy` fails with `python3: command not found`. curl+ca-certificates
# are only used at BUILD time to fetch the verified helm/kubectl tarballs below.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    ca-certificates curl git make bash coreutils python3 \
    && rm -rf /var/lib/apt/lists/*

# Helm — baked in, checksum-verified. The published per-release .sha256sum
# (which already carries the `<hash>  <file>` line format) is fetched from the
# same release path and `sha256sum -c`'d before the tarball is unpacked, so a
# tampered/MITM'd helm fails the BUILD (not silently at provision time). This ARG
# is the only declaration of the version (see the header); Renovate bumps it here.
# renovate: datasource=github-releases depName=helm/helm
ARG HELM_VERSION=v3.21.3
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
    && helm version --short \
    # Bake the expected version where the entrypoint can read it back for its
    # provenance line. A file, not ENV: env is the Job's caller-supplied input
    # surface, and what the image says about itself must not be overridable from
    # the manifest. Root-owned, world-readable, outside the 1777 /tmp the Job works in.
    && mkdir -p /etc/provisioner \
    && printf '%s\n' "${HELM_VERSION}" > /etc/provisioner/helm-version

# kubectl — same pinned version + published-checksum pattern as devlaptop.
# renovate: datasource=github-releases depName=kubernetes/kubernetes
ARG KUBECTL_VERSION=v1.36.3
RUN ARCH=$(dpkg --print-architecture) \
    && curl -fsSLO "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl" \
    && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl.sha256" -o kubectl.sha256 \
    && echo "$(cat kubectl.sha256)  kubectl" | sha256sum -c - \
    && install -m 0755 kubectl /usr/local/bin/kubectl \
    && rm -f kubectl kubectl.sha256 \
    && kubectl version --client=true --output=yaml >/dev/null

# The Job's code (#422 item 1). Root-owned and 0755: the unprivileged uid the
# Job runs as can execute it but cannot rewrite it, so a foothold inside the
# container cannot repoint the next provision. Build context is the repo root
# (.github/workflows/release.yml sets `context: .`), hence the path prefix.
COPY --chmod=0755 provisioner/provision.sh /usr/local/bin/provision.sh

# Run as a non-root user. The Job exports HOME=/tmp and clones/deploys entirely
# under /tmp (1777), so an unprivileged uid is sufficient — and it keeps this
# image inside the provisioner ValidatingAdmissionPolicy's non-root expectation.
RUN useradd -u 65532 -m -s /usr/sbin/nologin provisioner
USER 65532
ENV HOME=/tmp
WORKDIR /tmp

# The provisioning script IS the image. The Job supplies env only and no
# `command` — see build_job_manifest() in controller.py and the no-command
# validation in charts/workspace-controller/templates/provisioner-vap.yaml.
ENTRYPOINT ["/usr/local/bin/provision.sh"]
