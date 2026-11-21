#!/usr/bin/env node
/**
 * README screenshots of the admin console, against a mocked controller API so
 * no real tenant shows up. Serves the built SPA (dist/) itself.
 *
 *   make controller-web
 *   node charts/workspace-controller/web/scripts/shoot-readme.mjs docs/screenshots/readme
 *
 * Needs playwright-core resolvable (run from charts/workspace/web, or set
 * NODE_PATH) and a Chromium (KC_CHROMIUM, as the dashboard scripts use).
 */
import http from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { extname, join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(join(here, '../../../workspace/web/package.json'));
const { chromium } = require('playwright-core');
const { chromiumPath } = await import(join(here, '../../../workspace/web/scripts/chromium-path.mjs'));

const out = resolve(process.argv[2] || 'docs/screenshots/readme');
await mkdir(out, { recursive: true });
const dist = join(here, '..', 'dist');
const now = 1_763_740_000;

const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2' };
const server = http.createServer(async (req, res) => {
  const p = new URL(req.url, 'http://x').pathname;
  const file = join(dist, p === '/' ? 'index.html' : p);
  try {
    res.writeHead(200, { 'content-type': TYPES[extname(file)] || 'application/octet-stream' });
    res.end(await readFile(file));
  } catch {
    res.writeHead(200, { 'content-type': 'text/html' });
    res.end(await readFile(join(dist, 'index.html')));
  }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const BASE = `http://127.0.0.1:${server.address().port}`;

// ── Fixtures ───────────────────────────────────────────────────────────────
const LATEST = 'v1.86.0';
const ws = (user, state, version, extra = {}) => ({
  user, deployment: `ws-${user}`, namespace: `ws-${user}`, isolated: true, state,
  desiredReplicas: state === 'running' ? 1 : 0, readyReplicas: state === 'running' ? 1 : 0,
  url: `https://${user}.dev.example.com`,
  pods: state === 'running' ? [{ name: `ws-${user}-7d9f-x2k4p`, phase: 'Running', ready: true, restarts: 0, reason: null }] : [],
  detail: state === 'running' ? '1/1 ready' : state === 'sleeping' ? 'asleep — wakes on visit' : 'stopped',
  image: `registry.example.com/coder:devlaptop-${version}`, imageTag: `devlaptop-${version}`, version,
  updateAvailable: version !== LATEST, ...extra,
});
const WORKSPACES = {
  namespace: 'coder', latestVersion: LATEST,
  workspaces: [
    ws('dana', 'running', LATEST),
    ws('priya', 'running', LATEST),
    ws('marcus', 'sleeping', LATEST),
    ws('lee', 'sleeping', LATEST),
    ws('sofia', 'running', 'v1.85.0'),
    ws('omar', 'stopped', 'v1.79.0'),
  ],
};
const series = (base, amp, n = 60) => Array.from({ length: n }, (_, i) => [now - (n - i) * 60, +(base + amp * Math.sin(i / 6) + amp * 0.4 * Math.sin(i / 1.7)).toFixed(3)]);
const metrics = (user) => ({
  user, namespace: `ws-${user}`, running: true,
  cpu: { cores: 0.92, limitCores: 4, pct: 23 },
  memory: { bytes: 6.2e9, limitBytes: 16e9, pct: 38 },
  disk: { usedBytes: 41e9, capacityBytes: 100e9, pct: 41 },
  network: { rxBps: 182000, txBps: 64000 },
  uptimeSeconds: 3 * 86400 + 4 * 3600,
  cost: { perHour: 0.071, computePerMonth: 41.2, storagePerMonth: 10, perMonth: 51.2 },
  spark: { rangeSeconds: 3600, step: 60, cpu: series(0.9, 0.35), memory: series(6.1e9, 0.4e9), disk: series(41e9, 0.05e9) },
  metricsError: null,
});
const roll = (alloc, w, c) => ({ allocatable: alloc, workspace: w, cluster: c, other: c - w, workspacePct: Math.round((w / alloc) * 100), clusterPct: Math.round((c / alloc) * 100) });
const CAPACITY = {
  generatedAt: now, namespace: 'coder',
  cluster: { nodeCount: 3, cpu: roll(12, 3.1, 7.4), memory: roll(48e9, 19e9, 31e9), pods: { allocatable: 330, workspace: 14, cluster: 96 } },
  nodes: ['pool-a-1', 'pool-a-2', 'pool-a-3'].map((name, i) => ({ name, cpu: roll(4, 1 + i * 0.1, 2.4 + i * 0.2), memory: roll(16e9, 6e9 + i * 5e8, 10e9 + i * 6e8), pods: { allocatable: 110, workspace: 4 + i, cluster: 30 + i * 2 } })),
  history: { rangeSeconds: 3600, step: 60, cpu: { allocatable: series(12, 0), workspace: series(3.1, 0.4), cluster: series(7.4, 0.6) }, memory: { allocatable: series(48e9, 0), workspace: series(19e9, 1e9), cluster: series(31e9, 1.5e9) } },
  metricsError: null,
};

async function mock(page) {
  const json = (r, body) => r.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
  await page.route(/\/api\//, (r) => json(r, {}));
  await page.route(/\/api\/workspaces(\?|$)/, (r) => json(r, WORKSPACES));
  await page.route(/\/api\/workspaces\/[^/]+\/metrics/, (r) => json(r, metrics(new URL(r.request().url()).pathname.split('/')[3])));
  await page.route(/\/api\/insights/, (r) => json(r, { generatedAt: now, windowSeconds: 3600, advisories: [], error: null }));
  await page.route(/\/api\/capacity(\?|$)/, (r) => json(r, CAPACITY));
  await page.route(/\/api\/capacity\/summary/, (r) => json(r, { generatedAt: now, namespace: 'coder', cluster: { nodeCount: 3, cpu: CAPACITY.cluster.cpu, memory: CAPACITY.cluster.memory }, status: 'ok', metricsError: null }));
  await page.addInitScript(`Date.now = () => ${(now + 5) * 1000};`);
}

const SHOTS = [
  { name: 'console-workspaces', hash: '#/', vp: { width: 1280, height: 900 }, filter: 'All (6)' },
  { name: 'console-edit-limits', hash: '#/w/dana', vp: { width: 1280, height: 1000 }, click: 'Edit limits', element: '.res-editor' },
  { name: 'console-mobile', hash: '#/', vp: { width: 390, height: 844 }, filter: 'All (6)' },
];
const browser = await chromium.launch({ executablePath: chromiumPath(), headless: true });
try {
  for (const s of SHOTS) {
    const ctx = await browser.newContext({ viewport: s.vp, deviceScaleFactor: 2, colorScheme: 'dark' });
    const page = await ctx.newPage();
    await mock(page);
    await page.goto(`${BASE}/${s.hash}`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(800);
    if (s.filter) {
      await page.getByText(s.filter, { exact: true }).click();
      await page.waitForTimeout(400);
    }
    if (s.click) {
      await page.getByText(s.click, { exact: true }).click();
      await page.locator('.res-editor input').first().fill('4');
      await page.locator('.res-editor input').nth(1).fill('16Gi');
      await page.waitForTimeout(300);
    }
    if (s.element) await page.locator(s.element).screenshot({ path: `${out}/${s.name}.png` });
    else await page.screenshot({ path: `${out}/${s.name}.png` });
    await ctx.close();
    console.log(`${s.name}.png`);
  }
} finally {
  await browser.close();
  server.close();
}
