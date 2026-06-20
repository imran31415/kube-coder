# Memory — concepts

> **What memory is.** A persistent key/value store, shared between you
> (via the **Memory** tab) and Claude (via MCP tools), backed by SQLite
> on the persistent volume. Memories survive every task, browser tab,
> and pod restart.

For the wire-level architecture (SQLite WAL, MCP server, the inject
hook), see [Memory → Architecture](/docs/memory-architecture). This
page is the conceptual model.

## The shape of a memory

Every entry has:

| Field | Purpose |
| --- | --- |
| **`namespace`** | Dotted scope, e.g. `user.preferences` or `project.kube-coder`. Group by topic. |
| **`key`** | Stable identifier within the namespace. |
| **`value`** | The fact. Keep it concise — one fact per entry. |
| **`importance`** | 0..1, default 0.5. Raise to surface more often. |
| **`tags`** | Comma-separated. Special tag: `secret` (see below). |
| **`kind`** | `semantic` (fact), `episodic` (event), `procedural` (how-to), `preference` (stable preference). |

Updates bump a version and write a history row — you can see who
changed what, when, from the Memory tab's **History** view.

## How Claude reads memory

Two paths:

1. **On-demand lookup (default).** Claude has `memory_recall`,
   `memory_search`, `memory_neighbors`, `memory_list` MCP tools. When you
   ask "remember when I told you…", Claude calls `memory_search` rather
   than guessing. This is how memory works out of the box.
2. **Optional pre-injection.** With `KC_MEMORY_PREINJECT=1`, a new build's
   prompt is prefixed with the top-K relevant memories as a
   `<workspace_memories>` block. It's **off by default** — and the older
   per-prompt `UserPromptSubmit` hook that injected on *every* prompt is
   disabled (kept out of `settings.json` on boot).

The auto-inject is gated by importance, recency, and a token budget —
big memory stores don't flood the prompt.

## Namespaces — pick well

Some conventions worth following:

- **`user.*`** — personal facts (name, email, preferences, working
  style). Auto-injected by default.
- **`project.<repo>.*`** — facts about a specific project. Auto-inject
  only when working in that project.
- **`session.*`** — reserved for future auto-pruning. Don't use yet.

`memory_remember` accepts any `namespace.key` — there's no schema. Be
consistent across entries you want Claude to find together.

## The `secret` tag

Tag an entry `secret` (in tags) and:

- It is **excluded from auto-injection** into the prompt.
- It is **still readable** by explicit `memory_recall` / `memory_search`.

Use this for tokens, private notes, anything you don't want appearing
in every Claude prompt.

> :::scenario
> **Pattern: per-project conventions.**
> Save `project.acme-api.lint = "ruff, line length 100"`. Every time
> Claude touches that project it sees the convention automatically and
> stops re-asking. Save `user.preferences.commits = "use Conventional
> Commits prefixes"` once and Claude follows it everywhere.
> :::

## From the dashboard

- **+ New memory** — pick a namespace, key, value, optional tags.
- **Edit** — bumps version, history row is written.
- **Delete** — soft-delete by default; the history row remains.

The Memory tab has three views per entry:

- **Value** — the current row.
- **History** — every prior version with timestamp.
- **Relations** — link entries to each other (e.g. *parent_of*,
  *contradicts*).

## From Claude

Just say:

- *"Remember that I prefer TypeScript over Flow"* → Claude calls
  `memory_remember(namespace="user.preferences.lang", key="ts_vs_flow",
  value="prefers TypeScript")`.
- *"What's our deploy command?"* → Claude calls `memory_search` and
  reads back the answer.
- *"Forget that I work at Acme"* → Claude confirms, then calls
  `memory_forget`.

You can audit every call from **Memory → History**.

## Pitfalls

- **One fact per entry.** Don't pack `"name: Imran; email: x; lang:
  Go"` into one value. Claude's auto-scoring rewards short, focused
  entries.
- **Version conflict on rapid edits.** If you edit the same key from
  two tabs at once, the last write wins; the loser still appears in
  history. Refresh before editing.
- **Importance creep.** Default 0.5. Bump to 0.7 for things you want
  surfaced often; reserve 0.9+ for hard rules.
