# Claude Task API

The Claude Task API allows remote services, scripts, and automation tools to programmatically create and manage Claude Code tasks running inside kube-coder workspace pods. Each workspace pod exposes an HTTP API that spawns **interactive** `claude` sessions in isolated tmux sessions, giving callers full access to Claude Code's capabilities -- file editing, code generation, git operations, and more. Users can attach to any running session to approve permissions, provide input, or observe progress.

## Architecture Overview

```
                                          +-----------------------------------+
                                          |  Workspace Pod                    |
  External Service / Script               |  (e.g. imran.dev.archon.cx)       |
  +-----------------------------+         |                                   |
  |  curl / HTTP client         |         |  +-----------------------------+  |
  |  Authorization: Bearer ...  | ------> |  |  Python server (port 6080)  |  |
  +-----------------------------+         |  |  BrowserHandler class       |  |
         |                                |  |  ClaudeTaskManager class    |  |
         |  HTTPS                         |  +-----------------------------+  |
         |                                |       |                           |
         v                                |       | subprocess / tmux         |
  +-----------------------------+         |       v                           |
  |  K8s Ingress (nginx)        |         |  +-----------------------------+  |
  |  /api/claude/* -> pod:6080  |         |  |  tmux session               |  |
  |  (no OAuth2 proxy)          |         |  |  claude (interactive)       |  |
  +-----------------------------+         |  |  user can attach & interact |  |
                                          |  +-----------------------------+  |
                                          |       |                           |
                                          |       | reads/writes              |
                                          |       v                           |
                                          |  /home/dev/ (workspace fs)        |
                                          |  /home/dev/.claude-tasks/         |
                                          +-----------------------------------+
```

**Key components:**

