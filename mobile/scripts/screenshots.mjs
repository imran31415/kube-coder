/**
 * Capture store-ready screenshots of the app (mock/demo build) at the exact
 * pixel dimensions Apple App Store Connect and Google Play require.
 *
 * Pipeline: serve the exported web build (mobile/dist) → drive it with
 * Playwright/Chromium at each device's CSS viewport × deviceScaleFactor so the
 * PNG lands at the precise store pixel size → walk the app's screens.
 *
 * Navigation is the hamburger NavDrawer (#196) that replaced the bottom tab
 * bar: every top-level screen exposes an "Open menu" ☰ button, and the drawer
 * lists the destinations by label (Desktop, Builds, Apps, Memory, Metrics…).
 * The demo build boots on the Desktop home. Hidden tab screens stay in the
 * react-native-web DOM, so we always act on the *topmost* match (see
 * clickVisible / waitVisible), mirroring scripts/check-nav.mjs.
 *
 * Prereq:  EXPO_PUBLIC_MOCK=1 npx expo export --platform web   (writes dist/)
 * Run:     node scripts/screenshots.mjs        (or: npm run screenshots)
 * Output:  ../ios-assets/<device>/*.png  and  ../android-assets/<device>/*.png
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
const iosDir = path.join(root, '..', 'ios-assets');
const androidDir = path.join(root, '..', 'android-assets');

// device CSS viewport × scale = final PNG pixels (the store-required size)
const DEVICES = [
  { platform: 'ios', name: 'iphone-6.7', label: 'iPhone 6.7" (1290×2796)', w: 430, h: 932, scale: 3 },
  { platform: 'ios', name: 'iphone-6.5', label: 'iPhone 6.5" (1242×2688)', w: 414, h: 896, scale: 3 },
  { platform: 'ios', name: 'ipad-12.9', label: 'iPad 12.9" (2048×2732)', w: 1024, h: 1366, scale: 2 },
  { platform: 'android', name: 'phone', label: 'Android phone (1080×1920)', w: 360, h: 640, scale: 3 },
  { platform: 'android', name: 'tablet-10', label: 'Android 10" tablet (1600×2560)', w: 800, h: 1280, scale: 2 },
];

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
// match an off-screen duplicate. Pick the element that's actually topmost at
// its own center point (same technique as scripts/check-nav.mjs).
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

/** Wait until a locator has a hit-testable (topmost) match, then return it. */
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
  const el = await waitVisible(loc, what);
  await el.click();
}

/** Open the drawer and jump to a top-level destination by its drawer label. */
async function go(page, label) {
  await clickVisible(page.getByLabel('Open menu'), 'menu button');
  await sleep(450);
  await clickVisible(page.getByText(label, { exact: true }), `drawer item ${label}`);
  await sleep(900);
}

async function shots(page, device, outDir) {
  const shot = async (file) => {
    await sleep(450);
    await page.screenshot({ path: path.join(outDir, file) });
    console.log(`    ✓ ${device.name}/${file}`);
  };

  // Boots on the Desktop home (the "AI workspace" launcher shared with the web
  // dashboard). Wait for its build composer before touching anything.
  await waitVisible(page.getByPlaceholder('Describe a build to run…'), 'Desktop home');
  await sleep(600);

  // 3) Desktop — the launcher/home
  await shot('03-desktop.png');

  // 1) Builds list (formerly "Tasks" — the everyday list with search + New)
  await go(page, 'Builds');
  await waitVisible(page.getByPlaceholder('Search tasks…'), 'Builds list');
  await shot('01-tasks.png');

  // 2) Task detail: session output + composer, with the control-key tray open
  await clickVisible(
    page.getByText('Add a /healthz endpoint to server.py and a unit test for it'),
    'task row',
  );
  await sleep(900);
  await clickVisible(page.getByLabel('Show control keys'), 'control keys').catch(() => {});
  await shot('02-task-detail.png');

  // Detail screens carry a back button, not the ☰ drawer — step back to the
  // (top-level) Builds list before navigating on.
  await clickVisible(page.getByLabel(/back/i), 'back from task detail');
  await sleep(700);

  // 5) New task form (opens as a modal from the Builds list's New button)
  await clickVisible(page.getByText('New', { exact: true }), 'New task button');
  await sleep(800);
  await shot('05-new-task.png');
  await clickVisible(page.getByLabel(/back/i), 'back from new task');
  await sleep(700);

  // 4) Apps — running dev servers, openable in-app
  await go(page, 'Apps');
  await shot('04-apps.png');

  // 6) Memory
  await go(page, 'Memory');
  await shot('06-memory.png');

  // 7) Metrics
  await go(page, 'Metrics');
  await shot('07-metrics.png');
}

async function main() {
  const { server, port } = await startServer();
  const url = `http://127.0.0.1:${port}/`;
  console.log(`Serving ${distDir} at ${url}`);
  const browser = await chromium.launch();
  try {
    for (const device of DEVICES) {
      const base = device.platform === 'ios' ? iosDir : androidDir;
      const outDir = path.join(base, device.name);
      await rm(outDir, { recursive: true, force: true });
      await mkdir(outDir, { recursive: true });
      console.log(`\n${device.label}  →  ${path.relative(root, outDir)}`);
      const ctx = await browser.newContext({
        viewport: { width: device.w, height: device.h },
        deviceScaleFactor: device.scale,
        isMobile: device.platform === 'android' || device.name.startsWith('iphone'),
        hasTouch: device.platform === 'android' || device.name.startsWith('iphone'),
        colorScheme: 'dark',
      });
      const page = await ctx.newPage();
      await page.goto(url, { waitUntil: 'networkidle' });
      await shots(page, device, outDir);
      await ctx.close();
    }
  } finally {
    await browser.close();
    server.close();
  }
  console.log('\nAll screenshots captured.');
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
