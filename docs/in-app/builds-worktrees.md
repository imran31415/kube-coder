# Isolated worktrees

> **What it is.** Tick **Isolated worktree** when you start a Build and it
> gets its own copy of the repository's files, on its own git branch, with its
> own port for a dev server. Two Builds working on the same repo at the same
> time can no longer overwrite each other's files, commit onto each other's
> branch, or both try to use port 3000.

## Why it exists

Every Build in a workspace shares one disk. Without isolation, two Builds
started in `/home/dev/app` edit the **same** files in the **same** checkout:

```
without isolation                         with isolation
build A ─┐                                build A ─► ~/.worktrees/app/t-A   (kc/t-A,  :3100)
build B ─┼─► /home/dev/app (one tree)     build B ─► ~/.worktrees/app/t-B   (kc/t-B,  :3101)
build C ─┘                                build C ─► ~/.worktrees/app/t-C   (kc/t-C,  :3102)
                                                     └── one shared .git history
```

A worktree is a second (third, fourth…) folder that git keeps in step with
the same repository. The history is shared, so it is cheap: only the working
files are copied.

## Turning it on

- **New Build** — pick a folder marked **(git)** and tick *Isolated
  worktree*. Optionally fill in *Branch from* (a branch, tag or commit, e.g.
  `origin/main`); by default the worktree starts from the folder's current
  commit.
- **Board runs** — choose a *Repository* and leave *Isolated worktree per
  item* on. Every ticket gets its own worktree, and the same ticket always
  gets the same one — a sent-back ticket continues where it left off (see
  [Boards](/docs/board-processor)). The Review card shows each ticket's branch
  and a **View changes** link.
- **From an agent** — the dashboard tool `create_task` takes `isolate: true`
  (plus `base_ref` / `worktree_slug`), and the sub-agent tool `spawn_agent`
  takes `isolate: true` to fan work out: each sub-agent gets its own worktree,
  branched from the parent's current commit, and the parent merges their
  branches when they finish. The `kc-issue` skill uses this for per-issue work
  on any repository.
- **The API** — `POST /api/claude/tasks` with `"isolate": true` (and
  optionally `base_ref`, or `worktree_slug` to continue in a named worktree).

The option is only offered for git folders. A folder that is not inside a
repository, a repository with no commits yet, or `/home/dev` itself cannot be
isolated, and the Build is refused with the reason — nothing is created.

## What the Build sees

The agent starts **inside** its worktree
(`~/.worktrees/<repo>/<name>/`, or the matching sub-folder if you started it
in one) on branch `kc/<name>`, with these set:

| Variable | Meaning |
|---|---|
| `KC_WT` | the worktree folder |
| `KC_WT_BRANCH` | its branch |
| `PORT`, `KC_PORT` | the port its dev server should use |

The Preview tab opens on that port. The agent is told to commit on its
branch and not to switch branches.

## What is **not** isolated

A worktree separates files, the branch and the port. It does not separate:

- **Files git ignores** — `node_modules`, `.venv`, `.env`, build output. A new
  worktree starts without them; the agent installs or creates what it needs.
  A devcontainer's setup commands are not re-run for a worktree.
- **Shared workspace state** — memory (`memory.db`), the Build records in
  `~/.claude-tasks/` and your credentials in `~/.credentials/` are one copy
  for every Build.
- **CPU and memory** — parallel Builds still share the workspace's limits.
  On a small workspace, run fewer at once.

## Surviving a pod restart

A workspace pod restarts from time to time — resource pressure, node
recycling, maintenance — and a restart kills every Build's tmux session. The
worktree and its commits are on the persistent disk and survive; a Build's
**uncommitted** edits survive only as files nobody has recorded, and the Build
itself never gets to report what it did.

So when you start a long Build — especially when you fan several out — tell it
to **commit and push as soon as its change compiles**, before it validates or
writes up anything:

```
Commit and push your branch as soon as your change compiles — before you
validate, gather evidence or open a PR. Push early, push often.
```

