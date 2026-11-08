/** Native components rendered by Expo Web. Physical OS push is a separate gate. */
import assert from 'node:assert/strict';
import { mkdir } from 'node:fs/promises';
import { serveDist, launchBrowser, freshPage, openDrawerAndGo } from './lib/harness.mjs';
const server = await serveDist(import.meta.url);
const browser = await launchBrowser();
await mkdir('artifacts/publish', { recursive: true });
let page;
try {
  page = await freshPage(browser, server.port);
  page.on('pageerror', error => console.error('App error:', error.message));
  page.setDefaultTimeout(20000);
  await openDrawerAndGo(page, 'Builds');
  await page.getByRole('tab', { name: /^Done/ }).click();
  await page.getByText('Review a completed Build and open a PR', { exact: true }).click();
  await page.getByLabel('View changes', { exact: true }).click();
  await page.getByRole('button', { name: 'Prepare review', exact: true }).click();
  const title = page.getByLabel('PR title', { exact: true });
  await title.fill('Edited on a phone');
  await page.getByRole('button', { name: 'Save description', exact: true }).click();
  const action = page.getByRole('button', { name: 'Push & Open PR', exact: true });
  await action.scrollIntoViewIfNeeded();
  assert.ok((await action.boundingBox()).height >= 44);
  assert.equal(await title.inputValue(), 'Edited on a phone');
  await page.screenshot({ path: 'artifacts/publish/native-review.png' });
  await action.click();
  await page.getByRole('button', { name: 'Open PR #42 · open', exact: true }).waitFor();
  const viewPR = page.getByRole('button', { name: 'View PR', exact: true });
  await viewPR.waitFor();
  assert.equal(await viewPR.isEnabled(), true);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.screenshot({ path: 'artifacts/publish/native-published.png' });
  console.log('PASS: Expo phone layout, review, edit, save, publish result, 44-point target and no overflow.');
} catch (error) {
  if (page) {
    await page.screenshot({ path: 'artifacts/publish/native-failure.png' });
    console.error((await page.locator('body').innerText()).slice(-3000));
  }
  throw error;
} finally { await browser.close(); server.close(); }
