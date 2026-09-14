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
 * The hit-test helpers live in scripts/lib/harness.mjs, shared with
 * check-board.mjs — this file's private copies had drifted (no waitVisible, so
 * every wait here was a fixed sleep).
 *
 * Prereq:  npm run export:web                (writes dist/ with the demo data)
 * Run:     node scripts/check-nav.mjs        (or: npm run check:nav)
 * Output:  PASS/FAIL per flow; exit 1 on any dead end. Screenshots land in
 *          $SHOTS_DIR if set.
 */
import path from 'node:path';
import { mkdir } from 'node:fs/promises';
import {
  clickVisible,
  freshPage as newPage,
  launchBrowser,
  openDrawerAndGo,
  serveDist,
  sleep,
  visibleNth,
} from './lib/harness.mjs';

const shotsDir = process.env.SHOTS_DIR || null;
if (shotsDir) await mkdir(shotsDir, { recursive: true });

const { port, close } = await serveDist(import.meta.url);
const browser = await launchBrowser();
let failures = 0;

const freshPage = () => newPage(browser, port);

async function assertEscape(page, tag, shotName) {
  const hasMenu = (await visibleNth(page.getByLabel('Open menu'))) >= 0;
  const hasBack = (await visibleNth(page.getByLabel(/back/i))) >= 0;
  const ok = hasMenu || hasBack;
  if (!ok) failures++;
  console.log(`${ok ? 'PASS' : 'FAIL'} ${tag}  menu=${hasMenu} back=${hasBack}`);
  if (shotsDir && shotName) await page.screenshot({ path: path.join(shotsDir, shotName) });
  return { hasMenu, hasBack };
}

// ---------- Path A: cold start on Desktop → Mission strip row → Mission
// Control → card → TaskDetail → back. The Tasks tab has never been visited,
// so this is the deep-link-as-first-route case that used to strand the user
// on the detail screen.
{
  const page = await freshPage();
  await assertEscape(page, 'Desktop (home)', '01-desktop.png');
  await clickVisible(
    page.getByLabel('Open Mission Control — Auth middleware refactor'),
    'mission strip row',
  );
  await sleep(1100);
  await assertEscape(page, 'Mission Control via Desktop strip', '02-mission-from-desktop.png');
  await clickVisible(page.getByText('Auth middleware refactor'), 'mission card');
  await sleep(1100);
  const r = await assertEscape(page, 'TaskDetail via Mission Control card', '02b-detail-from-mission.png');
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
  // The composer defaults to chat mode (screenshots.mjs already accounts for
  // this) — flip the mode pill to build before the build placeholder exists.
  await clickVisible(page.getByLabel('Mode: chat — switch to build'), 'switch to build mode');
  await sleep(300);
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
  // Drawer labels from src/navGroups.ts (#267): Mission Control is now the
  // section header; its screen entry is "Overview", and Hypervisor is "Chat".
  //
  // Feed, AI CTO and Board were absent from this list — which is precisely why
  // nothing here ever noticed that Board was unreachable from a notification
  // (#692). A dead-end check that skips a screen cannot report on it.
  for (const item of ['Overview', 'AI CTO', 'Feed', 'Board', 'Chat', 'Walkie-Talkie', 'Builds', 'Triggers', 'Apps', 'Memory', 'Files', 'Skills', 'Docs', 'Metrics', 'Controller', 'Settings', 'Desktop']) {
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

  // Docs → article (#250): the article is a stack screen, so its escape is the
  // header back button, not the drawer. Step out of AppView first — a detail
  // screen has no ☰ to open the drawer from.
  await clickVisible(page.getByLabel(/back/i), 'back from app view');
  await sleep(700);
  await openDrawerAndGo(page, 'Docs');
  await clickVisible(page.getByLabel('Open Getting started'), 'docs row');
  await sleep(1000);
  await assertEscape(page, 'DocsArticle via Docs list', '09-docs-article.png');
  await page.close();
}

console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILURES`);
await browser.close();
close();
process.exit(failures === 0 ? 0 : 1);
