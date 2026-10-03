/**
 * Capture the Security screens (#726) for a pull request.
 *
 * Deliberately separate from scripts/screenshots.mjs: that one produces the
 * curated store listing at the exact pixel sizes Apple and Google require, and
 * a feature PR should not quietly change what ships to the stores.
 *
 * Prereq:  npm run export:web      (writes dist/ with the mock backend)
 * Run:     node scripts/shoot-security.mjs [outDir]
 */
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { mkdir, rm } from 'node:fs/promises';
import handler from 'serve-handler';
import { chromium } from 'playwright';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, '..');
const distDir = path.join(root, 'dist');
const outDir = path.resolve(process.argv[2] || path.join(root, '..', 'pr-assets', 'security'));

// One phone. These are PR illustrations, not store assets, so a single
// representative size beats five near-identical ones in a review.
const DEVICE = { w: 430, h: 932, scale: 3 };

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function startServer() {
  const server = http.createServer((req, res) =>
    handler(req, res, { public: distDir, rewrites: [{ source: '**', destination: '/index.html' }] }),
  );
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => resolve({ server, port: server.address().port }));
  });
}

// Hidden tab screens stay mounted in the react-native-web DOM, so a locator can
// match an off-screen duplicate. Pick the element actually topmost at its own
// centre point (same technique as scripts/screenshots.mjs).
const isTopmost = (el) => {
  const r = el.getBoundingClientRect();
  if (!r.width || !r.height) return false;
  const t = document.elementFromPoint(
    Math.max(0, Math.min(r.x + r.width / 2, window.innerWidth - 1)),
    Math.max(0, Math.min(r.y + r.height / 2, window.innerHeight - 1)),
  );
  return t === el || el.contains(t) || (t && t.contains(el));
};

async function visibleNth(loc) {
  const n = await loc.count();
  for (let i = 0; i < n; i++) {
    if (await loc.nth(i).evaluate(isTopmost).catch(() => false)) return i;
  }
  return -1;
}

async function waitVisible(loc, what, timeout = 15000) {
  const start = performance.now();
  for (;;) {
    const i = await visibleNth(loc);
    if (i >= 0) return loc.nth(i);
    if (performance.now() - start > timeout) throw new Error(`timed out waiting for: ${what}`);
    await sleep(150);
  }
}

async function clickVisible(loc, what) {
  // Awaited: an unawaited click rejection surfaced as ERR_UNHANDLED_REJECTION
  // instead of waitVisible's own "timed out waiting for:" message, and the
  // screenshot that follows leaned on the sleep rather than on the click
  // having actually landed.
  await (await waitVisible(loc, what)).click();
  await sleep(700);
}

async function go(page, label) {
  await clickVisible(page.getByLabel('Open menu'), 'menu button');
  await sleep(450);
  const entry = page.getByLabel(`Go to ${label}`);
  await entry.first().scrollIntoViewIfNeeded();
  await clickVisible(entry, `drawer item ${label}`);
  await sleep(900);
}

async function main() {
  const { server, port } = await startServer();
  const browser = await chromium.launch();
  const errors = [];
  try {
    await rm(outDir, { recursive: true, force: true });
    await mkdir(outDir, { recursive: true });

    const page = await browser.newPage({
      viewport: { width: DEVICE.w, height: DEVICE.h },
      deviceScaleFactor: DEVICE.scale,
      isMobile: true,
      hasTouch: true,
    });
    page.on('pageerror', (e) => errors.push(String(e)));

    const shot = async (file) => {
      await sleep(500);
      await page.screenshot({ path: path.join(outDir, file) });
      console.log('  shot:', file);
    };

    await page.goto(`http://127.0.0.1:${port}/`, { waitUntil: 'domcontentloaded' });
    await waitVisible(
      page.getByPlaceholder(/Ask anything or start a build|Describe a build to run/),
      'Desktop home',
    );

    await go(page, 'Security');
    await shot('01-security-list.png');

    // A finished scan with findings.
    await clickVisible(page.getByText(/to look at/i), 'scan row with findings');
    await shot('02-scan-detail.png');

    // Open the worst finding to show the reproduction detail. Matched on the
    // demo backend's own wording, as scripts/screenshots.mjs does.
    const toggle = page.getByText(/injected SQL|SQL injection|Stored XSS|IDOR/i);
    if (await visibleNth(toggle) >= 0) {
      await clickVisible(toggle, 'finding row');
      await shot('03-finding-detail.png');
    }

    await clickVisible(page.getByLabel(/back/i), 'back to list');

    // A scan still in flight — the live view, not a spinner.
    const live = page.getByText(/Scanning…|Scanning\.\.\./i);
    if (await visibleNth(live) >= 0) {
      await clickVisible(live, 'running scan row');
      await shot('06-scan-running.png');
      await clickVisible(page.getByLabel(/back/i), 'back to list');
    }

    // The failure case: an empty findings list that must not read as a pass.
    const failed = page.getByText(/could not run|nothing was checked/i);
    if (await visibleNth(failed) >= 0) {
      await clickVisible(failed, 'failed scan row');
      await shot('04-failed-scan.png');
      await clickVisible(page.getByLabel(/back/i), 'back to list');
    }

    // The Apps screen carries the scan call to action.
    await go(page, 'Apps');
    await shot('05-apps-scan-cta.png');

    console.log(errors.length ? 'PAGE ERRORS:\n' + errors.join('\n') : 'no page errors');
    console.log('wrote', outDir);
  } finally {
    await browser.close();
    server.close();
  }
}

await main();
