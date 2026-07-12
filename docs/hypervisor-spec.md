# Hypervisor — Workspace-Aware Chat over the User's CLI Agents

The **Hypervisor** is a dashboard tab: a clean chat interface that lets a user
talk to their workspace — *"how many tasks are running / how much CPU?"*, *"spin
up a task to run the tests"*, *"remember that I deploy with `make ship`"*, *"pin
port 3000 to Apps"*. It reports live state **and** acts on it.

Tracking issue: imran31415/kube-coder#212.

![Hypervisor chat](screenshots/hypervisor-chat.png)

## Architecture (the key decision)

**The existing CLI agents power the conversation; the Hypervisor only adds a
chat layer.** `claude`, `ante`, `opencode`, `librefang`, `kc-harness` are
CLI-only agents the user already installs and configures. The Hypervisor does
**not** build its own LLM/provider/tool loop. It is a clean chat UI over the
existing task/tmux agent machinery (`ClaudeTaskManager`) plus a curated
**dashboard MCP server** that gives those agents UI-equivalent powers.

```
Browser (/hypervisor tab, Preact)
  │  POST /api/hypervisor/threads[/{id}/messages]
  │  GET  /api/hypervisor/threads/{id}          (polled for live agent output)
  ▼
server.py  (thin /api/hypervisor facade → ClaudeTaskManager)
        │  spawns the user's chosen CLI agent in tmux (source="hypervisor")
        ▼
   CLI agent  (claude | ante | opencode | …)  — its own loop, user-configured auth
        ├─ seeded MCP: memory, agent-orchestrator   (existing)
        └─ seeded MCP: dashboard  (NEW) ── curated UI actions ──► localhost:6080 REST
```

- **Session model:** a chat thread is a hypervisor-flavoured task
  (`source="hypervisor"`); each message is a `send_followup`. Persistence,
  streaming, reconciliation and kill are reused from `ClaudeTaskManager`.
- **Provider pluggability:** it's just the assistant selector — any enabled
  assistant, configured the way the user already knows.

## Dashboard MCP server (`charts/workspace/mcp_dashboard.py`)

Stdio JSON-RPC MCP server. Every tool calls the dashboard's own local REST API
with the workspace bearer token — zero duplicated business logic.

| Kind | Tools |
|---|---|
| read | `get_metrics`, `list_tasks`, `get_task`, `get_task_output`, `get_service_health`, `get_github_status`, `search_memory`, `list_memory`, `list_apps`, `list_triggers` |
| safe write | `create_task`, `send_task_message`, `add_memory`, `pin_app` |
| destructive | `kill_task`, `delete_memory` — require `confirm=true` |

- **Confirm-on-destructive (in-chat):** a destructive tool called without
  `confirm=true` returns `CONFIRMATION_REQUIRED`, so the agent asks the user in
  chat and only re-calls with `confirm=true` after explicit approval. Works with
  any CLI agent, no special UI plumbing.
- **READONLY gating:** when `READONLY_MODE=true`, write + destructive tools are
  omitted entirely.

The MCP is seeded into every agent's config alongside `memory` /
`agent-orchestrator` (`seed_claude_config.py`, the `~/.ante/settings.json` and
`opencode.json` seeds in `start.sh`), so it's also usable from the Build tab.

## Backend facade (`server.py`)

All behind `check_claude_auth()`; writes gated by the global `_readonly_block()`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/hypervisor/config` | enabled?, default + available assistants, workdir, readOnly |
| GET | `/api/hypervisor/threads` | list threads (`source=hypervisor`) |
| POST | `/api/hypervisor/threads` | create a chat session |
| GET | `/api/hypervisor/threads/{id}` | detail + reconstructed messages + recent output |
| POST | `/api/hypervisor/threads/{id}/messages` | send a chat message (follow-up) |
| DELETE | `/api/hypervisor/threads/{id}` | delete a thread |
| GET | `/api/hypervisor/threads/{id}/stream` | SSE (reuses the task output stream) |

A short role/context preamble is pasted as the first message of a new chat via a
new `system_preamble` arg on `create_task` (kept out of `meta['prompt']` so
titles stay clean).

## Frontend (`charts/workspace/web/`)

- Tab registered in `store/router.ts`, `routes/Shell.tsx`, `components/Rail.tsx`,
  `components/Icon.tsx` (new `hypervisor` chip icon), `components/BottomNav.tsx`
  (primary mobile slot, "Chat").
- `routes/hypervisor/{index,Chat}.tsx` + `hypervisor.css`: thread sidebar,
  assistant selector, chat transcript (user bubbles + a live agent-output
  block, polled), composer.
- `api/hypervisor.ts`, `store/hypervisor.ts`: data + signals.

## Config (`values.yaml`)

```yaml
hypervisor:
  enabled: true
  defaultAssistant: "claude"   # user can switch per-thread
  workdir: "/home/dev"
```

Provider/model/keys reuse the existing `claude.apiKey` / `assistant.*` config —
nothing new. No new pip dependency.

## Known limitations / follow-ups (v1)

- **Rendering:** the assistant's output is the agent's rendered tmux pane
  (cleaned text), polled. Structured per-CLI rendering (Claude `stream-json`,
  `kc-harness` JSONL → true bubbles/tool cards) is a follow-up.
- **Trust boundary:** an agent selected here has its own `bash`, and because a
  chat can only paste text (there is no way to answer an in-terminal approval
  menu), the CLI is launched with its skip-permissions flag
  (`claude --dangerously-skip-permissions`, `ante --yolo`, `agy
  --dangerously-skip-permissions`) so it never blocks mid-turn. The agent thus
  acts without per-command approval — appropriate for a chat over your *own*
  workspace pod, but more permissive than the Build tab's prompting terminal.
  The dashboard MCP's destructive tools (`kill_task`, `delete_memory`) still
  gate on `confirm=true`, so those keep asking in chat. The shell is not
  sandboxed.
- **Mobile:** the thread sidebar is hidden on small screens (chat pane only).
