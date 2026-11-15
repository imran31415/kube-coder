#!/usr/bin/env node
// Re-pin code-server's per-arch .deb SHA-256 after a CODE_SERVER_VERSION bump.
//
// devlaptop/Dockerfile verifies the downloaded .deb against
// CODE_SERVER_SHA256_AMD64 / _ARM64. Renovate can bump CODE_SERVER_VERSION but
// not those digests, so without this every code-server PR would fail the image
// build on `sha256sum -c`. Renovate runs this as a postUpgradeTask (see
// renovate.json); run it by hand after a manual bump too:
//
//   node scripts/renovate/code-server-sha.mjs [path/to/Dockerfile]
//
// The digests come from GitHub's release API (`assets[].digest`), the same
// source the Dockerfile comment documents. Fails closed: any missing release,
// asset or digest exits non-zero and leaves the file untouched.
// No dependencies: it must run inside Renovate's own container.
import { repinReleaseDigests } from './release-digests.mjs';

await repinReleaseDigests({
  label: 'code-server',
  file: process.argv[2] ?? 'devlaptop/Dockerfile',
  repo: 'coder/code-server',
  versionArg: 'CODE_SERVER_VERSION',
  tag: (v) => `v${v}`,
  pins: (v) => ({
    CODE_SERVER_SHA256_AMD64: `code-server_${v}_amd64.deb`,
    CODE_SERVER_SHA256_ARM64: `code-server_${v}_arm64.deb`,
  }),
});
