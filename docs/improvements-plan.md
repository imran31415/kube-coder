# kube-coder Improvements & In-App Documentation Plan

> Living planning doc. Inputs: thorough review of the Vite/Preact SPA
> (`charts/workspace/web/`) and the Python backend (`charts/workspace/`).
> Two parallel streams of work:
>
> 1. **Ship an in-app documentation site** at `/docs` so users can
>    learn features without leaving the dashboard or hunting through
>    the repo. Reuses the existing `docs/` markdown and `claude-md.txt`.
> 2. **Bring the code to "world-class" baseline** — focused on the
>    handful of issues that actually compound (polling, focus traps,
>    `server.py` monolith, auth gaps, missing memory-subsystem tests).

---

## TL;DR — what we ship, in order

| Phase | Theme | Effort | Why this slot |
| --- | --- | --- | --- |
| **1. Safety** | Auth gap on legacy `/api/launch-chrome`/`/api/github/*` etc., VNC XSS, `useEscape` stacking, `MessageChat` poll-after-finish | 1–2 days | Anything else can wait if a port-forward exposes a write endpoint. |
| **2. Docs site MVP** | `/docs` route, markdown renderer, ToC, three seed pages (Tasks, Memory, Triggers) wired to the existing `docs/` markdown via a new `/api/docs` endpoint | 2–3 days | Highest user-visible value. Lights up the rest of the work as "look at /docs to learn". |
| **3. Shared primitives** | `usePoll`, `useFocusTrap`, `<ConfirmDialog>`, `<ResponsiveOverlay>`, scrim/text/spacing tokens | 2–3 days | Foundations the next batches build on. |
| **4. Test coverage uplift** | Frontend: routes/memory/triggers/files + dialog primitives. Backend: full `memory/*` + auth-check matrix + files traversal | 3–4 days | Locks in current behaviour before refactors. |
| **5. server.py decomposition** | Extract to `api/routes/*` modules + declarative route table + `logging` migration + request IDs | 3–5 days | Biggest carrying-cost reduction; unlocks unit-level handler tests. |
| **6. Docs site polish** | Search (palette + dedicated), screenshots, recipe pages, "Try it" embeds that deep-link into the SPA, code-copy buttons, version stamp | 2–3 days | Compounds value as Phase 1–5 land. |

Each phase produces one or more reviewable PRs. Phases 2 and 4 can land in parallel with 3 if we split owners; everything else is naturally sequential.

---

# Part A — Code review findings

## A.1 Frontend (Vite + Preact + signals)

The SPA is well-organised; signals usage is idiomatic; the API client is solid. The recurring patterns to lift before they ossify are **polling lifecycle, focus management in modals, and native `confirm/prompt` for destructive actions**.

### Top 10 highest-impact fixes

1. **Polling is reinvented in eight places and never backs off when the tab is hidden.**
   `store/tasks.ts:160`, `store/memory.ts:108`, `store/triggers.ts:76`, `store/metrics.ts:28`, `routes/tasks/TaskDetail.tsx:104`, `routes/tasks/SubagentsTab.tsx:31`, `routes/tasks/MessageChat.tsx:52`, `components/HealthDot.tsx:34` — all `setInterval`-based, all naive. Replace with a single `usePoll(fn, intervalMs, { pauseOnHidden: true, backoffOnError: true })` hook.

2. **`loadSelectedTask` has no in-flight dedup**, so slow detail fetches stack under polling (`store/tasks.ts:163-166`). Mirror the `inFlight` guard from `refreshTasks` on every per-resource loader.

3. **`useEscape` doesn't honour overlay stacking.** `hooks/useEscape.ts:8` — `e.stopPropagation()` on a window listener is meaningless to siblings. Drawer-inside-Sheet or Onboarding-over-Palette all dismiss together. Build a topmost-overlay stack in `store/ui.ts`; only the topmost handles Escape.

4. **No focus trap and no focus restoration in any dialog.** `Drawer.tsx`, `BottomSheet.tsx`, `CommandPalette.tsx`, `Onboarding.tsx`, `ShortcutsHelp.tsx` all set `role="dialog" aria-modal="true"` but Tab/Shift-Tab escapes to background. Add `useFocusTrap(ref, active)` + restore `document.activeElement` on close.

5. **Native `confirm()` / `prompt()` for destructive and edit actions** — `routes/tasks/TaskDetail.tsx:131,137`, `routes/memory/index.tsx:283`, `routes/triggers/index.tsx:135`, `routes/files/index.tsx:70`. Replace with `<ConfirmDialog>` + `<InlineRenameInput>` primitives. (Visually jarring, blocked in some browsers, and breaks accessibility.)

