#!/usr/bin/env bash
# kc-issue: deterministic "one issue -> one clean worktree" setup for kube-coder.
#
# Does the filesystem/git half of the framework so it's reliable and testable:
#   1. fetch a FRESH origin/main (never trust a stale/dirty local HEAD)
#   2. resolve the issue (gh) -> title/body/url/labels  (numeric arg = issue #)
#   2b. LINT the body before spending anything on it (#569)
#   3. create OR reuse an isolated worktree branched from origin/main
#   4. write a ready-to-use agent prompt into the worktree
#   5. print a JSON blob {issue,title,url,worktree,branch,port,prompt_file,lint}
#
# It does NOT launch the agent -- the caller (the Hypervisor) reads the JSON and
# launches a background task with workdir=<worktree>, so the agent is BORN inside
# its worktree and cannot forget to use it.
#
# Usage:
#   kc-issue.sh <issue-number>            # e.g. kc-issue.sh 284  (or "#284")
#   kc-issue.sh <slug> "free text desc"   # ad-hoc, no GitHub issue
#   kc-issue.sh list                      # show issue worktrees for the repo
#   kc-issue.sh lint [file]               # lint a body (file or stdin); no git
#
# Env:
#   KC_AUTO_PR=1       bake "open a PR" into the done-list
#   KC_ISSUE_STRICT=1  refuse to spawn when the body trips a lint blocker
set -euo pipefail

REPO_ROOT="${KC_REPO_ROOT:-/home/dev/kube-coder}"
REPO_SLUG="${KC_REPO_SLUG:-imran31415/kube-coder}"
WT_HELPER="$REPO_ROOT/.claude/skills/worktree/worktree.sh"
AUTO_PR="${KC_AUTO_PR:-0}"   # set to 1 to bake "open a PR" into the done-list
# Refuse to spawn when the issue body trips a lint blocker (default: warn only).
# Env rather than a flag to match KC_AUTO_PR / KC_REPO_* — the positional args
# are already <issue|slug> [desc] and adding a flag would mean reworking parsing.
STRICT="${KC_ISSUE_STRICT:-0}"

die() { echo "kc-issue: $*" >&2; exit 1; }

# ── Issue-body lint (#569) ──────────────────────────────────────────────────
# The body is baked verbatim into the agent prompt, so a thin issue silently
# produces a thin prompt: the agent spawns, burns a worktree and real tokens,
# and only discovers the ambiguity 20 minutes in — or doesn't, and confidently
# ships the wrong thing. This looks at the text BEFORE any of that is spent.
#
# Warn, never block (unless KC_ISSUE_STRICT=1). A terse issue is sometimes
# genuinely fine and the operator knows it, so the script surfaces the signal
# and leaves the judgement to a human.
#
# Findings go to STDERR and into the JSON. Never stdout — that is the caller's
# machine-readable contract and anything else there breaks its `jq`.
LINT_FINDINGS=()   # "severity|code|message"
LINT_BLOCKERS=0

_lint_add() {  # severity code message
  LINT_FINDINGS+=("$1|$2|$3")
  [ "$1" = blocker ] && LINT_BLOCKERS=$((LINT_BLOCKERS + 1))
  return 0
}

