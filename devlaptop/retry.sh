#!/bin/sh
# devlaptop/retry.sh — run a command, retrying with backoff if it fails.
#
# Installed into the image as /usr/local/bin/retry and used by every package
# install in devlaptop/Dockerfile that reaches a package registry.
#
# Why this exists, when npm already has --fetch-retries: npm's retry logic
# (make-fetch-happen) covers network errors, 408, 420, 429 and 5xx. A 404 is
# treated as a definitive answer and fails the install immediately, with no
# retry at any setting. On 2026-09-07 registry.npmjs.org served exactly that
# for a tarball that does exist — @tanstack/virtual-core 3.17.9, a transitive
# dependency of @deepseek-ai/dsh:
#
#     npm error code E404
#     npm error 404 Not Found - GET .../virtual-core-3.17.9.tgz - Not found
#
# It reddened the Docker Build Smoke Test on main and on every open PR inside
# the same few minutes, and the tarball served 200 again shortly after. Only a
# whole-command retry rides that out. This matches what the curl/wget fetches
# further down the Dockerfile already do (`--retry 5`, `--tries=5`); the
# package installs were the one class of network fetch with no cover at all.
#
# This buys resilience to registry blips, NOT tolerance of a bad pin: a version
# that is genuinely unpublished still fails, just after RETRY_ATTEMPTS tries.
set -eu

max=${RETRY_ATTEMPTS:-4}
delay=${RETRY_DELAY:-5}
attempt=1

while :; do
    status=0
    "$@" || status=$?

    if [ "$status" -eq 0 ]; then
        exit 0
    fi

    if [ "$attempt" -ge "$max" ]; then
        echo "retry: '$*' failed $max/$max times, giving up (exit $status)" >&2
        exit "$status"
    fi

    echo "retry: '$*' failed (exit $status); attempt $attempt/$max, retrying in ${delay}s" >&2
    sleep "$delay"
    attempt=$((attempt + 1))
    delay=$((delay * 2))
done