- **Workspace pods** run at `https://{user}.dev.archon.cx`, one per developer.
- **Python server** (`server.py`, port 6080) handles all Claude Task API requests inside the pod.
- **tmux sessions** provide isolated execution environments. Each task runs an interactive `claude` session in its own named tmux session (`claude-{task_id}`).
- **Interactive sessions** allow users to attach via the dashboard or `tmux attach` to approve file writes, provide input, and observe Claude working in real time.
- **Claude Code authentication** supports two methods: OAuth (uses your Claude Pro/Max subscription) or API key (uses pay-per-use API credits). See [Claude Code Authentication](#claude-code-authentication) below.

## Claude Code Authentication

This is how Claude Code itself authenticates with Anthropic (separate from the Task API auth). You must choose one method:

### Method 1: OAuth Login (Claude Pro/Max Subscription)

**Use this when:** You have a Claude Pro or Max subscription and want tasks to run against your subscription with no additional cost per API call.

**How it works:** You do a one-time interactive OAuth login inside the pod. The credentials are stored on the persistent volume (`/home/dev/.claude/`) and survive pod restarts.

**Setup:**

1. Deploy without setting `claude.apiKey` (the default):
   ```bash
   make deploy-imran
   ```

2. Shell into the pod and run Claude to trigger the OAuth flow:
   ```bash
   make shell-imran
   claude
   ```

3. Follow the device-code flow -- open the URL in your browser, paste the code, and log in with your Anthropic account.

4. Done. Credentials persist on the PVC at `/home/dev/.claude/`.

**Trade-offs:**
- No per-call API cost (included in your subscription).
- Subject to subscription rate limits.
- Requires one-time interactive setup per workspace.
- OAuth tokens may expire after extended inactivity (weeks/months) -- re-login with `make shell-{user} && claude` if this happens.

### Method 2: API Key (Pay-Per-Use Credits)

**Use this when:** You want fully automated, zero-touch authentication (e.g., CI/CD pipelines, unattended workspaces) or need higher rate limits than your subscription provides.

**How it works:** An `ANTHROPIC_API_KEY` environment variable is injected into the pod from a Kubernetes Secret. Claude Code detects it automatically -- no interactive login needed.

**Setup:**

1. Get an API key from https://console.anthropic.com/settings/keys

2. Create a gitignored secrets file at `secrets/{user}/claude.yaml`:
   ```yaml
   claude:
     apiKey: "sk-ant-api03-..."
   ```

3. Deploy (secrets files are included automatically if they exist):
   ```bash
   make deploy-imran
   ```

**Trade-offs:**
- Fully automated -- no interactive login required.
- Charges per API call against your Anthropic credit balance.
- API key must be managed securely (never commit to git).
- If `ANTHROPIC_API_KEY` is set, Claude Code always uses it (ignores any OAuth session).

### Precedence

If both methods are configured, **API key takes precedence**. To switch from API key back to OAuth, redeploy without the secrets file so the env var is removed.

## GitHub App Authentication

Workspace pods can authenticate with GitHub using a GitHub App, enabling automatic access to private repositories without SSH keys or personal access tokens.

**How it works:** The pod generates a JWT signed with the App's private key, exchanges it for a short-lived installation access token, and configures git credential helper and `GH_TOKEN`/`GITHUB_TOKEN` environment variables. Tokens are refreshed every 50 minutes automatically.

**Setup:**

1. Create a GitHub App with repository access permissions.

2. Create a gitignored secrets file at `secrets/{user}/github-app.yaml`:
   ```yaml
   github:
     app:
       appId: "1234567"
       installationId: "12345678"
       privateKey: |
         -----BEGIN RSA PRIVATE KEY-----
         ...
         -----END RSA PRIVATE KEY-----
   ```

3. Deploy (secrets files are included automatically if they exist):
   ```bash
   make deploy-imran
   ```

The token refresh daemon runs as a background process in the pod. New shells (including tmux sessions) automatically pick up the token via `.bashrc` hooks.

## Task API Authentication

This is how external services authenticate with the Task API (separate from Claude Code's Anthropic auth above).

### Bearer Token (for scripts and services)

This is the primary method for programmatic access. It bypasses the OAuth2 proxy entirely via a dedicated Kubernetes Ingress rule.

**Step 1: Obtain your token**

Visit the following URL in a browser (you must be logged in via GitHub OAuth):

```
https://{user}.dev.archon.cx/oauth/api/claude/auth/token
```

The response contains your bearer token:

```json
{
  "token": "abc123..."
}
```

The token is stored on disk inside the pod at `/home/dev/.claude-tasks/.api-token` with `0600` permissions. It is generated automatically on first access using `secrets.token_urlsafe(36)`.

**Step 2: Use the token in API calls**

Include the token in the `Authorization` header:

```
Authorization: Bearer {token}
```

**Regenerating a token**

If a token is compromised, regenerate it from a browser session:

```bash
curl -X POST https://{user}.dev.archon.cx/oauth/api/claude/auth/token/regenerate
```

This endpoint requires OAuth2 authentication (browser session with GitHub login). After regeneration, the old token is immediately invalidated.

### OAuth2 Proxy (for browser sessions)

When accessing the API through the `/oauth/api/claude/...` path, requests pass through the OAuth2 proxy. The proxy injects headers (`X-Auth-Request-User`, `X-Auth-Request-Email`) that the server accepts as proof of authentication. This is used by the web dashboard and browser-based tools.

### Ingress Routing Summary

| Path Pattern | Auth Method | Use Case |
|---|---|---|
| `/api/claude/*` | Bearer token only | Scripts, services, CI/CD, automation |
| `/oauth/api/claude/*` | OAuth2 proxy (GitHub login) | Browser-based dashboard access |

## API Reference

All endpoints are served at `https://{user}.dev.archon.cx`. Replace `{user}` with the workspace username (e.g., `imran`).

### Create a Task

**`POST /api/claude/tasks`**

Spawns a new interactive Claude Code session in an isolated tmux session.

**Request:**

```bash
curl -X POST https://{user}.dev.archon.cx/api/claude/tasks \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Create a Python script that prints hello world", "workdir": "/home/dev/myproject"}'
```

| Field | Type | Required | Description |
|---|---|---|---|
| `prompt` | string | Yes | The prompt to send to Claude Code. |
| `workdir` | string | No | Working directory for the task. Defaults to `/home/dev`. |

**Response (201 Created):**

```json
{
  "task_id": "1707000000-a1b2c3d4",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "prompt": "Create a Python script that prints hello world",
  "workdir": "/home/dev/myproject",
  "status": "running",
  "created_at": 1707000000.123,
  "tmux_session": "claude-1707000000-a1b2c3d4"
}
```

| Field | Description |
|---|---|
| `task_id` | Unique identifier for the task (`{unix_timestamp}-{random_hex}`). |
| `session_id` | UUID for the session. |
| `status` | Task status: `running`, `completed`, `error`, or `killed`. |
| `tmux_session` | Name of the tmux session running the task. Attach to this to interact with Claude. |

---

### List All Tasks

**`GET /api/claude/tasks`**

Returns a summary of all tasks, sorted by creation time (newest first).

**Request:**

```bash
curl https://{user}.dev.archon.cx/api/claude/tasks \
  -H "Authorization: Bearer $TOKEN"
```

**Response (200 OK):**

```json
{
  "tasks": [
    {
      "task_id": "1707000000-a1b2c3d4",
      "prompt": "Create a Python script that prints hello world",
      "status": "completed",
      "created_at": 1707000000.123
    },
    {
      "task_id": "1706999000-e5f6a7b8",
      "prompt": "Fix the bug in server.py where...",
      "status": "running",
      "created_at": 1706999000.456
    }
  ]
}
```

Note: The `prompt` field is truncated to 120 characters in list responses.

---

### Get Task Detail

**`GET /api/claude/tasks/{task_id}`**

Returns full task metadata plus recent terminal output captured from the live tmux pane.

**Request:**

```bash
curl https://{user}.dev.archon.cx/api/claude/tasks/$TASK_ID \
  -H "Authorization: Bearer $TOKEN"
```

**Response (200 OK):**

```json
{
  "task_id": "1707000000-a1b2c3d4",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "prompt": "Create a Python script that prints hello world",
  "workdir": "/home/dev/myproject",
  "status": "completed",
  "created_at": 1707000000.123,
  "finished_at": 1707000045.678,
  "tmux_session": "claude-1707000000-a1b2c3d4",
  "recent_output": "..."
}
```

The `recent_output` field contains the last 50 lines captured from the tmux pane (for running sessions) or is empty (for completed sessions where the tmux session has ended).

---

### Get Task Output

**`GET /api/claude/tasks/{task_id}/output`**

Returns terminal output captured from the tmux pane. For live sessions, this is the current pane content. For ended sessions, falls back to the output log file.

**Request (full output):**

```bash
curl https://{user}.dev.archon.cx/api/claude/tasks/$TASK_ID/output \
  -H "Authorization: Bearer $TOKEN"
```

**Request (last N lines):**

```bash
curl "https://{user}.dev.archon.cx/api/claude/tasks/$TASK_ID/output?tail=20" \
  -H "Authorization: Bearer $TOKEN"
```

| Parameter | Type | Required | Description |
|---|---|---|---|
| `tail` | integer | No | Return only the last N lines of output. |

**Response (200 OK):**

```
Content-Type: text/plain

[terminal output from the claude session]
```

---

### Send Follow-up Message

**`POST /api/claude/tasks/{task_id}/message`**

Sends a follow-up prompt into a running interactive Claude session by typing it into the tmux pane.

**Request:**

```bash
curl -X POST https://{user}.dev.archon.cx/api/claude/tasks/$TASK_ID/message \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Now add unit tests for that script"}'
```

| Field | Type | Required | Description |
|---|---|---|---|
| `prompt` | string | Yes | The follow-up prompt to send. |

**Behavior:**

- The follow-up text is typed into the running interactive Claude session via tmux.
- If the tmux session has ended, an error is returned.

**Response (200 OK):**

```json
{
  "task_id": "1707000000-a1b2c3d4",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "running",
  "followups": [
    {
      "prompt": "Now add unit tests for that script",
      "sent_at": 1707000100.789
    }
  ]
}
```

**Error (session ended):**

```json
{
  "error": "Session is no longer running"
}
```

---

### Prepare Terminal Attach

**`POST /api/claude/tasks/{task_id}/prepare-terminal`**

Prepares the terminal for one-click attach from the dashboard. Writes a marker file that the terminal wrapper script reads on the next terminal connection.

**Request:**

```bash
curl -X POST https://{user}.dev.archon.cx/api/claude/tasks/$TASK_ID/prepare-terminal \
  -H "Authorization: Bearer $TOKEN"
```

**Response (200 OK):**

```json
{
  "ok": true,
  "session": "claude-1707000000-a1b2c3d4"
}
```

---

### Kill a Task

**`DELETE /api/claude/tasks/{task_id}`**

Kills the tmux session associated with a running task.

**Request:**

```bash
curl -X DELETE https://{user}.dev.archon.cx/api/claude/tasks/$TASK_ID \
  -H "Authorization: Bearer $TOKEN"
```

**Response (200 OK):**

```json
{
  "task_id": "1707000000-a1b2c3d4",
  "status": "killed",
  "killed_at": 1707000200.123
}
```

---

### Get Bearer Token (OAuth2 only)

**`GET /oauth/api/claude/auth/token`**

Returns the current bearer token. This endpoint requires an OAuth2 browser session; it cannot be called with a bearer token.

---

### Regenerate Bearer Token (OAuth2 only)

**`POST /oauth/api/claude/auth/token/regenerate`**

Generates a new bearer token, invalidating the previous one. Requires OAuth2 browser session.

---

### Error Responses

All endpoints return errors as JSON with an appropriate HTTP status code:

| Status | Meaning |
|---|---|
| `400` | Bad request (missing or invalid `prompt`, malformed JSON). |
| `401` | Unauthorized (missing or invalid bearer token, missing OAuth2 headers). |
| `404` | Task not found. |
| `500` | Internal server error. |

## Dashboard

The workspace dashboard at `https://{user}.dev.archon.cx/oauth/` includes a **Claude Tasks** section that:

- Polls `GET /api/claude/tasks` every 10 seconds
- Shows each task as a card with status badge (running/completed/error/killed), prompt preview, and relative timestamp
- Provides **Attach** button for running tasks -- opens a new terminal tab auto-attached to the task's tmux session
- Provides **Kill** button to stop running tasks

## Using the `/remote-task` Skill

If you're working in the kube-coder repository with Claude Code, you can use the `/remote-task` skill to manage tasks without leaving your terminal:

```bash
# Launch a new task
/remote-task analyze the codebase and create a CLAUDE.md

# Check status of all tasks
/remote-task status

# View output of a specific task
/remote-task output <TASK_ID>

# Attach to a running task
/remote-task attach <TASK_ID>

# Kill a task
/remote-task kill <TASK_ID>
```

The skill handles port-forwarding, token retrieval, and API calls automatically.

## Deployment and Setup

### Prerequisites

- A running kube-coder workspace deployed via Helm.
- OAuth2 authentication enabled (`ingress.auth.type: oauth2` in the user's values file).
- Claude Code authentication configured via **either** OAuth login (subscription) or API key. See [Claude Code Authentication](#claude-code-authentication).

### Ingress Configuration

The file `charts/workspace/templates/ingress-claude-api.yaml` creates a dedicated Kubernetes Ingress for the Claude Task API:

- **Path:** `/api/claude/(.*)`
- **No OAuth2 proxy:** This Ingress does not include OAuth2 annotations, allowing bearer token authentication directly.
- **Long timeouts:** `proxy-read-timeout` and `proxy-send-timeout` are set to 3600 seconds to support long-running tasks.
- **No buffering:** `proxy-buffering: off` for streaming support.

### Files Involved

| File | Purpose |
|---|---|
| `charts/workspace/server.py` | Python HTTP server with `ClaudeTaskManager` class. Handles all API endpoints. |
| `charts/workspace/dashboard.html` | Dashboard UI with Claude Tasks section. |
| `charts/workspace/templates/deployment.yaml` | Pod spec with entrypoint, env vars, and volume mounts. |
| `charts/workspace/templates/terminal-entry-configmap.yaml` | Wrapper script for ttyd enabling one-click tmux attach. |
| `charts/workspace/templates/github-app-secret.yaml` | Kubernetes Secret for GitHub App credentials. |
| `charts/workspace/templates/github-app-token-refresh.yaml` | ConfigMap with Python script for GitHub App token refresh. |
| `charts/workspace/templates/claude-secret.yaml` | Kubernetes Secret for `ANTHROPIC_API_KEY`. |
| `charts/workspace/templates/ingress-claude-api.yaml` | Ingress for external API access (bypasses OAuth2). |
| `charts/workspace/templates/ingress-oauth2.yaml` | Ingress for browser-based access (includes OAuth2 proxy). |
| `charts/workspace/values.yaml` | Default Helm values. |
| `deployments/{user}/values.yaml` | Per-user Helm values. |
| `secrets/{user}/claude.yaml` | Gitignored file containing the Anthropic API key. |
| `secrets/{user}/github-app.yaml` | Gitignored file containing GitHub App credentials. |
| `.claude/skills/remote-task/SKILL.md` | Claude Code skill for managing remote tasks. |

## Integration Guide

### Calling from Another Service

Any service that has the bearer token can create and manage Claude tasks via standard HTTP requests.

**1. Store the token**

Retrieve the token once from the browser endpoint and store it as a secret in your service.

**2. Create a task**

```python
import requests

BASE_URL = "https://imran.dev.archon.cx"
TOKEN = "your-bearer-token"

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
}

response = requests.post(
    f"{BASE_URL}/api/claude/tasks",
    headers=headers,
    json={
        "prompt": "Refactor the authentication module to use JWT tokens",
        "workdir": "/home/dev/myproject",
    },
)
task = response.json()
task_id = task["task_id"]
print(f"Task created: {task_id}, status: {task['status']}")
```

**3. Poll for completion**

```python
import time

while True:
    response = requests.get(
        f"{BASE_URL}/api/claude/tasks/{task_id}",
        headers=headers,
    )
    task = response.json()
    status = task["status"]
    print(f"Status: {status}")

    if status in ("completed", "error", "killed"):
        break

    time.sleep(10)
```

**4. Send a follow-up**

```python
response = requests.post(
    f"{BASE_URL}/api/claude/tasks/{task_id}/message",
    headers=headers,
    json={"prompt": "Now write tests for the changes you made"},
)
```

### Bash Script Example

```bash
#!/bin/bash
set -euo pipefail

HOST="https://imran.dev.archon.cx"
TOKEN="${CLAUDE_API_TOKEN}"

# Create a task
TASK_ID=$(curl -s -X POST "$HOST/api/claude/tasks" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "List all TODO comments in the codebase"}' \
  | jq -r '.task_id')

echo "Task created: $TASK_ID"

# Poll until done
while true; do
  STATUS=$(curl -s "$HOST/api/claude/tasks/$TASK_ID" \
    -H "Authorization: Bearer $TOKEN" \
    | jq -r '.status')
  echo "Status: $STATUS"
  [ "$STATUS" != "running" ] && break
  sleep 5
done

# Get output
curl -s "$HOST/api/claude/tasks/$TASK_ID/output" \
  -H "Authorization: Bearer $TOKEN"
```

### Attaching to a Running Task

Since tasks run as interactive Claude sessions, you can attach to them to approve permissions or provide input:

**From the dashboard:**
Click the "Attach" button on any running task card. This opens a new terminal tab auto-attached to the tmux session.

**Via kubectl:**
```bash
kubectl exec -it -n coder <pod-name> -c ide -- tmux attach-session -t claude-<task_id>
```

**Via the workspace terminal:**
```bash
tmux attach-session -t claude-<task_id>
```

### What Claude Has Access To

When a task runs inside the workspace pod, Claude Code has access to:

- The full workspace filesystem under `/home/dev/`.
- Git repositories cloned in the workspace.
- All installed CLI tools (node, python, git, docker, etc.).
- Network access from within the pod.
- The ability to read and write files, run commands, and create commits (with user approval when attached).

### Task Lifecycle

```
POST /tasks  -->  status: "running"
                       |
                       |  (interactive claude session in tmux)
                       |
                       +---> user attaches, interacts, approves permissions
                       |
                       v
              tmux session ends (claude exits)
                       |
                       +---> status: "completed"
                       |
  DELETE /tasks/{id}  -+---> explicitly killed -->  status: "killed"
```

Status reconciliation happens automatically: when any API call queries a task whose tmux session has ended, the server updates the status to `completed`.

### Concurrency

Multiple tasks can run simultaneously -- each runs in its own tmux session. There is no built-in concurrency limit at the API level. However, all tasks within a workspace share the same Claude authentication and pod resources (CPU, memory), so running too many concurrent tasks may lead to rate limiting or resource exhaustion.

## Troubleshooting

### Common Issues

**401 Unauthorized**

- Verify your bearer token is correct: `curl https://{host}/oauth/api/claude/auth/token` (from browser).
- Check that the token file exists inside the pod: `cat /home/dev/.claude-tasks/.api-token`.
- If the token was regenerated, update it in all services that use it.

**Task stays in "running" but nothing is happening**

- Attach to the tmux session to see what Claude is doing: `tmux attach -t claude-{task_id}`.
- Claude may be waiting for permission approval -- attach and approve or deny.
- Verify Claude Code is authenticated:
  - **API key method:** `echo $ANTHROPIC_API_KEY` (should not be empty).
  - **OAuth method:** `claude` should start without a login prompt. If it prompts for login, the OAuth token has expired -- re-authenticate.
- Check if Claude Code is installed: `which claude`.

**Task immediately shows "error" status**

- The tmux session failed to start. Check the `error` field in the task response.
- Common cause: a tmux session with the same name already exists.

**"prompt is required" error**

- Ensure you are sending `Content-Type: application/json` and the body contains a `prompt` field.
- The prompt must be a non-empty string after trimming whitespace.

**Can't clone private repos**

- Verify GitHub App credentials are configured: check for `GITHUB_APP_ID` env var in the pod.
- Run the token refresh manually: `python3 /github-app/github-app-token.py --once`.
- Check that the GitHub App has the correct repository permissions.

**OAuth token expired**

- Shell into the pod and re-authenticate: `make shell-{user}` then run `claude`.
- Complete the device-code flow in your browser.
- Credentials are saved on the PVC and persist until they expire again.

### Inspecting Tasks Inside the Pod

```bash
# SSH or exec into the pod
kubectl exec -it $(kubectl get pod -n coder -l app=ws-{user} -o name) -n coder -- bash

# List all tasks
ls -la /home/dev/.claude-tasks/

# Check a specific task's metadata
cat /home/dev/.claude-tasks/{task_id}/task.json | jq .

# List active tmux sessions
tmux list-sessions

# Attach to a running task's tmux session
tmux attach -t claude-{task_id}

# Check the bearer token
cat /home/dev/.claude-tasks/.api-token
```

### Server Logs

The Python server (`server.py`) runs in the foreground inside the pod. Its logs are part of the container's stdout. If the server crashes, the monitoring loop in the deployment's entrypoint script restarts it automatically every 30 seconds.

```bash
kubectl logs $(kubectl get pod -n coder -l app=ws-{user} -o name) -n coder -c ide
```