# lint_issue_body <body> [labels_csv]
# Populates LINT_FINDINGS/LINT_BLOCKERS. Always returns 0 — the caller decides
# what to do about the findings.
lint_issue_body() {
  local body="${1:-}" labels="${2:-}"
  LINT_FINDINGS=(); LINT_BLOCKERS=0

  # Lowercased copy for case-insensitive matching; `compact` strips whitespace
  # so "near-empty" means real content, not blank lines and a heading.
  local lower compact
  lower=$(printf '%s' "$body" | tr '[:upper:]' '[:lower:]')
  compact=$(printf '%s' "$body" | tr -d '[:space:]')

  # 1. needs-scoping label — the repo itself says this isn't ready to implement.
  #    Cheapest and strongest signal, so it is the one blocker.
  if printf '%s' "$labels" | tr '[:upper:]' '[:lower:]' | grep -q 'needs-scoping'; then
    _lint_add blocker needs-scoping \
      "issue carries the 'needs-scoping' label — the repo says it is not ready to implement"
  fi

  # 2. Empty / near-empty body — a title-only issue gives the agent a headline.
  if [ "${#compact}" -lt 80 ]; then
    _lint_add warn thin-body \
      "body is empty or near-empty (${#compact} non-whitespace chars) — the agent gets little beyond the title"
  fi

  # 3. No acceptance criteria — nothing states what "done" looks like, so the
  #    agent invents its own bar.
  if ! grep -qE 'acceptance|criteria|done when|definition of done|- \[ \]|- \[x\]' <<<"$lower"; then
    _lint_add warn no-acceptance-criteria \
      "no acceptance criteria found — nothing states what 'done' looks like"
  fi

  # 4. No file/path/component pointer — the agent has to guess where the change
  #    goes, and guessing wrong is expensive to unwind.
  if ! grep -qE '[a-z0-9_./-]+\.(sh|py|ts|tsx|js|jsx|ya?ml|json|md|toml)|`[^`]*/[^`]*`|(charts|scripts|docs|mobile|devlaptop|provisioner)/' <<<"$lower"; then
    _lint_add warn no-file-pointers \
      "no file/path/component pointers — the agent has to guess where the change goes"
  fi

  # 5. No verification path — nothing says how to confirm the change works.
  if ! grep -qE 'test|verif|reproduc|repro |how to check|expected|assert|preflight' <<<"$lower"; then
    _lint_add warn no-verification \
      "no verification path — nothing says how to confirm the change works"
  fi

  # 6. Unquantified comparatives — "make it faster" with no target. Only fires
  #    when the body carries no digit at all, so "cut p95 to 200ms" is fine.
  if grep -qE 'faster|slower|improve|better|optimi[sz]e|clean up|cleanup|reduce|speed up' <<<"$lower" \
     && ! grep -qE '[0-9]' <<<"$body"; then
    _lint_add warn unquantified-comparative \
      "comparative goal with no number (faster/improve/reduce/…) — no target to hit or measure"
  fi
  return 0
}

# Print findings for a human. stderr only.
lint_report() {
  [ "${#LINT_FINDINGS[@]}" -eq 0 ] && return 0
  echo "kc-issue: lint: ${#LINT_FINDINGS[@]} finding(s) on this issue body:" >&2
  local f sev code msg
  for f in "${LINT_FINDINGS[@]}"; do
    sev="${f%%|*}"; code="${f#*|}"; code="${code%%|*}"; msg="${f##*|}"
    echo "kc-issue: lint:   [$sev] $code: $msg" >&2
  done
  if [ "$LINT_BLOCKERS" -gt 0 ] && [ "$STRICT" != 1 ]; then
    echo "kc-issue: lint: proceeding anyway (set KC_ISSUE_STRICT=1 to refuse on blockers)." >&2
  elif [ "$STRICT" != 1 ]; then
    echo "kc-issue: lint: warnings only — proceeding." >&2
  fi
  return 0
}

# Findings as a JSON array, for the machine-readable result.
lint_json() {
  local f sev code msg
  # jq is a hard dependency of the spawn path, but `lint` is meant to run
  # anywhere (CI, a laptop). Degrade to an empty array rather than dying — the
  # human report on stderr is the part that matters offline.
  command -v jq >/dev/null 2>&1 || { echo '[]'; return 0; }
  { for f in "${LINT_FINDINGS[@]:-}"; do
      [ -n "$f" ] || continue
      sev="${f%%|*}"; code="${f#*|}"; code="${code%%|*}"; msg="${f##*|}"
      jq -n --arg s "$sev" --arg c "$code" --arg m "$msg" \
        '{severity:$s, code:$c, message:$m}'
    done; } | jq -s '.'
}

# Only the git-touching subcommands need the repo + worktree helper. `lint` is
# pure text analysis, so it must stay runnable anywhere — including CI and a
# laptop that has no pod layout.
require_repo() {
  [ -f "$WT_HELPER" ] || die "worktree helper missing at $WT_HELPER"
  [ -d "$REPO_ROOT/.git" ] || die "$REPO_ROOT is not a git repo"
}

