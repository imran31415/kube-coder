# Persistent Memory

Every kube-coder workspace ships with a **persistent memory subsystem** —
a SQLite-backed key/value (plus graph + history) store that is shared
between the dashboard's Memory tab and Claude Code via an MCP server.

Memory survives across tasks, browser tabs, and pod restarts. Users can
say things like *"remember I prefer Go"* and ask later *"what language
do I prefer?"* — Claude reads the entry back without you re-supplying it.

---

## How it works

```
┌─────────────────────┐     ┌────────────────────┐
│ Dashboard (browser) │     │ Claude Code (tmux) │
│ Memory tab          │     │ memory_* MCP tools │
└────────┬────────────┘     └────────┬───────────┘
         │ HTTPS                     │ stdio JSON-RPC
         ▼                           ▼
┌─────────────────────┐     ┌────────────────────┐
│ server.py           │     │ mcp_memory.py      │
│ /api/memory*        │     │ (per-claude spawn) │
└────────┬────────────┘     └────────┬───────────┘
         └────────────┬──────────────┘
                      ▼
          /home/dev/.claude-memory/memory.db
                  (SQLite WAL, PVC)
```

Two entry points, **one SQLite file**. WAL mode + `BEGIN IMMEDIATE`
retries keep concurrent writes safe.

### Key components

| Path | What it is |
|---|---|
| `/home/dev/.claude-memory/memory.db` | The SQLite store. WAL-mode, future-proofed schema. |
| `/home/dev/.claude-memory/mcp_memory.py` | Stdio MCP server invoked by `claude` per-session. |
| `/home/dev/.claude-memory/memory_inject_hook.py` | A `UserPromptSubmit` hook script that *can* prepend a `<workspace_memories>` block. It ships but is **not wired into `settings.json` by default** — memory is on-demand via MCP (see "How Claude reads memory" below). |
| `/home/dev/.claude-memory/memory/` | Python package the MCP server imports (mirrors `/tmp/browser/memory/`). |
| `~/.claude.json` | Has `mcpServers.memory` (plus `playwright`, `sequential-thinking`) registered by the entrypoint. |
| `~/.claude/settings.json` | MCP/hook config. The legacy `UserPromptSubmit` memory-hook entry is **removed on every boot** (`seed_claude_config.py`), so by default there is no per-prompt memory hook. |
| `/api/memory*` on port 6080 | HTTP surface used by the dashboard. |

### Default MCP servers seeded

Every workspace pod boots with these MCP servers pre-registered in
`~/.claude.json`:

| Name | Purpose | Transport |
|---|---|---|
| `memory` | This document. SQLite-backed persistent K/V + graph + history. | stdio (`python3 mcp_memory.py`) |
| `playwright` | Full browser automation (click, type, screenshot, eval). Uses Firefox (already in the image). First use auto-downloads browsers into `~/.cache/ms-playwright`. | stdio (`npx -y @playwright/mcp@latest`) |
| `sequential-thinking` | Scratchpad `think` tool for explicit chain-of-thought during complex tasks. | stdio (`npx -y @modelcontextprotocol/server-sequential-thinking`) |

To add more, edit `~/.claude.json` directly inside the workspace — the
seeder only manages the keys above and leaves your additions alone.

### Schema (Phase 1)

- `memories` — `(namespace, key)` → `value`, plus `kind`, `tags`,
  `importance`, `confidence`, `source`, timestamps, soft-delete flag.
- `memory_history` — every write/delete keeps an immutable revision row.
- `memory_refs` — read/write log per memory (tasks, dashboard, crons).
- `relations` — graph edges between memories (Phase 3 surfaces them).
- `memories_fts` — FTS5 virtual table for keyword search.
- `embeddings` / `vec_memories` / `embeddings_pending` — wired but
  inactive in Phase 1 (activated when an embedding provider is set).

Migrations run idempotently on every server boot.

### Memory kinds

