---
name: remote-task
description: Launch a Claude task on a remote kube-coder workspace, check task status, or attach to a running session. Use when the user wants to send work to a remote workspace pod.
user-invocable: true
allowed-tools: Bash, Read, Grep
argument-hint: [prompt or "status" or "attach TASK_ID"]
---

# Remote Task Skill

Launch Claude tasks on remote kube-coder workspace pods, check their status, or view output.

## Prerequisites

This skill requires:
1. `kubectl` access to the cluster
2. A running workspace pod (deployed via `make deploy-<user>`)
3. An API token generated on the pod

## How It Works

The workspace pod runs a Python HTTP server on port 6080 that exposes a Claude Task API. Tasks launch as interactive tmux sessions running `claude` — users can attach to approve permissions and provide input.

## Commands

Based on `$ARGUMENTS`, perform ONE of the following:

### 1. If `$ARGUMENTS` is "status" — List all tasks

```bash
# Port-forward if needed
kubectl port-forward -n coder svc/ws-imran 6080:6080 &
PF_PID=$!
sleep 2

TOKEN=$(kubectl exec -n coder $(kubectl get pods -n coder -l app=ws-imran -o jsonpath='{.items[0].metadata.name}') -c ide -- cat /home/dev/.claude-tasks/.api-token 2>/dev/null)

curl -s "http://localhost:6080/api/claude/tasks" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

kill $PF_PID 2>/dev/null
```

Display the results as a formatted table showing: task_id, status, prompt (truncated), and created_at (as relative time).

### 2. If `$ARGUMENTS` starts with "attach" — Show how to attach to a task

Extract the task ID from the arguments. Then tell the user:
- Open the dashboard at the workspace URL and click "Attach" on the task
- Or run: `kubectl exec -it -n coder <pod> -c ide -- tmux attach-session -t claude-<task_id>`

Also show recent output:
```bash
kubectl exec -n coder <pod> -c ide -- tmux capture-pane -t claude-<TASK_ID> -p -S -30
```

### 3. If `$ARGUMENTS` starts with "output" — Show task output

Extract the task ID from the arguments. Run:
```bash
kubectl exec -n coder <pod> -c ide -- tmux capture-pane -t claude-<TASK_ID> -p -S -50
```

### 4. If `$ARGUMENTS` starts with "kill" — Kill a task

Extract the task ID from the arguments. Run:
```bash
kubectl port-forward -n coder svc/ws-imran 6080:6080 &
PF_PID=$!
sleep 2

TOKEN=$(kubectl exec -n coder $(kubectl get pods -n coder -l app=ws-imran -o jsonpath='{.items[0].metadata.name}') -c ide -- cat /home/dev/.claude-tasks/.api-token 2>/dev/null)

curl -s -X DELETE "http://localhost:6080/api/claude/tasks/<TASK_ID>" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

kill $PF_PID 2>/dev/null
```

### 5. Otherwise — Launch a new task

The `$ARGUMENTS` is the prompt to send to the remote Claude. Execute:

```bash
# Ensure port-forward is active
lsof -ti:6080 | xargs kill 2>/dev/null
kubectl port-forward -n coder svc/ws-imran 6080:6080 &
PF_PID=$!
sleep 2

# Get the API token from the pod
POD=$(kubectl get pods -n coder -l app=ws-imran -o jsonpath='{.items[0].metadata.name}')
TOKEN=$(kubectl exec -n coder $POD -c ide -- cat /home/dev/.claude-tasks/.api-token 2>/dev/null)

# If no token exists, create one
if [ -z "$TOKEN" ]; then
  TOKEN=$(kubectl exec -n coder $POD -c ide -- python3 -c "
import secrets, os
d = '/home/dev/.claude-tasks'
os.makedirs(d, mode=0o700, exist_ok=True)
t = secrets.token_urlsafe(36)
with open(os.path.join(d, '.api-token'), 'w') as f: f.write(t)
os.chmod(os.path.join(d, '.api-token'), 0o600)
print(t)
")
fi

# Create the task
curl -s -X POST "http://localhost:6080/api/claude/tasks" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d "$(python3 -c "import json; print(json.dumps({'prompt': $(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$ARGUMENTS"), 'workdir': '/home/dev'}))")" \
  | python3 -m json.tool

kill $PF_PID 2>/dev/null
```

After launching, wait a few seconds and check the tmux pane to confirm the task started:
```bash
sleep 8
kubectl exec -n coder $POD -c ide -- tmux capture-pane -t claude-<TASK_ID> -p -S -20
```

Report the task_id and tmux_session name back to the user so they know how to attach.

## API Reference

All endpoints are under `http://localhost:6080/api/claude/` (via port-forward) and require `Authorization: Bearer <token>` header.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/claude/tasks` | List all tasks |
| POST | `/api/claude/tasks` | Create task. Body: `{"prompt": "...", "workdir": "/home/dev"}` |
| GET | `/api/claude/tasks/{id}` | Get task details + recent output |
| GET | `/api/claude/tasks/{id}/output?tail=N` | Get task output (last N lines) |
| POST | `/api/claude/tasks/{id}/message` | Send follow-up. Body: `{"prompt": "..."}` |
| DELETE | `/api/claude/tasks/{id}` | Kill a task |
| POST | `/api/claude/tasks/{id}/prepare-terminal` | Prepare terminal for one-click attach |

## Notes

- Tasks run as **interactive** Claude sessions in tmux. The user can attach to approve file writes or provide input.
- The workspace must have Claude authenticated (run `claude` then `/login` in the pod terminal if needed).
- GitHub App credentials are auto-configured if set in the Helm values — private repo access works out of the box.
- The default workspace is `ws-imran` in the `coder` namespace. Adjust pod labels/names for other users.
