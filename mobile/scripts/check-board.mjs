/**
 * Board-on-mobile check (#692).
 *
 * The maintainer's report was that Board "only works on web" and needs to be
 * "bigger/easier to see". Investigation found the screen itself renders — what
 * was broken was everything around it: both ways of ARRIVING at an item landed
 * nowhere, and every control was roughly 27pt against the iOS 44pt minimum.
 * This gate pins the three things a screenshot cannot argue with.
 *
 * Local only, deliberately: `check-nav.mjs` set that convention, and a browser
 * download does not belong in this repo's CI for a bug fix.
 *
 * Prereq:  npm run export:web          (writes dist/ with the demo data)
 * Run:     node scripts/check-board.mjs
 * Output:  PASS/FAIL per assertion; exit 1 on any failure. Screenshots land in
 *          $SHOTS_DIR if set.
 */
import path from 'node:path';
import { mkdir } from 'node:fs/promises';
import {
  MIN_TOUCH,
  clickVisible,
  freshPage,
  launchBrowser,
  makeReport,
  openDrawerAndGo,
  serveDist,
  sleep,
  visibleNth,
  waitVisible,
} from './lib/harness.mjs';

const shotsDir = process.env.SHOTS_DIR || null;
if (shotsDir) await mkdir(shotsDir, { recursive: true });
const shot = async (page, name) => {
  if (shotsDir) await page.screenshot({ path: path.join(shotsDir, name) });
};

const { port, close } = await serveDist(import.meta.url);
const browser = await launchBrowser();
const report = makeReport();

/** The rendered box of the first hit-testable match, in CSS px. */
async function boxOf(page, label) {
  const loc = page.getByLabel(label);
  const i = await visibleNth(loc);
  if (i < 0) return null;
  return loc.nth(i).boundingBox();
}

/** Every control a thumb has to hit must clear the iOS minimum. Height is what
 *  actually failed — 4pt of padding around 13pt text measured ~27 — so width is
 *  reported but not asserted: a wrapping row legitimately narrows a button. */
async function assertTouchTarget(page, label) {
  const box = await boxOf(page, label);
  if (!box) {
    report.fail(`${label} — not hit-testable`);
    return;
  }
  report.check(
    box.height >= MIN_TOUCH,
    `${label} touch target`,
    `${Math.round(box.height)}pt tall (min ${MIN_TOUCH})`,
  );
}

/** Font size of the first element whose text matches, in CSS px. */
async function fontSizeOf(page, text) {
  const loc = page.getByText(text, { exact: false });
  const i = await visibleNth(loc);
  if (i < 0) return null;
  return loc.nth(i).evaluate((el) => parseFloat(getComputedStyle(el).fontSize));
}

// ── 1. The drawer reaches Board, and says how much is waiting ──────────────
{
  const page = await freshPage(browser, port);

  await clickVisible(page.getByLabel('Open menu'), 'menu button');
  await sleep(500);
  // The badge is counted from the FEED (one waiting board item in the demo
  // data), never from the boards API — see NavDrawer's comment on why.
  const badged = page.getByLabel(/Go to Board, \d+ waiting/);
  const hasBadge = (await visibleNth(badged)) >= 0;
  report.check(hasBadge, 'Drawer shows a waiting count on Board');
  await shot(page, 'b01-drawer-badge.png');

  await page.getByLabel(/Go to Board/).first().scrollIntoViewIfNeeded();
  await clickVisible(page.getByLabel(/Go to Board/), 'drawer item Board');
  await sleep(1200);

  const onBoard = (await visibleNth(page.getByText('Needs review', { exact: true }))) >= 0;
  report.check(onBoard, 'Drawer → Board reaches the review queue');
  await shot(page, 'b02-board.png');

  // ── 2. Every control clears 44pt ─────────────────────────────────────────
  for (const label of ['Approve', 'Reject', 'Send back', 'Open ticket']) {
    await assertTouchTarget(page, label);
  }
  // The board selector is a control too, and was the smallest thing on screen.
  await assertTouchTarget(page, /^Show /);

  // ── 3. Type scale ────────────────────────────────────────────────────────
  const titleSize = await fontSizeOf(page, 'Refund not received');
  report.check(
    titleSize !== null && titleSize >= 18,
    'Item title is the largest thing on the card',
    `${titleSize}px`,
  );
  const previewSize = await fontSizeOf(page, 'I confirmed the refund was issued');
  report.check(
    previewSize !== null && previewSize >= 15,
    'Staged write is readable without squinting',
    `${previewSize}px`,
  );

  // ── 4. The send-back modal ───────────────────────────────────────────────
  await clickVisible(page.getByLabel('Send back'), 'send back');
  await sleep(600);
  const confirm = page.getByLabel('Confirm send back');
  const confirmIdx = await visibleNth(confirm);
  if (confirmIdx < 0) {
    report.fail('Send-back modal opens');
  } else {
    const disabled = await confirm
      .nth(confirmIdx)
      .evaluate((el) => el.getAttribute('aria-disabled') === 'true' || el.disabled === true);
    report.check(disabled, 'Send back stays disabled until a note is typed');
    await assertTouchTarget(page, 'Confirm send back');
    await assertTouchTarget(page, 'Cancel');
    await shot(page, 'b03-send-back.png');

    await page.getByPlaceholder('What should it do differently?').fill('Check the EU refund path too.');
    await sleep(300);
    const stillDisabled = await confirm
      .nth(confirmIdx)
      .evaluate((el) => el.getAttribute('aria-disabled') === 'true' || el.disabled === true);
    report.check(!stillDisabled, 'Send back enables once there is a note');
    await clickVisible(page.getByLabel('Cancel'), 'cancel send back');
    await sleep(400);
  }

  await page.close();
}

// ── 5. The Feed chip — the bug. It did nothing at all before this change ───
{
  const page = await freshPage(browser, port);
  await openDrawerAndGo(page, 'Feed');
  await shot(page, 'b04-feed.png');

  // The demo feed's board link points at the SECOND board with a colon-bearing
  // GraphQL id, so following it exercises the board switch AND the ref split,
  // not the trivial already-selected case.
  const chip = page.getByText('Board runs OOM a 4GiB workspace', { exact: false });
  const chipIdx = await visibleNth(chip);
  if (chipIdx < 0) {
    report.fail('Feed shows the board link chip');
  } else {
    await chip.nth(chipIdx).click();
    await sleep(1800);

    const onBoard = (await visibleNth(page.getByText('Needs review', { exact: true }))) >= 0;
    report.check(onBoard, 'Feed board chip lands on Board');

    // The card it named, by the id the ref carried — verbatim, colon included.
    // If the ref had been split on every colon, or the id encoded on the way
    // through, this selector is what would come up empty.
    // Addressed by ATTRIBUTE, not `#id` — a CSS id selector cannot hold a
    // raw colon, and the colon is the whole point: this is a GitHub GraphQL
    // global id, carried through the ref and into the DOM unchanged.
    const card = page.locator('[id="board-item-I_kwDOA:4102"]');
    let found = false;
    try {
      await waitVisible(card, 'the linked card', 6000);
      found = true;
    } catch {
      found = false;
    }
    report.check(found, 'The item named by the ref is on screen');
    await shot(page, 'b05-board-focused.png');
  }
  await page.close();
}

await browser.close();
close();
process.exit(report.finish());
