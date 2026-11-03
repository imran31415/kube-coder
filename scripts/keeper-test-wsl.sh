#!/usr/bin/env bash
# Run the keeper's gofmt + vet + -race gate inside WSL, from a Windows checkout.
#
# WHY THIS EXISTS. `make keeper-test` ends in `go test -race`, and the race
# detector needs CGO, which needs a 64-bit gcc. The gcc on a typical Windows
# box is 32-bit, so the race leg does not merely fail — it cannot build:
#
#   cc1.exe: sorry, unimplemented: 64-bit mode not compiled in
#
# The result is that the one gate protecting the keeper's concurrency gets
# skipped on the machine where the code is written, and only CI ever runs it.
# WSL has a 64-bit gcc, so the same gate runs there instead:
#
#   make keeper-test-race
#
# On Linux and macOS this script is unnecessary — `make keeper-test` already
# runs the race leg natively.
#
# It builds from a copy rather than the checkout because git's core.autocrlf
# writes the .go files out with CRLF on Windows, while git stores them with LF.
# gofmt counts a stray CR as unformatted, so gofmt -l would name every file and
# say nothing useful. Stripping CR into a tmpdir checks what git actually
# stores, which is also what CI checks.
set -u

KEEPER_SRC="$(cd "$(dirname "$0")/../keeper" && pwd)"
export PATH="/usr/local/go/bin:$PATH"

for tool in go gcc; do
    command -v "$tool" >/dev/null 2>&1 && continue
    echo "error: $tool is not on PATH inside WSL." >&2
    if [ "$tool" = gcc ]; then
        echo "  The race detector needs it. Install with:" >&2
        echo "    sudo apt-get update && sudo apt-get install -y gcc libc6-dev" >&2
    else
        echo "  Install the Go toolchain under /usr/local/go (go.dev/dl)." >&2
    fi
    exit 1
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Copy with CR stripped. nullglob so an unmatched pattern vanishes rather than
# being copied literally, and an empty result is an error rather than a silent
# pass on an empty package.
shopt -s nullglob
sources=("$KEEPER_SRC"/*.go)
if [ ${#sources[@]} -eq 0 ]; then
    echo "error: no .go files found in $KEEPER_SRC" >&2
    exit 1
fi

# go.sum only exists once the keeper takes a dependency; it is stdlib-only
# today, so copy what is there rather than assuming both files.
for meta in go.mod go.sum; do
    [ -f "$KEEPER_SRC/$meta" ] && sources+=("$KEEPER_SRC/$meta")
done
for f in "${sources[@]}"; do
    tr -d '\r' < "$f" > "$WORK/$(basename "$f")" || exit 1
done

cd "$WORK" || exit 1
echo "keeper: $(go version), $(gcc -dumpmachine) gcc"

status=0
unformatted="$(gofmt -l .)"
if [ -n "$unformatted" ]; then
    echo "gofmt needed:" >&2
    echo "$unformatted" >&2
    status=1
fi

go vet ./... || status=1
CGO_ENABLED=1 go test -race -count=1 ./... || status=1

[ $status -eq 0 ] && echo "keeper: gofmt, vet and -race all clean"
exit $status