cmd_list() {
  require_repo
  echo "# issue worktrees for $REPO_SLUG" >&2
  git -C "$REPO_ROOT" worktree list | grep -E 'kc/issue-|issue-' || echo "(none)" >&2
}

# kc-issue.sh lint [file]   (no file / "-" => stdin)
# Lint a body without touching GitHub, git, or a worktree. Exists so every
# signal is exercisable offline (the repo has no shell test harness) and so an
# operator can sanity-check a draft before filing it. Prints the findings as
# JSON on stdout and the human report on stderr; exits 1 on a blocker.
cmd_lint() {
  local src="${1:--}" body
  if [ "$src" = "-" ]; then body=$(cat)
  else [ -f "$src" ] || die "no such file: $src"; body=$(cat "$src"); fi
  lint_issue_body "$body" "${KC_ISSUE_LABELS:-}"
  lint_report
  lint_json
  [ "$LINT_BLOCKERS" -gt 0 ] && return 1
  return 0
}

cmd_new() {
  require_repo
  local arg="${1:?usage: kc-issue.sh <issue-number|slug> [desc]}" ; shift || true
  local desc="${*:-}"
  local n slug title body url labels="" is_gh_issue=0

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
    # `labels` feeds the needs-scoping lint signal (#569) — we did not fetch it
    # before. An older gh that omits the field degrades to an empty list rather
    # than failing the spawn.
    ij=$(gh issue view "$n" --repo "$REPO_SLUG" --json number,title,body,url,labels 2>/dev/null) \
      || die "could not fetch issue #$n via gh (does it exist? is gh authed?)"
    title=$(jq -r '.title' <<<"$ij")
    body=$(jq -r '.body // ""' <<<"$ij")
    url=$(jq -r '.url' <<<"$ij")
    labels=$(jq -r '[(.labels // [])[].name] | join(",")' <<<"$ij" 2>/dev/null || true)
  else
    # The ad-hoc path is MORE exposed than the issue path: a one-line --desc
    # becomes the entire specification, and nothing else ever looks at it.
    title="$desc"; body="$desc"; url="(no GitHub issue)"
  fi

  # 2b. Lint the body BEFORE spending a worktree or a spawn on it (#569).
  # Warn and continue by default; KC_ISSUE_STRICT=1 refuses on a blocker.
  lint_issue_body "$body" "$labels"
  lint_report
  if [ "$STRICT" = 1 ] && [ "$LINT_BLOCKERS" -gt 0 ]; then
    die "refusing to spawn: $LINT_BLOCKERS lint blocker(s) and KC_ISSUE_STRICT=1 (unset it to proceed anyway)"
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
    # worktree.sh derives the repo from $PWD, so run it FROM the repo — never
    # rely on the caller's cwd (that made this cwd-dependent and flaky).
    env_block=$(cd "$REPO_ROOT" && bash "$WT_HELPER" new "$slug" origin/main) \
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

  # 5. machine-readable result for the caller. `lint`/`lint_blockers` are
  # additive (#569) so existing consumers keep working; the skill renders them
  # so the operator sees the findings before deciding to proceed.
  jq -n \
    --arg issue "$n" --arg title "$title" --arg url "$url" \
    --arg worktree "$wt" --arg branch "$branch" \
    --arg port "${port:-}" --arg prompt_file "$prompt_file" \
    --arg auto_pr "$AUTO_PR" \
    --argjson lint "$(lint_json)" \
    --argjson lint_blockers "$LINT_BLOCKERS" \
    '{issue:$issue, title:$title, url:$url, worktree:$worktree,
      branch:$branch, port:$port, prompt_file:$prompt_file, auto_pr:$auto_pr,
      lint:$lint, lint_blockers:$lint_blockers}'
}

case "${1:-}" in
  ""|-h|--help) die "usage: kc-issue.sh <issue-number|slug> [desc] | list | lint [file]" ;;
  list)         cmd_list ;;
  lint)         shift; cmd_lint "$@" ;;
  *)            cmd_new "$@" ;;
esac
