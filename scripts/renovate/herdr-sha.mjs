#!/usr/bin/env node
// Re-pin herdr's per-arch binary SHA-256 after a HERDR_VERSION bump (#834).
//
// herdr's GitHub releases ship bare binaries and no checksum file, so
// devlaptop/Dockerfile verifies each download against HERDR_SHA256_AMD64 /
// _ARM64, taken from GitHub's release API (`assets[].digest`). Renovate runs
// this as a postUpgradeTask (see renovate.json); run it by hand after a manual
// bump too:
//
//   node scripts/renovate/herdr-sha.mjs [path/to/Dockerfile]
//
// Fails closed: any missing release, asset or digest exits non-zero and leaves
// the file untouched.
import { repinReleaseDigests } from './release-digests.mjs';

await repinReleaseDigests({
  label: 'herdr',
  file: process.argv[2] ?? 'devlaptop/Dockerfile',
  repo: 'herdrdev/herdr',
  versionArg: 'HERDR_VERSION',
  tag: (v) => v,                    // the ARG carries the tag's own `v` prefix
  pins: () => ({
    HERDR_SHA256_AMD64: 'herdr-linux-x86_64',
    HERDR_SHA256_ARM64: 'herdr-linux-aarch64',
  }),
});