| Kind | Use for | Retention |
|---|---|---|
| `preference` | Stable user prefs ("I prefer Go") | Never decayed |
| `semantic`   | Facts ("project foo uses postgres") | Decays slowly when unused (Phase 3) |
| `procedural` | How-tos ("to deploy run `make deploy USER=<name>`") | Decays slowly |
| `episodic`   | Events ("deployed v1.8.0 on 2026-05-10") | TTL-able; consolidated into semantic (Phase 3) |

---

## Using it

### From the dashboard

Open the **Memory** rail tab. You can:

- **List / search** all entries (grouped by namespace).
- **Create** an entry: `New` → fill `namespace`, `key`, optional `tags`, `value`.
- **Edit** a value in the Value tab → click `Save`.
- **History** tab shows every revision with op (create / update / delete /
  consolidate) and who made it.
- **Used by** tab shows which tasks / crons / dashboard sessions have
  read or written this entry.
- **Delete** soft-deletes (the row stays in history; cannot be recovered
  through the UI but `sqlite3` still shows the tombstone).

### From Claude (in any task)

Claude Code instances spawned by the workspace pick up the memory MCP
server automatically. Trigger phrases the model is taught to handle:

| You say | Claude does |
|---|---|
| "remember I prefer Go" | `memory_remember(namespace='user.preferences', key='language', value='Go', kind='preference')` |
| "what did I say about databases?" | `memory_search(q='databases')` |
| "forget my favorite editor" | `memory_forget(namespace='user.preferences', key='editor')` (after confirming) |

Available tools: `memory_remember`, `memory_update`, `memory_recall`,
`memory_search`, `memory_list`, `memory_link`, `memory_neighbors`,
`memory_forget`, `memory_stats`.

### How Claude reads memory

By default memory is **on-demand**: Claude pulls it through the memory
MCP tools (`memory_search`, `memory_recall`, `memory_neighbors`,
`memory_list`) when a prompt calls for it. There is no implicit
per-prompt injection in the default configuration.

Optional **task-creation pre-injection** is available but **off by
default**. Set `KC_MEMORY_PREINJECT=1` and, when a Claude task is created
via the dashboard's `New` button or `POST /api/claude/tasks`, the server
picks the top-K (default 8) most relevant memories for the prompt and
prefixes the pasted prompt with a `<workspace_memories>` block. With it
off (the default), tasks carry `memory_injected: []`. The per-task
"Don't inject memories" toggle force-disables it regardless.

> The legacy **per-prompt `UserPromptSubmit` hook** (`memory_inject_hook.py`)
> that prepended a block to *every* interactive prompt is **disabled** —
> `seed_claude_config.py` strips that entry from `~/.claude/settings.json`
> on every boot. The script still ships for anyone who wants to wire it up
> manually, but the supported default is on-demand MCP access.

Claude treats the injected block as authoritative prior context; per
CLAUDE.md it must call `memory_search` before saying "I don't know"
about anything that might be remembered.

To **opt out for a specific task**:

- **Dashboard**: tick *"Don't inject memories"* in the new-task form.
- **API**: pass `"disable_memory_injection": true` in the request body.

The task detail's Info tab lists exactly which memories were injected,
with click-through to the Memory rail.

### HTTP API (also usable from `curl`)

All endpoints require a bearer token (`/home/dev/.claude-tasks/.api-token`)
or OAuth2 proxy headers.

```bash
TOK=$(cat /home/dev/.claude-tasks/.api-token)

# Upsert
curl -X POST localhost:6080/api/memory \
  -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
  -d '{"namespace":"user","key":"editor","value":"neovim","tags":"workflow"}'

# Get one
curl localhost:6080/api/memory/user/editor -H "Authorization: Bearer $TOK"

# History
curl localhost:6080/api/memory/user/editor/history -H "Authorization: Bearer $TOK"

# Soft-delete
curl -X DELETE localhost:6080/api/memory/user/editor -H "Authorization: Bearer $TOK"

# Stats
curl localhost:6080/api/memory/stats -H "Authorization: Bearer $TOK"
```

Full endpoint table:

