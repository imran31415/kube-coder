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
 *          SCREENSHOT_PLATFORM=android|ios limits the run to one store, so
 *          refreshing one listing doesn't rewrite the other's tracked PNGs.
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
  // Play wants exactly 9:16 portrait for every class — a 10:16 tablet frame
  // (the natural 1600×2560) is off-spec.
  { platform: 'android', name: 'phone', label: 'Android phone (1080×1920)', w: 360, h: 640, scale: 3 },
  { platform: 'android', name: 'tablet-7', label: 'Android 7" tablet (1216×2160)', w: 608, h: 1080, scale: 2 },
  { platform: 'android', name: 'tablet-10', label: 'Android 10" tablet (1620×2880)', w: 810, h: 1440, scale: 2 },
];

// Play caps each device class at 8 screenshots (App Store: 10) and both stores
// show them in filename order, so both get this one curated subset, re-numbered
// to lead with the strongest screens. Anything not listed here is skipped.
const STORE_SET = {
  '03-desktop.png': '01-desktop.png',
  '02-task-detail.png': '02-task-detail.png',
  '10-board.png': '03-board.png',
  '09-chat.png': '04-chat.png',
  '01-tasks.png': '05-builds.png',
  '04-apps.png': '06-apps.png',
  '06-memory.png': '07-memory.png',
  '07-metrics.png': '08-metrics.png',
};

const ONLY_PLATFORM = process.env.SCREENSHOT_PLATFORM;

const DEMO_CHAT = [
  { seq: 1, ts: 1, role: 'user', type: 'message', text: "What's running and how much CPU am I using?" },
  { seq: 2, ts: 2, role: 'assistant', type: 'tool_call', tool: { name: 'Bash', input: { command: 'kc status' } }, tool_id: 't1' },
  { seq: 3, ts: 3, role: 'system', type: 'tool_result', text: 'ok', tool_use_id: 't1' },
  {
    seq: 4,
    ts: 4,
    role: 'assistant',
    type: 'message',
    text: "You're at 37% CPU and 2.1 GB of 4 GB memory, so there's plenty of headroom.\n\nRunning now: the /healthz build, the onboarding polish, and two web apps (storefront on 3000, another on 8080). The auth middleware refactor is waiting on you.",
  },
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

/** Open the drawer and jump to a top-level destination by its drawer label.
 *  Entries expose an aria-label ("Go to Memory") and the list scrolls, so the
 *  target is scrolled in before it's clicked (mirrors scripts/check-nav.mjs). */
async function go(page, label) {
  await clickVisible(page.getByLabel('Open menu'), 'menu button');
  await sleep(450);
  const entry = page.getByLabel(`Go to ${label}`);
  await entry.first().scrollIntoViewIfNeeded();
  await clickVisible(entry, `drawer item ${label}`);
  await sleep(900);
}

async function shots(page, device, outDir) {
  const shot = async (name) => {
    const file = STORE_SET[name];
    if (!file) return;
    await sleep(450);
    await page.screenshot({ path: path.join(outDir, file) });
    console.log(`    ✓ ${device.name}/${file}`);
  };

  // Boots on the Desktop home (the "AI workspace" launcher shared with the web
  // dashboard). The composer defaults to chat mode ("Ask anything or start a
  // build…") with a pill toggle to build mode ("Describe a build to run…");
  // accept either so the wait doesn't depend on which default the app ships.
  await waitVisible(
    page.getByPlaceholder(/Ask anything or start a build|Describe a build to run/),
    'Desktop home',
  );
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

  // 8) Walkie-Talkie — the in-app WhatsApp gateway preview
  await go(page, 'Walkie-Talkie');
  await sleep(700);
  await shot('08-walkie.png');

  // 10) Board — agent runs waiting on the user's decision
  await go(page, 'Board');
  await waitVisible(page.getByText('Needs review', { exact: true }), 'Board');
  // Keep the first card's Approve/Reject row on screen on short phone frames.
  await page.getByLabel('Send back').first().scrollIntoViewIfNeeded();
  await shot('10-board.png');

  // 9) Chat — the demo build has no chat backend, so serve one past thread's
  // transcript here (figures match the mock Builds/Metrics screens) and open it.
  await page.route('**/api/hypervisor/threads/th-1*', (route) =>
    route.fulfill({
      status: 200,
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
      json: { thread: { id: 'th-1', title: 'Ship the chart change', assistant: 'claude', status: 'idle' }, events: DEMO_CHAT },
    }),
  );
  await go(page, 'Chat');
  await clickVisible(page.getByLabel(/Past chats/), 'past chats');
  await sleep(500);
  await clickVisible(page.getByText('Ship the chart change'), 'demo thread');
  await waitVisible(page.getByText(/37% CPU/), 'demo transcript');
  await shot('09-chat.png');
}

async function main() {
  const { server, port } = await startServer();
  const url = `http://127.0.0.1:${port}/`;
  console.log(`Serving ${distDir} at ${url}`);
  const browser = await chromium.launch();
  try {
    for (const device of DEVICES.filter((d) => !ONLY_PLATFORM || d.platform === ONLY_PLATFORM)) {
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
