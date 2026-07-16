#!/usr/bin/env bash
# kc-issue: deterministic "one issue -> one clean worktree" setup for kube-coder.
#
# Does the filesystem/git half of the framework so it's reliable and testable:
#   1. fetch a FRESH origin/main (never trust a stale/dirty local HEAD)
#   2. resolve the issue (gh) -> title/body/url  (numeric arg = issue #)
#   3. create OR reuse an isolated worktree branched from origin/main
#   4. write a ready-to-use agent prompt into the worktree
#   5. print a JSON blob {issue,title,url,worktree,branch,port,prompt_file}
#
# It does NOT launch the agent -- the caller (the Hypervisor) reads the JSON and
# launches a background task with workdir=<worktree>, so the agent is BORN inside
# its worktree and cannot forget to use it.
#
# Usage:
#   kc-issue.sh <issue-number>            # e.g. kc-issue.sh 284  (or "#284")
#   kc-issue.sh <slug> "free text desc"   # ad-hoc, no GitHub issue
#   kc-issue.sh list                      # show issue worktrees for the repo
set -euo pipefail

REPO_ROOT="${KC_REPO_ROOT:-/home/dev/kube-coder}"
REPO_SLUG="${KC_REPO_SLUG:-imran31415/kube-coder}"
WT_HELPER="$REPO_ROOT/.claude/skills/worktree/worktree.sh"
AUTO_PR="${KC_AUTO_PR:-0}"   # set to 1 to bake "open a PR" into the done-list

die() { echo "kc-issue: $*" >&2; exit 1; }

[ -f "$WT_HELPER" ] || die "worktree helper missing at $WT_HELPER"
[ -d "$REPO_ROOT/.git" ] || die "$REPO_ROOT is not a git repo"

cmd_list() {
  echo "# issue worktrees for $REPO_SLUG" >&2
  git -C "$REPO_ROOT" worktree list | grep -E 'kc/issue-|issue-' || echo "(none)" >&2
}

cmd_new() {
  local arg="${1:?usage: kc-issue.sh <issue-number|slug> [desc]}" ; shift || true
  local desc="${*:-}"
  local n slug title body url is_gh_issue=0

  arg="${arg#\#}"
  if [[ "$arg" =~ ^[0-9]+$ ]]; then
    n="$arg"; slug="issue-$n"; is_gh_issue=1
  else
    # ad-hoc: slugify the arg, description is the remaining text
    slug=$(printf '%s' "$arg" | tr '[:upper:]' '[:lower:]' \
      | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' | cut -c1-40)
    n="$slug"
    [ -n "$slug" ] || die "empty slug"
  fi

  # 1. fresh main
  git -C "$REPO_ROOT" fetch origin main --quiet \
    || die "git fetch origin main failed (auth?)"

  # 2. resolve the issue text
  if [ "$is_gh_issue" = 1 ]; then
    source /home/dev/.credentials/.github-env 2>/dev/null || true
    local ij
    ij=$(gh issue view "$n" --repo "$REPO_SLUG" --json number,title,body,url 2>/dev/null) \
      || die "could not fetch issue #$n via gh (does it exist? is gh authed?)"
    title=$(jq -r '.title' <<<"$ij")
    body=$(jq -r '.body // ""' <<<"$ij")
    url=$(jq -r '.url' <<<"$ij")
  else
    title="$desc"; body="$desc"; url="(no GitHub issue)"
  fi

  # 3. create OR reuse the worktree (branched from FRESH origin/main)
  local wt branch port existing
  existing=$(git -C "$REPO_ROOT" worktree list --porcelain 2>/dev/null \
    | awk -v b="kc/$slug" '/^worktree /{p=$2} /^branch /{if($2=="refs/heads/"b)print p}')
  if [ -n "$existing" ] && [ -d "$existing" ]; then
    wt="$existing"
    branch="kc/$slug"
    port=$(jq -r '.port // empty' "$wt/.kc-worktree.json" 2>/dev/null || true)
    echo "kc-issue: reusing existing worktree $wt" >&2
  else
    local env_block
    env_block=$(bash "$WT_HELPER" new "$slug" origin/main) \
      || die "worktree helper failed"
    # env_block = export KC_WT=... KC_WT_BRANCH=... PORT=...
    eval "$env_block"
    wt="$KC_WT"; branch="$KC_WT_BRANCH"; port="$PORT"
  fi

  # 4. write the agent prompt INTO the worktree
  local done_list prompt_file
  if [ "$AUTO_PR" = 1 ]; then
    done_list=$'3. Run the **kc-preflight** skill (local CI mirror) and fix every failure.\n4. Commit to your branch with a message referencing (#'"$n"$').\n5. Run the **kc-ship-pr** skill to push and open a PR (base main). Put `Fixes #'"$n"$'` in the PR body.\n6. Report the PR URL.'
  else
    done_list=$'3. Run the **kc-preflight** skill (local CI mirror) and fix every failure.\n4. Commit to your branch with a message referencing (#'"$n"$').\n5. STOP. Do NOT push or open a PR. Report a concise summary and `git diff --stat`.'
  fi

  prompt_file="$wt/.kc-issue-prompt.md"
  cat > "$prompt_file" <<EOF
You are an autonomous agent assigned to kube-coder issue #${n}.

# Issue: ${title}
${url}

## Description
${body}

## Your workspace — READ THIS FIRST
You are ALREADY inside your own isolated git worktree:
  path:   ${wt}
  branch: ${branch}   (freshly branched from origin/main)

Do ALL work here. Do NOT \`cd\` to ${REPO_ROOT} or edit the shared clone.
Do NOT check out \`main\`. The repo's skills (worktree, kc-preflight, kc-ship-pr)
are in scope from this directory — use them.

## Definition of done
1. Understand the issue; read the relevant files (repo conventions live in ./CLAUDE.md).
2. Implement the change.
${done_list}

## Handy
- Dev-server/preview port for THIS worktree: ${port}  ->  /api/app-proxy/${port}/
- One fact per change; keep the branch focused on issue #${n} only.
EOF

  # 5. machine-readable result for the caller
  jq -n \
    --arg issue "$n" --arg title "$title" --arg url "$url" \
    --arg worktree "$wt" --arg branch "$branch" \
    --arg port "${port:-}" --arg prompt_file "$prompt_file" \
    --arg auto_pr "$AUTO_PR" \
    '{issue:$issue, title:$title, url:$url, worktree:$worktree,
      branch:$branch, port:$port, prompt_file:$prompt_file, auto_pr:$auto_pr}'
}

case "${1:-}" in
  ""|-h|--help) die "usage: kc-issue.sh <issue-number|slug> [desc] | list" ;;
  list)         cmd_list ;;
  *)            cmd_new "$@" ;;
esac
