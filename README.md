# kube-coder

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Helm](https://img.shields.io/badge/Helm-3.0%2B-blue?logo=helm)](https://helm.sh)
[![Kubernetes](https://img.shields.io/badge/Kubernetes-1.19%2B-326CE5?logo=kubernetes&logoColor=white)](https://kubernetes.io)
[![CI](https://github.com/imran31415/kube-coder/actions/workflows/ci.yml/badge.svg)](https://github.com/imran31415/kube-coder/actions/workflows/ci.yml)

Per-user development workspaces on Kubernetes. Each pod packages **VS Code** in
the browser, a **persistent tmux terminal**, a **Vite + Preact dashboard**, an
**in-pod browser with noVNC**, and an interactive **Claude Code / OpenCode**
assistant — all behind **GitHub OAuth2** at a per-user subdomain.

> Built so a single Helm chart can host as many independent IDE pods as your
> cluster has room for: separate namespace, ingress, persistent volume, and
> assistant config per user.

[Public demo and Docs site
](https://demo-public.dev.scalebase.io/docs/getting-started)

<img width="1377" height="861" alt="image" src="https://github.com/user-attachments/assets/40b36ba0-1673-448b-b4c3-f529a3042a37" />


---

## What's in a workspace

| Surface | What you get | URL |
|---|---|---|
| **Dashboard SPA** | Vite + Preact app: Build sessions, Memory, Triggers, Files, Settings | `/` |
| **Terminal** | ttyd-attached tmux, accessible from any browser | `/oauth/terminal/` |
| **VS Code** | `code-server` at `/home/dev` | `/oauth/vscode/?folder=/home/dev` |
| **In-pod browser** | Chrome on an X display, viewable via noVNC | `/oauth/vnc-direct/vnc.html` |
| **Metrics + health** | Live CPU / Mem / Disk + service health | `/oauth/metrics`, `/oauth/health` |
| **Assistant** | Claude Code (default) or OpenCode | per-task |
| **Auth** | oauth2-proxy injects `X-Auth-Request-User` headers | every `/oauth/*` route |

<!-- TODO: screenshot of the topbar showing CPU/MEM/DSK pills + VS Code / New terminal buttons -->

---

## Dashboard at a glance

The next-generation dashboard at `/` is a single-page Preact app. Key surfaces:

- **Build** — list of live + past Claude/OpenCode sessions on the left, a detail
  pane on the right with tabs for **Terminal**, **Preview** (split: ttyd ┃
  noVNC), **Send message** (chat-style mirror of the tmux pane), **Info**,
  and **Subagents** (hidden when empty). Live sessions default to Terminal;
  finished sessions hide interactive tabs and show a status banner.
- **Memory** — persistent SQLite-backed memory + history + relations across
  build sessions; mirrored over MCP for the assistant to read/write.
- **Triggers** — webhooks + cron jobs that spawn build sessions on schedule
  or HTTP POST.
- **Files** — read the workspace PVC, upload, mkdir.
- **Settings** — appearance, GitHub identity, browser/VNC controls, **system
  metrics with bars + alerts + service health**.

<!-- TODO: side-by-side screenshots: Build list + Terminal tab, Preview split, Send message chat -->

### Mobile

The dashboard is fully responsive. Below 720px the Rail collapses into a
BottomNav (Build / Memory / Triggers / More), the detail pane moves into a
swipe-able bottom sheet, and the topbar slims to just brand + search + the
two primary actions (VS Code, New terminal).

<!-- TODO: mobile screenshot of Build list + BottomSheet with task detail -->

---

## Quick start

### Prerequisites

- Kubernetes 1.19+ with `kubectl` configured
- Helm 3.0+
- An nginx-ingress controller
- A GitHub OAuth App (for oauth2-proxy)
- A `regcred` image-pull Secret in the target namespace pointing at your
  registry (we use `registry.digitalocean.com/<org>/<repo>`)

### One-time: base infrastructure

```bash
make deploy-base                  # base-infrastructure helm release
```

### Onboard a user

```bash
# Scaffold a private workspace under users-private/<name>/ — generates
# values.yaml, mints an OAuth2 cookie secret, and prints a checklist of
# the manual fields you still need to fill in (DNS host, GitHub App
# creds, optional assistant keys). See NEW_USER_PROVISIONING.md.
make new-user USER=<name>
$EDITOR users-private/<name>/values.yaml

# Pre-deploy sanity check (DNS, image pull, base release)
make validate-user USER=<name>

# Deploy the workspace
make deploy USER=<name>

# Tail logs / shell in / sanity-test
make logs   USER=<name>
make shell  USER=<name>
make test   USER=<name>
```

The script `setup.sh` walks first-time users through GitHub OAuth, DNS, and
Claude credentials interactively for the basic-auth flow (older path).

---

## Common commands

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

# Cluster status
make status                       # helm + pod status
```

`make help` (or just `make`) lists everything with one-line descriptions.

---

## Build sessions (Claude / OpenCode)

Each **build session** is an interactive Claude Code or OpenCode tmux
session inside the workspace pod. Sessions are created via the dashboard
("New build") or the JSON API and survive pod restarts; output is mirrored
to a log file under `~/.kube-coder/tasks/<task_id>/`.

The dashboard's **New build** flow is intentionally minimal — pick an
assistant + working directory, give the session a memorable random name
(e.g. `funny-kitty-37`), hit **Start build**, and you land directly in the
live terminal. No prompt textarea: type your first prompt inside the
session, the way you would in any REPL.

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

Sends the prompt to a remote workspace's `/api/claude/tasks` endpoint and
streams the result back. Used to dispatch work to a stronger workspace from
a lighter local one.

---

## Persistent memory

Each workspace has a SQLite-backed memory store accessible via:

- **Dashboard** → Memory route (CRUD with history + relations)
- **MCP server** auto-spawned by Claude/OpenCode (read + write from inside
  the assistant)
- **REST** at `/api/memory`

Memory records are auto-injected into new build prompts when relevant — the
server runs a similarity search over namespace+key+value tags and prepends
the top matches inside a `<workspace_memories>` block.

<!-- TODO: screenshot of Memory route with history tab open -->

---

## Triggers — webhooks, crons, completion hooks

Three ways to dispatch a build session without clicking "New build":

1. **Completion hooks** — register a webhook URL on a session that fires
   when the assistant finishes (status, output URL, summary).
2. **Webhooks** — accept inbound POSTs; convert the body to a build prompt
   using a template.
3. **Crons** — UNIX cron expressions that POST to a webhook on schedule.

All three live under `Triggers` in the dashboard. Mutual references between
tasks and triggers are tracked in the memory store so you can see what
fired what.

<!-- TODO: screenshot of Triggers route with webhook + cron example -->

---

## Pluggable AI assistants

Configure per-workspace via `assistant.provider` in values.yaml:

- **`claude`** (default) — Anthropic Claude Code with your API key
- **`opencode`** — OpenRouter or any OpenAI-compatible base URL via on-disk
  config written at pod start

Each build session picks its assistant at create-time; you can mix Claude
and OpenCode sessions in the same workspace.

---

## Pre-installed stack

| Component | Version |
|---|---|
| Node.js | 20 LTS |
| `code-server` | 4.95.3 |
| Claude Code CLI | 2.1.143 |
| OpenCode CLI | 1.15.3 |
| ttyd | 1.7.7 |
| tmux, yarn, gh, jq, ripgrep, fzf | latest from Ubuntu |

Bump versions in `devlaptop/Dockerfile` and run `make push` to rebuild.

---

## Repository layout

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
deployments/               # public per-user values.yaml + secrets
users-private/             # gitignored per-user values.yaml + secrets
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

Per-user PVC mounted at `/home/dev` survives pod restarts; tmux sessions
attached to it survive too, so an in-flight Claude build keeps running even
if the dashboard tab is closed.

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
```

Pull requests welcome — please run `make dashboard-web-test` and
`make python-tests` before opening a PR.

---

## License

MIT — see [LICENSE](LICENSE).
