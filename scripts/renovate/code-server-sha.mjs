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
import { readFileSync, writeFileSync } from 'node:fs';

const file = process.argv[2] ?? 'devlaptop/Dockerfile';
const src = readFileSync(file, 'utf8');

const version = src.match(/^ARG CODE_SERVER_VERSION=(\S+)$/m)?.[1];
if (!version) throw new Error(`no ARG CODE_SERVER_VERSION in ${file}`);

const headers = { Accept: 'application/vnd.github+json', 'User-Agent': 'kube-coder-renovate' };
const token = process.env.GITHUB_COM_TOKEN || process.env.GITHUB_TOKEN;
if (token) headers.Authorization = `Bearer ${token}`;

const url = `https://api.github.com/repos/coder/code-server/releases/tags/v${version}`;
const res = await fetch(url, { headers });
if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
const { assets = [] } = await res.json();

let out = src;
for (const arch of ['amd64', 'arm64']) {
  const name = `code-server_${version}_${arch}.deb`;
  const digest = assets.find((a) => a.name === name)?.digest ?? '';
  const sha = digest.match(/^sha256:([0-9a-f]{64})$/)?.[1];
  if (!sha) throw new Error(`no sha256 digest for ${name} (got ${JSON.stringify(digest)})`);
  const key = `CODE_SERVER_SHA256_${arch.toUpperCase()}`;
  const re = new RegExp(`^ARG ${key}=[0-9a-f]{64}$`, 'm');
  if (!re.test(out)) throw new Error(`no ARG ${key} in ${file}`);
  out = out.replace(re, `ARG ${key}=${sha}`);
}

if (out !== src) writeFileSync(file, out);
console.log(`code-server ${version}: SHA-256 pins ${out === src ? 'already current' : 'updated'}`);
