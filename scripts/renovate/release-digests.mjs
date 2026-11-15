// Re-pin per-arch SHA-256 ARGs in devlaptop/Dockerfile after Renovate bumps a
// GitHub-release version whose release ships no checksum file of its own.
//
// Renovate can bump `ARG <TOOL>_VERSION` but not the digest ARGs beside it, so
// without a postUpgradeTask every such PR would fail the image build on
// `sha256sum -c`. The digests come from GitHub's release API
// (`assets[].digest`), which GitHub computes on upload.
//
// Fails closed: a missing ARG, release, asset or digest throws (non-zero exit)
// and leaves the file untouched. No dependencies: it runs inside Renovate's own
// container. Callers are the per-tool scripts beside this one
// (code-server-sha.mjs, herdr-sha.mjs), which Renovate runs by exact command.
import { readFileSync, writeFileSync } from 'node:fs';

/** Read `ARG <name>=<value>` from a Dockerfile's text. */
export function readArg(src, name) {
  return src.match(new RegExp(`^ARG ${name}=(\\S+)$`, 'm'))?.[1];
}

/**
 * Pure: return `src` with each `ARG <arg>=<64 hex>` replaced by the digest of
 * its asset. `assets` is GitHub's release `assets` array; `pins` maps
 * ARG name -> asset file name.
 */
export function applyDigests(src, assets, pins) {
  let out = src;
  for (const [arg, name] of Object.entries(pins)) {
    const digest = assets.find((a) => a.name === name)?.digest ?? '';
    const sha = digest.match(/^sha256:([0-9a-f]{64})$/)?.[1];
    if (!sha) throw new Error(`no sha256 digest for ${name} (got ${JSON.stringify(digest)})`);
    const re = new RegExp(`^ARG ${arg}=[0-9a-f]{64}$`, 'm');
    if (!re.test(out)) throw new Error(`no ARG ${arg}=<sha256> to re-pin`);
    out = out.replace(re, `ARG ${arg}=${sha}`);
  }
  return out;
}

/**
 * Fetch the release named by `versionArg` and rewrite the digest ARGs in place.
 *   label      — tool name for the log line
 *   repo       — GitHub "owner/name"
 *   versionArg — the ARG Renovate bumps
 *   tag(v)     — release tag for that ARG value
 *   pins(v)    — { DIGEST_ARG: assetName } for that ARG value
 */
export async function repinReleaseDigests({ label, file, repo, versionArg, tag, pins }) {
  const src = readFileSync(file, 'utf8');
  const version = readArg(src, versionArg);
  if (!version) throw new Error(`no ARG ${versionArg} in ${file}`);

  const headers = { Accept: 'application/vnd.github+json', 'User-Agent': 'kube-coder-renovate' };
  const token = process.env.GITHUB_COM_TOKEN || process.env.GITHUB_TOKEN;
  if (token) headers.Authorization = `Bearer ${token}`;

  const url = `https://api.github.com/repos/${repo}/releases/tags/${tag(version)}`;
  const res = await fetch(url, { headers });
  if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
  const { assets = [] } = await res.json();

  const out = applyDigests(src, assets, pins(version));
  if (out !== src) writeFileSync(file, out);
  console.log(`${label} ${version}: SHA-256 pins ${out === src ? 'already current' : 'updated'}`);
}
