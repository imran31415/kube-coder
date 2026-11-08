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
  PHONE,
  clickVisible,
  freshPage as newPage,
  launchBrowser,
  openDrawerAndGo,
  serveDist,
  sleep,
  visibleNth,
  waitVisible,
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
  // Feed and Board were absent from this list — which is precisely why
  // nothing here ever noticed that Board was unreachable from a notification
  // (#692). A dead-end check that skips a screen cannot report on it. The AI
  // CTO has no entry of its own since #683 folded it into Chat.
  for (const item of ['Overview', 'Feed', 'Board', 'Chat', 'Walkie-Talkie', 'Builds', 'Triggers', 'Apps', 'Memory', 'Files', 'Skills', 'Docs', 'Metrics', 'Controller', 'Settings', 'Desktop']) {
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

// ---------- Path E (#833): screens load the first time they are opened.
// The home screen fetches no screen chunk; the drawer fetches one on demand;
// and in the desktop app a deep link to a Board card opens Board, whose chunk
// was never loaded, on that card — also on top of a screen restored after the
// webview was dropped. The desktop bridge is a stand-in (an init script)
// answering the same commands desktop/src-tauri implements.
{
  const report = (ok, tag) => {
    if (!ok) failures++;
    console.log(`${ok ? 'PASS' : 'FAIL'} ${tag}`);
  };
  const track = (page) => {
    const chunks = [];
    page.on('request', (r) => {
      const m = /\/(\w+Screen)-[0-9a-f]+\.js$/.exec(r.url());
      if (m) chunks.push(m[1]);
    });
    return chunks;
  };

  const page = await browser.newPage(PHONE);
  const chunks = track(page);
  await page.goto(`http://127.0.0.1:${port}/`);
  await sleep(1400);
  report(chunks.length === 0, `Home screen loads no screen chunk  [${chunks}]`);
  await openDrawerAndGo(page, 'Feed');
  report(chunks.join() === 'FeedScreen', `Opening Feed loads its chunk, and only it  [${chunks}]`);
  await page.close();

  const fakeBridge = (items) => {
    let targets = [{ url: 'kubecoder://board/kube-coder-gh/I_kwDOA:4102' }];
    const store = new Map(Object.entries(items));
    window.__TAURI__ = {
      core: {
        invoke: async (cmd, args = {}) => {
          switch (cmd) {
            case 'kc_take_targets': {
              const t = targets;
              targets = [];
              return t;
            }
            case 'kc_item_get':
              return store.get(args.key) ?? null;
            case 'kc_item_set':
              store.set(args.key, args.value);
              return null;
            case 'kc_item_remove':
              store.delete(args.key);
              return null;
            case 'kc_secret_get':
              return null;
            case 'kc_live_status':
              return { mode: 'off', keeper: null, needsYou: 0, feedFetches: 0 };
            default:
              return null;
          }
        },
      },
      event: { listen: async () => () => {} },
    };
  };
  const card = (p) => p.locator('[id="board-item-I_kwDOA:4102"]');

  for (const [tag, items, expectRestored] of [
    ['Desktop link, cold start', {}, null],
    [
      'Desktop link onto a restored screen',
      { 'kc.session.navState': JSON.stringify({ index: 0, routes: [{ name: 'Feed' }] }) },
      'FeedScreen',
    ],
  ]) {
    const p = await browser.newPage(PHONE);
    const got = track(p);
    await p.addInitScript(fakeBridge, items);
    await p.goto(`http://127.0.0.1:${port}/`);
    let onCard = false;
    try {
      await waitVisible(card(p), 'the linked card', 8000);
      onCard = true;
    } catch {
      onCard = false;
    }
    report(onCard && got.includes('BoardScreen'), `${tag}: opens the Board card from an unloaded chunk  [${got}]`);
    if (expectRestored) {
      report(got.includes(expectRestored), `${tag}: restored ${expectRestored} first, link on top  [${got}]`);
    }
    await p.close();
  }
}

console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILURES`);
await browser.close();
close();
process.exit(failures === 0 ? 0 : 1);
