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

### One-time setup

Add a repo secret **`RENOVATE_TOKEN`** — a token with `contents:write` +
`pull-requests:write` (fine-grained PAT: Contents RW, Pull requests RW,
Workflows RW; or a classic PAT with `repo` + `workflow`). A PAT rather than the
default `GITHUB_TOKEN` is required so Renovate's PRs trigger CI. Then trigger a
first run from the Actions tab (**Run workflow → dry run: true**) to sanity-check
before it opens live PRs.

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

## Remaining work (follow-up PRs on #104)

- [ ] **Blocking SCA** — make `yarn audit` / `safety` in `ci.yml` fail the build
      at a chosen severity, with a curated allowlist (mirrors `.trivyignore`).
- [ ] **SBOM** — generate with Syft in CI and attach to releases.
- [ ] **Signing** — cosign keyless image signing + provenance attestation.
