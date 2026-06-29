# kube-coder

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Helm](https://img.shields.io/badge/Helm-3.0%2B-blue?logo=helm)](https://helm.sh)
[![Kubernetes](https://img.shields.io/badge/Kubernetes-1.19%2B-326CE5?logo=kubernetes&logoColor=white)](https://kubernetes.io)
[![CI](https://github.com/imran31415/kube-coder/actions/workflows/ci.yml/badge.svg)](https://github.com/imran31415/kube-coder/actions/workflows/ci.yml)
[![Test Coverage](https://img.shields.io/badge/coverage-60%25-yellow)](https://github.com/imran31415/kube-coder)
---

## Enterprise-Grade Development Workspaces on Kubernetes

kube-coder delivers **per-user, isolated development environments** that combine the power of cloud-native infrastructure with cutting-edge AI assistance. Each workspace provides a comprehensive suite of development tools—**VS Code in the browser**, **persistent tmux terminals**, an **interactive dashboard**, **in-pod browser sessions**, and **AI-powered build automation**—all secured behind GitHub OAuth and accessible via per-user subdomains.

> **Architected for scale:** A single Helm chart can deploy as many independent IDE pods as your Kubernetes cluster can accommodate—each with separate namespaces, ingress rules, persistent volumes, and assistant configurations.

### Why Choose kube-coder?

**🚀 Developer Productivity Revolution**
- **Zero-setup onboarding** - New developers get a fully configured environment in minutes, not days
- **Persistent workspaces** - Your environment survives pod restarts, preserving in-flight work and tmux sessions
- **AI-powered workflows** - Integrate Claude Code or OpenCode directly into your development process
- **Multi-modal access** - Code, terminal, and browser interfaces available simultaneously

**🔒 Enterprise Security & Isolation**
- **Per-user isolation** - Separate Kubernetes namespaces, network policies, and persistent volumes
- **GitHub OAuth integration** - Secure authentication with your existing identity provider
- **Zero-trust networking** - Internal services protected behind authentication proxies

**📈 Operational Excellence**
- **Kubernetes-native** - Leverage built-in scaling, healing, and resource management
- **Helm-based deployment** - Repeatable, version-controlled infrastructure as code
- **Comprehensive monitoring** - Built-in metrics, health checks, and logging

[Documentation & Limited Public Demo](https://demo-public.dev.scalebase.io/docs)

## Subreddit
- https://www.reddit.com/r/kubecoder/

## Example Screenshots

<table>
  <tr>
    <td width="50%" valign="top">
      <img width="100%" alt="Fullstack Python app in split-pane with terminal" src="https://github.com/user-attachments/assets/c48d004e-a97b-4107-8035-dcf36c1d9186" />
      <br/><sub><b>Fullstack Python</b> — split-pane editor with live terminal</sub>
    </td>
    <td width="50%" valign="top">
      <img width="100%" alt="Fullstack Go app in split-pane with Claude" src="https://github.com/user-attachments/assets/9901202e-a11b-4013-ab94-32745d2bc8f5" />
      <br/><sub><b>Fullstack Go</b> — split-pane editor with Claude Code</sub>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img width="100%" alt="LibreFang agent running alongside its UI dashboard" src="https://github.com/user-attachments/assets/8b30e4ab-aeeb-486b-87c3-16c912c966cf" />
      <br/><sub><b>LibreFang agent</b> — agent + its UI dashboard in split-pane</sub>
    </td>
    <td width="50%" valign="top" align="center">
      <img height="300" alt="Multi-tenant controller dashboard" src="https://github.com/user-attachments/assets/c67b5c2f-aafa-430f-9456-e909800910d9" />
      <br/><sub><b>Controller dashboard</b> — cluster-wide multi-tenant management</sub>
    </td>
  </tr>
</table>

---

## What's in a Workspace

| Surface | Capabilities | Access URL |
|---|---|---|
| **Dashboard SPA** | Vite + Preact app: Build sessions, Memory, Triggers, Files, Settings | `/` |
| **Terminal** | ttyd-attached tmux, accessible from any browser | `/oauth/terminal/` |
| **VS Code** | `code-server` at `/home/dev` | `/oauth/vscode/?folder=/home/dev` |
| **In-pod browser** | Chrome on an X display, viewable via noVNC | `/oauth/vnc-direct/vnc.html` |
| **Metrics + health** | Live CPU / Mem / Disk + service health | `/oauth/metrics`, `/oauth/health` |
| **Assistant** | Claude Code (default) or OpenCode | per-task |
| **Auth** | oauth2-proxy injects `X-Auth-Request-User` headers | every `/oauth/*` route |

---

## Dashboard at a Glance

The next-generation dashboard at `/` is a single-page Preact app delivering unified control over your development environment:

- **Build** — List of live + past Claude/OpenCode sessions on the left, with a detail pane on the right featuring tabs for **Terminal**, **Preview** (split: ttyd ┃ noVNC), **Send message** (chat-style mirror of the tmux pane), **Info**, and **Subagents** (hidden when empty). Live sessions default to Terminal; finished sessions hide interactive tabs and show a status banner.
- **Memory** — Persistent SQLite-backed memory + history + relations across build sessions; mirrored over MCP for the assistant to read/write.
- **Triggers** — Webhooks + cron jobs that spawn build sessions on schedule or HTTP POST.
- **Files** — Read the workspace PVC, upload files, create directories.
- **Settings** — Appearance customization, GitHub identity management, browser/VNC controls, **system metrics with real-time visualizations + alerts + service health**.

### Top Bar Interface
The dashboard includes a persistent top bar showing real-time CPU, memory, and disk usage metrics, along with quick-access buttons for VS Code and creating new terminal sessions.

### Mobile Experience

The dashboard is fully responsive. Below 720px the Rail collapses into a BottomNav (Build / Memory / Triggers / More), the detail pane moves into a swipe-able bottom sheet, and the topbar slims to just brand + search + the two primary actions (VS Code, New terminal).

Mobile users experience an optimized interface with touch-friendly controls and intuitive navigation patterns suitable for on-the-go development environment management.

## Quick Start

kube-coder runs two ways — pick the one that fits:

| | **Local (minikube)** | **Cloud / multi-tenant** |
|---|---|---|
| Best for | trying it out, dev, offline | real deployments, teams |
| Needs | Docker + minikube | a cluster, registry, DNS, GitHub OAuth |
| Auth | http basic (`admin`/`admin`) | GitHub OAuth2 (or basic) |
| TLS | none (plain HTTP, localhost) | cert-manager + Let's Encrypt |
| Guide | [Option A](#option-a--local-minikube) + [docs/local-development.md](docs/local-development.md) | [Option B](#option-b--cloud--multi-tenant) + [docs/NEW_USER_PROVISIONING.md](docs/NEW_USER_PROVISIONING.md) |

> **📖 Step-by-step walkthroughs** — follow-along, manual-style guides for each path:
> - [Getting started on a MacBook with minikube](docs/getting-started-minikube-macos.md) — clean laptop → local dashboard
> - [Deploying on Kubernetes (multi-tenant, OAuth + TLS)](docs/deploy-on-kubernetes.md) — cluster → per-user workspace

### Option A — Local (minikube)

Run the whole stack on a local single-node cluster — no cloud account, registry, DNS, or TLS.

**Prerequisites:** Docker, minikube, kubectl, helm (`brew install minikube kubectl helm` on macOS).

**One command:**

```bash
make local          # start minikube, build the image, deploy, and print access info
```

**Then reach the dashboard:**

```bash
echo '127.0.0.1  kube-coder.local' | sudo tee -a /etc/hosts   # one time
make local-forward                                            # keep running in a terminal
# open http://kube-coder.local:8080/   →   basic auth: admin / admin
```

`make local` is a wrapper for these steps, each runnable on its own (e.g. to rebuild after a change):

```bash
make local-up       # start the minikube cluster + enable the nginx ingress addon
make local-build    # build the image inside minikube (native arm64 on Apple Silicon — no emulation)
make local-secret   # create the namespace + basic-auth secret (override LOCAL_AUTH_USER/PASS)
make local-deploy   # deploy base-infrastructure + the workspace, force-rolled
make local-info     # reprint the /etc/hosts line, URL, and credentials
make local-down     # remove the workspace (DELETE=1 also deletes the minikube cluster)
```

Everything targets the minikube context explicitly, so it never touches a remote cluster. Full walkthrough, configuration, limitations, and troubleshooting: **[docs/local-development.md](docs/local-development.md)**.

### Option B — Cloud / multi-tenant

#### Prerequisites

- Kubernetes 1.19+ with `kubectl` configured
- Helm 3.0+
- An nginx-ingress controller (note its external IP)
- **Wildcard DNS** — `*.<your-domain>` pointing at that ingress IP, so every `<login>.<your-domain>` workspace resolves with no per-user DNS
- A GitHub OAuth App for the **controller console** (oauth2-proxy); per-user OAuth Apps are created during onboarding, above
- A **private GitHub repo** to hold provisioned workspace config (the GitOps store) + a token that can push to it
- A `regcred` image-pull Secret in the target namespace pointing at your registry (we use `registry.digitalocean.com/<org>/<repo>`)

#### One-time setup

```bash
make deploy-base                  # base infra: nginx-ingress, oauth2-proxy, cert-manager
make ship-controller-config       # the admin console (workspace-controller)
```

Two things make self-service onboarding work; you set them once:

- **Wildcard DNS.** Point `*.<your-domain>` (e.g. `*.dev.example.com`) at your nginx-ingress controller's external IP. Every workspace lives at `<github-login>.<your-domain>`, so the wildcard means **no per-user DNS** — a new user's host resolves the moment they're created, and **cert-manager + Let's Encrypt** issues that host's TLS certificate automatically on first request.
- **The console's own GitHub OAuth App + admin allowlist.** Gates the dashboard at `controller.<your-domain>`; only the GitHub logins you allowlist may administer workspaces. One-time controller config is in **[docs/PROVISIONING.md](docs/PROVISIONING.md)**.

#### Onboard a user — the 2-minute dashboard flow

> **Scenario.** Dana (`@dana-codes`) joins the team Monday morning and needs a full cloud workspace before standup. Here's the whole flow.

1. **Open the console** at `https://controller.<your-domain>` → click **New workspace**.
2. **Look up the user.** Type `dana-codes` and hit **Look up**. The controller confirms it's a real GitHub account and shows the host it will create: `dana-codes.<your-domain>`.
3. **Create a GitHub OAuth App** — the one manual step, because GitHub has no API to create these. The form shows the exact values; in GitHub → *Settings → Developer settings → **OAuth Apps** → New OAuth App*:
   - **Homepage URL:** `https://dana-codes.<your-domain>`
   - **Authorization callback URL:** `https://dana-codes.<your-domain>/oauth2/callback`
   - **Register application** → **Generate a new client secret**.
   > ⚠️ It must be an **OAuth App** (Client ID starts with `Ov…`) — *not* a GitHub App (`Iv…`), which silently 404s the login.
4. **Paste & create.** Drop the **Client ID** and **Client Secret** into the form → **Create workspace**. The controller commits Dana's config to your private GitOps repo and launches a provisioner Job; the page streams the rollout live: *Starting provisioner → Deploying workspace → **Workspace ready***.
5. **Hand it off.** Click **Open dana-codes ↗** to confirm, then send Dana her URL: `https://dana-codes.<your-domain>`. She signs in with her own GitHub account — she, and only she, is on this workspace's allowlist — and lands in VS Code, a persistent terminal, and Claude Code. Elapsed time: a couple of minutes, most of it the image pull.

**What happened when you clicked Create:** the controller validated the GitHub user, rendered a `values.yaml` + secret and committed them to the GitOps repo (the single source of truth), then ran a one-shot privileged Job that `helm upgrade`s the workspace chart — pod, Service, ingress, PVC, and a dedicated oauth2-proxy pinned to Dana's login. Wildcard DNS already resolves her host; cert-manager mints the TLS cert on first hit. Nothing else to wire.

#### Manage a user — request limits, updates, lifecycle

All per-workspace, in the same console:

- **Right-size their resources.** Open a workspace → **Edit limits** → set CPU / memory (e.g. bump Dana to `4` cores / `8Gi` for a heavy build). Applying it patches the live pod **and commits the new limits back to the GitOps repo**, so the change is durable across redeploys instead of being silently reverted on the next reconcile. (It restarts the pod, so it warns you first.) Live CPU/mem/disk usage and an estimated monthly cost sit right above the control.
- **Keep them current.** Each workspace shows its running release and an **update** action that rolls it to the latest version — the new image tag is written back to GitOps too. Users can also self-update from their own dashboard.
- **Pause to save spend.** **Stop** scales the pod to zero (compute freed, PVC + data preserved); **Start** brings it back unchanged.

#### Automation / break-glass: the CLI

The same workspaces are fully operable from the Makefile — for scripting, CI, or when the console is unavailable. Config lives in the GitOps repo; `make users-sync` checks it out locally so the CLI and the console share one source of truth:

```bash
make users-sync                   # pull the GitOps config store into .users/
make new-user      USER=<name>    # scaffold a workspace (prints the OAuth-App checklist + a cookie secret)
make validate-user USER=<name>    # placeholder / DNS / cluster-prereq sanity check
make deploy        USER=<name>    # helm upgrade --install
make logs|shell|test USER=<name>  # operate a running workspace
make stop|start    USER=<name>    # scale to zero / back
```

Full CLI walkthrough: **[docs/NEW_USER_PROVISIONING.md](docs/NEW_USER_PROVISIONING.md)**.

---

## Common Commands

```bash
# Docker image
make build                        # build for amd64 (loads into local Docker)
make push                         # build + push (single buildx invocation)
make clean                        # remove the local image tag

# Per-user lifecycle
make deploy   USER=<name>         # helm upgrade --install
make ship     USER=<name>         # build + push + roll the pod
make rollback USER=<name>         # helm rollback to previous revision
make logs     USER=<name>         # tail pod logs
make shell    USER=<name>         # exec into the IDE container
make test     USER=<name>         # node/yarn/gh/code-server version check

# Dashboard SPA
make dashboard-web                # type-check + vite build → web/dist
make dashboard-web-test           # vitest unit tests (~50 tests)
make dashboard-web-install        # yarn install only
make dashboard-web-clean          # rm -rf dist + node_modules

# Tests across the repo
make test-all-units               # SPA (vitest) + server.py (unittest)
make python-tests                 # server.py only
make test-coverage                # Run tests with terminal coverage summary
make coverage                     # Generate comprehensive HTML coverage reports

# Cluster status
make status                       # helm + pod status
```

`make help` (or just `make`) lists everything with one-line descriptions.

---

## Build Sessions (Claude / OpenCode)

Each **build session** is an interactive Claude Code or OpenCode tmux session inside the workspace pod. Sessions are created via the dashboard ("New build") or the JSON API and survive pod restarts; output is mirrored to a log file under `~/.kube-coder/tasks/<task_id>/`.

The dashboard's **New build** flow is intentionally minimal — pick an assistant + working directory, give the session a memorable random name (e.g. `funny-kitty-37`), hit **Start build**, and you land directly in the live terminal. No prompt textarea: type your first prompt inside the session, the way you would in any REPL.

### API

```bash
# POST /api/claude/tasks  (oauth2 headers OR Authorization: Bearer <token>)
curl -s https://<user>.dev.example.com/oauth/api/claude/tasks \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "review this PR", "assistant": "claude"}'
```

| Endpoint | Purpose |
|---|---|
| `POST /api/claude/tasks` | Create a build (prompt optional) |
| `POST /api/claude/tasks/terminal` | Register a bare-bash session |
| `GET  /api/claude/tasks` | List sessions |
| `GET  /api/claude/tasks/{id}` | Detail |
| `GET  /api/claude/tasks/{id}/output` | Tail the tmux pane |
| `GET  /api/claude/tasks/{id}/stream` | SSE live stream |
| `POST /api/claude/tasks/{id}/message` | Send a follow-up prompt |
| `POST /api/claude/tasks/{id}/prepare-terminal` | Wire ttyd to this session |
| `POST /api/claude/tasks/{id}/rename` | Rename (display only) |
| `DELETE /api/claude/tasks/{id}` | Kill the tmux session |

### `/remote-task` Skill (Claude Code)

```bash
/remote-task --workspace imran --prompt "investigate flaky test in foo_test.py"
```

Sends the prompt to a remote workspace's `/api/claude/tasks` endpoint and streams the result back. Used to dispatch work to a stronger workspace from a lighter local one.

---

## Persistent Memory

Each workspace has a SQLite-backed memory store accessible via:

- **Dashboard** → Memory route (CRUD with history + relations)
- **MCP server** auto-spawned by Claude/OpenCode (read + write from inside the assistant)
- **REST** at `/api/memory`

Claude reads memory on demand through the memory MCP tools. Optional pre-injection (`KC_MEMORY_PREINJECT=1`, off by default) prefixes a new build's prompt with the top relevant records — a similarity search over namespace+key+value+tags — inside a `<workspace_memories>` block.

<!-- TODO: screenshot of Memory route with history tab open -->

---

## Triggers — Webhooks, Crons, Completion Hooks

Three ways to dispatch a build session without clicking "New build":

1. **Completion hooks** — Register a webhook URL on a session that fires when the assistant finishes (status, output URL, summary).
2. **Webhooks** — Accept inbound POSTs; convert the body to a build prompt using a template.
3. **Crons** — UNIX cron expressions that POST to a webhook on schedule.

All three live under `Triggers` in the dashboard. Mutual references between tasks and triggers are tracked in the memory store so you can see what fired what.

<!-- TODO: screenshot of Triggers route with webhook + cron example -->

---

## Pluggable AI Assistants

Every build session — and every orchestrator sub-agent — picks its assistant at create-time, so you can mix them freely in a single workspace. Keys live in `users-private/<name>/secrets/assistant.yaml` (gitignored); the public-repo defaults are empty, so it ships Claude-only out of the box.

| Assistant | Backend | Configure with |
|---|---|---|
| **Claude Code** (default) | Anthropic API key or subscription login | `claude.apiKey`, or `make shell USER=<name>` → `claude` to log in once |
| **Ante** | Antigma's terminal-native agent; defaults to **DeepSeek v3.2 via OpenRouter** | `assistant.openrouter.apiKey` (CLI pre-installed — no separate key) |
| **Google Gemini** | Google's open-source `gemini` CLI against the native Gemini API (default `gemini-2.5-pro`) | `assistant.gemini.apiKey` (+ optional `model`) |
| **OpenCode → OpenRouter** | any OpenRouter model (default `anthropic/claude-sonnet-4`) | `assistant.openrouter.apiKey` + `model` |
| **OpenCode → DeepSeek** | DeepSeek native API (`deepseek-chat` / `deepseek-reasoner`) | `assistant.deepseek.apiKey` |
| **LibreFang** | open-source agent OS; reuses whatever provider keys are set | `assistant.librefang.agent` |

(A narrow in-pod `kc-harness` against a local/fallback model is also available for advanced setups.)

### A "Claude-like" agent without an Anthropic key — Ante + DeepSeek

Pair the **[Ante](https://ante.run/)** CLI — a terminal-native, tool-using coding agent pre-installed in every workspace — with **DeepSeek** and you get a Claude-Code-style experience: autonomous multi-step edits, shell/file tools, and the **same MCP memory + orchestrator servers** Claude uses, at a fraction of the cost.

- Set **`OPENROUTER_API_KEY`** (`assistant.openrouter.apiKey`) and Ante automatically defaults to **`deepseek/deepseek-v3.2`** (~$0.23 / $0.34 per 1M input/output tokens). Override per workspace with `KC_ANTE_MODEL`.
- It's fast and cheap enough to be the **default background sub-agent** in the orchestrator, and in practice handles real coding tasks well — a solid daily driver when you'd rather not spend Claude tokens.
- Prefer DeepSeek's native API? Set **`DEEPSEEK_API_KEY`** (`assistant.deepseek.apiKey`) and choose the **DeepSeek** assistant (OpenCode → `deepseek-chat`).

---

## Pre-installed Stack

| Component | Version |
|---|---|
| Node.js | 20 LTS |
| `code-server` | v4.123.0 |
| Claude Code CLI | 2.1.172 |
| OpenCode CLI | 1.17.3 |
| Ante CLI | 0.preview.37 (stable channel) |
| LibreFang | v2026.6.10-beta.17 |
| ttyd | 1.7.7 |
| tmux, yarn, gh, jq, ripgrep, fzf | latest from Ubuntu |

Bump versions in `devlaptop/Dockerfile` and run `make push` to rebuild.

---

## Testing & Code Quality

kube-coder includes comprehensive test suites for both frontend and backend components with detailed coverage reporting.

### Test Coverage

| Component | Coverage | Framework |
|---|---|---|
| **Frontend (Dashboard)** | 41.6% | Vitest + @testing-library |
| **Backend (Python API)** | 74% | unittest + coverage.py |
| **Overall** | 60% | Statement-weighted average |

### Running Tests

```bash
# Run all unit tests (SPA + Python)
make test-all-units

# Run tests with coverage reports
make coverage

# Quick terminal coverage summary
make test-coverage

# Frontend tests only
make dashboard-web-test

# Python tests only  
make python-tests
```

### Coverage Reports

Detailed HTML coverage reports are generated:

- **Frontend**: `charts/workspace/web/coverage/index.html`
- **Backend**: `charts/workspace/htmlcov/index.html`

Run `make coverage` to generate comprehensive reports with overall coverage calculation.

### Test Structure

- **Frontend**: 50+ Vitest unit tests covering React components, state management, and API integration
- **Backend**: 180+ Python unit tests covering API endpoints, business logic, and integration scenarios
- **CI Integration**: All tests run automatically on GitHub Actions

---

## Repository Layout

```
charts/
├── base-infrastructure/   # ingress, oauth2-proxy, cert-manager bits
└── workspace/             # per-user workspace chart
    ├── server.py          # API + dashboard backend (tmux, memory, metrics)
    └── web/               # Vite + Preact SPA (the dashboard at /)
        ├── src/
        │   ├── routes/    # /tasks, /memory, /triggers, /files, /settings
        │   ├── components/  # Topbar, Rail, BottomSheet, Drawer, MetricsBar, …
        │   ├── store/     # signals: tasks, ui, metrics, router
        │   └── api/       # typed fetch wrappers (client.ts, tasks.ts, metrics.ts)
        ├── scripts/shoot.mjs   # playwright screenshots
        └── package.json   # node 20, yarn 1.22.x
deployments/               # public sample per-user values.yaml + secrets
users-private/             # gitignored; the _controller bootstrap config lives here
.users/                    # gitignored checkout of the GitOps store (make users-sync)
                           #   ← provisioned workspace config (values + secrets) lives here
devlaptop/Dockerfile       # workspace image (Vite SPA baked into /opt/dashboard-dist)
secrets/                   # template + per-user secret YAMLs
Makefile                   # all common commands (`make help`)
```

---

## Architecture

```
   ┌──── browser ────┐
   │                 │ ───► oauth2-proxy ───► nginx-ingress ───► ws-<user> Service
   └────────────────┘                                                  │
                                                                       ▼
                                                        ┌── ws-<user> Pod ──┐
                                                        │  server.py (8080) │
                                                        │  code-server      │
                                                        │  ttyd  (7681)     │
                                                        │  novnc (6081)     │
                                                        │  Chrome + Xvfb    │
                                                        │  tmux sessions    │
                                                        └───────────────────┘
```

Per-user PVC mounted at `/home/dev` survives pod restarts; tmux sessions attached to it survive too, so an in-flight Claude build keeps running even if the dashboard tab is closed.

---

## Development

```bash
# Run the SPA locally against a built dist with auth bypassed
make dashboard-web
DASHBOARD_DIST_DIR=$(pwd)/charts/workspace/web/dist \
  python3 charts/workspace/web/dev_server.py
# → http://127.0.0.1:7070

# Tests
make test-all-units
make coverage           # Generate comprehensive coverage reports
```

Pull requests welcome — please run `make test-all-units` and ensure adequate test coverage before opening a PR. Use `make coverage` to verify coverage thresholds are met.

---

## Dependencies and Acknowledgments

kube-coder builds upon the shoulders of remarkable open-source projects and services that make modern development environments possible:

**Core Infrastructure & Orchestration**
- **[Kubernetes](https://kubernetes.io)** and **[Helm](https://helm.sh)** for enterprise-grade container orchestration and deployment
- **[NGINX Ingress Controller](https://kubernetes.github.io/ingress-nginx/)** for sophisticated routing and traffic management

**Development Environments**
- **[VS Code / code-server](https://github.com/coder/code-server)** for a full-featured browser-based IDE experience
- **[tmux](https://github.com/tmux/tmux)** for persistent terminal sessions and multiplexing
- **[ttyd](https://github.com/tsl0922/ttyd)** for browser-based terminal access
- **[noVNC](https://novnc.com/)** and **[Xvfb](https://www.x.org/releases/X11R7.6/doc/man/man1/Xvfb.1.xhtml)** for in-pod browser virtualization

**AI-Powered Development Assistants**
- **[Claude Code](https://code.anthropic.com/)** for state-of-the-art AI pair programming
- **[OpenCode](https://opencode.ai/)** for flexible, open-source compatible AI assistance
- **[Ante](https://antigma.ai/)** for advanced terminal-based AI interactions

**Security & Authentication**
- **[oauth2-proxy](https://github.com/oauth2-proxy/oauth2-proxy)** for robust OAuth2 authentication with GitHub integration

**Dashboard & User Interface**
- **[Preact](https://preactjs.com/)** and **[Vite](https://vitejs.dev/)** for lightning-fast, modern dashboard development
- **[Playwright](https://playwright.dev/)** for comprehensive testing and automation

**Utility & Tooling**
- **Ubuntu** base system with latest versions of `yarn`, `gh`, `jq`, `ripgrep`, `fzf`, and other essential developer tools


## Contact & Demo Requests

Interested in a demonstration, enterprise deployment, or custom integration? Our team is ready to help you transform your development workflow.

**Professional Inquiries:** scalebaseio@gmail.com

## New provisioning video

https://github.com/user-attachments/assets/d9e6c19c-28ab-4f5e-963a-08b1d0a7085a

## Marketing Video

https://github.com/user-attachments/assets/1e4d1bd5-ec9c-4f4e-88ba-7c2b79593a4c

## Demo Video

https://github.com/user-attachments/assets/f5821e5c-a834-4db2-a34d-2d405c3daef2





---

## License

MIT — see [LICENSE](LICENSE).