| Method | Path | Notes |
|---|---|---|
| GET    | `/api/memory`                              | `?namespace=&kind=&q=&limit=` |
| POST   | `/api/memory`                              | Upsert with `{namespace,key,value,kind?,tags?,importance?}` |
| GET    | `/api/memory/{ns}/{key}`                   | Single row |
| DELETE | `/api/memory/{ns}/{key}`                   | Soft-delete |
| GET    | `/api/memory/{ns}/{key}/history`           | Revisions |
| GET    | `/api/memory/{ns}/{key}/refs`              | Read/write log |
| GET    | `/api/memory/{ns}/{key}/neighbors?depth=`  | Graph walk |
| POST   | `/api/memory/{ns}/{key}/relations`         | Link two memories |
| DELETE | `/api/memory/{ns}/{key}/relations/{id}`    | Remove relation |
| GET    | `/api/memory/stats`                        | Counts + DB size |
| POST   | `/api/memory/_consolidate`                 | Phase 3 — currently a stub |

---

## Provenance (who wrote what)

Every memory row records a `source`:

- `dashboard:<email>` — written by the Memory tab.
- `task:<task_id>` — written by a Claude task via MCP.
- `cron:<cron_id>` — written by a cron-triggered task.
- `api:<fp>` — written via bearer-token HTTP (where `fp` is a token fingerprint).

The same value lands in `memory_history.updated_by` for every revision
and in `memory_refs` for every read/write access.

---

## Clearing or resetting

The data lives at `/home/dev/.claude-memory/memory.db` (plus
`memory.db-wal` / `memory.db-shm`) on the workspace PVC. Resetting means
removing those files; the next time the server (or an MCP spawn) opens
the DB, it recreates the schema from scratch.

> ⚠️ Deletion is **permanent** — there is no built-in undo. If you have
> entries you care about, copy the DB first.

### Option 1 — Soft-delete a single entry (preferred)

From the dashboard's Memory tab, select the row and click `Delete`. Or:

```bash
TOK=$(cat /home/dev/.claude-tasks/.api-token)
curl -X DELETE localhost:6080/api/memory/<namespace>/<key> \
  -H "Authorization: Bearer $TOK"
```

The row is tombstoned but its history is preserved.

### Option 2 — Backup before wiping

```bash
ts=$(date +%Y%m%d-%H%M%S)
mkdir -p /home/dev/.claude-memory/backups
sqlite3 /home/dev/.claude-memory/memory.db \
  ".backup /home/dev/.claude-memory/backups/memory-$ts.db"
ls -lh /home/dev/.claude-memory/backups/
```

### Option 3 — Full reset (drops everything)

Run inside the workspace pod (open a terminal from the dashboard):

```bash
# Stop any in-progress memory writes — server keeps running, MCP processes
# spawn per Claude session and pick up the fresh DB on their next start.
rm -f /home/dev/.claude-memory/memory.db \
      /home/dev/.claude-memory/memory.db-wal \
      /home/dev/.claude-memory/memory.db-shm

# Restart the dashboard API server so it re-runs migrations:
pkill -HUP -f "python3 server.py"
# (the supervision loop in the entrypoint will respawn it within 30s)
```

The DB is recreated empty on the next access. Existing tasks keep
running; subsequent memory tool calls just see an empty store.

### Option 4 — Wipe everything from kubectl (no pod shell needed)

```bash
POD=$(kubectl get pod -n coder -l app=ws-<user> -o name | head -1)
kubectl exec -n coder $POD -c ide -- bash -c '
  rm -f /home/dev/.claude-memory/memory.db* &&
  pkill -HUP -f "python3 server.py" || true
'
```

### Option 5 — Targeted wipe via SQL

```bash
sqlite3 /home/dev/.claude-memory/memory.db \
  "DELETE FROM memories WHERE namespace LIKE 'project.foo.%';"
# FTS5 and history rows cascade via foreign keys.
```

### Restoring from a backup

