# Getting started

> **What this workspace is.** A Kubernetes-hosted dev container with a
> persistent `/home/dev`, full developer tooling, a built-in dashboard
> at `/`, and a Claude Code task runner. You SSH in, work in VS Code in
> the browser, or kick off Claude tasks from the dashboard — your
> choice, all against the same files.

## The 60-second tour

When you load the dashboard at `https://<you>.dev.<domain>/`, you land
on the **Desktop** tab. The left rail (or the bottom-nav on mobile)
switches you between the core surfaces:

| Surface | What it's for |
| --- | --- |
| **Desktop** | VNC viewer for the pod's virtual display — Firefox/Chrome, GUI apps, dev-server preview. |
| **Build** | Create and watch Claude tasks. Each task runs in its own tmux session. |
| **Memory** | Persistent key/value store shared between you and Claude. Survives pod restarts. |
| **Triggers** | Webhooks and cron jobs that fire tasks on a schedule or external event. |
| **Files** | Browse and upload to `/home/dev`. |
| **Docs** | This site. |
| **Settings** | Theme, Git identity, browser preview, metrics. |

On mobile the bottom-nav surfaces Desktop / Build / Memory plus a
**More** sheet for Triggers, Files, Docs, and Settings.

The top bar has a search button (or press **⌘K** / **Ctrl-K**) for the
command palette — jump to any task, memory, trigger, or doc page from
there. Press **?** for the keyboard-shortcut sheet.

## Run your first task

1. Hit **Build** in the rail (or tap **Build** at the bottom).
2. Click **+ New task**.
3. Type a prompt — e.g. *"Clone https://github.com/some/repo and tell me what it does."*
4. Pick a working directory (defaults to `/home/dev`).
5. **Create.**

The task appears in the list with a green dot — it's running. Click in
to see two views:

- **Chat**: a chat-style mirror of the tmux pane. Send follow-up
  messages from here.
- **Terminal**: the raw tmux session, rendered in-browser. Approve
  permission prompts, scroll history, copy output.

When the task finishes, the dot turns blue (completed) or red (error).
Tasks survive pod restarts — output and chat history are persisted to
disk under `~/.claude-tasks/`.

> :::scenario
> **Pattern: hand off long-running refactors.**
> Start a task with `Refactor src/auth to use the new TokenStore`, close
> the tab, come back an hour later. The task kept running; the chat
> view shows the full history. If Claude asked a permission question
> while you were away, the Terminal tab lets you answer it.
> :::

## Save your first memory

Memory lets you tell Claude something *once* and have it remembered
across every future task.

1. Go to **Memory** → **+ New memory**.
2. Namespace: `user.preferences.editor`. Key: `editor`. Value:
   `neovim`.
3. **Save.**

Now in any task, ask Claude *"what editor do I prefer?"* — it'll
answer "neovim" without you re-supplying the fact. The memory is
auto-injected into Claude's prompt context.

Memory you don't want auto-injected (tokens, private notes) — tag it
`secret`. It stays readable on explicit lookup but never leaks into
the prompt.

## Where to go next

- **[Tasks → Concepts](/docs/tasks-concepts)** — what really happens when you start a task.
- **[Memory → Concepts](/docs/memory-concepts)** — namespaces, importance, how Claude reads memory back.
- **[Triggers → Webhooks](/docs/triggers-webhooks)** — fire tasks from external systems.
- **[Tasks → HTTP API](/docs/tasks-api)** — script the workspace from outside.
- **[Tasks → Assistants](/docs/tasks-assistants)** — swap Claude for OpenRouter or a self-hosted GPU model.

If something seems broken, check **Settings → Metrics** for live CPU /
memory / disk usage — most "Claude is slow today" turns out to be
"the pod is at 95% RAM."
