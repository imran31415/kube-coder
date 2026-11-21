// Signup allowlist: the real compiled console and signup UI against the
// real admin and signup HTTP handlers (tests/integration/signup_fixture.py).
// External services (GitHub, Dex, Kubernetes) are in-memory fixtures.
import { chromium } from 'playwright';
import assert from 'node:assert/strict';

const admin = 'http://127.0.0.1:8088';
const signup = 'http://127.0.0.1:8089';
const ADMIN = { 'X-Forwarded-Preferred-Username': 'alice' };
const browser = await chromium.launch();
const pageErrors = [];

async function reset() {
  const result = await fetch(signup + '/__fixture/reset', { method: 'POST', body: JSON.stringify({ mode: 'ready' }) });
  assert.equal(result.status, 200);
}

async function open(url, headers, cookieId, viewport) {
  const context = await browser.newContext({ extraHTTPHeaders: headers, ...(viewport ? { viewport } : {}) });
  if (cookieId) await context.addCookies([{ name: 'fixture_session', value: cookieId, url: signup }]);
  const page = await context.newPage();
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.goto(url);
  return { context, page };
}

const signupAs = (id, login) => open(signup, { 'X-Forwarded-Groups': id, 'X-Forwarded-Preferred-Username': login }, id);

async function lookUp(page, login) {
  await page.getByPlaceholder('octocat').fill(login);
  await page.getByRole('button', { name: 'Look up' }).click();
}

try {
  await reset();

  // Not on the list: the signup page says so and offers no Create button.
  const mona = await signupAs('456', 'Mona');
  await mona.page.getByText(/isn’t approved yet/).waitFor();
  assert.equal(await mona.page.getByRole('button', { name: 'Create workspace', exact: true }).count(), 0);

  // The admin console lists the Helm seed read-only.
  const { context: consoleContext, page: console_ } = await open(admin + '/#/signup-access', ADMIN);
  await console_.getByText('GitHub ID 123').waitFor();
  assert.equal(await console_.getByRole('button', { name: 'Remove GitHub ID 123' }).count(), 0);
  assert.match(await console_.locator('.sub').first().innerText(), /127\.0\.0\.1:8089/);

  // Add by username: resolve, confirm, store the ID.
  await lookUp(console_, 'mona');
  await console_.getByText('@Mona · ID 456').waitFor();
  await console_.getByPlaceholder('e.g. Spring intern').fill('e2e');
  await console_.getByRole('button', { name: 'Allow @Mona' }).click();
  await console_.getByText('@Mona can now sign up.').waitFor();
  const addedAt = Date.now();

  // The person can create a workspace within seconds, with no restart.
  await mona.page.getByRole('button', { name: 'Check again' }).click();
  await mona.page.getByRole('button', { name: 'Create workspace', exact: true }).waitFor({ timeout: 6000 });
  assert.ok(Date.now() - addedAt < 6000, 'an added person must be able to create within a few seconds');
  await mona.page.getByRole('button', { name: 'Create workspace', exact: true }).click();
  await mona.page.getByRole('link', { name: 'Open workspace' }).waitFor({ timeout: 20000 });

  // The console shows their workspace.
  await console_.getByRole('button', { name: 'Refresh' }).click();
  await console_.getByRole('link', { name: 'Workspace →' }).waitFor();

  // Already allowed: no second Allow.
  await lookUp(console_, 'mona');
  await console_.getByText('Already on the list.').waitFor();
  await console_.getByRole('button', { name: 'Done' }).click();

  // Remove: blocks new creation at once; the existing workspace keeps working.
  await console_.getByRole('button', { name: 'Remove @Mona' }).click();
  await console_.getByRole('group').getByRole('button', { name: 'Remove' }).click();
  await console_.getByText('@Mona removed. Their existing workspace keeps working.').waitFor();
  const me = await (await mona.page.request.get(signup + '/api/signup/me')).json();
  assert.equal(me.eligible, false);
  const blocked = await mona.page.request.post(signup + '/api/signup/retry', {
    headers: { Origin: signup, 'X-CSRF-Token': me.csrfToken }, data: {} });
  assert.equal(blocked.status(), 403);
  assert.equal((await blocked.json()).code, 'not_eligible');
  await mona.page.reload();
  await mona.page.getByRole('link', { name: 'Open workspace' }).waitFor();
  await mona.context.close();

  // Organizations are refused by the real server path.
  await lookUp(console_, 'acme');
  await console_.getByRole('alert').filter({ hasText: /organization or bot account/ }).waitFor();

  // GitHub outage: a short reason and a Retry that recovers.
  await console_.route('**/api/allowlist/lookup*', route => route.fulfill({ status: 502, contentType: 'application/json',
    body: JSON.stringify({ error: "GitHub didn't respond. Try again.", code: 'github_unavailable', retryable: true, error_id: 'e2eref' }) }));
  await lookUp(console_, 'mona');
  await console_.getByRole('alert').filter({ hasText: "GitHub didn't respond." }).waitFor();
  await console_.getByText('Ref e2eref').waitFor();
  await console_.unroute('**/api/allowlist/lookup*');
  await console_.getByRole('button', { name: 'Retry' }).click();
  await console_.getByText('@Mona · ID 456').waitFor();
  await console_.getByRole('button', { name: 'Cancel' }).click();

  // The list failing to load shows why and recovers on Retry.
  await console_.route('**/api/allowlist', route => route.fulfill({ status: 503, contentType: 'application/json',
    body: JSON.stringify({ error: 'unavailable', code: 'state_unavailable', retryable: true }) }));
  await console_.reload();
  await console_.getByText("Couldn't reach the signup list storage. Try again.").waitFor();
  await console_.unroute('**/api/allowlist');
  await console_.getByRole('button', { name: 'Retry' }).click();
  await console_.getByText('GitHub ID 123').waitFor();
  await consoleContext.close();

  // The boundary, from outside a browser.
  const post = (base, headers) => fetch(base + '/oauth/api/allowlist', { method: 'POST',
    headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify({ login: 'mona', githubId: '456' }) });
  assert.equal((await post(admin, { ...ADMIN, Origin: 'https://evil.test' })).status, 403, 'cross-site write');
  assert.equal((await post(admin, {})).status, 401, 'no admin identity');
  for (const path of ['/api/allowlist', '/oauth/api/allowlist', '/api/allowlist/lookup?login=mona']) {
    assert.equal((await fetch(signup + path, { headers: ADMIN })).status, 404, `signup listener GET ${path}`);
  }
  assert.equal((await post(signup, ADMIN)).status, 404, 'signup listener POST');
  assert.notEqual((await fetch(signup + '/api/allowlist/456', { method: 'DELETE', headers: ADMIN })).status, 200);

  // Phone layout: no sideways scroll, actions stay reachable.
  const phone = await open(admin + '/#/signup-access', ADMIN, null, { width: 390, height: 844 });
  await phone.page.getByText('GitHub ID 123').waitFor();
  assert.ok(await phone.page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'mobile layout must not overflow');
  if (process.env.ALLOWLIST_SCREENSHOT) await phone.page.screenshot({ path: process.env.ALLOWLIST_SCREENSHOT, fullPage: true });
  await phone.context.close();

  assert.deepEqual(pageErrors, []);
  console.log('PASS: Helm seed read-only, lookup + confirm + add, signup within seconds without restart, workspace link, already-allowed, remove blocks new creation but keeps the workspace, org refused, GitHub outage + Retry, list outage + Retry, cross-site and anonymous writes refused, no route on the signup listener, mobile layout');
} finally { await browser.close(); }
