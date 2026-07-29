# Supply-chain hardening

This repo aims for **reproducible builds**: every external artifact that goes
into the devlaptop image or the Helm charts is pinned to an explicit version,
and a weekly [Renovate](https://docs.renovatebot.com/) job proposes bumps so the
pins never silently rot.

Tracks issue [#104](https://github.com/imran31415/kube-coder/issues/104).

## What is pinned

| Artifact | Where | Pin |
|----------|-------|-----|
| Base images (`node`, `ubuntu`) | `devlaptop/Dockerfile`, `provisioner/Dockerfile` `FROM` | tag + digest (Renovate `pinDigests`) |
| Kaniko builder image | `charts/*/values.yaml`, `controller.py` | `gcr.io/kaniko-project/executor:v1.24.0` (+digest) |
| **oauth2-proxy** (the auth gate) | `charts/*/templates/oauth2-proxy.yaml` | `v7.15.3` **+ digest** — see [below](#oauth2-proxy-the-authentication-gate) |
| docker-compose | `devlaptop/Dockerfile` `COMPOSE_VERSION` | release tag (was `releases/latest`) |
| code-server, ttyd, sqlite-vec, librefang | `devlaptop/Dockerfile` `*_VERSION` | release tag **+ checksum-verified** (see below) |
| helm, kubectl (provisioner) | `provisioner/Dockerfile` `HELM_VERSION`/`KUBECTL_VERSION` | version + published-checksum-verified |
| npm, claude-code, opencode, codex | `devlaptop/Dockerfile` `*_VERSION` | npm versions |
| GitHub Actions | `.github/workflows/*` | commit SHA (Renovate `pinGitHubActionDigests`) |
| SPA / controller deps | `charts/**/package.json`, Python reqs | native npm / pip managers |

## Artifact integrity verification (finding 7)

Beyond version-pinning, every external binary/archive pulled into an image is
**checksum- or signature-verified at build time and fails the build on
mismatch** (download → verify → install; no `curl | bash`, no `curl | tar`):

| Artifact | Image | Verification source |
|----------|-------|---------------------|
| Node.js / npm | `devlaptop` | **digest-pinned `node:20-bookworm-slim`** — `node`/`npm`/`corepack` are `COPY --from` the same pinned image the SPA builder uses (content-addressed; no third-party apt repo). Replaced the NodeSource GPG keyring + `signed-by=` apt source, which was sound but left `deb.nodesource.com` in every uncached build's critical path (it began 403-ing repo-wide on 2026-07-27) |
| code-server `.deb` | `devlaptop` | per-arch `sha256` pinned as `CODE_SERVER_SHA256_{AMD64,ARM64}` (from the GitHub release API `digest`) |
| ttyd | `devlaptop` | release `SHA256SUMS` |
| sqlite-vec | `devlaptop` | release `checksums.txt` (reversed `<file> <hash>` layout) |
| librefang | `devlaptop` | per-asset `.tar.gz.sha256` sidecar |
| Ante | `devlaptop` | pinned release `manifest.json` `sha256` (download tarball direct, no installer pipe) |
| docker-compose, kubectl | `devlaptop` | vendor-published `.sha256` |
| helm, kubectl | `provisioner` | `get.helm.sh` `.sha256sum` / `dl.k8s.io` `.sha256` |

**Accepted exception (one):** **Antigravity (`agy`)** is distributed *only* as a
`curl | bash` installer that resolves `latest` server-side with no versioned URL
and no published per-artifact checksum, so there is nothing to pin or verify. It
is documented here and marked `# SUPPLY-CHAIN … ACCEPTED EXCEPTION` in the
Dockerfile; it will be pinned the moment the vendor exposes a versioned URL +
checksum.

Each pinned `ARG *_VERSION` in the Dockerfile carries a `# renovate:` annotation
naming its datasource, so Renovate's custom manager can resolve upgrades.

## The weekly update system

- **Config:** [`renovate.json`](../renovate.json) — datasources, grouping,
  `pinDigests`, and a `schedule` restricting PRs to Monday mornings
  (America/Los_Angeles).
- **Runner:** [`.github/workflows/renovate.yml`](../.github/workflows/renovate.yml)
  runs self-hosted Renovate on a weekly cron **and** on `workflow_dispatch`
  (with a dry-run option). No Mend GitHub App install required.
- **Output:** grouped `chore(deps)` PRs (devlaptop CLI tools, base images,
  kaniko, GitHub Actions, npm dev deps) plus a **Dependency Dashboard** issue
  summarizing everything pending. A human reviews and merges — nothing
  auto-merges.

### Setup

**No setup required to start** — the workflow falls back to the built-in
`GITHUB_TOKEN`, so a first run works out of the box. Trigger one from the
Actions tab (**Run workflow → dry run: true**) to sanity-check before it opens
live PRs.

**Recommended:** add a repo secret **`RENOVATE_TOKEN`** (a PAT). Two things a
PAT buys you that `GITHUB_TOKEN` can't:

1. Renovate's PRs **trigger CI** — PRs opened by `GITHUB_TOKEN` do not fire
   other workflows, so without a PAT you'd have to nudge each Renovate PR to
   run CI.
2. A higher github.com datasource rate limit.

Create it at **Settings → Developer settings → Personal access tokens**
(fine-grained: Contents RW + Pull requests RW + Workflows RW on this repo; or a
classic PAT with `repo` + `workflow`), then add it under **Settings → Secrets
and variables → Actions → New repository secret** named `RENOVATE_TOKEN`, or:

```bash
gh secret set RENOVATE_TOKEN --repo imran31415/kube-coder   # paste the PAT when prompted
```

## oauth2-proxy — the authentication gate

**Pinned to `quay.io/oauth2-proxy/oauth2-proxy:v7.15.3@sha256:10a1165743…`** in
both `charts/workspace/templates/oauth2-proxy.yaml` and
`charts/workspace-controller/templates/oauth2-proxy.yaml`. The digest is a
multi-arch OCI index (linux/amd64 + linux/arm64); the tag is kept alongside it
for humans. Tracked by its own Renovate manager and labelled `security` so a
bump is reviewed as a security change, not as dependency noise.

This is the one pin that gates *everything* — every workspace and the admin
console — so it gets its own section.

### Why 7.15.3

The charts previously pinned **7.5.0**, which sits inside the affected range of
two CVSS 9.1 authentication bypasses ([issue #567](https://github.com/imran31415/kube-coder/issues/567)):

| Advisory | Issue | Fixed in | Were we exposed? |
|---|---|---|---|
| [GHSA-5hvv-m4w4-gf6v](https://github.com/oauth2-proxy/oauth2-proxy/security/advisories/GHSA-5hvv-m4w4-gf6v) | health-check `User-Agent` bypass in auth_request deployments | 7.15.2 | No — requires `--ping-user-agent` or `--gcp-healthchecks`; we set neither |
| [GHSA-7x63-xv5r-3p2x](https://github.com/oauth2-proxy/oauth2-proxy/security/advisories/GHSA-7x63-xv5r-3p2x) | `X-Forwarded-Uri` spoofing | 7.15.2 | No — requires `--reverse-proxy` **and** a skip-auth rule; the controller sets the former, neither chart sets the latter |

We were not vulnerable to either, but in both cases only because of a flag we
happen not to set — not because of our version. 7.15.3 additionally carries a
Go 1.26 upgrade and a batch of dependency CVE fixes.

### The two deployment modes differ, deliberately

- **workspace chart** — runs as nginx's `auth_request` backend. It does **not**
  set `--reverse-proxy`, so client-supplied `X-Forwarded-*` is never treated as
  canonical.
- **workspace-controller chart** — runs as a real reverse proxy
  (`--reverse-proxy=true`), forwarding to the controller via `--upstream`.

### `--trusted-proxy-ip` (controller only)

New in 7.15.2 and the hardening that accompanies the `X-Forwarded-Uri` fix.
With `--reverse-proxy` alone, oauth2-proxy trusts forwarded headers from *any*
direct caller — upstream defaults the trusted set to `0.0.0.0/0` + `::/0` for
backwards compatibility and logs a warning.

The check is against the **direct TCP peer** (`req.RemoteAddr`), not a header,
so the correct value is the range the ingress-nginx controller pods draw
addresses from. Pod IPs are dynamic, so it is a CIDR; the tightest correct value
is cluster-specific, hence `oauth2.trustedProxyIPs` in
`charts/workspace-controller/values.yaml` rather than a hardcoded constant. The
default is the private/in-cluster space (RFC1918 + CGNAT + IPv6 ULA), which
narrows trust from "the whole internet" to "in-cluster" without needing to know
your CNI's layout. **Narrow it to your real pod CIDR if you know it.**

This is the L7 complement to a control already enforced at L3: the controller
NetworkPolicy admits port 4180 only from the `ingress-nginx` namespace. The flag
still holds if that policy is removed or the CNI does not enforce it.

It is deliberately **not** set on the workspace chart: `CanTrustForwardedHeaders`
returns false outright when `--reverse-proxy` is unset, so it would be dead
config implying a protection it does not provide.

### Do not add a skip-auth rule to the controller

`--skip-auth-route` / `--skip-auth-regex` combined with `--reverse-proxy` was the
precondition pair for GHSA-7x63-xv5r-3p2x. That specific bypass is fixed, but the
combination is not one to reintroduce casually. Helm tests in both charts assert
the complete `args` list, so adding one fails CI.

### Upgrading is not a pure version bump

Ten releases of config churn sit between 7.5.0 and 7.15.3. All 20 flags we pass
still exist under the same names, but two behaviours changed:

- **The GitHub OAuth scope widened.** `githubDefaultScope` went from
  `user:email` to `user:email read:org`, and org/team enumeration now runs on
  *every* login rather than only when `--github-org`/`--github-team` are set.
  Users may see a fresh GitHub consent screen on first login after the upgrade.
- **CSRF cookie expiry** now uses `--cookie-csrf-expire` (default 15m) instead of
  `--cookie-expire` (7.15.0). A login left idle on the GitHub consent screen for
  more than 15 minutes must be restarted.

**Verifying an oauth2-proxy bump requires a real login on both surfaces.** Helm
rendering proves the manifest is well-formed; it proves nothing about whether
authentication works. A broken config either fails closed (nobody can log in) or
fails open (the allowlist stops being enforced). Plan for a pod restart and a
manual login test against both the workspace and the controller console.

## Manual-bump exceptions

Two artifacts have no datasource Renovate can track and are bumped by hand
(current version at the linked probe, then rebuild the image):

- **Ante** (`ANTE_VERSION`) — channel/manifest distribution, no GitHub release or
  OCI tag. Pinned to a concrete release manifest under
  `https://download.ante.run/releases/<version>/manifest.json`; current stable at
  `https://download.ante.run/channels/stable/manifest.json`.
- **Firefox** (`FIREFOX_VERSION`) — Mozilla CDN, no Renovate datasource. Current
  at `https://product-details.mozilla.org/1.0/firefox_versions.json`
  (`LATEST_FIREFOX_VERSION`).

## Software Composition Analysis (SCA)

CI fails on high/critical dependency advisories, with curated allowlists for
accepted/unfixable cases (the `.trivyignore` pattern, applied to source deps):

| Layer | Tool | Gate | Allowlist |
|-------|------|------|-----------|
| SPA deps (yarn classic) | `audit-ci` | high+ | `charts/*/web/audit-ci.jsonc` (GHSA IDs) |
| Python deps | `pip-audit` | any | `--ignore-vuln <ID>` in `ci.yml` |
| Image (OS + Python + Node CLIs) | Trivy | CRITICAL/HIGH | `.trivyignore` + `ignore-unfixed` |

Notes:
- The SPA allowlist currently holds five **dev/build-time** advisories
  (vite / vitest / happy-dom) that never ship in the runtime image, which
  carries only built static assets. Renovate proposes their upgrades weekly;
  prune each entry once the dep is bumped past its patched version.
- Python runtime deps are pinned in **`devlaptop/requirements.txt`** — the
  single source of truth the image installs from *and* CI audits. The app is
  otherwise stdlib-only.
- **`pip-audit`** (PyPA, OSV-backed, no account) replaces the former
  `safety check`, which required an account for its DB and, without a target
  file, scanned only the runner's own install rather than the project.

## Software Bill of Materials (SBOM)

CI generates an SPDX-JSON SBOM of the built image with Syft
(`anchore/sbom-action`) and uploads it as the `kube-coder-sbom.spdx.json`
artifact on every run.

## Image signing + provenance (releases)

On each release tag (`devlaptop-v*`), the [`release`](../.github/workflows/release.yml)
workflow builds the image, publishes it to **GHCR**, and:

- **Signs it** with **keyless cosign** (Sigstore OIDC — no long-lived keys;
  the signature is logged to the Rekor transparency log).
- Attaches a BuildKit-native **SBOM** and **max-mode SLSA provenance**
  attestation to the image (OCI referrers).
- **Self-verifies** the signature in-job, so a broken signing step fails the
  release.

This is **additive** and needs **no secrets** (the built-in `GITHUB_TOKEN`
pushes to GHCR; the OIDC `id-token` mints the certificate). The DigitalOcean
deploy image still ships via `make push`; GHCR is the signed, publicly
verifiable artifact.

The same job now builds **two** images from one matrix: `devlaptop` and
`provisioner` (below).

## Privileged provisioner image (finding 7)

The self-service provisioner Job (`charts/workspace-controller/controller.py`)
runs `make deploy` under the cluster-privileged `workspace-provisioner`
ServiceAccount. It used to reuse the fat workspace image **and install Helm at
runtime** with a `curl` from `get.helm.sh` — a moving part on a privileged path.
That is now closed:

- **Dedicated minimal image** — [`provisioner/Dockerfile`](../provisioner/Dockerfile)
  bakes `helm` + `kubectl` + `git` + `make` (each checksum-verified at build),
  so provisioning performs **no runtime tool downloads** — only the approved
  GitOps `git clone`s. The Job **fails closed** if a tool is missing instead of
  fetching it.
- **Signed + attested** — built, keyless-cosign **signed**, and SBOM/SLSA-
  provenance **attested** by the release workflow, exactly like `devlaptop`.
- **Pinned by digest** — set `provision.image` to a `repo@sha256:…` ref. The CEL
  `ValidatingAdmissionPolicy` (`provisioner-vap.yaml`) **requires** the digest
  form (`provision.admissionPolicy.requireDigest`, on by default) alongside the
  existing shape/repository pinning.
- **Signature verified at admission** *(opt-in)* — CEL cannot check a cosign
  signature, so `provision.admissionPolicy.verifyImageSignature=true` renders a
  signature-verifying policy (`provisioner-image-policy.yaml`) for either
  **Kyverno** (`engine: kyverno`, default) or **Sigstore policy-controller**
  (`engine: sigstore`). It is off by default because it requires that controller
  installed in the cluster; the digest pin holds regardless. The trusted keyless
  identity/issuer default to the release signer and are overridable
  (`signatureIdentityRegexp` / `signatureOidcIssuer`) for forks.

Result: the exact provisioner bytes are immutable (digest), provably built by
this repo's CI (signature + provenance), and reconstructible from pinned inputs.

### Verifying a released image

```bash
IMG=ghcr.io/imran31415/kube-coder/devlaptop:<version>
cosign verify "$IMG" \
  --certificate-identity-regexp '^https://github.com/imran31415/kube-coder/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
cosign download sbom "$IMG"
cosign verify-attestation "$IMG" --type slsaprovenance \
  --certificate-identity-regexp '^https://github.com/imran31415/kube-coder/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

With all four items (pinning + weekly Renovate, blocking SCA, SBOM, signing)
in place, #104 is complete.
