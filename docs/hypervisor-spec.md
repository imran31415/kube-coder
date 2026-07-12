# Hypervisor — Workspace-Aware Chat over the User's CLI Agents

The **Hypervisor** is a dashboard tab: a clean chat interface that lets a user
talk to their workspace — *"how many tasks are running / how much CPU?"*, *"spin
up a task to run the tests"*, *"remember that I deploy with `make ship`"*, *"pin
port 3000 to Apps"*. It reports live state **and** acts on it.

Tracking issue: imran31415/kube-coder#212.

![Hypervisor chat](screenshots/hypervisor-chat.png)

## Architecture (the key decision)

**A Hypervisor thread is a _structured agent session_, not a rendered
terminal.** The selected CLI runs in its machine-readable streaming mode over
pipes (no tmux, no TTY); a small per-CLI **adapter** normalizes its native
output into ONE canonical event stream that the frontend renders directly.

This replaced the original tmux-pane screen-scrape. That approach was
fundamentally fragile: a TUI has interactive menus a paste-only chat can't
answer (bypass / permission / API-key dialogs), and its rendered pane
(box-drawing tables, ANSI, wrapping) can't be reliably un-scraped into clean
chat. Structured sessions fix both at the root — no dialogs to answer, no pane
to un-scrape — and scale: adding a CLI means writing one adapter; the server and
frontend never change.

```
Browser (/hypervisor tab, Preact)
  │  POST /api/hypervisor/threads[/{id}/messages]
  │  GET  /api/hypervisor/threads/{id}?since=<seq>   (polls canonical events)
  ▼
server.py  (thin /api/hypervisor facade → HypervisorSession)
        ▼
hypervisor_session.py  — per-thread runner + per-CLI adapter
        │  runs the CLI headless over pipes, normalizes → events.jsonl
        ▼
   CLI turn:
     claude      → `claude -p --output-format stream-json --resume <id>`
                    (full structured: prose + tool_use + tool_result)
     kc-harness  → dashboard JSONL passthrough
     others      → non-TTY plain-line fallback (clean prose)
        └─ seeded MCP: memory, agent-orchestrator, dashboard (curated UI actions)
```

- **Canonical event schema** (`events.jsonl`, append-only per thread):
  `{seq, ts, role, type: message|tool_call|tool_result|error|status, …}`.
  The frontend only ever knows this; adapters are the only CLI-specific seam.
- **Multi-turn / restart-safe:** the Claude adapter captures the stream's
  `session_id` and `--resume`s it on later turns; the id lives in `thread.json`
  and Claude persists the session on disk, so continuity survives pod restarts.
- **Auth:** the Claude session drops `ANTHROPIC_API_KEY` from its env so it uses
  the workspace's Claude subscription (oauth), matching the interactive Build
  tab — headless `-p` otherwise silently prefers the API key.
- **Provider pluggability:** the assistant selector picks the adapter; Claude
  and kc-harness are first-class, every other CLI gets the clean fallback.

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

## Backend facade (`server.py` → `hypervisor_session.py`)

All behind `check_claude_auth()`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/hypervisor/config` | enabled?, default + available assistants, workdir, readOnly |
| GET | `/api/hypervisor/threads` | list threads |
| POST | `/api/hypervisor/threads` | create a session + start the first turn |
| GET | `/api/hypervisor/threads/{id}?since=<seq>` | thread summary + canonical events after `<seq>` |
| POST | `/api/hypervisor/threads/{id}/messages` | send a chat message (next turn) |
| DELETE | `/api/hypervisor/threads/{id}` | delete a thread |

The role/context preamble goes into Claude's **system prompt**
(`--append-system-prompt`) on the first turn, not the user message, so it never
shows as a bubble or pollutes the title. `hypervisor_session.py` is delivered
next to `server.py` at `/tmp/browser/` via `browser-configmap.yaml`.

## Frontend (`charts/workspace/web/`)

- Tab registered in `store/router.ts`, `routes/Shell.tsx`, `components/Rail.tsx`,
  `components/Icon.tsx`, `components/BottomNav.tsx` (primary mobile slot, "Chat").
- `routes/hypervisor/{index,Chat}.tsx` + `hypervisor.css`: thread sidebar,
  assistant selector, and a transcript rendered from canonical events —
  `transcript.ts`'s `buildTurns()` groups events into user bubbles + agent turns
  (markdown prose + expandable tool-activity chips). No screen scraping.
- `api/hypervisor.ts`, `store/hypervisor.ts`: canonical event types + polling.

## Config (`values.yaml`)

```yaml
hypervisor:
  enabled: true
  defaultAssistant: "claude"   # user can switch per-thread
  workdir: "/home/dev"
```

Provider/model/keys reuse the existing `claude.apiKey` / `assistant.*` config —
nothing new. No new pip dependency.

## Trust boundary

The Claude session runs headless in `bypassPermissions` mode
(`claude -p --permission-mode bypassPermissions`), so it acts without
per-command approval — appropriate for a chat over your *own* workspace pod, but
more permissive than the Build tab's prompting terminal. `bypassPermissions` is
pre-accepted via `skipDangerousModePermissionPrompt: true` in
`~/.claude/settings.json` (seeded by `seed_claude_config.py`); that key is only
consulted under a bypass flag, so the Build tab's plain `claude` still prompts.
The dashboard MCP's destructive tools (`kill_task`, `delete_memory`) still gate
on `confirm=true`, so those keep asking in chat. The shell is not sandboxed.

## Known limitations / follow-ups

- **Non-Claude adapters:** `ante`, `opencode`, `agy`, `librefang` use the
  non-TTY plain-line fallback (clean prose, but stateless per turn and no
  structured tool cards). kc-harness JSONL and per-CLI structured adapters are
  the next deepening step; the canonical schema already supports them.
- **Mobile:** the thread sidebar is a slide-over on small screens.
