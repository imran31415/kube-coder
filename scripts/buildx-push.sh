#!/usr/bin/env bash
#
# scripts/buildx-push.sh — wrap `docker buildx build --push` with a
# completion-watch + safety-kill so the recurring post-push CLI hang
# can't block the rest of `make ship`.
#
# Background:
# `docker buildx build --push` reliably finishes uploading the image
# manifest (logged as `pushing manifest … done` + `#NN DONE`), then the
# CLI sits idle for minutes. Cause varies: docker-container driver
# integration with Docker Desktop, registry slow-ack on the post-push
# verify, leftover attestation upload paths, etc. --provenance=false +
# --sbom=false + BUILDX_NO_DEFAULT_ATTESTATIONS=1 reduce but do not
# eliminate it on every machine.
#
# Approach: tail the buildx output, watch for the "pushing manifest …
# done" line (the definitive signal that the image is in the registry),
# give buildx a short grace period to exit cleanly, and SIGTERM it if
# still alive. Exit 0 only if the push-success line was observed.
#
# Usage:
#   scripts/buildx-push.sh IMAGE_TAG DOCKERFILE PLATFORM
#
# Example:
#   scripts/buildx-push.sh registry.example.com/foo:bar devlaptop/Dockerfile linux/amd64

set -uo pipefail

IMAGE="${1:?usage: $0 IMAGE DOCKERFILE PLATFORM}"
DOCKERFILE="${2:?usage: $0 IMAGE DOCKERFILE PLATFORM}"
PLATFORM="${3:?usage: $0 IMAGE DOCKERFILE PLATFORM}"

LOG=$(mktemp -t buildx-push.XXXXXX)
trap 'rm -f "$LOG"' EXIT

echo "[buildx-push] $IMAGE  platform=$PLATFORM  log=$LOG" >&2

# BUILDX_NO_DEFAULT_ATTESTATIONS kills any default attestation pipeline
# that --provenance=false might not catch. DOCKER_CLI_HINTS=false stops
# Docker Desktop's "did you know" hints which can add latency.
export BUILDX_NO_DEFAULT_ATTESTATIONS=1
export DOCKER_CLI_HINTS=false

# Background buildx writing to LOG directly (not via `| tee ... &`,
# which makes $! the tee PID — we'd kill the wrong process and never
# unstick buildx itself). A separate `tail -f` mirrors the log to the
# terminal so the user still sees progress.
docker buildx build \
  --platform "$PLATFORM" \
  --provenance=false \
  --sbom=false \
  -t "$IMAGE" \
  -f "$DOCKERFILE" \
  --push \
  . > "$LOG" 2>&1 &
BUILDX_PID=$!
tail -f "$LOG" &
TAIL_PID=$!
trap 'kill $TAIL_PID 2>/dev/null; rm -f "$LOG"' EXIT

# Wait up to 15 min for the push-complete signal. Most builds with warm
# layer cache complete in ~3-5 min; cold builds 10+. After we observe
# the signal we give buildx 10s grace before SIGKILL.
SIGNAL_RE='pushing manifest .* done'
DEADLINE=$(( $(date +%s) + 900 ))
PUSH_OK=false

while kill -0 "$BUILDX_PID" 2>/dev/null; do
  if grep -qE "$SIGNAL_RE" "$LOG"; then
    PUSH_OK=true
    echo "[buildx-push] manifest push detected — giving CLI 10s to exit, then terminating" >&2
    # Hand the CLI a chance to wind down on its own (some runs do exit
    # cleanly within a few seconds of "DONE").
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$BUILDX_PID" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$BUILDX_PID" 2>/dev/null; then
      echo "[buildx-push] CLI still alive 10s after push — sending TERM" >&2
      kill -TERM "$BUILDX_PID" 2>/dev/null || true
      sleep 2
      kill -KILL "$BUILDX_PID" 2>/dev/null || true
    fi
    break
  fi
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    echo "[buildx-push] FATAL: 15 min deadline exceeded without seeing push-complete" >&2
    kill -KILL "$BUILDX_PID" 2>/dev/null || true
    exit 1
  fi
  sleep 1
done

# Best-effort: clean up any orphaned buildx subprocesses (the wrapper
# CLI sometimes outlives the parent we just killed).
pkill -KILL -f "buildx build.*${IMAGE}" 2>/dev/null || true

if [ "$PUSH_OK" = "true" ]; then
  echo "[buildx-push] push verified for $IMAGE" >&2
  exit 0
fi

# If buildx exited on its own without us seeing the signal, double-check
# the log one more time (race between exit and final flush).
if grep -qE "$SIGNAL_RE" "$LOG"; then
  echo "[buildx-push] push verified (post-exit log re-check) for $IMAGE" >&2
  exit 0
fi

echo "[buildx-push] FATAL: buildx exited without reporting a successful manifest push" >&2
echo "[buildx-push] tail of log follows:" >&2
tail -20 "$LOG" >&2
exit 1
