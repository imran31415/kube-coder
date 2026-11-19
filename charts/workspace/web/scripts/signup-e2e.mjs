// Real compiled signup UI and Python HTTP/state machine; external services are fixtures.
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
const browser = await chromium.launch();
const origin = 'http://127.0.0.1:8089';
const pageErrors = [];
async function reset(mode = 'ready') {
  const result = await fetch(origin + '/__fixture/reset', { method: 'POST', body: JSON.stringify({ mode }) });
  assert.equal(result.status, 200);
}
async function ownerSession() {
  const context = await browser.newContext({ extraHTTPHeaders: { 'X-Forwarded-Groups': '123', 'X-Forwarded-Preferred-Username': 'octocat' } });
  await context.addCookies([{ name: 'fixture_session', value: '123', url: origin }]);
  const page = await context.newPage();
  page.on('pageerror', error => pageErrors.push(error.message));
  page.on('request', request => {
    if (new URL(request.url()).pathname.startsWith('/api/')) assert.ok(request.url().includes('/api/signup/'), 'UI must not call admin APIs');
  });
  return { context, page };
}
async function ready(page) {
  await page.getByRole('link', { name: 'Open workspace' }).waitFor({ timeout: 20000 });
}
try {
  await reset();
  const guest = await browser.newPage();
  await guest.goto(origin);
  await guest.getByRole('link', { name: 'Continue with GitHub' }).waitFor();
  assert.equal((await guest.request.get(origin + '/api/workspaces')).status(), 404);
  await guest.close();
  for (const [id, login] of [['999', 'unapproved'], ['123', 'octocat']]) {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 },
      extraHTTPHeaders: { 'X-Forwarded-Groups': id, 'X-Forwarded-Preferred-Username': login } });
    await context.addCookies([{ name: 'fixture_session', value: id, url: origin }]);
    const page = await context.newPage();
    await page.goto(origin);
    if (id === '999') {
      await page.getByText(/isn’t approved yet/).waitFor();
      assert.equal(await page.getByRole('button', { name: 'Create workspace', exact: true }).count(), 0);
    } else {
      await page.getByRole('button', { name: 'Create workspace', exact: true }).click();
      await page.getByRole('link', { name: 'Open workspace' }).waitFor({ timeout: 20000 });
      await page.reload();
      assert.equal(await page.getByRole('link', { name: 'Open workspace' }).getAttribute('href'), 'https://octocat.example.test/');
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'mobile layout must not overflow');
      if (process.env.SIGNUP_SCREENSHOT) await page.screenshot({ path: process.env.SIGNUP_SCREENSHOT, fullPage: true });
    }
    await context.close();
  }
  await reset();
  const owner = await ownerSession();
  await owner.page.goto(origin);
  let submissions = 0;
  owner.page.on('request', request => { if (request.url().endsWith('/api/signup/workspace')) submissions++; });
  await owner.page.getByRole('button', { name: 'Create workspace', exact: true }).dblclick();
  await ready(owner.page);
  assert.equal(submissions, 1, 'double click must not create twice');
  assert.equal((await owner.page.request.post(origin + '/__fixture/delete')).status(), 200);
  await owner.page.reload();
  await owner.page.getByRole('button', { name: 'Create workspace again' }).click();
  await ready(owner.page);
  await owner.context.close();

  await reset('dex_failure');
  const failed = await ownerSession();
  await failed.page.goto(origin);
  await failed.page.getByRole('button', { name: 'Create workspace', exact: true }).click();
  await failed.page.getByRole('button', { name: 'Retry setup' }).waitFor({ timeout: 15000 });
  assert.match(await failed.page.getByRole('alert').innerText(), /temporarily unavailable/);
  await failed.page.getByRole('button', { name: 'Retry setup' }).click();
  await failed.page.getByText('Workspace setup is busy. Try again shortly.').waitFor();
  await new Promise(resolve => setTimeout(resolve, 16000));
  await failed.page.getByRole('button', { name: 'Retry setup' }).click();
  await ready(failed.page);
  await failed.context.close();

  await reset('owner_conflict');
  const conflict = await ownerSession();
  await conflict.page.goto(origin);
  await conflict.page.getByRole('button', { name: 'Create workspace', exact: true }).click();
  await conflict.page.getByText('This workspace address is already reserved.').waitFor();
  assert.equal(await conflict.page.getByRole('link', { name: 'Open workspace' }).count(), 0);
  await conflict.context.close();

  await reset();
  const lost = await ownerSession();
  await lost.page.route('**/api/signup/workspace', async route => {
    await route.fetch(); // Server accepted it; only the response is lost.
    await route.abort('connectionreset');
  });
  await lost.page.goto(origin);
  await lost.page.getByRole('button', { name: 'Create workspace', exact: true }).click();
  await lost.page.getByText(/Checking whether setup started/).waitFor();
  await ready(lost.page);
  await lost.context.close();

  const unavailable = await ownerSession();
  await unavailable.page.route('**/api/signup/me', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ code: 'state_unavailable', error: 'Workspace progress is temporarily unavailable. Try again.', retryable: true }) }));
  await unavailable.page.goto(origin);
  await unavailable.page.getByRole('alert').waitFor();
  await unavailable.page.unroute('**/api/signup/me');
  await unavailable.page.getByRole('button', { name: 'Check again' }).click();
  await ready(unavailable.page);
  await unavailable.context.clearCookies();
  await unavailable.page.reload();
  await unavailable.page.getByRole('link', { name: 'Continue with GitHub' }).waitFor();
  await unavailable.context.close();
  assert.deepEqual(pageErrors, []);
  console.log('PASS: Helm-packaged UI, sign-in, eligibility, duplicate clicks, readiness, reload, deletion/recreation, failure/retry/cooldown, ownership conflict, lost response, outage recovery, session expiry, mobile layout, no admin API calls');
} finally { await browser.close(); }
