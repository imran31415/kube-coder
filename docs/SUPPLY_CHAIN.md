# Supply-chain hardening

This repo aims for **reproducible builds**: every external artifact that goes
into the devlaptop image or the Helm charts is pinned to an explicit version,
and a weekly [Renovate](https://docs.renovatebot.com/) job proposes bumps so the
pins never silently rot.

Tracks issue [#104](https://github.com/imran31415/kube-coder/issues/104).

## What is pinned

| Artifact | Where | Pin |
|----------|-------|-----|
| Base images (`node`, `ubuntu`) | `devlaptop/Dockerfile` `FROM` | tag + digest (Renovate `pinDigests`) |
| Kaniko builder image | `charts/*/values.yaml`, `controller.py` | `gcr.io/kaniko-project/executor:v1.24.0` (+digest) |
| docker-compose | `devlaptop/Dockerfile` `COMPOSE_VERSION` | release tag (was `releases/latest`) |
| code-server, ttyd, sqlite-vec, librefang | `devlaptop/Dockerfile` `*_VERSION` | release tags |
| npm, claude-code, opencode, codex | `devlaptop/Dockerfile` `*_VERSION` | npm versions |
| GitHub Actions | `.github/workflows/*` | commit SHA (Renovate `pinGitHubActionDigests`) |
| SPA / controller deps | `charts/**/package.json`, Python reqs | native npm / pip managers |

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
