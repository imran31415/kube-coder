#!/usr/bin/env bash
# Copy the workspace memory database to a throwaway path — the safe way to do
# schema, migration or ad-hoc script work against a realistic store (#599).
#
# WHY THIS EXISTS. /home/dev/.claude-memory/memory.db is live shared state:
# the dashboard's Memory tab, the MCP server and every concurrent agent write
# it. An agent doing migration work opened the live file read-write and left
# the workspace half-broken — new memories saved, updates failed with a UNIQUE
# constraint error, and nothing surfaced the breakage. A rule ("don't touch
# it") only gets followed when the correct alternative is one command, so:
#
#   export KC_MEMORY_DB="$(scripts/memory-db-copy.sh)"
#
# ...and every process you launch afterwards uses the copy instead
# (memory/store.py resolves KC_MEMORY_DB at import).
#
# The source is opened READ-ONLY (`file:...?mode=ro`) and duplicated with
# SQLite's online backup API, so this is safe while the server is writing and
# cannot mutate the source — a plain `cp` of a WAL database can silently
# produce a torn copy. (Like any reader, the read-only handle may materialise
# an empty -wal/-shm sidecar when nothing else has the DB open; the database
# file itself is never written.)
#
# Usage:
#   scripts/memory-db-copy.sh [DEST]
#     DEST          where to write the copy (default: a fresh mktemp -d dir)
#   Env:
#     KC_MEMORY_DB  source database (default /home/dev/.claude-memory/memory.db)
#
# Prints the copy's path — and nothing else — on stdout, so it composes:
#   export KC_MEMORY_DB="$(scripts/memory-db-copy.sh)"
# Progress and stats go to stderr.

set -euo pipefail

DEFAULT_DB=/home/dev/.claude-memory/memory.db
SRC="${KC_MEMORY_DB:-$DEFAULT_DB}"

case "${1:-}" in
  -h|--help)
    cat <<EOF
Usage: scripts/memory-db-copy.sh [DEST]

Copy the workspace memory database to a throwaway path, read-only at the
source, so schema/migration work never touches the live shared store (#599).

  DEST            where to write the copy (default: a fresh mktemp -d dir)
  \$KC_MEMORY_DB   source database (default $DEFAULT_DB)

Prints the copy's path on stdout:
  export KC_MEMORY_DB="\$(scripts/memory-db-copy.sh)"
EOF
    exit 0
    ;;
esac

DEST="${1:-}"
if [ -z "$DEST" ]; then
  DEST="$(mktemp -d -t kc-memory-XXXXXX)/memory.db"
fi

if [ ! -f "$SRC" ]; then
  echo "memory-db-copy: source database not found: $SRC" >&2
  echo "  (set KC_MEMORY_DB to point at one, or run this inside a workspace)" >&2
  exit 1
fi

mkdir -p "$(dirname "$DEST")"

# Python's stdlib sqlite3 gives us the online backup API and a genuinely
# read-only source handle; both matter more than saving a process spawn.
KC_COPY_SRC="$SRC" KC_COPY_DEST="$DEST" python3 - <<'PY' >&2
import os
import sqlite3
import sys

src_path = os.environ['KC_COPY_SRC']
dest_path = os.environ['KC_COPY_DEST']

# mode=ro: SQLite refuses any write to the source through this handle.
src = sqlite3.connect(f'file:{src_path}?mode=ro', uri=True)
try:
    dest = sqlite3.connect(dest_path)
    try:
        src.backup(dest)
    finally:
        dest.close()
finally:
    src.close()

check = sqlite3.connect(f'file:{dest_path}?mode=ro', uri=True)
try:
    ok = check.execute('PRAGMA integrity_check').fetchone()[0]
    try:
        rows = check.execute(
            'SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL'
        ).fetchone()[0]
    except sqlite3.Error:
        rows = 'n/a'
finally:
    check.close()

if ok != 'ok':
    print(f'memory-db-copy: integrity_check on the copy said: {ok}', file=sys.stderr)
    sys.exit(1)

print(f'memory-db-copy: copied {src_path} -> {dest_path} '
      f'({os.path.getsize(dest_path)} bytes, {rows} live memories)',
      file=sys.stderr)
PY

echo "memory-db-copy: source untouched. Point processes at the copy with:" >&2
echo "  export KC_MEMORY_DB=$DEST" >&2

echo "$DEST"