A Build the restart caught mid-run reads **`terminated`**, not `completed`
(#792), which is the signal that its work is unfinished: open its Changes tab,
commit whatever is there as a labelled unvalidated checkpoint, and start a
short follow-up Build on that branch to rebase, validate and finish. Cleanup
never touches a worktree with uncommitted changes, so there is no rush.

## Reviewing and getting the work out

Open the Build and choose the **Changes** tab. It shows:

- the branch and the commit it started from, how many commits it has made,
  and whether the starting branch has moved on since;
- every file it changed — committed or not — with a diff for each;
- a ready-to-copy push command (it pushes to your `fork` remote when there is
  one, otherwise `origin`). Push, then open a pull request from the branch.

Clashes between two Builds now show up where they belong: as a normal merge
conflict when you combine their branches.

## Removing a worktree

**Remove worktree** on the Changes tab (or in **Settings → Worktrees**)
deletes the folder. **The branch and its commits are always kept** — the
toast tells you the branch name, and you delete the branch yourself once it
is merged.

- A running Build's worktree cannot be removed — stop the Build first.
- Nor can a worktree **any** live Build is working in, even one it does not
  own: a Build started with a worktree as its working directory (the
  `kc-issue` script flow, or one you made by hand with the `worktree` skill)
  holds that folder for as long as it runs. Removal is refused with
  `409 live` and names the Build, because on disk a worktree whose agent
  started seconds ago — clean, nothing to push, no remote branch — looks
  exactly like one whose work is finished and merged. Only task liveness
  tells them apart.
- A worktree with uncommitted changes asks again, spelling out that those
  changes will be lost.

## Automatic cleanup

Every 10 minutes the workspace tidies up — carefully. It only removes a
worktree when its Build has **finished** and the worktree holds nothing
unique:

| The worktree… | What happens |
|---|---|
| changed nothing at all | removed, and its empty branch deleted |
| has every commit on a remote, and its Build finished over 7 days ago | removed, branch kept |
| has uncommitted changes | kept |
| has commits that are on no remote | kept |
| belongs to a running Build | kept |
| has a live Build working in it, owned or not | kept |

**Settings → Worktrees** lists every worktree, who owns it and why cleanup
kept it, and has a *Clean up now* button.

## Limits

A workspace holds at most **20** worktrees by default. At the limit, a new
isolated Build is refused with a message pointing at Settings → Worktrees —
after the cleanup has first taken anything it safely can. The message counts
the ones pruning cannot help with, so you know before you start whether there
is anything to free:

```
worktree limit reached (20/20; 7 owned by running tasks and not removable).
Remove finished worktrees in Settings → Worktrees, or raise KC_MAX_WORKTREES.
```

> :::scenario
> **Two Builds, one repo.** You start "fix the login bug" and "add dark mode"
> on `/home/dev/app`, both isolated. They run side by side on `kc/t-…`
> branches and ports 3100 and 3101. Both touch `src/theme.ts`. Neither
> notices the other. When you merge the second branch, git shows the conflict
> in `src/theme.ts` — and you decide how the two changes fit together.
> :::

## Settings for workspace owners

| Variable | Default | Meaning |
|---|---|---|
| `KC_MAX_WORKTREES` | `20` | most worktrees at once |
| `KC_WORKTREE_ROOT` | `/home/dev/.worktrees` | where worktrees live |
| `KC_WORKTREE_GC_DAYS` | `7` | how long a finished, pushed worktree is kept |
| `KC_WORKTREE_GRACE_S` | `600` | never clean up a worktree younger than this |
| `KC_WORKTREE_SWEEP_INTERVAL_S` | `600` | how often cleanup runs |
| `KC_WORKTREE_SWEEP_KEEP_BRANCHES` | off | keep even empty branches when cleaning up |
| `KC_WT_PORT_LO` / `KC_WT_PORT_HI` | `3100` / `3999` | the port range handed out |