6. **`as DrawerKey` casts everywhere** — `routes/memory/index.tsx:44,55,123,144`, `routes/triggers/index.tsx:46,74,92,101`. Define `const MEMORY_EDIT: DrawerKey = 'memory-edit'` once and re-use. Also: `'new-task'` straddles both `DrawerKey` and `SheetKey` (`store/ui.ts:55-56`) — discriminate by signal name, not by string identity.

7. **`MessageChat` polls every 3s forever**, even when the task is finished and the tab is hidden (`routes/tasks/MessageChat.tsx:23-57`). Stop polling once `status !== 'running'`. The two `setTimeout(…, 1500/4000)` follow-ups (lines 79-80) are also un-cancelled timers (the `cancelledRef` guards the setState but the timers themselves leak).

8. **`taskCounts.error` mixes `error` and `killed`** (`store/tasks.ts:63`) but the label says "error". `TaskList.tsx:66` reads `counts.completed + counts.error` and calls it `pastCount`. Either rename the bucket to `past` or split.

9. **`TaskDetail.tsx` has a tab-snap race** — two effects on `[t?.task_id]` and `[t?.status]` (lines 67-82) plus a third (`[tab, subagentsCount]`, line 113) fight when status flips just after selection. Two `eslint-disable react-hooks/exhaustive-deps` markers (72, 81) are the smell. Consolidate into one effect that computes desired tab in one pass.

10. **`Onboarding.dismiss()` doesn't wrap `localStorage.setItem` in try/catch** the way `loadPrefs` does (`components/Onboarding.tsx:22-39`); throws in private-mode browsers. Minor.

### Test coverage matrix — frontend

| Area | Has tests? | Priority | Notes |
| --- | --- | --- | --- |
| `api/client.ts`, `store/router.ts`, `store/tasks.ts`, `store/ui.ts` | ✅ | — | adequate |
| `routes/tasks/TaskList.tsx`, `components/CommandPalette.tsx`, `Drawer.tsx`, `hooks/useShortcut.ts`, `app.tsx`, `styles/tokens.css` | ✅ | — | adequate |
| **`store/memory.ts`** | ❌ | **HIGH** | filtered/namespaces signals, save/delete |
| **`store/triggers.ts`** | ❌ | **HIGH** | cron+webhook merge, fire/suspend |
| **`store/metrics.ts`** | ❌ | medium | partial-failure path (`Promise.allSettled`) |
| **`components/BottomSheet.tsx`** | ❌ | **HIGH** | snap states + touch-drag math |
| **`components/Onboarding.tsx`** | ❌ | **HIGH** | multi-step state machine, gating on `githubStatus` |
| **`routes/tasks/TaskDetail.tsx`** | ❌ | **HIGH** | tab snap, banner per status, subagent gating |
| **`routes/tasks/TerminalPane.tsx`** | ❌ | **HIGH** | prepare → ready → error phases, port validation |
| **`routes/tasks/NewTaskForm.tsx`** | ❌ | **HIGH** | rename-after-create best-effort branch |
| **`routes/tasks/MessageChat.tsx`** | ❌ | **HIGH** | polling lifecycle, send → refetch |
| **`routes/memory/index.tsx`** | ❌ | **HIGH** | edit/save/delete, history+relations lazy-load |
| **`routes/triggers/index.tsx`** | ❌ | **HIGH** | TriggerForm validation, fire/suspend/delete |
| **`routes/files/index.tsx`** | ❌ | **HIGH** | breadcrumb, goUp, upload error, mkdir |
| `routes/settings/*` | ❌ | medium | GitSection refresh, MetricsSection alerts |
| `components/Topbar.tsx`, `BottomNav.tsx`, `MetricsBar.tsx`, `HealthDot.tsx`, `Toast.tsx` | ❌ | low–med | small surfaces, useful smoke tests |
| `hooks/useEscape.ts`, `useScrollLock.ts`, `useMediaQuery.ts` | ❌ | medium | stacking, refcount, SSR fallback |

### CSS / design tokens

- Scrim repeated 4× — `Drawer.css:3`, `BottomSheet.css:3`, `Onboarding.css:3`, `CommandPalette.css:3`, `ShortcutsHelp.css:3`. Add `--scrim`, `--scrim-strong`.
- Hardcoded blacks: `#000` (`detail.css:390`), `#1a1a1a` (`detail.css:406`), `#ffffff` (`Button.css:47`). Token as `--terminal-bg`, `--vnc-bg`, `--on-accent-hover`.
- **Type-scale chaos** — 13 distinct sizes (`10/10.5/11/11.5/12/12.5/13/13.5/14/15/18/20/22`). Collapse to `--text-xs/sm/base/md/lg/xl/h2/h1` (~6).
- `letter-spacing: -0.01em` and `-0.015em` repeated ~12× → `--tracking-tight`, `--tracking-tighter`.
- `border-radius: 4px` hardcoded in `reset.css:14`, `Topbar.css:70` despite `--radius-sm: 6px` existing.
- Inline styles for spacing in `routes/settings/MetricsSection.tsx:52,63`, `routes/tasks/index.tsx:43`, `routes/tasks/TerminalPane.tsx:181`, `routes/settings/BrowserSection.tsx:98` — move to CSS.

