---
name: kc-issue
description: Spin up an isolated agent to work a kube-coder issue. Given an issue number, creates a clean git worktree branched from a freshly-fetched origin/main, pulls the issue text, and launches a background Claude task that is BORN inside that worktree (workdir=worktree) so it cannot work in the shared clone. Use whenever the user wants to "work on issue N", "start an agent on issue N", or set up a per-issue worktree. Also lists issue worktrees and their task status.
user-invocable: true
allowed-tools: Bash, Read, mcp__dashboard__create_task, mcp__dashboard__list_tasks, mcp__dashboard__get_task
argument-hint: "<issue-number> [--pr]  |  list  |  lint [file]  |  <slug> \"free text\""
---

# kc-issue — one issue, one clean worktree, one agent

This is the reliable entrypoint for per-issue work on **kube-coder**. It removes
the two things that made ad-hoc worktree use flaky:

1. It branches from a **freshly-fetched `origin/main`**, never a stale local HEAD.
2. It launches the agent with **`workdir` = the worktree**, so the agent starts
   *inside* its isolation and physically cannot forget to use it.

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
`{issue,title,url,worktree,branch,port,prompt_file,auto_pr,lint,lint_blockers}`.

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
cat <worktree>/.kc-issue-prompt.md
```

**Step 3 — launch the agent** with `mcp__dashboard__create_task`:
- `prompt`  = the full contents of `.kc-issue-prompt.md`
- `workdir` = the `worktree` path from the JSON  ← this is what forces isolation
- `assistant` = `claude` (default)

**Step 4 — confirm & report.** Tell the user: the task id, the branch, the
worktree path, and the preview port. Offer to show live output with
`mcp__dashboard__get_task`. If they want to embed a preview once a dev server is
up, use the port at `/api/app-proxy/<port>/`.

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
- **The agent runs inside the repo**, so from its cwd the repo skills
  (`worktree`, `kc-preflight`, `kc-ship-pr`) ARE in scope — the baked prompt
  tells it to use them. (Those skills are NOT in scope for *you* at /home/dev,
  which is why this orchestration skill lives in the user-global skills dir.)
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
- **Cleanup when done** (after the PR merges): remove the worktree dir but keep
  history via the repo's worktree helper:
  ```bash
  bash /home/dev/kube-coder/.claude/skills/worktree/worktree.sh rm issue-<N>
  git -C /home/dev/kube-coder branch -D kc/issue-<N>   # after merge only
  ```
- **Disk.** Each worktree gets its own `node_modules`/build output — keep only a
  handful live at once; tear down finished ones.

## Parallel: several issues at once
Just invoke this once per issue. Each gets its own worktree, branch, port, and
background task, so agents run truly in parallel without colliding. Use `list`
to keep track.
