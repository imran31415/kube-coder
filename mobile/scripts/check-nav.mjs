/**
 * Navigation dead-end check: every screen, reached by every entry path, must
 * expose a hit-testable escape — the ☰ drawer button or a header back button.
 *
 * Guards the hamburger-drawer navigation (#196): the drawer only appears on
 * top-level screens, so detail screens depend on the stack back button — which
 * silently disappears if a deep link makes a detail screen the first route in
 * its stack (that trapped users on TaskDetail when Desktop became the home).
 *
 * Hidden tab screens stay in the DOM on react-native-web, so every check
 * verifies the element is actually topmost (elementFromPoint), not merely
 * present.
 *
 * Prereq:  EXPO_PUBLIC_MOCK=1 npx expo export --platform web   (writes dist/)
 * Run:     node scripts/check-nav.mjs        (or: npm run check:nav)
 * Output:  PASS/FAIL per flow; exit 1 on any dead end. Screenshots land in
 *          $SHOTS_DIR if set.
 */
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { mkdir } from 'node:fs/promises';
import handler from 'serve-handler';
import { chromium } from 'playwright';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const distDir = path.resolve(__dirname, '..', 'dist');
const shotsDir = process.env.SHOTS_DIR || null;
if (shotsDir) await mkdir(shotsDir, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const server = http.createServer((req, res) =>
  handler(req, res, { public: distDir, rewrites: [{ source: '**', destination: '/index.html' }] }),
);
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const port = server.address().port;

const browser = await chromium.launch();
let failures = 0;

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

async function clickVisible(loc, what) {
  const i = await visibleNth(loc);
  if (i < 0) throw new Error(`no hit-testable match for: ${what}`);
  await loc.nth(i).click();
}

async function freshPage() {
  const page = await browser.newPage({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 2,
    isMobile: true,
    hasTouch: true,
  });
  await page.goto(`http://127.0.0.1:${port}/`);
  await sleep(1400);
  return page;
}

async function assertEscape(page, tag, shotName) {
  const hasMenu = (await visibleNth(page.getByLabel('Open menu'))) >= 0;
  const hasBack = (await visibleNth(page.getByLabel(/back/i))) >= 0;
  const ok = hasMenu || hasBack;
  if (!ok) failures++;
  console.log(`${ok ? 'PASS' : 'FAIL'} ${tag}  menu=${hasMenu} back=${hasBack}`);
  if (shotsDir && shotName) await page.screenshot({ path: path.join(shotsDir, shotName) });
  return { hasMenu, hasBack };
}

async function openDrawerAndGo(page, item) {
  await clickVisible(page.getByLabel('Open menu'), 'menu button');
  await sleep(450);
  await clickVisible(page.getByText(item, { exact: true }), `drawer item ${item}`);
  await sleep(900);
}

// ---------- Path A: cold start on Desktop → Activity row → TaskDetail → back.
// The Tasks tab has never been visited, so this is the deep-link-as-first-
// route case that used to strand the user on the detail screen.
{
  const page = await freshPage();
  await assertEscape(page, 'Desktop (home)', '01-desktop.png');
  await clickVisible(
    page.getByText('Add a /healthz endpoint to server.py and a unit test for it'),
    'activity row',
  );
  await sleep(1100);
  const r = await assertEscape(page, 'TaskDetail via Desktop activity row', '02-detail-from-desktop.png');
  if (r.hasBack) {
    await clickVisible(page.getByLabel(/back/i), 'back button');
    await sleep(800);
    const onList = (await visibleNth(page.getByPlaceholder('Search tasks…'))) >= 0;
    console.log(`${onList ? 'PASS' : 'FAIL'} Back from detail lands on TaskList`);
    if (!onList) failures++;
  }
  await page.close();
}

// ---------- Path B: cold start → Desktop build composer → TaskDetail
{
  const page = await freshPage();
  await page.getByPlaceholder('Describe a build to run…').fill('nav check: escape from detail');
  await clickVisible(page.getByLabel('Start build'), 'start build');
  await sleep(1500);
  await assertEscape(page, 'TaskDetail via Desktop build composer', '03-detail-from-composer.png');
  await page.close();
}

// ---------- Path C: cold start → Desktop task shortcut → TaskDetail
{
  const page = await freshPage();
  await clickVisible(page.getByLabel('Launch Fix flaky test'), 'task shortcut');
  await sleep(1700);
  await assertEscape(page, 'TaskDetail via Desktop task shortcut', '04-detail-from-shortcut.png');
  await page.close();
}

// ---------- Path D: drawer to every top-level screen, then nested details
{
  const page = await freshPage();
  for (const item of ['Builds', 'Apps', 'Memory', 'Metrics', 'Controller', 'Settings', 'Desktop']) {
    await openDrawerAndGo(page, item);
    await assertEscape(page, `Top-level: ${item}`, `05-top-${item.toLowerCase()}.png`);
  }

  // Tasks list → detail → back
  await openDrawerAndGo(page, 'Builds');
  await clickVisible(
    page.getByText('Refactor the auth middleware to share the Bearer-token check'),
    'task row',
  );
  await sleep(1000);
  await assertEscape(page, 'TaskDetail via Tasks list', '06-detail-from-list.png');
  await clickVisible(page.getByLabel(/back/i), 'back');
  await sleep(700);

  // Tasks → New task (modal)
  await clickVisible(page.getByText('New', { exact: true }), 'new task button');
  await sleep(900);
  await assertEscape(page, 'NewTask modal', '07-new-task.png');
  const backIdx = await visibleNth(page.getByLabel(/back/i));
  if (backIdx >= 0) await page.getByLabel(/back/i).nth(backIdx).click();
  await sleep(700);

  // Apps → AppView
  await openDrawerAndGo(page, 'Apps');
  await clickVisible(page.getByText('storefront', { exact: true }), 'app row');
  await sleep(1100);
  await assertEscape(page, 'AppView via Apps list', '08-app-view.png');
  await page.close();
}

console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILURES`);
await browser.close();
server.close();
process.exit(failures === 0 ? 0 : 1);
