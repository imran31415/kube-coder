# Kube-Coder

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Helm](https://img.shields.io/badge/Helm-3.0%2B-blue?logo=helm)](https://helm.sh)
[![Kubernetes](https://img.shields.io/badge/Kubernetes-1.19%2B-326CE5?logo=kubernetes&logoColor=white)](https://kubernetes.io)
[![CI](https://github.com/imran31415/kube-coder/actions/workflows/ci.yml/badge.svg)](https://github.com/imran31415/kube-coder/actions/workflows/ci.yml)

A Helm chart for deploying secure, isolated development workspaces in Kubernetes. Each workspace provides VS Code IDE, terminal access, remote browser capabilities, and AI-powered Claude Code integration with remote task management -- all protected by GitHub OAuth2 authentication.

<table>
  <tr>
    <td align="center" width="25%">
      <img width="1509" height="908" alt="image" src="https://github.com/user-attachments/assets/6f871a1c-cc5f-43f2-8574-f7cfa33b67f6" />
      <strong>Dashboard</strong><br/>
      <sub>Clean workspace hub with system metrics, service health, and GitHub config</sub>
    </td>
    <td align="center" width="25%">
      <img src="https://github.com/user-attachments/assets/34f1b356-22be-40da-bc99-c0a8a2a205a2" width="250" alt="Remote Browser" /><br/>
      <strong>VS Code IDE</strong><br/>
      <sub>Full-featured browser IDE with extensions and terminal access</sub>
    </td>
    <td align="center" width="25%">
      <img src="https://github.com/user-attachments/assets/dcf81a8a-da6a-42c9-a738-c331cc8aa36d" width="250" alt="VS Code IDE" /><br/>
      <strong>Remote Browser</strong><br/>
      <sub>Full Firefox browser via VNC — test web apps from anywhere</sub>
    </td>
    <td align="center" width="25%">
      <img width="707" height="690" alt="TTYD with claude" src="https://github.com/user-attachments/assets/7c3aa084-c52f-4b17-b3b9-059390eeddf1" />
      <strong>TTYD terminal interface accessible from browser</strong><br/>
      <sub>Claude built in with environment awareness </sub>
    </td>

  </tr>
</table>

## Update 5/10: Updated UX and Cron Job/Webhook adapters

- Updated UI/UX for the dashboard.
- Webhook adapters
- Cron Job adapters
- Overall improved UX especially on mobile.

<img width="1512" height="862" alt="image" src="https://github.com/user-attachments/assets/309f350c-0545-42c8-a78f-9ed254cfb765" />




[![L

## Features

### Core Development Environment
- **VS Code IDE** - Browser-based IDE with extensions support
- **Terminal** - Full system access via browser (ttyd)
- **Remote Browser** - Firefox with VNC viewer for testing web apps
- **System Monitoring** - Real-time CPU, memory, disk usage dashboard
- **GitHub Integration** - Easy SSH key and git config setup from dashboard

### Claude Code Integration
- **Claude Code CLI** - AI-powered development assistant built-in
- **Claude Task API** - Launch and manage Claude tasks remotely via REST API
- **Claude Tasks Dashboard** - Monitor running tasks, view status, one-click attach to interactive sessions
- **Remote Task Skill** - `/remote-task` Claude Code skill to manage tasks from your local terminal
- **Interactive Sessions** - Attach to any running Claude session to approve permissions, provide input, or observe progress
- **Completion hooks** - Tasks can `POST` their final state to a `response_url` with optional HMAC signing
- **Webhooks** - Inbound HTTP triggers that spawn Claude tasks; native verifiers for GitHub, Slack, Stripe, plus a generic mode
- **Crons** - Scheduled triggers backed by real Kubernetes `CronJob` objects, with suspend/resume/run-now and token rotation from the dashboard

### Security & Authentication
- **GitHub OAuth2** - Secure authentication with configurable user authorization
- **GitHub App Auth** - Automatic private repo access via GitHub App installation tokens (auto-refreshed every 50 minutes)
- **HTTPS Everywhere** - Let's Encrypt certificates with automatic renewal
- **Isolated Workspaces** - Complete isolation between user environments

### Development Stack
- **Node.js 20 + Yarn** - Latest Node.js with Yarn package manager
- **Container Builds** - Docker-in-Docker with BuildKit support
- **Persistent Storage** - Dedicated storage that survives restarts

## Architecture

```
┌─────────────────────────────────────────────────┐
│             Base Infrastructure                 │
│  • Shared ConfigMaps (kaniko-wrapper, etc.)     │
└─────────────────────────────────────────────────┘
                        │
            ┌───────────┴───────────┐
            │                       │
┌───────────▼──────────┐  ┌─────────▼──────────────┐
│   Imran Workspace    │  │   Gerard Workspace     │
│  • Independent Helm  │  │  • Independent Helm    │
│  • Own PVC & secrets │  │  • Own PVC & secrets   │
│  • Dedicated ingress │  │  • Dedicated ingress   │
│  • Claude Task API   │  │  • Claude Task API     │
└──────────────────────┘  └────────────────────────┘
```

## Quick Start

### Prerequisites
- Kubernetes cluster (1.19+)
- Helm 3.0+
- nginx ingress controller
- cert-manager for HTTPS
- GitHub OAuth App

### Deploy

```bash
# Create namespace
kubectl create namespace coder

# Deploy base infrastructure
make deploy-base

# Deploy a workspace (auto-includes secrets if present)
make deploy-imran
```

### Access (OAuth2)

- **Dashboard**: `https://username.yourdomain.com/oauth`
- **VS Code**: `https://username.yourdomain.com/oauth/ide`
- **Terminal**: `https://username.yourdomain.com/oauth/terminal`

## Claude Task API

Each workspace exposes a REST API for remotely launching and managing Claude Code tasks. Tasks run as interactive tmux sessions that users can attach to for approving permissions and providing input.

See [docs/claude-task-api.md](./docs/claude-task-api.md) for full API documentation.

### Quick Example

```bash
# Create a task
curl -X POST https://imran.dev.archon.cx/api/claude/tasks \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Analyze the codebase and create a CLAUDE.md"}'

# Check status
curl https://imran.dev.archon.cx/api/claude/tasks \
  -H "Authorization: Bearer $TOKEN"
```

### `/remote-task` Skill

When working in this repo with Claude Code, use the `/remote-task` skill:

```bash
/remote-task analyze the codebase and create a CLAUDE.md   # Launch task
/remote-task status                                         # List all tasks
/remote-task output <TASK_ID>                              # View output
/remote-task attach <TASK_ID>                              # Attach info
/remote-task kill <TASK_ID>                                # Kill task
```

## Triggers: Webhooks + Crons + Completion Hooks

Once a workspace is deployed, you can drive it from outside via three composable
primitives — all backed by the same `ClaudeTaskManager` and visible on the
dashboard.

### 1. Completion hooks (foundational)

Every `POST /api/claude/tasks` accepts two optional fields:

| Field | Purpose |
|---|---|
| `response_url` | Where to `POST` the task's final state when it reaches `completed` / `error` / `killed`. Must be `http(s)`. |
| `response_secret` | Optional HMAC-SHA256 key. If set, requests carry `X-Kube-Coder-Signature-256: sha256=<hex>` over the body. |

```bash
curl -X POST https://$WORKSPACE/api/claude/tasks \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Run the test suite and report failures",
    "response_url": "https://hooks.slack.com/services/...",
    "response_secret": "shared-secret"
  }'
```

When the task ends, the workspace POSTs `{task_id, status, prompt, workdir,
source, output (tail 200 lines), ...}` to your URL. Delivery is at-most-once
(`hook_fired_at` is set under a meta lock before firing); on network failure
the error is logged and not retried — layer your own queue if you need
at-least-once.

### 2. Webhooks (inbound triggers)

A webhook is a config that turns an inbound `POST` into a Claude task. Each
webhook gets a stable URL: `https://$WORKSPACE/api/webhooks/<id>`. Configure
them from the **Webhooks** panel on the dashboard or via the API:

```bash
curl -X POST https://$WORKSPACE/oauth/api/webhooks \
  -H "Content-Type: application/json" \
  -d '{
    "id": "github-pr-review",
    "provider": "github",
    "prompt_template": "Review the PR at {{ payload.pull_request.html_url }}.",
    "workdir": "/home/dev/myproject",
    "interpolate_mode": "interpolate"
  }'
```

The server auto-mints an `hmac_secret` and **returns it exactly once** in
the response (or via the dashboard's "Save this secret now" banner). Paste it
into GitHub's webhook config alongside the URL.

#### Providers

| `provider` | Signature header | Format | Notes |
|---|---|---|---|
| `github` (default) | `X-Hub-Signature-256` | `sha256=<hex>` of body | Works out of the box with GitHub repo/org webhooks. |
| `slack` | `X-Slack-Signature` + `X-Slack-Request-Timestamp` | `v0=<hex>` of `v0:<ts>:<body>` | Timestamp must be within ±5 min of pod clock. |
| `stripe` | `Stripe-Signature` | `t=<unix>,v1=<hex>` of `<ts>.<body>` | Accepts multiple `v1=` entries (for Stripe's key-rotation window). Timestamp within ±5 min. |
| `generic` | `X-Hub-Signature-256` (or `signature_header` you set) | `sha256=<hex>` or bare hex of body | Anything that signs the body with HMAC-SHA256. |

All verifiers use `hmac.compare_digest` (constant-time) and reject when the
secret isn't set the way the provider expects.

#### Prompt templates: `attach` vs `interpolate`

Templates use `{{ payload.path.to.field }}` syntax. The mode controls what
happens to the payload:

- **`attach` (default, safe)** — the template is the literal instruction; the
  full payload is appended as a fenced ```` ```json ```` block. Sender-controlled
  data can't drive Claude because it appears as data, not instructions.
- **`interpolate`** — `{{ payload.x.y }}` is substituted with the matching JSON
  value. Pick this when you trust the sender (GitHub for your own repos, your
  own internal service, etc.). Hostile values land verbatim in the prompt.

#### Replay protection

The receiver maintains an in-memory `(webhook_id, sha256(body))` cache with a
5-minute TTL. A second request with the same body inside the window returns
**409 Conflict**. Combined with the per-provider timestamp checks (Slack /
Stripe), this closes both offline-replay and intra-window replay.

#### Testing without an external sender

Each webhook has a **Test fire** button on the dashboard (and `POST
/oauth/api/webhooks/<id>/test` with `{"payload": {...}}` over OAuth/bearer
auth). The test path bypasses HMAC verification — same effect as if a real
signed request had arrived.

### 3. Crons (scheduled triggers)

A cron is a config plus a real Kubernetes `CronJob` named
`cron-<user>-<id>`. The CronJob's container is just `curlimages/curl`
POSTing to the workspace's internal service URL — the IDE pod is still the
executor. Schedules use standard cron syntax (`0 9 * * *`) or `@daily` /
`@hourly` / etc.

```bash
curl -X POST https://$WORKSPACE/oauth/api/crons \
  -H "Content-Type: application/json" \
  -d '{
    "id": "daily-status",
    "schedule": "0 9 * * 1-5",
    "timezone": "America/Los_Angeles",
    "prompt_template": "Summarize yesterdays git commits across all repos in /home/dev",
    "workdir": "/home/dev"
  }'
```

The cron's `payload` field plays the same role as a webhook's inbound payload
— you can pre-populate static data the template uses:

```json
{
  "id": "weekly-deps",
  "schedule": "0 9 * * 1",
  "prompt_template": "Audit dependencies in {{ payload.repos }} for CVEs",
  "payload": {"repos": "/home/dev/api,/home/dev/web"}
}
```

#### Managing a cron

| Action | Endpoint | Dashboard |
|---|---|---|
| Pause | `POST /api/crons/<id>/suspend` | **Suspend** button |
| Resume | `POST /api/crons/<id>/resume` | **Resume** button |
| Fire manually | `POST /api/crons/<id>/run` | **Run now** button |
| Rotate the fire token | `POST /api/crons/<id>/rotate-token` | **Rotate token** button |
| Delete (cleans up CronJob + Secret) | `DELETE /api/crons/<id>` | **Delete** button |

Suspend flips `spec.suspend: true` on the CronJob — `kubectl get cronjobs -n
coder` shows it. Run now does `kubectl create job --from=cronjob/<name>`.

#### Token rotation

Each cron has a `fire_token` stored in a Kubernetes `Secret` and mounted as an
env var into the curl pod. Rotation mints a fresh token, persists it, and
re-applies the Secret in place — in-flight pods using the old token fail
immediately (intended), and the next scheduled fire picks up the new token.
The new token is returned **exactly once** in the response.

### Triggers + tasks at a glance

```
┌────────────────────────────────────────────────────────────────┐
│  External sender (GitHub, Slack, Stripe, custom curl, k8s     │
│  CronJob pod, …)                                              │
└──────────────────────────────────┬─────────────────────────────┘
                                   │  HTTPS, body signed
                                   ▼
┌────────────────────────────────────────────────────────────────┐
│  Ingress (TLS + nginx)                                         │
│    /api/webhooks/<id>      → no OAuth (HMAC verified in pod)   │
│    /api/triggers/cron-fire → in-cluster only (bearer token)    │
│    /oauth/api/{webhooks,crons,claude/tasks}  → OAuth gate      │
└──────────────────────────────────┬─────────────────────────────┘
                                   ▼
┌────────────────────────────────────────────────────────────────┐
│  server.py (workspace pod, port 6080)                          │
│    WebhookManager       — config CRUD, HMAC verify, replay     │
│    CronManager          — CronJob+Secret apply, suspend, run   │
│    ClaudeTaskManager    — spawns tmux session, fires response  │
└──────────────────────────────────┬─────────────────────────────┘
                                   ▼
┌────────────────────────────────────────────────────────────────┐
│  tmux session  →  claude (interactive)  →  writes/commits      │
└──────────────────────────────────┬─────────────────────────────┘
                                   ▼     (if response_url set)
┌────────────────────────────────────────────────────────────────┐
│  HMAC-signed POST  to your callback URL with task output       │
└────────────────────────────────────────────────────────────────┘
```

### End-to-end example: GitHub PR → review → comment

1. Create a webhook on the workspace, provider `github`, mode `interpolate`:
   ```bash
   curl -X POST https://imran.dev.scalebase.io/oauth/api/webhooks \
     -H "Content-Type: application/json" \
     -d '{
       "id": "pr-review",
       "provider": "github",
       "prompt_template": "Review the PR at {{ payload.pull_request.html_url }} and post a comment with your findings using `gh pr comment`.",
       "interpolate_mode": "interpolate",
       "workdir": "/home/dev/myproject"
     }'
   ```
2. Copy the returned `hmac_secret_once` value.
3. In GitHub → repo Settings → Webhooks → Add webhook:
   - **Payload URL**: `https://imran.dev.scalebase.io/api/webhooks/pr-review`
   - **Content type**: `application/json`
   - **Secret**: paste the secret
   - **Events**: "Pull requests"
4. Open a PR. The workspace receives the webhook, verifies the HMAC, spawns
   a Claude task with the rendered prompt, and (because of `gh pr comment`)
   posts the review back to GitHub. Watch progress on the **Claude Tasks**
   panel of the dashboard — the task card carries a `from webhook:pr-review`
   badge.

### Configuration files (advanced)

Triggers persist as JSON on the workspace PVC at `0600`:
- `/home/dev/.claude-triggers/webhooks/<id>.json`
- `/home/dev/.claude-triggers/crons/<id>.json`

You can edit them directly if you prefer — the dashboard re-reads on every
GET. For crons, hand-editing the schedule won't re-apply the K8s CronJob; use
the API or delete+recreate.

### Security notes (brief)

- All secrets (`hmac_secret`, `response_secret`, `fire_token`) are stored
  `0600` on the per-user PVC and never appear in list-endpoint responses
  (only `*_set: true` flags do).
- The cron fire token is also stored in a Kubernetes `Secret` and mounted
  as an env var (never in `args` or the URL — `ps` and access logs stay clean).
- The cron `schedule` / `timezone` fields are strictly regex-validated before
  being interpolated into the kubectl-apply manifest.
- `response_url` is rejected unless the scheme is `http` or `https` — closes
  off `file://` / `gopher://` as SSRF / local-file primitives via `urlopen`.

Full reference: [docs/claude-task-api.md](./docs/claude-task-api.md).

## Pre-installed Stack

| Category | Tools |
|----------|-------|
| Runtime | Node.js 20.19.4, Python 3.12, Go 1.22 |
| Package Managers | Yarn 1.22.22, npm, pip |
| Build Tools | Docker CLI, docker-compose, make, gcc |
| Cloud Tools | kubectl, GitHub CLI |
| AI Assistant | Claude Code CLI |
| Utilities | curl, jq, tmux, vim, nano |

## Commands

```bash
make help             # Show all commands
make deploy-imran     # Deploy Imran's workspace
make deploy-gerard    # Deploy Gerard's workspace
make deploy-all       # Deploy everything
make status           # Check deployment status
make test-imran       # Test workspace setup
make shell-imran      # Shell into workspace
make logs-imran       # View logs
make rollback-imran   # Rollback workspace
make version          # Show versions and config
```

## Adding New Users

See [NEW_USER_PROVISIONING.md](./NEW_USER_PROVISIONING.md) for details.

```bash
# Automated
./scripts/provision-user.sh john john_doe "John Doe" john@company.com dev.company.com

# Manual
mkdir deployments/john
cp templates/user-values-template.yaml deployments/john/values.yaml
# Edit values, then deploy
```

## Configuration Reference

### Workspace Values
```yaml
# deployments/username/values.yaml
namespace: coder

user:
  name: username
  pvcSize: 50Gi
  host: username.dev.yourdomain.com
  env:
    - name: GIT_USER_NAME
      value: "User Name"
    - name: GIT_USER_EMAIL
      value: "user@domain.com"

image:
  repository: registry.digitalocean.com/resourceloop/coder
  tag: devlaptop-v1.6.2-browser-stealth
  pullPolicy: Always

oauth2:
  githubUsers: "user1,user2"  # Authorized GitHub usernames

resources:
  requests:
    cpu: "2"
    memory: 3Gi
  limits:
    cpu: "3"
    memory: 5Gi
```

### Secrets (gitignored)

```yaml
# secrets/username/claude.yaml — Anthropic API key (optional)
claude:
  apiKey: "sk-ant-api03-..."

# secrets/username/github-app.yaml — GitHub App credentials (optional)
github:
  app:
    appId: "1234567"
    installationId: "12345678"
    privateKey: |
      -----BEGIN RSA PRIVATE KEY-----
      ...
      -----END RSA PRIVATE KEY-----
```

## Project Structure

```
charts/
├── base-infrastructure/       # Shared ConfigMaps
└── workspace/                 # Workspace template
    ├── dashboard.html         # Dashboard UI with Claude Tasks section
    ├── server.py              # Python HTTP server (dashboard, APIs, task management)
    └── templates/
        ├── deployment.yaml
        ├── service.yaml
        ├── ingress.yaml
        ├── ingress-oauth2.yaml
        ├── ingress-claude-api.yaml
        ├── oauth2-proxy.yaml
        ├── browser-configmap.yaml
        ├── claude-configmap.yaml
        ├── claude-secret.yaml
        ├── terminal-entry-configmap.yaml
        ├── github-app-secret.yaml
        ├── github-app-token-refresh.yaml
        ├── pvc.yaml
        └── serviceaccount.yaml

deployments/
├── imran/values.yaml          # User-specific config
└── gerard/values.yaml

secrets/                       # Gitignored
├── imran/
│   ├── claude.yaml            # Anthropic API key
│   └── github-app.yaml        # GitHub App credentials
└── gerard/

.claude/
└── skills/
    └── remote-task/SKILL.md   # /remote-task skill for managing remote Claude tasks

docs/
├── claude-task-api.md         # Full Claude Task API documentation
└── ...
```

## Documentation

- [Claude Task API](./docs/claude-task-api.md) - REST API for remote Claude task management
- [Browser Architecture](./BROWSER_ARCHITECTURE.md) - Remote browser VNC architecture
- [New User Provisioning](./NEW_USER_PROVISIONING.md) - Adding new workspace users

## Troubleshooting

```bash
# Check pods
kubectl get pods -n coder

# Check logs
make logs-imran

# Test workspace
make test-imran

# Shell access
make shell-imran

# Certificate issues
kubectl get certificate -n coder

# Check Claude task sessions
kubectl exec -n coder <pod> -c ide -- tmux list-sessions
```

## Security Features

- **GitHub OAuth2** - Secure authentication with user authorization
- **GitHub App tokens** - Short-lived installation tokens for private repo access (no long-lived PATs)
- **TLS encryption** - All traffic encrypted with Let's Encrypt
- **Workspace isolation** - Users cannot access each other's environments
- **Non-root containers** - All processes run as uid/gid 1000
- **Isolated storage** - Dedicated PVC per user
- **Interactive permissions** - Claude Code runs with standard permission mode; users approve file writes

## License

MIT
