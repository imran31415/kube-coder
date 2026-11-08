/** Rendered phone review -> actual HTTP publisher -> real Git/bare remote.
 * Start tests.publish_fixture on Linux (7108) and Vite (5173) first.
 * GitHub/model are fixture boundaries; this is not a live GitHub/device test.
 */
import assert from 'node:assert/strict';
import { mkdir } from 'node:fs/promises';
import { chromium } from 'playwright';
const backend = process.env.PUBLISH_FIXTURE_URL || 'http://127.0.0.1:7108';
const origin = process.env.PUBLISH_UI_URL || 'http://127.0.0.1:5173';
const browser = await chromium.launch({ executablePath: process.env.KC_CHROMIUM || undefined });
await mkdir('artifacts/publish', { recursive: true });
try {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  page.setDefaultTimeout(20000);
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (!url.pathname.startsWith('/api/')) return route.continue();
    const response = await route.fetch({ url: backend + url.pathname + url.search });
    await route.fulfill({ response });
  });
  await page.goto(origin + '/next/scripts/publish-harness.html');
  await page.getByRole('button', { name: 'Prepare review', exact: true }).click();
  const title = page.getByLabel('PR title', { exact: true });
  await title.waitFor();
  await title.fill('Reviewed from a phone');
  await page.getByLabel('Create as draft PR').check();
  await page.getByRole('button', { name: 'Save description', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('button') && [...document.querySelectorAll('button')].find(b => b.textContent === 'Save description')?.disabled);
  await page.reload();
  await title.waitFor();
  assert.equal(await title.inputValue(), 'Reviewed from a phone');
  assert.equal(await page.getByLabel('Create as draft PR').isChecked(), true);
  await page.getByText('Reviewed snapshot · 1 files', { exact: true }).click();
  await page.getByRole('button', { name: 'f0.txt +1 −1', exact: true }).click();
  await page.getByText('+reviewed', { exact: false }).waitFor();
  const action = page.getByRole('button', { name: 'Push & Open PR', exact: true });
  await action.scrollIntoViewIfNeeded();
  assert.ok((await action.boundingBox()).height >= 44);
  await page.screenshot({ path: 'artifacts/publish/phone-review.png', fullPage: true });
  await action.click();
  // Reopening immediately simulates loss of UI lifetime; the server owns work.
  await page.reload();
  await page.getByRole('link', { name: 'Open PR #9 · open', exact: true }).waitFor({ timeout: 20000 });
  let result = await (await fetch(backend + '/fixture/assertions')).json();
  assert.equal(result.status.operation.stage, 'published');
  assert.equal(result.remote_sha, result.head_sha);
  assert.equal(result.commit_count, 1);
  assert.equal(result.pr_creates, 1);
  assert.equal(result.main_clean, true);
  await fetch(backend + '/fixture/change', { method: 'POST' });
  assert.equal(await page.getByRole('link', { name: 'View PR', exact: true }).getAttribute('href'), result.status.pr.url);
  await page.getByRole('button', { name: 'Refresh review', exact: true }).click();
  const update = page.getByRole('button', { name: 'Push updates', exact: true });
  await update.waitFor();
  const acceptedUpdate = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname.endsWith('/publish'));
  await update.click();
  const accepted = await (await acceptedUpdate).json();
  assert.notEqual(accepted.operation.id, result.status.operation.id);
  const deadline = Date.now() + 20000;
  do {
    result = await (await fetch(backend + '/fixture/assertions')).json();
    if (result.status.operation.stage === 'published') break;
    await new Promise(resolve => setTimeout(resolve, 100));
  } while (Date.now() < deadline);
  await page.waitForFunction(() => [...document.querySelectorAll('[role="status"]')].some(e => e.textContent.includes('Publishing: published')));
  result = await (await fetch(backend + '/fixture/assertions')).json();
  assert.equal(result.commit_count, 2);
  assert.equal(result.pr_creates, 1);
  assert.equal(result.remote_sha, result.head_sha);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.screenshot({ path: 'artifacts/publish/phone-published.png', fullPage: true });
  console.log('PASS: rendered phone review, saved draft, reload recovery, exact SHA, single PR, reviewed update, touch target and no overflow.');
} finally {
  await browser.close();
}
