# Chat vs Builds

> **One sentence each.** **Chat** is an interactive conversation with
> an assistant that can see and act on your workspace. **Builds** are
> dispatched background jobs — you hand over a prompt, close the tab,
> and come back to the result.

Both run *real* agents (Claude, OpenCode, …) against the same live
`/home/dev`. The difference is the interaction model, not the power.

## Chat

The **Chat** tab (bottom-nav on mobile, Mission Control → Chat in the
rail) is a threaded conversation. You ask, it answers; you steer
mid-flight; the thread persists so you can pick it back up tomorrow.

Reach for Chat when you want to:

- **Ask about the workspace** — *"what's running on port 3000?"*,
  *"why is this build failing?"*
- **Think out loud** before committing to an approach.
- **Iterate in small steps**, reading each answer before the next
  instruction.

Chats live under **Active** and **Past**, can be renamed, deleted and
restored, and each one remembers the agent, model and starting folder
you picked.

> **Why the code says "Hypervisor".** Chat is the user-facing name.
> Internally — route paths (`/hypervisor`), stores, and the
> `hypervisor_session.py` backend — the same subsystem is still called
> the Hypervisor. Same thing, two audiences.

## Builds

The **Builds** tab (**New build**) dispatches a task: a `claude` /
`opencode` process in its own tmux session, with a transcript on disk.
It runs whether or not you're watching, and its status shows up in the
task list, the Feed, and mobile push.

Reach for Builds when you want to:

- **Hand off a self-contained job** — *"run the test suite in ./api,
  fix what fails, open a PR"*.
- **Run several things at once** — each build is its own session.
- **Fire and forget**, then read the transcript later.

Seed the job in the **First prompt** field of the New Build form and
the agent starts on it immediately. Leave that field empty and the
session boots into a live terminal instead, waiting for you to type —
useful when you want a REPL rather than a job.

### Saved prompts

Prompts you run often can be saved as **templates** from the New Build
form: type the prompt, hit **Save as template**, name it. Saved
templates appear as chips under the field — click one to fill the
prompt, click its **×** to delete it. Templates are stored in your
browser (per-browser, not synced across devices).

## Which one?

| You want to… | Use |
| --- | --- |
| Ask a question about your workspace | **Chat** |
| Debug something interactively | **Chat** |
| Hand over a task and walk away | **Builds** |
| Run three jobs in parallel | **Builds** |
| Get a live preview while iterating on an app | **AI CTO** (a Chat with a project bound to it) |

Still not sure? Start in **Chat** — you can always ask it to dispatch a
build for you.
