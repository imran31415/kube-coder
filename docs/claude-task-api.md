# Claude Task API

The Claude Task API allows remote services, scripts, and automation tools to programmatically create and manage Claude Code tasks running inside kube-coder workspace pods. Each workspace pod exposes an HTTP API that spawns `claude -p` commands in isolated tmux sessions, giving callers full access to Claude Code's capabilities -- file editing, code generation, git operations, and more -- without requiring an interactive terminal session.

## Architecture Overview

```
                                          +-----------------------------------+
                                          |  Workspace Pod                    |
  External Service / Script               |  (e.g. imran.dev.scalebase.io)    |
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
  |  (no OAuth2 proxy)          |         |  |  claude -p "..." \          |  |
  +-----------------------------+         |  |    --output-format           |  |
                                          |  |    stream-json              |  |
                                          |  +-----------------------------+  |
                                          |       |                           |
                                          |       | reads/writes              |
                                          |       v                           |
                                          |  /home/dev/ (workspace fs)        |
                                          |  /home/dev/.claude-tasks/         |
                                          +-----------------------------------+
```

**Key components:**

- **Workspace pods** run at `https://{user}.dev.scalebase.io`, one per developer.
- **Python server** (`server.py`, port 6080) handles all Claude Task API requests inside the pod.
- **tmux sessions** provide isolated execution environments. Each task runs `claude -p` in its own named tmux session (`claude-{task_id}`).
- **Claude Code authentication** supports two methods: OAuth (uses your Claude Pro/Max subscription) or API key (uses pay-per-use API credits). See [Claude Code Authentication](#claude-code-authentication) below.
- **Output** is captured in `stream-json` format and persisted to `/home/dev/.claude-tasks/{task_id}/output.log`.

## Claude Code Authentication

This is how Claude Code itself authenticates with Anthropic (separate from the Task API auth). You must choose one method:

### Method 1: OAuth Login (Claude Pro/Max Subscription)

**Use this when:** You have a Claude Pro or Max subscription and want tasks to run against your subscription with no additional cost per API call.

**How it works:** You do a one-time interactive OAuth login inside the pod. The credentials are stored on the persistent volume (`/home/dev/.claude/`) and survive pod restarts.

**Setup:**

1. Deploy without setting `claude.apiKey` (the default):
   ```bash
   helm upgrade imran-workspace ./charts/workspace \
     -f ./deployments/imran/values.yaml \
     --namespace coder --install --wait
   ```

2. Shell into the pod and run Claude to trigger the OAuth flow:
   ```bash
   make shell-imran
   claude
   ```

3. Follow the device-code flow — open the URL in your browser, paste the code, and log in with your Anthropic account.

4. Done. Credentials persist on the PVC at `/home/dev/.claude/`.

**Trade-offs:**
- No per-call API cost (included in your subscription).
- Subject to subscription rate limits.
- Requires one-time interactive setup per workspace.
- OAuth tokens may expire after extended inactivity (weeks/months) — re-login with `make shell-{user} && claude` if this happens.

### Method 2: API Key (Pay-Per-Use Credits)

**Use this when:** You want fully automated, zero-touch authentication (e.g., CI/CD pipelines, unattended workspaces) or need higher rate limits than your subscription provides.

**How it works:** An `ANTHROPIC_API_KEY` environment variable is injected into the pod from a Kubernetes Secret. Claude Code detects it automatically — no interactive login needed.

**Setup:**

1. Get an API key from https://console.anthropic.com/settings/keys

2. Create a gitignored secrets file at `secrets/{user}/claude.yaml`:
   ```yaml
   claude:
     apiKey: "sk-ant-api03-..."
   ```

3. Deploy with the secrets file:
   ```bash
   helm upgrade imran-workspace ./charts/workspace \
     -f ./deployments/imran/values.yaml \
     -f ./secrets/imran/claude.yaml \
     --namespace coder --install --wait
   ```

   Or pass the key inline (useful for CI):
   ```bash
   helm upgrade ... --set claude.apiKey=sk-ant-api03-...
   ```

**Trade-offs:**
- Fully automated — no interactive login required.
- Charges per API call against your Anthropic credit balance.
- API key must be managed securely (never commit to git).
- If `ANTHROPIC_API_KEY` is set, Claude Code always uses it (ignores any OAuth session).

### Precedence

If both methods are configured, **API key takes precedence**. To switch from API key back to OAuth, redeploy without the secrets file so the env var is removed.

## Task API Authentication

This is how external services authenticate with the Task API (separate from Claude Code's Anthropic auth above).

### Bearer Token (for scripts and services)

This is the primary method for programmatic access. It bypasses the OAuth2 proxy entirely via a dedicated Kubernetes Ingress rule.

**Step 1: Obtain your token**

Visit the following URL in a browser (you must be logged in via GitHub OAuth):

```
https://{user}.dev.scalebase.io/oauth/api/claude/auth/token
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
curl -X POST https://{user}.dev.scalebase.io/oauth/api/claude/auth/token/regenerate
```

This endpoint requires OAuth2 authentication (browser session with GitHub login). It cannot be called with a bearer token. After regeneration, the old token is immediately invalidated.

### OAuth2 Proxy (for browser sessions)

When accessing the API through the `/oauth/api/claude/...` path, requests pass through the OAuth2 proxy. The proxy injects headers (`X-Auth-Request-User`, `X-Auth-Request-Email`) that the server accepts as proof of authentication. This is used by the web dashboard and browser-based tools.

### Ingress Routing Summary

| Path Pattern | Auth Method | Use Case |
|---|---|---|
| `/api/claude/*` | Bearer token only | Scripts, services, CI/CD, automation |
| `/oauth/api/claude/*` | OAuth2 proxy (GitHub login) | Browser-based dashboard access |

## API Reference

All endpoints are served at `https://{user}.dev.scalebase.io`. Replace `{user}` with the workspace username (e.g., `imran`).

### Create a Task

**`POST /api/claude/tasks`**

Spawns a new Claude Code process in an isolated tmux session.

**Request:**

```bash
curl -X POST https://{user}.dev.scalebase.io/api/claude/tasks \
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
| `session_id` | UUID used for Claude Code's `--session-id` flag. Used for conversation continuity with `--resume`. |
| `status` | Task status: `running`, `completed`, `error`, or `killed`. |
| `tmux_session` | Name of the tmux session running the task. |

**Error Response (on tmux failure):**

```json
{
  "task_id": "1707000000-a1b2c3d4",
  "status": "error",
  "error": "duplicate session: claude-1707000000-a1b2c3d4"
}
```

---

### List All Tasks

**`GET /api/claude/tasks`**

Returns a summary of all tasks, sorted by creation time (newest first).

**Request:**

```bash
curl https://{user}.dev.scalebase.io/api/claude/tasks \
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

Returns full task metadata plus the last 50 lines of output.

**Request:**

```bash
curl https://{user}.dev.scalebase.io/api/claude/tasks/$TASK_ID \
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
  "recent_output": "{\"type\":\"assistant\",\"message\":...}\n..."
}
```

The `recent_output` field contains the last 50 lines from the output log file. Output is in Claude Code's `stream-json` format (one JSON object per line).

**Error Response (404):**

```json
{
  "error": "Task not found"
}
```

---

### Get Task Output

**`GET /api/claude/tasks/{task_id}/output`**

Returns the raw output log as plain text. Supports a `tail` query parameter.

**Request (full output):**

```bash
curl https://{user}.dev.scalebase.io/api/claude/tasks/$TASK_ID/output \
  -H "Authorization: Bearer $TOKEN"
```

**Request (last N lines):**

```bash
curl "https://{user}.dev.scalebase.io/api/claude/tasks/$TASK_ID/output?tail=20" \
  -H "Authorization: Bearer $TOKEN"
```

| Parameter | Type | Required | Description |
|---|---|---|---|
| `tail` | integer | No | Return only the last N lines of output. |

**Response (200 OK):**

```
Content-Type: text/plain

{"type":"system","subtype":"init","session_id":"550e8400-...","tools":["Read","Write",...]}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"I'll create..."}]}}
{"type":"result","subtype":"success","result":"..."}
__CLAUDE_EXIT_CODE_0__
```

The output is in `stream-json` format. Each line is a JSON object with a `type` field. The final line contains an exit code marker (`__CLAUDE_EXIT_CODE_N__`) used by the server to determine completion status.

---

### Send Follow-up Message

**`POST /api/claude/tasks/{task_id}/message`**

Sends a follow-up prompt to an existing task, resuming the Claude Code conversation using the original `session_id`.

**Request:**

```bash
curl -X POST https://{user}.dev.scalebase.io/api/claude/tasks/$TASK_ID/message \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Now add unit tests for that script"}'
```

| Field | Type | Required | Description |
|---|---|---|---|
| `prompt` | string | Yes | The follow-up prompt to send. |

**Behavior:**

- If the original tmux session is still alive, the follow-up command is sent via `tmux send-keys`, which queues it to run after the current command finishes.
- If the tmux session has ended, a new tmux session is created with the same name, and the `--resume` flag is used to continue the Claude Code conversation.
- Output is appended to the same `output.log` file.

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

---

### Kill a Task

**`DELETE /api/claude/tasks/{task_id}`**

Kills the tmux session associated with a running task.

**Request:**

```bash
curl -X DELETE https://{user}.dev.scalebase.io/api/claude/tasks/$TASK_ID \
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

**Request (from browser or via OAuth2 session):**

```bash
# This must go through the OAuth2 proxy path
curl https://{user}.dev.scalebase.io/oauth/api/claude/auth/token
```

**Response (200 OK):**

```json
{
  "token": "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXo..."
}
```

If no token exists, one is automatically generated and persisted.

---

### Regenerate Bearer Token (OAuth2 only)

**`POST /oauth/api/claude/auth/token/regenerate`**

Generates a new bearer token, invalidating the previous one. Requires OAuth2 browser session.

**Request:**

```bash
curl -X POST https://{user}.dev.scalebase.io/oauth/api/claude/auth/token/regenerate
```

**Response (200 OK):**

```json
{
  "token": "bmV3dG9rZW5oZXJl..."
}
```

---

### Error Responses

All endpoints return errors as JSON with an appropriate HTTP status code:

| Status | Meaning |
|---|---|
| `400` | Bad request (missing or invalid `prompt`, malformed JSON). |
| `401` | Unauthorized (missing or invalid bearer token, missing OAuth2 headers). |
| `404` | Task not found. |
| `500` | Internal server error. |

Error response body:

```json
{
  "error": "Description of the error"
}
```

## Deployment and Setup

### Prerequisites

- A running kube-coder workspace deployed via Helm.
- OAuth2 authentication enabled (`ingress.auth.type: oauth2` in the user's values file).
- Claude Code authentication configured via **either** OAuth login (subscription) or API key. See [Claude Code Authentication](#claude-code-authentication).

### How the API Key Flow Works (when using Method 2)

1. `charts/workspace/values.yaml` defines the default (empty) `claude.apiKey` value.
2. `charts/workspace/templates/claude-secret.yaml` creates a Kubernetes Secret named `claude-secrets-{username}` containing the key, but only if `claude.apiKey` is non-empty.
3. `charts/workspace/templates/deployment.yaml` injects the Secret as the `ANTHROPIC_API_KEY` environment variable in the `ide` container.
4. When `server.py` spawns `claude -p ...`, Claude Code picks up `ANTHROPIC_API_KEY` from the environment automatically.

### Ingress Configuration

The file `charts/workspace/templates/ingress-claude-api.yaml` creates a dedicated Kubernetes Ingress for the Claude Task API:

- **Path:** `/api/claude/(.*)`
- **Rewrite:** `nginx.ingress.kubernetes.io/rewrite-target: /api/claude/$1`
- **No OAuth2 proxy:** This Ingress does not include `nginx.ingress.kubernetes.io/auth-url` annotations, allowing bearer token authentication directly.
- **Long timeouts:** `proxy-read-timeout` and `proxy-send-timeout` are set to 3600 seconds to support long-running tasks.
- **No buffering:** `proxy-buffering: off` for streaming support.

This Ingress is only created when `ingress.auth.type` is set to `oauth2`.

### Files Involved

| File | Purpose |
|---|---|
| `charts/workspace/server.py` | Python HTTP server with `ClaudeTaskManager` class. Handles all API endpoints. |
| `charts/workspace/templates/claude-secret.yaml` | Kubernetes Secret template for `ANTHROPIC_API_KEY`. |
| `charts/workspace/templates/deployment.yaml` | Pod spec that injects `ANTHROPIC_API_KEY` and starts `server.py`. |
| `charts/workspace/templates/ingress-claude-api.yaml` | Ingress for external API access (bypasses OAuth2). |
| `charts/workspace/templates/ingress-oauth2.yaml` | Ingress for browser-based access (includes OAuth2 proxy). |
| `charts/workspace/values.yaml` | Default Helm values (empty `claude.apiKey`). |
| `deployments/{user}/values.yaml` | Per-user Helm values. |
| `secrets/{user}/claude.yaml` | Gitignored file containing the actual API key. |

## Integration Guide

### Calling from Another Service

Any service that has the bearer token can create and manage Claude tasks via standard HTTP requests. Here is a complete workflow example.

**1. Store the token**

Retrieve the token once from the browser endpoint and store it as a secret in your service (environment variable, Kubernetes Secret, vault, etc.).

**2. Create a task**

```python
import requests

BASE_URL = "https://imran.dev.scalebase.io"
TOKEN = "your-bearer-token"

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
}

# Create a task
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

    time.sleep(10)  # Poll every 10 seconds
```

**4. Retrieve the output**

```python
response = requests.get(
    f"{BASE_URL}/api/claude/tasks/{task_id}/output",
    headers=headers,
)
print(response.text)
```

**5. Send a follow-up**

```python
response = requests.post(
    f"{BASE_URL}/api/claude/tasks/{task_id}/message",
    headers=headers,
    json={"prompt": "Now write tests for the changes you made"},
)
followup = response.json()
print(f"Follow-up sent, status: {followup['status']}")
```

### Bash Script Example

```bash
#!/bin/bash
set -euo pipefail

HOST="https://imran.dev.scalebase.io"
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

### CI/CD Integration

You can trigger Claude tasks from CI/CD pipelines (GitHub Actions, GitLab CI, etc.) to automate code reviews, generate documentation, or run refactoring tasks.

```yaml
# Example GitHub Actions step
- name: Run Claude Code Review
  env:
    CLAUDE_TOKEN: ${{ secrets.CLAUDE_API_TOKEN }}
    WORKSPACE_HOST: https://imran.dev.scalebase.io
  run: |
    TASK_ID=$(curl -s -X POST "$WORKSPACE_HOST/api/claude/tasks" \
      -H "Authorization: Bearer $CLAUDE_TOKEN" \
      -H "Content-Type: application/json" \
      -d '{"prompt": "Review the latest commit and suggest improvements", "workdir": "/home/dev/project"}' \
      | jq -r '.task_id')

    # Wait for completion
    for i in $(seq 1 60); do
      STATUS=$(curl -s "$WORKSPACE_HOST/api/claude/tasks/$TASK_ID" \
        -H "Authorization: Bearer $CLAUDE_TOKEN" | jq -r '.status')
      [ "$STATUS" != "running" ] && break
      sleep 10
    done

    # Get results
    curl -s "$WORKSPACE_HOST/api/claude/tasks/$TASK_ID/output" \
      -H "Authorization: Bearer $CLAUDE_TOKEN"
```

### What Claude Has Access To

When a task runs inside the workspace pod, Claude Code has access to:

- The full workspace filesystem under `/home/dev/`.
- Git repositories cloned in the workspace.
- All installed CLI tools (node, python, git, docker, etc.).
- Network access from within the pod.
- The ability to read and write files, run commands, and create commits.

### Task Lifecycle

```
POST /tasks  -->  status: "running"
                       |
                       |  (tmux session alive, claude -p executing)
                       |
                       v
              tmux session ends
                       |
                       +---> exit code 0  -->  status: "completed"
                       |
                       +---> exit code != 0 -->  status: "error"
                       |
  DELETE /tasks/{id}  -+---> explicitly killed -->  status: "killed"
```

Status reconciliation happens automatically: when any API call queries a task whose tmux session has ended, the server checks the output log for the `__CLAUDE_EXIT_CODE_N__` marker and updates the stored status accordingly.

### Output Format

Task output is in Claude Code's `stream-json` format. Each line is a self-contained JSON object. Common types include:

| Type | Description |
|---|---|
| `system` | Initialization info (session ID, available tools). |
| `assistant` | Claude's response messages (text, tool use). |
| `tool` | Tool invocations and results (file reads, writes, bash commands). |
| `result` | Final result summary when the task completes. |

The last line of a completed task's output contains the exit code marker:

```
__CLAUDE_EXIT_CODE_0__
```

### Concurrency

Multiple tasks can run simultaneously -- each runs in its own tmux session. There is no built-in concurrency limit at the API level. However, all tasks within a workspace share the same Anthropic API key and pod resources (CPU, memory), so running too many concurrent tasks may lead to rate limiting or resource exhaustion.

## Troubleshooting

### Common Issues

**401 Unauthorized**

- Verify your bearer token is correct: `curl https://{host}/oauth/api/claude/auth/token` (from browser).
- Check that the token file exists inside the pod: `cat /home/dev/.claude-tasks/.api-token`.
- If the token was regenerated, update it in all services that use it.

**Task stays in "running" but no output appears**

- Check if the tmux session exists: `tmux list-sessions` inside the pod.
- Verify Claude Code is authenticated:
  - **API key method:** `echo $ANTHROPIC_API_KEY` (should not be empty).
  - **OAuth method:** `claude -p "hello"` should respond without a login prompt. If it prompts for login, the OAuth token has expired — re-run `claude` interactively to re-authenticate.
- Check if Claude Code is installed: `which claude`.
- Look at the output log directly: `cat /home/dev/.claude-tasks/{task_id}/output.log`.

**Task immediately shows "error" status**

- The tmux session failed to start. Check the `error` field in the task response.
- Common cause: a tmux session with the same name already exists (duplicate task ID collision, extremely rare).

**"prompt is required" error**

- Ensure you are sending `Content-Type: application/json` and the body contains a `prompt` field.
- The prompt must be a non-empty string after trimming whitespace.

**Token endpoint returns 401**

- The `/api/claude/auth/token` and `/api/claude/auth/token/regenerate` endpoints require OAuth2 browser authentication. They must be accessed through the `/oauth/api/claude/...` path, not the direct `/api/claude/...` path.

**"Credit balance is too low" error**

- This means you're using the API key method and your Anthropic account has insufficient credits.
- Add credits at https://console.anthropic.com/settings/billing, or switch to the OAuth method (your Claude Pro/Max subscription).

**ANTHROPIC_API_KEY not set (when using API key method)**

- Verify the Helm values include `claude.apiKey` or that the secrets file is passed during deployment.
- Check the Kubernetes Secret: `kubectl get secret claude-secrets-{user} -n coder -o yaml`.
- Check the pod's environment: `kubectl exec -it {pod-name} -n coder -- env | grep ANTHROPIC`.

**OAuth token expired (when using OAuth method)**

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

# View output log
cat /home/dev/.claude-tasks/{task_id}/output.log

# List active tmux sessions
tmux list-sessions

# Attach to a running task's tmux session (for debugging)
tmux attach -t claude-{task_id}

# Check the bearer token
cat /home/dev/.claude-tasks/.api-token
```

### Server Logs

The Python server (`server.py`) runs in the foreground inside the pod. Its logs are part of the container's stdout. If the server crashes, the monitoring loop in the deployment's entrypoint script restarts it automatically every 30 seconds.

```bash
# View pod logs
kubectl logs $(kubectl get pod -n coder -l app=ws-{user} -o name) -n coder -c ide
```
