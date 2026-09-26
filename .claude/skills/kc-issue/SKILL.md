---
name: kc-issue
description: Spin up an isolated agent to work a GitHub issue of kube-coder or any other repo checked out in the workspace. Given an issue number, pulls the issue text and launches a background Claude task that is BORN inside its own git worktree, branched from a freshly-fetched origin default branch, so it cannot work in the shared clone. Use whenever the user wants to "work on issue N", "start an agent on issue N", or set up a per-issue worktree. Also lists issue worktrees and their task status.
user-invocable: true
allowed-tools: Bash, Read, mcp__dashboard__create_task, mcp__dashboard__list_tasks, mcp__dashboard__get_task
argument-hint: "<issue-number> [--pr]  |  list  |  lint [file]  |  <slug> \"free text\""
---

# kc-issue — one issue, one clean worktree, one agent

This is the reliable entrypoint for per-issue work — on **kube-coder** or any
other repository checked out in the workspace. It removes the two things that
made ad-hoc worktree use flaky:

1. It branches from a **freshly-fetched `origin/<default branch>`**, never a
   stale local HEAD.
2. The agent is **launched inside its own worktree**, so it starts *inside* its
   isolation and physically cannot forget to use it.

**Which repository:** the git checkout you run the script from (any folder in
it, or one of its worktrees), or `KC_REPO_ROOT=/home/dev/<repo>`; the GitHub
repo is read from its `origin` remote (`KC_REPO_SLUG=owner/repo` overrides).
From `/home/dev` itself it falls back to `/home/dev/kube-coder`.

The heavy lifting is in `kc-issue.sh` next to this file. It does the git/fs part
and prints JSON; **you** (the assistant) do the launch via `create_task`.

## Do this when invoked

### `list` — show existing issue worktrees + task status
```bash
bash "$CLAUDE_SKILL_DIR/kc-issue.sh" list
```
Then call `mcp__dashboard__list_tasks` and correlate by worktree/branch so the
user sees which issues have a live agent.

### `<issue-number>` (optionally `--pr`) — start an agent on an issue

**Step 1 — create/reuse the worktree and get the prompt.** If the user passed
`--pr` (auto-open a PR when done), set `KC_AUTO_PR=1`:
```bash
# without --pr (default: preflight then STOP for review):
bash "$CLAUDE_SKILL_DIR/kc-issue.sh" <N>
# with --pr (preflight, push, open PR):
KC_AUTO_PR=1 bash "$CLAUDE_SKILL_DIR/kc-issue.sh" <N>
```
Capture the JSON it prints on stdout:
`{issue,title,url,worktree,branch,port,prompt_file,auto_pr,mode,repo_root,repo,slug,base_ref,lint,lint_blockers}`.