```bash
pkill -f "python3 server.py"
cp /home/dev/.claude-memory/backups/memory-<timestamp>.db \
   /home/dev/.claude-memory/memory.db
# The supervision loop respawns the server within 30s.
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Dashboard Memory tab shows "backend not enabled" | Server didn't initialize memory; check pod logs for `[memory]` lines | Restart server: `pkill -HUP -f "python3 server.py"` |
| Claude never calls the memory tools | `~/.claude.json` lacks the `mcpServers.memory` entry | Re-run `python3 /browser-config/seed_claude_config.py` |
| MCP server crashes | Bad SQLite write or import failure | Check `kubectl logs` (the MCP writes to stderr); fix DB or roll back image |
| Pre-injection not happening | `memory_injected: []` because pre-injection is **off by default**, no entries matched, or you ticked "Don't inject memories" | Set `KC_MEMORY_PREINJECT=1` (then a more topical prompt / untick the checkbox); otherwise rely on Claude's on-demand MCP lookups |
| Concurrent-write errors | Extreme parallel write load | Already retried up to 3× with backoff; if persistent, file an issue |

---

## Claude's native auto-memory (auto-sync)

Claude Code (the CLI) maintains its **own** file-based memory system
under `~/.claude/projects/<projectId>/memory/*.md` — that's how it
remembers things like "user prefers Go" or "this repo uses Postgres"
between sessions, independently of our SQLite store.

kube-coder runs a **background sync** (every 60 s) that reads those
files and upserts each one as a memory entry in SQLite, so they appear
in the dashboard alongside dashboard- and MCP-authored entries.

| Property | Value |
|---|---|
| Namespace | `claude.<projectId>` (the project-id slug from the path) |
| Key | The markdown filename without `.md` |
| Kind | Mapped from frontmatter `type`: user/feedback → `preference`, project → `semantic`, reference → `procedural` |
| Tags | `auto-imported,claude-memory,mtime:<unix-ts>` |
| Source | `claude-auto:<absolute-path>` |
| Importance | `0.6` (slightly above default, since Claude wrote it deliberately) |

The dashboard row gets a small **`auto`** badge so you can spot
imported entries at a glance. The Memory rail header shows a
**`sync`** button (manual trigger) and a one-line status indicator
(*"claude-auto sync · 12 seconds ago · 3 files · 1 changed"*).

**Properties:**

- **One-way** (files → SQLite). Editing the SQLite copy from the
  dashboard does **not** rewrite Claude's source file. If you want a
  permanent change, edit the markdown directly inside the workspace.
- **Idempotent.** Files unchanged since the last sync are skipped via
  the `mtime:<ts>` tag fingerprint.
- **Self-healing.** When a source file is removed, the imported entry
  is soft-deleted with `op='delete'` and `updated_by='claude-auto:removed'`,
  so full history is preserved.
- **Scan roots.** The syncer scans `/home/dev/.claude` and
  `/home/ubuntu/.claude` because the workspace runs services under
  varying users. Skip basenames: `MEMORY.md`, `CLAUDE.md`.

**Manual trigger:**

```bash
TOK=$(cat /home/dev/.claude-tasks/.api-token)
curl -X POST localhost:6080/api/memory/_sync_claude -H "Authorization: Bearer $TOK"
# → {"status":"ok","result":{"scanned":N,"changed":M,"pruned":K}}
```

The same trigger is wired to the **sync** button in the dashboard's
Memory rail header.

**To disable auto-sync** (e.g. you want Claude's native memory to stay
out of the dashboard): comment out the `ClaudeMemorySyncer.start(…)`
call in `server.py` and redeploy. (A `values.yaml` toggle is on the
Phase 3 list.)

---

## Roadmap

Shipped: SQLite + WAL, FTS5 search, history, refs, graph relations,
dashboard graph view, MCP tools, opt-in pre-injection (FTS-ranked),
claude-auto sync.

Next: semantic recall via embeddings (Voyage AI / OpenAI — deps are
pre-installed, value-flip in `values.yaml`), background consolidation
(dedupe + decay), nightly backups, encryption-at-rest for
`secret`-tagged entries, export/import.
