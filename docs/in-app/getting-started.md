# Getting started

> **What this workspace is.** A Kubernetes-hosted dev container with a
> persistent `/home/dev`, full developer tooling, and a built-in
> dashboard at `/`. At its front is an **AI CTO** that plans and builds
> for you: tell it what you want in plain language and it writes the
> code, runs it, and shows you a live preview — all against the same
> files you can open in VS Code or over SSH.

## Your first win

The fastest path from a fresh workspace to something running is the
**AI CTO**. A first-run visit walks you through a short setup and then
drops you straight into it:

1. **Onboarding.** On first load a short wizard appears. It sets your
   **git identity**, connects your **GitHub** account (browser sign-in,
   no terminal), offers to generate an **SSH key**, and — the step that
   matters most — has you **Connect Claude**. kube-coder builds *with*
   Claude, so it needs your account first: sign in with your Claude
   subscription (recommended) or paste an `ANTHROPIC_API_KEY`. You can
   skip the git/SSH steps, but the CTO can't build until Claude is
   connected.
2. **Land in the AI CTO.** When you finish, you land in the **AI CTO**
   with a warm opener: *"Tell me in one sentence what you'd like to
   build, and I'll get started right away."*
3. **Type one sentence.** Something like *"Build me a personal portfolio
   website"* (there are starter chips for exactly this). Your first
   message **builds immediately** — no confirmation gate, no form to
   fill in.
4. **Watch it come to life.** As the CTO works, a **live preview**
   auto-surfaces right inside the chat the moment your build's dev
   server comes up. Keep talking to it — *"make the header dark",
   "add an about page"* — and it iterates.

That's the whole loop: **connect → say one sentence → it builds →
preview appears.** Everything below is the map for what surrounds it.

## The surfaces

The left rail (or the bottom-nav + **More** sheet on mobile) is grouped
into three sections. Open the command palette with **⌘K** / **Ctrl-K**
to jump to any of them — or any task, memory, or doc — and press **?**
for the keyboard-shortcut sheet.

### Mission Control

| Surface | What it's for |
| --- | --- |
| **AI CTO** | The front door. A chat with an AI CTO that plans and builds across your whole workspace; a first message builds right away, and previews surface inline. |
| **Feed** | *What changed · what matters* — briefings, news, activity, and decisions your CTO and workspace post as things happen. |
| **Chat** | The Hypervisor: a raw chat layer over your coding agents. Every chat is a real agent session (Claude, OpenCode, …) acting on your live files — lower-level than the CTO. |
| **Builds** | Create and watch individual build tasks. Each runs in its own tmux session with a Chat and a raw Terminal view. |
| **Walkie-Talkie** | A voice-first, push-to-talk channel to your workspace — speak, and a real agent turn answers back on a card and aloud. |
| **Triggers** | Webhooks and cron jobs that fire builds on a schedule or an external event. |

The **Mission Control** board itself (the group header) is one queue of
*every* agent across builds, chats, and sub-agents — with "waiting on
you" pulled to the front.

### Workspace

| Surface | What it's for |
| --- | --- |
| **Desktop** | The workspace **home**: a greeting over a centered composer, a live Mission Control strip, and a dock of launcher shortcuts. The place to start work and see work in flight. |
| **Apps** | Locally-listening services discovered on the workspace. Pin a port to give it a friendly name and open it from the dashboard. |
| **Files** | Browse and upload to `/home/dev`. |

> The pod's virtual display (headless Firefox/Chrome, GUI apps, the VNC
> viewer) is still here — launch it from the Desktop dock. See
> **[Workspace → Browser & VNC](/docs/browser)**.

### Knowledge

| Surface | What it's for |
| --- | --- |
| **Memory** | A persistent store of facts shared between you and Claude. Tell it something once; every future task remembers. Survives pod restarts. |
| **Skills** | Agent capabilities discovered across every harness — reusable, named workflows Claude can invoke. |
| **Docs** | This site. |

**Settings** (its own item at the bottom of the rail) holds theme, git
identity, your Claude connection, browser preview, and live metrics.

## Prefer to drive it yourself?

The AI CTO is the front door, not the only door. If you'd rather run a
single, scoped task and watch the terminal:

1. Go to **Builds** and click **+ New task**.
2. Type a prompt — e.g. *"Clone https://github.com/some/repo and tell me
   what it does."*
3. Pick a working directory (defaults to `/home/dev`) and hit
   **Create.**

The task appears with a green dot (running). Open it for a **Chat** view
(a chat-style mirror of the tmux pane — send follow-ups here) and a
**Terminal** view (the raw session — approve permission prompts, scroll
history, copy output). The dot turns blue (completed) or red (error).
Tasks survive pod restarts; output and history persist under
`~/.claude-tasks/`.

> :::scenario
> **Pattern: hand off long-running refactors.**
> Start a build with `Refactor src/auth to use the new TokenStore`, close
> the tab, come back an hour later. The task kept running; the Chat view
> shows the full history. If Claude asked a permission question while you
> were away, the Terminal tab lets you answer it.
> :::

## Save your first memory

Memory lets you tell Claude something *once* and have it remembered
across every future task and CTO conversation.

1. Go to **Memory** → **+ New memory**.
2. Namespace: `user.preferences.editor`. Key: `editor`. Value:
   `neovim`.
3. **Save.**

Now in any task, ask Claude *"what editor do I prefer?"* — it looks the
fact up (via its `memory_search` tool) and answers "neovim" without you
re-supplying it. Memory you tag `secret` (tokens, private notes) stays
readable on explicit lookup but is kept out of any auto-injected
`<workspace_memories>` block.

## Where to go next

- **[Tasks → Concepts](/docs/tasks-concepts)** — what really happens when a build runs.
- **[Memory → Concepts](/docs/memory-concepts)** — namespaces, importance, how Claude reads memory back.
- **[Triggers → Webhooks](/docs/triggers-webhooks)** — fire builds from external systems.
- **[Tasks → HTTP API](/docs/tasks-api)** — script the workspace from outside.
- **[Tasks → Assistants](/docs/tasks-assistants)** — swap Claude for OpenRouter or a self-hosted GPU model.

If something seems broken, check **Settings → Metrics** for live CPU /
memory / disk usage — most "Claude is slow today" turns out to be
"the pod is at 95% RAM."
