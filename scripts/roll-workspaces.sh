#!/usr/bin/env bash
# Roll workspaces onto the current release image (devlaptop-$VERSION) with
# `make ship`, a few at a time, and report a per-workspace verdict.
#
#   scripts/roll-workspaces.sh                 # every RUNNING workspace
#   scripts/roll-workspaces.sh alice bob       # just these
#   PARALLEL=3 VERSION=v1.2.3 scripts/roll-workspaces.sh
#
# Why not just loop `make ship`:
#   * ship's exit code is unreliable: a Recreate rollout waits for the block
#     PVC to re-attach (and a node may pull a multi-GB image for the first
#     time), which outlasts ship's rollout-status timeout. Success is judged
#     instead by: every Helm release in ws-<user> `deployed`, the workspace
#     Deployment on devlaptop-$VERSION, and ready == updated == replicas == 1.
#   * home-network/API flakes are retried once; a `pending-*` Helm release is
#     NOT retried (it needs `helm rollback` to the last complete revision).
#   * stopped workspaces (replicas 0) are skipped when auto-discovering: the
#     chart would start them. Name one explicitly to roll it anyway.
#
# `make deploy` does no git operations, so concurrent ships are safe. The
# values.yaml repins are left uncommitted in the GitOps checkout for review.
set -uo pipefail

VERSION="${VERSION:?VERSION is required (make passes it)}"
PARALLEL="${PARALLEL:-3}"
WAIT_STEPS="${WAIT_STEPS:-42}"   # x10s, per attempt
LOGDIR="${LOGDIR:-$(mktemp -d "${TMPDIR:-/tmp}/kc-roll.XXXXXX")}"
PREFIX="${WORKSPACE_PREFIX:-ws-}"
MAKE="${MAKE:-make}"
mkdir -p "$LOGDIR"

discover_running() {
  kubectl get deploy -A -o jsonpath='{range .items[*]}{.metadata.namespace} {.metadata.name} {.spec.replicas}{"\n"}{end}' |
    awk -v p="$PREFIX" 'index($1,p)==1 && $2==$1 && $3+0>=1 {print substr($1,length(p)+1)}' | sort
}

state() { # <user> -> image|ready/updated/replicas|helm-statuses
  local u="$1" ns="$PREFIX$1" img rdy st
  img=$(kubectl -n "$ns" get deploy "$PREFIX$u" -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null)
  rdy=$(kubectl -n "$ns" get deploy "$PREFIX$u" -o jsonpath='{.status.readyReplicas}/{.status.updatedReplicas}/{.status.replicas}' 2>/dev/null)
  st=$(helm list -n "$ns" -a -o json 2>/dev/null | python3 -c 'import json,sys;print(",".join(sorted({r["status"] for r in json.load(sys.stdin)})))' 2>/dev/null)
  echo "${img##*:}|$rdy|$st"
}

roll_one() {
  local u="$1" log="$LOGDIR/$1.log" s attempt i
  for attempt in 1 2; do
    echo "=== attempt $attempt $(date -u +%FT%TZ)" >>"$log"
    "$MAKE" ship USER="$u" >>"$log" 2>&1
    for ((i = 0; i < WAIT_STEPS; i++)); do
      s=$(state "$u")
      case "$s" in
        "devlaptop-$VERSION|1/1/1|deployed") echo "OK     $u"; return 0 ;;
        *pending-*) echo "STUCK  $u  $s  (helm rollback to last complete rev, then re-run)"; return 2 ;;
      esac
      sleep 10
    done
    echo "--- not converged after attempt $attempt: $s" >>"$log"
  done
  echo "FAIL   $u  $s  (log: $log)"; return 1
}
export -f roll_one state
export VERSION PREFIX LOGDIR WAIT_STEPS MAKE

# Every `make ship` renders the charts from THIS checkout. Refuse to roll the
# fleet from a tree whose deployable files differ from the release tag (a
# feature branch or uncommitted chart edits would ship to everyone). Paths in
# charts/workspace/.helmignore (the dashboard sources under web/) never reach
# the cluster this way, so edits there are fine. FORCE=1 overrides.
deployable=(charts/workspace charts/base-infrastructure scripts Makefile ':!charts/workspace/web')
if [ "${FORCE:-0}" != 1 ]; then
  if ! git rev-parse -q --verify "refs/tags/$VERSION" >/dev/null; then
    echo "ERROR: release tag $VERSION not found locally (git fetch --tags)."; exit 1
  fi
  if ! git diff --quiet "$VERSION" -- "${deployable[@]}"; then
    echo "ERROR: this checkout's deployable files differ from $VERSION:"
    git diff --stat "$VERSION" -- "${deployable[@]}" | tail -5
    echo "Roll from a clean checkout of the tag, e.g.:"
    echo "  git worktree add ../kc-$VERSION $VERSION && make -C ../kc-$VERSION roll-fleet USERS_DIR=\$PWD/.users"
    echo "(or FORCE=1 to ship this tree anyway)"; exit 1
  fi
fi

users=()
if [ "$#" -gt 0 ]; then users=("$@"); else
  while IFS= read -r u; do [ -n "$u" ] && users+=("$u"); done < <(discover_running)
fi
[ "${#users[@]}" -gt 0 ] || { echo "no workspaces to roll"; exit 0; }

echo "Rolling ${#users[@]} workspace(s) to devlaptop-$VERSION, $PARALLEL at a time (logs: $LOGDIR)"
printf '%s\n' "${users[@]}" | xargs -P "$PARALLEL" -I{} bash -c 'roll_one "$@"' _ {} | tee "$LOGDIR/summary.txt"
bad=$(grep -cvE '^OK ' "$LOGDIR/summary.txt" || true)
echo "Done: $(grep -cE '^OK ' "$LOGDIR/summary.txt") ok, $bad not ok. Review + commit the repins in the GitOps checkout."
[ "$bad" -eq 0 ]
