# Tasks — concepts

> **What a task is.** A `claude` (or `opencode`, or `harness.py`)
> process running inside a named tmux session in your pod, with a
> JSONL transcript on disk. The dashboard's **Build** tab is a view
> over that process — start, observe, message, stop.

## Lifecycle

```
        pending ─► running ─► completed     (clean exit)
                       └────► error         (non-zero exit)
                       └────► killed        (you hit Stop)
                       └────► terminated    (a pod restart wiped it)
```

`pending` is brief — the dashboard creates the task row before tmux
finishes spawning. Once the pane is alive the status flips to
`running` and stays there until the underlying CLI exits.

Each task has:

- **`task_id`** — a stable identifier (`claude-2026-05-16-…`); used in
  the URL, the tmux session name (`claude-{task_id}`), and the
  `~/.claude-tasks/{task_id}/` directory.
- **`name`** — human-readable, optional, editable from the detail header.
- **`prompt`** — the initial instruction.
- **`workdir`** — the directory the CLI starts in. Defaults to
  `/home/dev`; pick a project root for less typing.
- **`assistant`** — which CLI/model backs this task.
  See [Assistants](/docs/tasks-assistants).

## Where state lives

```
~/.claude-tasks/
├── {task_id}/
│   ├── meta.json         # status, name, prompt, assistant, …
│   ├── output.log        # tmux pipe-pane mirror (everything the pane saw)
│   ├── followups.jsonl   # your follow-up messages, append-only
│   └── transcript/       # Claude's per-turn JSONL transcript (if claude CLI)
└── .api-token            # bearer token for the Task API
```

A pod restart drops the tmux session but keeps everything in
`~/.claude-tasks/`. Re-opening the task shows the transcript; the
session itself is gone, and anything that was still running reads
**`terminated`** — not `completed` (#792). That distinction matters: a
terminated task produced no result and may have left uncommitted work in
its worktree, so it needs re-running or finishing off, while a completed
one is done. Every status but `terminated` is something the task itself
did; `terminated` is something that happened to it.

## Two views, one task

- **Chat view** — friendly mirror. Refetches the tail of `output.log`
  while the task is `running`, pauses once it's terminal. Send
  follow-up messages from the composer; they're queued as new prompts
  to the same `claude` process.
- **Terminal view** — `ttyd` rendering of the live tmux pane. Use this
  when Claude asks for permission, when you need to scroll history, or
  when the chat view's "last 80 lines" isn't enough.

Both views write to the same place — sending a message from Chat and
typing in Terminal both end up in the same `claude` session.

## Attaching from SSH

The tmux session name is `claude-{task_id}`. From inside the pod:

```bash
tmux ls                       # list sessions
tmux attach -t claude-abc123  # attach
# Detach with C-b d, just like normal tmux.
```

The dashboard's terminal view is the same surface — you can have both
open at once.

## Stopping a task

- **Stop** (red button in the detail header) sends SIGTERM to the
  process group inside the tmux pane, then kills the session. Status
  flips to `killed`. Output is preserved.
- Closing the browser tab does **nothing** — the task keeps running.

## Follow-ups

The Chat composer sends a follow-up prompt via
`POST /api/claude/tasks/{id}/message` (see [HTTP API](/docs/tasks-api)).
Each follow-up is appended to `followups.jsonl` and replayed if the
task is resumed from a restart.

> :::scenario
> **Pattern: long-lived agent loop.**
> Start one task with a high-level goal, then drip follow-up
> instructions as you watch progress. The chat scrollback shows every
> turn so you can audit decisions after the fact.
> :::

## Common failure modes

- **Status stays `pending` for >10s.** Something failed during tmux
  spawn — check `/api/claude/tasks/{id}` for an `error` field.
- **Chat says "no output yet".** Claude is still booting (10–30s for
  cold-start of the API session). Switch to Terminal view to watch.
- **Followup returns "session not found".** The tmux session died. The
  task row will flip to `completed`/`killed` on next poll.
- **Permission prompt blocks forever.** Switch to Terminal view and
  answer it. The chat view doesn't render the interactive prompt
  cleanly.
