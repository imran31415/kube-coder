/**
 * Board-on-mobile check (#692), and its three tabs (#709 Part 3).
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
  // 'View changes' is the Build's branch row on an isolated item (#701).
  for (const label of ['Approve', 'Reject', 'Send back', 'Open ticket', 'View changes']) {
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

// ── 6. Three tabs — Needs you · Working · Done (#709 Part 3) ───────────────
{
  const page = await freshPage(browser, port);
  await openDrawerAndGo(page, 'Board');
  await sleep(800);

  /** The tab whose label starts with `name`, as a screen reader hears it. */
  const tabLabel = async (name) => {
    const loc = page.getByRole('tab', { name: new RegExp(`^${name}`) });
    const i = await visibleNth(loc);
    return i < 0 ? null : loc.nth(i).getAttribute('aria-label');
  };
  const showTab = async (name) => {
    await clickVisible(page.getByRole('tab', { name: new RegExp(`^${name}`) }), `${name} tab`);
    await sleep(500);
  };
  const seen = async (text) =>
    (await visibleNth(page.getByText(text, { exact: false }))) >= 0;

  // Every tab announces its count. Needs you counts from the FEED — the demo
  // feed holds two waiting rows for this board.
  report.check(
    (await tabLabel('Needs you')) === 'Needs you, 2 waiting',
    'Needs you tab announces its count from the Feed',
    String(await tabLabel('Needs you')),
  );
  report.check(/^Working, \d+ items?$/.test((await tabLabel('Working')) ?? ''),
    'Working tab announces its count', String(await tabLabel('Working')));
  report.check(/^Done, \d+ items?, \d+ unread$/.test((await tabLabel('Done')) ?? ''),
    'Done tab announces its count and unread', String(await tabLabel('Done')));
  for (const name of ['Needs you', 'Working', 'Done']) {
    await assertTouchTarget(page, new RegExp(`^${name},`));
  }
  report.check(await seen('Refund not received'), 'Needs you opens on the review queue');
  await shot(page, 'b06-tab-needs-you.png');

  // ── Working: claimed and working rows only ──
  await showTab('Working');
  report.check(await seen('Duplicate charge on invoice 4471'), 'Working shows a working item');
  report.check(await seen('CSV export times out'), 'Working shows a claimed item');
  report.check(!(await seen('Refund not received')), 'Working does not repeat a review card');
  report.check(!(await seen('Update the billing address')), 'Working does not show a finished item');
  await assertTouchTarget(page, /^SUP-818,/);
  await shot(page, 'b07-tab-working.png');

  // ── Done: one result line and an unread dot, cleared on open ──
  await showTab('Done');
  report.check(await seen('Reply posted on SUP-807'), 'Done shows a result line');
  report.check(await seen('Failed: Jira returned 403'), 'Done shows a failure line');
  report.check(!(await seen('Duplicate charge on invoice 4471')), 'Done does not show a working item');
  const dot = page.locator('[id="board-done-807"] [data-testid="unread-dot"]');
  report.check((await dot.count()) === 1, 'A finished item shows an unread dot');
  const unreadBefore = await tabLabel('Done');
  await assertTouchTarget(page, /^Unread\. SUP-807,/);
  await shot(page, 'b08-tab-done.png');
  await clickVisible(page.getByLabel(/^Unread\. SUP-807,/), 'the SUP-807 row');
  await sleep(500);
  report.check((await dot.count()) === 0, 'Opening the row clears its dot');
  report.check(await seen('Open ticket'), 'An opened row shows its detail');
  report.check((await tabLabel('Done')) !== unreadBefore, 'The Done tab unread count drops',
    `${unreadBefore} → ${await tabLabel('Done')}`);
  // Still cleared after the next poll: the dot is the Feed's read flag.
  await clickVisible(page.getByRole('tab', { name: /^Working/ }), 'Working tab');
  await sleep(300);
  await showTab('Done');
  report.check((await dot.count()) === 0, 'The cleared dot stays cleared');
  await shot(page, 'b09-done-opened.png');

  // ── Approve and reject still work under the tabs ──
  await showTab('Needs you');
  await clickVisible(page.getByLabel('Approve'), 'approve SUP-812');
  await sleep(1500);
  report.check(!(await seen('Refund not received')), 'An approved card leaves Needs you');
  report.check((await tabLabel('Needs you')) === 'Needs you, 1 waiting',
    'The Needs-you count drops after approving', String(await tabLabel('Needs you')));
  await clickVisible(page.getByLabel('Reject'), 'reject SUP-815');
  await sleep(1500);
  report.check(await seen('Nothing to review'), 'Needs you has an empty state');
  await showTab('Done');
  report.check(await seen('Written to SUP-812: comment, set status'),
    'The approved item lands on Done with its result');
  report.check(await seen('Rejected, nothing was written'),
    'The rejected item lands on Done with its result');
  report.check(
    (await page.locator('[id="board-done-812"] [data-testid="unread-dot"]').count()) === 1,
    'A freshly decided item is unread');
  await shot(page, 'b10-after-decisions.png');

  // ── Empty states, and the rule: the phone never starts a run ──
  await clickVisible(page.getByLabel('Show kube-coder issues'), 'second board');
  await sleep(1200);
  await showTab('Working');
  report.check(await seen('Nothing being worked'), 'Working has an empty state');
  for (const name of ['Needs you', 'Working', 'Done']) {
    await showTab(name);
    report.check(
      (await page.getByText(/start (a )?run/i).count()) === 0 &&
        (await page.getByLabel(/start (a )?run/i).count()) === 0,
      `No start-run control on ${name}`,
    );
  }
  await page.close();
}

await browser.close();
close();
process.exit(report.finish());