### Accessibility

- No focus trap or restoration anywhere (fix #4).
- `<div class="palette-scrim" onClick=…>` (`CommandPalette.tsx:188`) — clickable div with no role/keyboard handler. Wrap in `<dialog>` or use a `<button>` overlay.
- `ToastRack` has `role="status"` on the container but new toasts inside don't trigger live-region updates reliably. Move `aria-live="polite"` to the rack; `role="status"` per toast.
- `<input type="file" hidden>` inside `<label class="files-upload">` (`routes/files/index.tsx:99`) — visible "button" is a `<span class="btn">`, not a button; screen readers skip it.
- `role="tab"` in `TaskList:79`, `TaskDetail:274`, `routes/memory/index.tsx:300` — only `TaskDetail:290` declares the matching `tabpanel`. Fix the other two.
- Color contrast: `--text-subtle: #7e7e87` on `--bg: #0e0e10` is ~4.0:1 — fails AA-normal. Used for hints/kbd. Bump.
- No skip link rendered even though `<main tabIndex={-1}>` is set up to be one.

### Architectural observations

1. **Stores are feature modules, not pure state libraries.** Each store owns signals, derived signals, async actions, polling lifecycle, and toast wiring. Pragmatic, but extract a `createPollingResource({ fetcher, intervalMs })` factory — every store re-implements the same 30 lines of `start/stop/refresh/dedup/clear-timeout`.
2. **Routing is hand-rolled and shallow.** `store/router.ts` only matches top-level segments; nested paths live inside route components reading from stores. `TaskDetail.onCopyLink` (line 145) writes `/tasks?id=…` but nothing parses `?id=` on load — broken deep-link affordance. Either commit to URL-as-state (would resolve race #9) or remove the affordance.
3. **Mobile/desktop branching is duplicated per route.** `routes/tasks`, `memory`, `triggers` each call `useIsMobile()` and pick `<Drawer>` vs `<BottomSheet>`. Build `<ResponsiveOverlay open title>` — ~20 lines saved per route, fixes the "mid-edit viewport change loses your work" bug.

### Lower-priority smells

- `renameTask` dynamic `await import('../../api/tasks')` in `NewTaskForm.tsx:49` — already imported at top. Remove.
- `taskStatusFilterEffective` computed twice in `store/tasks.ts:31-50` and `53-56`.
- `getMemoryHistory`/`getMemoryNeighbors` cache (`routes/memory/index.tsx:241-259`) keys on truthy data; a failed fetch poisons the cache.
- `Topbar.tsx:25` sets `win.opener = null` without try/catch; some popup blockers return read-only windows.
- Two identical `STATUS_TONE` constants in `TaskList.tsx:26` and `TaskDetail.tsx:20` — extract to `api/tasks.ts` or `tasks/constants.ts`.
- `SubagentsTab` and `TaskDetail` both fetch `/api/subagents` on independent timers (15s + 20s). Hoist into `store/subagents.ts`.
- `tsconfig` aliases `react` → `preact/compat` but neither dep exists. Remove the alias.
- `vite.config.ts` `base: '/next/'` is migration cruft — SPA is now at `/`.

---

## A.2 Backend (Python)

The backend is functionally cohesive; the memory subsystem and webhook/cron triggers are well thought out (HMAC, replay cache, atomic flock'd JSON, schema-versioned migrations, WAL + `BEGIN IMMEDIATE`). The chief debt is **structural** — `server.py` is 3,912 LOC with a single `BrowserHandler` owning 59 methods that mixes routing, business logic, and presentation.

### Top 10 highest-impact fixes

1. 🔴 **Legacy `/api/launch-chrome`, `/api/test-chrome`, `/api/open-localhost`, `/api/github/*`, `/api/launch-firefox`, `/api/test-firefox` have NO authentication** (`server.py:3379-3398`, handlers `:3590-3655`, `:3668-3856`). Only ingress-OAuth2 protects them. Port-forward or NodePort exposes write endpoints (SSH key writes, git config). Add `check_claude_auth()` to every handler, including in `do_POST`'s outer switch.
2. **`open_localhost()` runs `pkill -f chrome|chromium|firefox`** unconditionally (`:3792-3797`) on every call — also kills the Playwright-MCP-managed Firefox seeded by `seed_claude_config.py`. Scope to a managed kiosk PID.
3. **Dead code + duplicate task-creation paths.** `session_id` generated never used (`:493, 644`), `import socketserver` unused (`:3`). `create_task` (`:488-631`) and `create_terminal_task` (`:633-690`) duplicate ~50 lines — extract `_make_tmux_task`.
4. **Bad-input → 500s.** `int(query.get('limit')[0])` raises and gets caught as generic 500 in memory list/neighbors/refs (`:2866, 2929`), `do_DELETE :2213`, memory upsert `importance/confidence` (`:2991-2992`). Validate, return 400 with a useful message.
5. **No pagination on `/api/files/list`** (`:3140`) or `WorkspaceManager.list_dirs` (`:1071`) — 50k-file folder is a single JSON blob.
6. 🔴 **VNC HTML reflects `self.path` and exception strings without escaping** (`:3327, 3364-3366`). Reflected XSS into an authenticated origin (even though VNC is OAuth-gated). `html.escape()` or return JSON.
7. **`/api/subagents` rescans `~/.claude/projects/*` on every poll** (`transcript_scanner.py:152`) — per-file parses are mtime-cached, the directory walk is not. Add a 5–10s TTL.
8. **No rate limiting on memory upserts or webhook receivers.** `WebhookManager.REPLAY_CACHE` is a 1024-entry in-memory LRU; a flood of distinct bodies fills it. Per-webhook QPS + outer global cap.
9. **`handle_claude_stream_output` opens an SSE per dashboard tab**, each forking `tmux capture-pane` every 1.5s (`:2331-2334`) and re-reading + JSON-parsing `meta_path` every loop. Cap concurrent streams per task; `tmux pipe-pane` already writes `output.log`, so a single file-tail thread could fan out to all SSE consumers.
10. **No structured logging.** ~10 distinct `print('[component] …', file=sys.stderr)` prefixes have reinvented half of `logging` without the request-id correlation. Migrate to `logging` + a `request_id` contextvar set in `handle_one_request`.

### Proposed `server.py` decomposition

`server.py` → ~400 lines of routing + 8 modules under a new `charts/workspace/api/` package:

```
api/
  __init__.py
  app.py               # BrowserHandler shell + do_GET/do_POST/do_DELETE
                       # dispatching to a route table (not if/elif)
  auth.py              # check_claude_auth, check_oauth_only, token CRUD
  routes/
    health.py          # /health/*, /metrics (absorb MetricsCollector)
    github.py          # GitHubManager + /api/github/*
    vnc.py             # send_vnc_viewer, redirect_to_vnc, proxy_vnc_request
    browser.py         # launch_chrome, open_localhost, test_chrome
    files.py           # files/list, upload, mkdir
    tasks.py           # ClaudeTaskManager + handle_claude_*
    triggers.py        # WebhookManager + CronManager + receivers
    memory.py          # handle_memory_*
    subagents.py       # transcript scanner shim
    spa.py             # serve_next_spa, end_headers cache logic
    docs.py            # NEW — /api/docs (see Part B)
  util/
    http.py            # send_json, read_json_body, query parsing
    paths.py           # _resolve_under_home_dev, _safe_filename
    shell.py           # _shell_quote, subprocess wrappers with timeout
```

Routing should be a declarative table:

```python
ROUTES = [
    ("GET",    re.compile(r"^/health$"),                health.status),
    ("GET",    re.compile(r"^/api/claude/tasks$"),      tasks.list),
    ("POST",   re.compile(r"^/api/claude/tasks$"),      tasks.create),
    ("DELETE", re.compile(r"^/api/claude/tasks/([^/]+)$"), tasks.delete),
    # …
]
```

Eliminates the ordering hazard in `do_POST` where `/api/webhooks/{id}/test` had to be listed before `/api/webhooks/{id}` with a manual comment (`:3445-3456`).

### Security findings

- 🔴 **Auth gap on legacy endpoints** (item #1).
- **No CORS** sent anywhere — fine given same-origin design, but document the assumption.
- **No CSRF on cookie-authenticated state-changing requests.** Bearer-token endpoints are safe. Cookie endpoints can be cross-site POSTed while the user is logged in. Either require `X-Requested-With: XMLHttpRequest` on cookie writes or issue a session CSRF token.
- **`workdir` is not validated** in `create_task`/`create_terminal_task` (`:488, 633`). Shell-quoted, so no injection — but webhook callers can spawn in `/etc`. Pin to `/home/dev/**`.
- **`/api/files/upload`** accepts 200 MiB per request with no per-pod cumulative quota. Add a free-space precondition.
- **`response_url` SSRF mitigation** is good (scheme allowlist `:943`) but DNS rebinding to RFC1918 is still possible. Post-DNS check or explicit RFC1918 block.
- 🟡 **VNC reflected XSS** (item #6).
- **`mcp_memory.py`** accepts caller-controlled namespace; a misbehaving task could spam `secret`-tagged entries that the auto-inject hook skips but fill the DB. Per-task write rate limit or source-scoped namespace.
- **Bearer token** stored plaintext (mode 0600, OK in single-user pod). `verify_token` reads disk on every request — cache in memory.

### Test coverage matrix — backend

| Area | Has tests? | Priority |
| --- | --- | --- |
| Webhook CRUD + signature (github/slack/stripe) | ✅ strong | — |
| Cron CRUD + kubectl + token rotation | ✅ strong | — |
| Completion-hook idempotency + HMAC | ✅ good | — |
| Assistant selection, replay cache | ✅ good | — |
| SPA static-file routing + traversal (`next_spa_test`) | ✅ good | — |
| **Memory API HTTP handlers (`handle_memory_*`)** | ❌ | **HIGH** |
| **`MemoryManager` core (upsert/search/neighbors/top_for_prompt)** | ❌ | **HIGH** |
| **Memory FTS + LIKE fallback + re-rank** | ❌ | **HIGH** |
| **`memory_inject_hook.py`** | ❌ | HIGH |
| **`mcp_memory.py` dispatch** | ❌ | HIGH |
| **Auth checks — assert every handler 401s without auth** | ❌ | **HIGH** |
| **Files API (list, upload, mkdir, traversal, size cap)** | ❌ | **HIGH** |
| **`_resolve_under_home_dev` path traversal** | ❌ | **HIGH** |
| SSE stream diff math + end events | ❌ | medium |
| Followup `send_followup` (flock concurrency) | ❌ | medium |
| `handle_claude_rename_task` validation | ❌ | medium |
| Transcript scanner mtime cache | ❌ | medium |
| GitHub/SSH/git-config handlers | ❌ | low → high after auth gate |
| VNC HTML/proxy | ❌ | low |
| Browser launch endpoints | ❌ | low |
| `ClaudeMemorySyncer.sync_once` | ❌ | medium |
| `seed_claude_config.py` idempotent merge | ❌ | low |
| `harness.py` `parse_xml_tool_calls` (unit, not live LLM) | partial | medium |

**Headline gap: zero coverage on ~1500 LOC of memory subsystem** — the most security-sensitive multi-process surface.

### Architectural observations

1. **HTTP-handler-as-business-logic doesn't scale.** Every `handle_*` couples auth + parsing + logic + serialization. Tests have to instantiate `BrowserHandler` or call Manager classes directly, never the route. Decomposition (above) unlocks pure handler functions and real route-level tests.
2. **No structured logging, no request IDs.** `except Exception: pass` blocks at `:567, 2895, 3007` swallow errors silently. After the `logging` migration these should at minimum log with the request id.
3. **Two writers into the SQLite memory store** (`server.py` threads + `mcp_memory.py` subprocesses) coordinate only via WAL. Correct, but: `MemoryStore.tx()` opens a fresh connection per transaction (`store.py:306`) — a per-thread connection pool would help at higher rates. Document the locking model before Phase-2 vector embeddings land.

---

# Part B — In-app documentation site

## B.1 Goals

- **First-class discoverability** of every workspace feature without leaving the UI.
- **Source of truth = repo markdown.** The `docs/` folder plus `charts/workspace/claude-md.txt` already covers Tasks, Assistants (Claude/OpenRouter/kc-harness), and Persistent Memory. The in-app site renders these; we add a few new pages for Triggers, Files, Browser/VNC, and "Getting started".
- **Examples and scenarios over reference walls.** Each page leads with a short "What it does" + a worked scenario, then drops into reference. Code blocks are copyable; "Try it" buttons deep-link into the SPA action (e.g. "Create your first task" → opens the New Task drawer).
- **Searchable** — content is indexed into the command palette plus a dedicated docs search.

## B.2 Information architecture

```
/docs                          Overview + getting started
├── /docs/getting-started      Pod boot → first task → first memory
├── /docs/tasks
│   ├── /tasks/concepts        What a task is, tmux sessions, lifecycle
│   ├── /tasks/api             Full HTTP API (sourced from claude-task-api.md)
│   └── /tasks/assistants      claude / openrouter / kc-harness (llm-setup.md)
├── /docs/memory
│   ├── /memory/concepts       Namespaces, importance, tags, secret tag
│   ├── /memory/usage          Saving, searching, forgetting via dashboard + claude
│   └── /memory/architecture   SQLite WAL, MCP, inject hook (persistent-memory.md)
├── /docs/triggers
│   ├── /triggers/webhooks     Signed webhooks, payload variables, response URL
│   └── /triggers/crons        Schedule grammar, supplying tokens
├── /docs/files                Files tab, upload, traversal model
├── /docs/browser              VNC viewer, kiosk mode, launching apps
├── /docs/cli-and-api          API token, curl recipes, language SDK examples
├── /docs/troubleshooting      Common failures + log locations
└── /docs/contributing         Fork+PR flow, where each piece lives
```

Each leaf is a markdown file; tree is declarative.

## B.3 Content model + rendering

**Source of truth.** Markdown files in `docs/` directory (already shipped). We add a tiny manifest:

```yaml
# docs/_manifest.yaml
sections:
  - id: getting-started
    title: Getting started
    file: in-app/getting-started.md
  - id: tasks
    title: Tasks
    children:
      - { id: tasks-concepts, title: Concepts, file: in-app/tasks-concepts.md }
      - { id: tasks-api, title: HTTP API, file: claude-task-api.md }
      - { id: tasks-assistants, title: Assistants, file: llm-setup.md }
  # …
```

Files under `docs/in-app/` are new content; files at the top level are the existing reference docs reused as-is.

**Serving.** New backend route group `api/routes/docs.py`:

| Method | Path | Returns |
| --- | --- | --- |
| `GET` | `/api/docs` | JSON manifest (nav tree) |
| `GET` | `/api/docs/{id}` | `{ id, title, breadcrumbs, markdown, edited_at }` |
| `GET` | `/api/docs/search?q=…` | `[ { id, title, snippet, score } ]` (substring + section-title weighting; FTS later) |

Docs are read from disk at request time (small files, with a per-file mtime cache). Markdown is **not** pre-rendered server-side — we ship the source to the client and render in Preact so links/anchors/code-copy work without round-trips.

**Markdown rendering.** `marked` (~30 KB min+gzip) + `prismjs` syntax highlighter, both tree-shaken to only the languages we use (`bash`, `python`, `typescript`, `yaml`, `json`, `tsx`). All HTML emitted through `marked` is sanitized via `DOMPurify`. Custom marked extensions:
- **Frontmatter fenced blocks** `:::scenario` → renders a callout with an icon.
- **`{deep-link:tasks-new-task}`** → renders a button that navigates to `/tasks` and opens the New Task drawer.
- **`![/screens/tasks-list.png]`** → bundled screenshots under `web/public/docs/`.

## B.4 SPA changes

1. New route `/docs` registered in `store/router.ts` (`ROUTES` array). Tab title "Docs".
2. New left-rail and bottom-nav entry behind a `book` icon (add to `Icon.tsx`).
3. New `routes/docs/index.tsx` with two-column layout:
   - **Left:** ToC tree (sticky on desktop, collapsible drawer on mobile).
   - **Right:** Rendered page + table-of-contents-within-page from `<h2>` headings.
4. Deep-link to subpage: `/docs/tasks/api` — requires the router to support nested paths. Smallest change is to keep top-level matching but expose `pathSuffix` (everything after the matched top segment) to the route via a derived signal. Aligns with Front-end architectural observation #2.
5. Command palette: include doc pages as a "Docs" group with full-text snippet matching (calls `/api/docs/search`).
6. Topbar gets a "?" → "Docs" button next to the keyboard-shortcut "?".
7. Onboarding final step links to `/docs/getting-started`.

## B.5 Content inventory & writing plan

Existing material (≈1,500 lines) provides ~60% of the docs site out of the box:

| Existing file | Maps to in-app page | Action |
| --- | --- | --- |
| `docs/claude-task-api.md` (897 lines) | `/docs/tasks/api` | use as-is, add intro callout linking to `/tasks/concepts` |
| `docs/llm-setup.md` (273 lines) | `/docs/tasks/assistants` | use as-is |
| `docs/persistent-memory.md` (366 lines) | `/docs/memory/architecture` | use as-is |
| `charts/workspace/claude-md.txt` (excerpts) | `/docs/getting-started` (partial), `/docs/memory/concepts` (partial), `/docs/contributing` | extract sections; do **not** show the whole file to users — it's written for Claude |

New material to write (≈800–1200 lines total):

- `getting-started.md` — pod boot, what you see at `/`, first task walkthrough, first memory, "where do I go next?" (~150 lines)
- `tasks-concepts.md` — task lifecycle, tmux sessions, attaching, killing, message followups, recovering output (~200 lines)
- `memory-concepts.md` — namespaces, importance, tags, secret tag, automatic injection, how it affects Claude behaviour (~150 lines)
- `memory-usage.md` — common operations from the dashboard + from Claude, with screenshots (~150 lines)
- `triggers-webhooks.md` — creating a webhook, signing, payload templating, response URL, replay protection (~200 lines)
- `triggers-crons.md` — schedule grammar, secret/token plumbing, hooks running as a service account, debugging missed runs (~150 lines)
- `files.md` — Files tab tour, upload limits, where data lives, persistence model (~80 lines)
- `browser.md` — VNC viewer, launching Firefox/Chrome, kiosk mode for headless apps (~100 lines)
- `cli-and-api.md` — getting the token, curl recipes for every endpoint, language SDK examples (Python + Node) (~250 lines)
- `troubleshooting.md` — VS Code 502, tmux session gone, OAuth token expired, memory DB locked, missing assistant in dropdown (~150 lines)

Each page follows a template:

```markdown
---
title: Webhooks
summary: One-line for nav hover + search snippet.
---

# Webhooks

> **What it does.** One sentence, no jargon.

## Walkthrough — your first webhook
:::scenario
Steps + commands.
:::

## How it works
…architecture diagram or short prose…

## Reference
…tables, full options…

## Troubleshooting
…common failures with the exact error message…
```

## B.6 Implementation steps

1. **Backend** — add `api/routes/docs.py` (or, before decomposition, a `handle_docs_*` group in `server.py`). 3 endpoints, mtime cache, plain-text full-text search over titles + body. **~150 LOC + tests.**
2. **Frontend scaffolding** — `routes/docs/index.tsx`, `routes/docs/Sidebar.tsx`, `routes/docs/Article.tsx`, `routes/docs/docs.css`. Add `marked` + `dompurify` + `prismjs` to `package.json` via `yarn add` (yarn, not npm — per project convention). Register `/docs` route. Add `book` icon. **~400 LOC + tests.**
3. **Custom extensions** — `:::scenario` callout, `{deep-link:…}` → button. **~80 LOC + tests.**
4. **Content** — port + write the pages listed in B.5. Use `yarn build` to check links, run a screenshot pass with `web/scripts/shoot.mjs`. **~1000 lines markdown.**
5. **Palette + Topbar integration** — palette query → `/api/docs/search`; Topbar "?" menu. **~80 LOC + tests.**
6. **Onboarding hook** — final step "Tour the docs" → `/docs/getting-started`. **~10 LOC.**

## B.7 Open design questions (to decide before kicking off Phase 2)

- **Should `/docs` be authenticated?** Today everything on port 6080 is ingress-OAuth-gated. Public docs would let teammates pre-read before getting workspace access. Recommendation: keep gated for v1, revisit.
- **Versioning** — when the SPA ships behind multiple chart releases, do users see the docs for *their* pod's version or for `main`? v1: pod's version (read from disk). Stamp every page with the `Chart.yaml` version.
- **Editability** — should the dashboard let an admin edit a docs page and `git commit` it back? Tempting; defer to v2 — too easy to get wrong on auth.

---

# Part C — Phased rollout

### Phase 1 — Safety fixes (1–2 days)

- ✅ Add `check_claude_auth()` to every legacy `/api/launch-*`, `/api/test-*`, `/api/github/*`, `/api/open-localhost` handler.
- ✅ HTML-escape `self.path` + exception messages in VNC HTML responses.
- ✅ Stop polling in `MessageChat` once status is terminal; cancel the two trailing `setTimeout`s on unmount.
- ✅ Fix `useEscape` to honour topmost-overlay stack.

Validation: existing tests pass; add a smoke test asserting `POST /api/github/config` returns 401 without auth.

### Phase 2 — Docs site MVP (2–3 days)

- ✅ Backend: `/api/docs`, `/api/docs/{id}`, `/api/docs/search`.
- ✅ Frontend: route, sidebar, article renderer (marked + DOMPurify + prism), `book` icon, palette entries.
- ✅ Content: getting-started + tasks-concepts + memory-concepts + reuse of the three existing reference docs.
- ✅ Tests: `/api/docs` handler coverage, route smoke test, palette docs-search test.

Validation: hit `/docs` and `/docs/tasks/api` in a browser; palette query "webhook" returns doc entries.

### Phase 3 — Shared primitives (2–3 days)

- ✅ `hooks/usePoll.ts` + migrate all 8 polling sites.
- ✅ `hooks/useFocusTrap.ts` + apply to every modal.
- ✅ `<ConfirmDialog>`, `<InlineRenameInput>` primitives + replace native `confirm/prompt` usages.
- ✅ `<ResponsiveOverlay>` + migrate `tasks`, `memory`, `triggers` routes.
- ✅ Token consolidation: scrim, on-accent, type scale, tracking, radius. Replace hardcoded values.

Validation: visual diff via `web/scripts/shoot.mjs`, vitest suite green.

### Phase 4 — Test coverage uplift (3–4 days)

Frontend (in priority order): `routes/memory`, `routes/triggers`, `routes/files`, `routes/tasks/{TaskDetail,TerminalPane,MessageChat,NewTaskForm}`, `components/{BottomSheet,Onboarding,Topbar}`, `store/{memory,triggers,metrics}`.

Backend (in priority order):

- `tests/memory_api_test.py` — full handler matrix incl. bad input, auth gating, traversal cases.
- `tests/memory_manager_test.py` — upsert/search/neighbors/top_for_prompt/FTS+LIKE fallback.
- `tests/auth_matrix_test.py` — parametrise over every endpoint, assert auth gate.
- `tests/files_api_test.py` — list/upload/mkdir + traversal + size cap.

### Phase 5 — `server.py` decomposition (3–5 days)

- ✅ Extract `api/` package per Part A.2.
- ✅ Replace `do_GET`/`do_POST`/`do_DELETE` cascade with a declarative route table.
- ✅ Migrate `print` → `logging` with request-id contextvar; JSON formatter in production.
- ✅ Connection-pool the SQLite memory store per thread.

Validation: all existing tests pass; new handler-level unit tests added per module.

### Phase 6 — Docs site polish (2–3 days)

- ✅ Screenshots and scenarios for every page (use `web/scripts/shoot.mjs`).
- ✅ "Try it" deep-links: `{deep-link:tasks-new-task}` etc.
- ✅ Code-copy buttons on every fenced block.
- ✅ FTS search backend (SQLite FTS5 over docs).
- ✅ Version stamp on every page.
- ✅ Cross-link checker in CI.

---

# Part D — Appendix

## D.1 File-by-file checklist (cross-reference)

**Frontend (paths under `charts/workspace/web/src/`):**

- `hooks/useEscape.ts:8` — overlay stack.
- `hooks/useShortcut.ts:13` — broaden `isEditable` to cover `contenteditable=""` and `role=textbox`.
- `store/tasks.ts:31-50, 53-56` — dedupe `taskStatusFilterEffective`.
- `store/tasks.ts:63` — rename or split `error` bucket.
- `store/tasks.ts:160-166` — `loadSelectedTask` dedup.
- `store/ui.ts:55-56` — split `'new-task'` from `DrawerKey`/`SheetKey`.
- `routes/tasks/MessageChat.tsx:23-57` — stop poll on terminal status, cancel trailing timers.
- `routes/tasks/TaskDetail.tsx:20, 67-82, 113, 131, 137, 145` — STATUS_TONE dedupe; tab-effect consolidation; replace `confirm/prompt`; URL parse for `?id=`.
- `routes/tasks/NewTaskForm.tsx:49` — remove dynamic import.
- `routes/tasks/SubagentsTab.tsx:31` — hoist into `store/subagents.ts`.
- `routes/memory/index.tsx:44, 55, 123, 144, 241-259, 283, 300` — `DrawerKey` constants; explicit loading/error states; replace `confirm`; tabpanel wiring.
- `routes/triggers/index.tsx:46, 74, 92, 101, 135` — same patterns.
- `routes/files/index.tsx:70, 99` — replace `prompt`; turn the `<span>` button into a real button.
- `components/CommandPalette.tsx:188` — scrim accessibility.
- `components/Onboarding.tsx:22-39` — try/catch storage.
- `components/Toast.tsx:8` — live-region semantics.
- `components/HealthDot.tsx:39` — visually-hidden state word.
- `components/Topbar.tsx:25` — try/catch on `win.opener`.
- `styles/tokens.css` — add the six tokens listed in A.1; remove duplicates.
- `vite.config.ts` — drop `base: '/next/'`.
- `tsconfig.json` — drop `react → preact/compat` alias.

**Backend (paths under `charts/workspace/`):**

- `server.py:3, 493, 644` — remove dead code.
- `server.py:488-631, 633-690` — extract `_make_tmux_task`.
- `server.py:567, 2895, 3007` — replace bare `except: pass` with logged exceptions.
- `server.py:2213, 2254, 2866, 2929, 2991-2992` — validate ints, return 400.
- `server.py:3140, 1071` — paginate list endpoints.
- `server.py:3327, 3364-3366` — `html.escape` in VNC HTML.
- `server.py:3379-3398, 3590-3655, 3668-3856` — gate legacy endpoints.
- `server.py:3792-3797` — kiosk PID scope, not `pkill -f`.
- `server.py:2331-2334` — SSE fan-out from a single tail thread.
- `transcript_scanner.py:152` — TTL on `_scan_all`.
- `memory/store.py:306` — per-thread connection pool.

## D.2 Test scaffolding decisions

- **Frontend**: keep Vitest + Testing-Library. For dialogs add `@testing-library/user-event` interactions (already installed). Don't introduce Storybook yet — overkill at current scale.
- **Backend**: stick with `unittest` style already used in `server_test.py` (no pytest migration in this batch). New module: `tests/conftest_helpers.py` for an `auth_headers()` fixture used across the auth-matrix test.

## D.3 Out of scope (intentionally not done now)

- Migrating Preact → React or adding Tanstack Router. Hand-rolled router is fine at this size; URL-as-state can land within it.
- Replacing the HTTP handler with FastAPI. Decomposition gets 80% of the benefit; framework swap is its own project.
- Vector embeddings for memory or docs search. Phase-2 work, both already designed elsewhere.
- Multi-pod / multi-user dashboard rollups. Single-pod focus stays.

---

*Owner: dashboard. Reviewers: backend (Phases 1, 5), frontend (Phases 2–4, 6).*
