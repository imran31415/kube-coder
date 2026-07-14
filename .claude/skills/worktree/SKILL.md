---
name: worktree
description: Create an isolated git worktree + branch + port for this session so concurrent kube-coder sessions and builds don't conflict. Use at the START of any change you want isolated, when two sessions touch the same repo, or before running a dev server/build that would collide on ports or output dirs. Also lists and tears down worktrees.
user-invocable: true
allowed-tools: Bash, Read
argument-hint: "[slug] | list | rm <slug> [--force]  (default: new, slug from task id)"
---

# Per-session git worktree (Phase 0)

kube-coder isolates sessions at the *terminal* level (each task gets its own
tmux session + `~/.claude-tasks/<id>/`) but everything still runs in the **same
`/home/dev` filesystem**. So two sessions on the same repo collide three ways:

- **git tree** — branch/checkout/index thrash, a dirty working tree
- **build output** — both write `./dist`, `./build`; Kaniko reads its context from the shared path
- **dev-server ports** — `EADDRINUSE` on `:3000` (ports are per-*pod*, not per-session)

This skill fixes all three for the current session with **no infra change**: it
gives you a dedicated `git worktree` (own checkout + branch) at a path outside
the repo, and leases a free port for any server you start. Worktrees share the
main clone's object store, so they're cheap on disk — only working files are
duplicated.

The helper is `worktree.sh` next to this file. Invoke it as
`bash "$CLAUDE_SKILL_DIR/worktree.sh" …` (or with its absolute path under
`/home/dev/kube-coder/.claude/skills/worktree/`).

## Start an isolated session (the common path)

Run from **inside the repo** you want to work on (`cd` there first). `$ARGUMENTS`
is an optional human slug; if omitted it derives one from the current task id
(`KC_TASK_ID`) or a random token, so it's always unique.

```bash
WT=/home/dev/kube-coder/.claude/skills/worktree/worktree.sh
eval "$(bash "$WT" new "$ARGUMENTS")"   # creates worktree+branch+port, sets KC_WT / PORT
cd "$KC_WT"                              # <-- this cd PERSISTS across your Bash calls
```

After this, **do all work for the change from `$KC_WT`.** The human summary
(path / branch / port / preview URL) is printed to stderr — note the **port**,
because exported env vars do *not* survive between separate Bash tool calls (the
`cd` does). The worktree also records everything in `$KC_WT/.kc-worktree.json`,
so you can re-read the port later:

```bash
jq -r .port "$KC_WT/.kc-worktree.json"   # or read .branch / .path
```

### What you get

- worktree dir: `/home/dev/.worktrees/<repo>/<slug>/`
- branch: `kc/<slug>` (branched from current HEAD, or a base-ref: `new <slug> <base>`)
- leased port in 3000–3999 (skips kube-coder's reserved ports and other leases)

## Running a dev server / preview

Bind the leased port explicitly (env doesn't persist between calls), then open
it through the app-proxy — no port config in the dashboard needed:

```bash
PORT=$(jq -r .port "$KC_WT/.kc-worktree.json")
cd "$KC_WT" && PORT=$PORT npm run dev      # or: python -m http.server "$PORT", etc.
# preview at:  /api/app-proxy/<port>/
```

## Builds

Because the worktree is its own path, `docker-build` (Kaniko) and local build
output (`./dist`, `./build`) no longer collide with other sessions. Run builds
with the worktree as context — e.g. `docker-build -t img:tag "$KC_WT"`.

## List / tear down

```bash
bash "$WT" list                 # worktrees + leased ports for the current repo
bash "$WT" rm <slug>            # remove the worktree DIR; the branch is KEPT
bash "$WT" rm <slug> --force    # also drop uncommitted changes in it
```

`rm` deliberately keeps `kc/<slug>` — that branch *is* your change. Push/PR it as
usual, then delete with the `git branch -D` line `rm` prints. `rm` refuses to
delete a dirty worktree unless you pass `--force`.

## Footguns

1. **Uncommitted work stays behind.** A worktree branches from **HEAD** (the
   committed state). Anything uncommitted on your original checkout is *not*
   carried in. Commit or stash first if you need it.
2. **`cd` persists, env does not.** In this harness the working directory
   survives between Bash calls but exported vars don't. Rely on `cd "$KC_WT"`
   and re-read the port from `.kc-worktree.json` when you need it; don't assume
   `$PORT` is still set in a later call.
3. **Disk.** Worktrees share `.git`, but a fresh `node_modules` / `.venv` /
   build tree per worktree is *not* shared and can fill the 20–50Gi PVC. Keep a
   handful live at once; `rm` ones you're done with.
4. **One repo, one clone.** All worktrees for a repo register under that clone's
   `.git/worktrees`. Don't `rm -rf` a worktree dir by hand — use `rm` (which runs
   `git worktree remove` + `prune`) so git's bookkeeping stays consistent.
5. **Not for the boot-time kube-coder pull.** `start.sh` does
   `git pull --ff-only origin main` on `/home/dev/kube-coder` at boot. Working in
   a `kc/<slug>` worktree keeps `main` clean so that pull still fast-forwards —
   which is the point. Don't check out `main` inside a worktree.

## Scope (why this is "Phase 0")

Pure userland, works today, teaches us the naming/cleanup ergonomics. It does
**not** yet integrate with the Task API or dashboard — the worktree/branch/port
aren't shown in the task UI and aren't auto-created when a task starts, and the
port lease is advisory (first-come). Phase 1 moves this into `server.py`'s
`create_task()` (record worktree in `task.json`, clean up in `delete_task`,
surface it in `NewTaskForm`/task detail). Sanity-check the helper before relying
on it:

```bash
bash -n /home/dev/kube-coder/.claude/skills/worktree/worktree.sh
```