`mode` says who makes the worktree:
- **`server`** — this workspace's dashboard creates it when the Build launches
  (#701), so the Build is recorded with it: its **Changes** tab shows the diff
  and push command, and **Settings → Worktrees** lists it. `worktree` is where
  it will be; `port` is empty until launch.
- **`script`** — an older workspace: the script created the worktree itself.

**Step 1b — surface the issue-body lint (#569).** The script lints the body
*before* the worktree exists, because the body is baked verbatim into the agent
prompt: a thin issue silently produces a thin prompt, and the cost is paid
before anyone can react. Findings arrive two ways — human-readable on **stderr**,
and structured in the JSON's `lint` array (`{severity,code,message}`).

If `lint` is non-empty, **show the user the findings before Step 3** — a short
list, one per line. Then:

- **`lint_blockers > 0`** (today: the `needs-scoping` label — the repo itself
  saying this isn't ready to implement): **stop and ask** whether to proceed.
  Spawning on a `needs-scoping` issue is almost always wrong, so this is the one
  case where you wait for an explicit yes.
- **warnings only**: mention them in one line and continue. A terse issue is
  sometimes genuinely fine and the operator knows it — the script warns, it
  never blocks, and the human makes the call.

Never edit or enrich the issue body to silence a warning; the lint reports, it
doesn't rewrite.

**Step 2 — read the baked prompt** (do NOT reconstruct it — use the file):
```bash
cat <prompt_file>
```

**Step 3 — launch the agent** with `mcp__dashboard__create_task`:
- `prompt` = the full contents of `prompt_file`
- `assistant` = `claude` (default)
- with **`mode: "server"`**:
  - `workdir` = `repo_root`
  - `isolate` = `true`  ← this is what forces isolation
  - `worktree_slug` = `slug` (so re-running an issue continues its worktree)
  - `base_ref` = `base_ref`
- with **`mode: "script"`**: `workdir` = the `worktree` path ← forces isolation

A `409` whose code is `busy` means an agent is **already working this issue**
in that worktree — tell the user and offer its task instead of starting another.

**Step 4 — confirm & report.** Tell the user: the task id, and — from the
`worktree` object in the response (server mode) or the JSON (script mode) — the
branch, the worktree path and the preview port. Offer to show live output with
`mcp__dashboard__get_task`; the Build's **Changes** tab is where they review the
diff and copy the push command.

### `<slug> "free text"` — ad-hoc (no GitHub issue)
Same flow; the description text is used in place of an issue body — and it is
linted the same way. This path is **more** exposed than the issue path: a
one-line `--desc` becomes the entire specification the agent ever sees, so take
its warnings seriously.

### `lint [file]` — check a body without spawning anything
```bash
bash "$CLAUDE_SKILL_DIR/kc-issue.sh" lint path/to/draft.md   # or pipe on stdin
```
Touches no git, no GitHub, no worktree. Findings print on stderr, JSON on
stdout; exits 1 on a blocker. Useful for sanity-checking a draft issue before
filing it, and it's what `lint_test.sh` drives.

## Notes & footguns

- **The launched agent, not you, does the work.** Your job is only to set up the
  worktree and launch. Don't start editing repo files in this chat.
- **The agent runs inside the repo**, so from its cwd a repo's own skills are
  in scope. In kube-coder the baked prompt names `kc-preflight` / `kc-ship-pr`;
  in any other repo it tells the agent to run that repo's own tests instead.
  (Repo skills are NOT in scope for *you* at /home/dev, which is why this
  orchestration skill lives in the user-global skills dir.)
- **Idempotent.** Re-running for the same issue reuses the existing worktree +
  branch and rewrites the prompt; it won't clobber committed work.
- **The lint warns, it never blocks.** Default behaviour always proceeds. Set
  `KC_ISSUE_STRICT=1` to make the script *refuse* to spawn when a blocker fires
  — it exits before creating the worktree, so nothing is spent. Leave it unset
  unless you want that guardrail.
  ```bash
  KC_ISSUE_STRICT=1 bash "$CLAUDE_SKILL_DIR/kc-issue.sh" <N>
  ```
- **Lint runs at spawn time, not issue-creation time.** That's deliberate: it's
  the cheaper place to catch the problem, and it also covers issues written
  before the lint existed.
- **Cleanup when done** (after the PR merges): **Remove worktree** on the
  Build's Changes tab (or in Settings → Worktrees) deletes the folder and keeps
  the branch; delete the branch once it is merged. The dashboard also cleans up
  on its own — but never a worktree with uncommitted or unpushed work. From a
  shell:
  ```bash
  bash /home/dev/kube-coder/.claude/skills/worktree/worktree.sh rm issue-<N>
  git -C <repo_root> branch -D kc/issue-<N>   # after merge only
  ```
- **Disk.** Each worktree gets its own `node_modules`/build output — keep only a
  handful live at once; tear down finished ones.

## Parallel: several issues at once
Just invoke this once per issue. Each gets its own worktree, branch, port, and
background task, so agents run truly in parallel without colliding. Use `list`
to keep track.
