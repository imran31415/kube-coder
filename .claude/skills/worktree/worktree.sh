#!/usr/bin/env bash
# kube-coder Phase 0 worktree helper.
# One git worktree + one branch + one port lease per session, so concurrent
# Claude sessions and builds stop fighting over a shared checkout.
#
# Pure userland — no infra/PR needed. Subcommands:
#   new [slug] [base-ref]   create a worktree (+branch kc/<slug>) and lease a port
#   list                    show all live worktrees for the current repo
#   rm <slug> [--force]     remove a worktree dir (branch is KEPT)
#   port                    just print a free port (skips reserved + leased)
#
# `new` prints a shell env block on stdout (KC_WT / KC_WT_BRANCH / PORT) and a
# human summary on stderr, so callers can:  eval "$(worktree.sh new foo)"; cd "$KC_WT"
set -euo pipefail

WT_ROOT="${KC_WORKTREE_ROOT:-/home/dev/.worktrees}"
# kube-coder's reserved in-pod ports (server.py INTERNAL_PORTS) — never lease these.
RESERVED="22 2376 5900 6080 6081 7681 8080"
PORT_LO="${KC_WT_PORT_LO:-3000}"
PORT_HI="${KC_WT_PORT_HI:-3999}"

die() { echo "worktree: $*" >&2; exit 1; }

slugify() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' \
    | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' | cut -c1-40
}

repo_root() {
  git -C "${1:-$PWD}" rev-parse --show-toplevel 2>/dev/null \
    || die "not inside a git repo — cd into one first"
}

# true (rc 0) if something is already listening on 127.0.0.1:<port>.
# Uses bash /dev/tcp so we don't depend on ss/netstat being installed.
port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

# lowest free TCP port in [PORT_LO,PORT_HI], skipping reserved + already-leased.
free_port() {
  local leased p
  leased=$(cat "$WT_ROOT"/*/*/.kc-worktree.json 2>/dev/null \
    | jq -r '.port' 2>/dev/null || true)
  for p in $(seq "$PORT_LO" "$PORT_HI"); do
    case " $RESERVED " in *" $p "*) continue ;; esac
    grep -qx "$p" <<<"$leased" && continue
    port_in_use "$p"          && continue
    printf '%s\n' "$p"; return 0
  done
  die "no free port in ${PORT_LO}-${PORT_HI}"
}

cmd_new() {
  local raw="${1:-}" base="${2:-}"
  local root name slug wt branch port
  root=$(repo_root)
  name=$(basename "$root")
  # slug precedence: explicit arg > current task id > time-ish + $RANDOM
  [ -n "$raw" ] || raw="${KC_TASK_ID:-}"
  [ -n "$raw" ] || raw="$(date +%H%M%S)-$RANDOM"
  slug=$(slugify "$raw")
  [ -n "$slug" ] || die "empty slug"
  wt="$WT_ROOT/$name/$slug"
  branch="kc/$slug"

  [ -e "$wt" ] && die "worktree exists: $wt  (reuse it, or: worktree.sh rm $slug)"
  mkdir -p "$WT_ROOT/$name"

  # branch from base-ref if given, else current HEAD; reuse branch if it exists.
  if git -C "$root" show-ref --quiet --verify "refs/heads/$branch"; then
    git -C "$root" worktree add "$wt" "$branch" >&2
  elif [ -n "$base" ]; then
    git -C "$root" worktree add -b "$branch" "$wt" "$base" >&2
  else
    git -C "$root" worktree add -b "$branch" "$wt" >&2
  fi

  port=$(free_port)
  jq -n --arg slug "$slug" --arg repo "$name" --arg path "$wt" \
        --arg branch "$branch" --argjson port "$port" \
        --arg root "$root" --arg task "${KC_TASK_ID:-}" \
    '{slug:$slug, repo:$repo, path:$path, branch:$branch, port:$port,
      source_root:$root, task_id:$task}' > "$wt/.kc-worktree.json"
  # keep the meta file out of `git status` / commits (shared exclude, idempotent).
  local exclude="$root/.git/info/exclude"
  if [ -f "$exclude" ] && ! grep -qx '.kc-worktree.json' "$exclude"; then
    printf '.kc-worktree.json\n' >> "$exclude"
  fi

  {
    echo "worktree ready:"
    echo "  path    $wt"
    echo "  branch  $branch"
    echo "  port    $port   (preview: /api/app-proxy/$port/)"
    echo "  cd \"$wt\""
  } >&2
  # machine-readable env for: eval "$(worktree.sh new ...)"
  echo "export KC_WT=$(printf '%q' "$wt")"
  echo "export KC_WT_BRANCH=$(printf '%q' "$branch")"
  echo "export PORT=$port KC_PORT=$port"
}

cmd_list() {
  local root name; root=$(repo_root); name=$(basename "$root")
  echo "worktrees for $name:"
  git -C "$root" worktree list
  echo
  local m
  for m in "$WT_ROOT/$name"/*/.kc-worktree.json; do
    [ -e "$m" ] || continue
    jq -r '"  \(.slug)\tport \(.port)\tbranch \(.branch)"' "$m"
  done
}

cmd_rm() {
  local slug="${1:-}" force="${2:-}"
  [ -n "$slug" ] || die "usage: worktree.sh rm <slug> [--force]"
  slug=$(slugify "$slug")
  local meta wt root branch
  meta=$(ls "$WT_ROOT"/*/"$slug"/.kc-worktree.json 2>/dev/null | head -1 || true)
  [ -n "$meta" ] || die "no worktree with slug '$slug'"
  wt=$(dirname "$meta")
  root=$(jq -r '.source_root' "$meta")
  branch=$(jq -r '.branch' "$meta")

  # ignore our own meta file when deciding "dirty".
  local dirty
  dirty=$(git -C "$wt" status --porcelain 2>/dev/null | grep -v '\.kc-worktree\.json$' || true)
  if [ "$force" != "--force" ] && [ -n "$dirty" ]; then
    die "$wt has uncommitted changes — commit them, or re-run: rm $slug --force"
  fi
  if [ "$force" = "--force" ]; then
    git -C "$root" worktree remove --force "$wt"
  else
    git -C "$root" worktree remove "$wt"
  fi
  git -C "$root" worktree prune
  echo "removed $wt" >&2
  echo "branch '$branch' KEPT — delete when done: git -C $(printf '%q' "$root") branch -D $branch" >&2
}

case "${1:-new}" in
  new)  shift; cmd_new "${1:-}" "${2:-}" ;;
  list) cmd_list ;;
  rm)   shift; cmd_rm "${1:-}" "${2:-}" ;;
  port) free_port ;;
  *)    die "unknown command '$1' (new|list|rm|port)" ;;
esac
