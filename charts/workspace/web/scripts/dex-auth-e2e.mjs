// Real Dex + pinned oauth2-proxy, local fixture identities. No GitHub accounts.
import { chromium } from 'playwright';
import assert from 'node:assert/strict';

const browser = await chromium.launch({ headless: true });
async function count() {
  return (await (await fetch('http://127.0.0.1:8099/count')).json()).protectedRequests;
}
async function login(port, email) {
  const context = await browser.newContext();
  const page = await context.newPage();
  const statuses = [];
  page.on('response', response => {
    if (response.url().startsWith(`http://127.0.0.1:${port}/oauth2/callback`)) statuses.push(response.status());
  });
  await page.goto(`http://127.0.0.1:${port}/oauth2/start?rd=%2Fprotected`);
  await page.locator('input[name="login"]').fill(email);
  await page.locator('input[name="password"]').fill('password');
  await page.locator('button[type="submit"]').click();
  await page.waitForURL(url => url.pathname === '/approval' || url.port === String(port));
  if (new URL(page.url()).pathname === '/approval') {
    await page.getByRole('button', { name: 'Grant Access' }).click();
  }
  await page.waitForURL(url => url.port === String(port));
  return { context, page, statuses };
}
try {
  const owner = await login(4181, 'owner@example.test');
  assert.equal((await owner.page.request.get('http://127.0.0.1:4181/oauth2/auth')).status(), 202);
  const otherContext = await browser.newContext();
  await otherContext.addCookies(await owner.context.cookies());
  assert.notEqual((await otherContext.request.get('http://127.0.0.1:4182/oauth2/auth')).status(), 202, 'workspace A cookie must not authenticate workspace B');
  await otherContext.close();
  await new Promise(resolve => setTimeout(resolve, 2200));
  assert.equal((await owner.page.request.get('http://127.0.0.1:4181/protected')).status(), 200);
  await owner.context.close();
  const before = await count();
  const denied = await login(4181, 'other@example.test');
  assert.ok(denied.statuses.includes(403), 'non-owner callback must return 403');
  assert.equal((await denied.page.request.get('http://127.0.0.1:4181/oauth2/auth', {
    headers: { 'X-Forwarded-User': '123', 'X-Forwarded-Groups': '123', 'X-Auth-Request-User': '123' },
  })).status(), 401, 'forged identity headers must not grant a session');
  await denied.page.request.get('http://127.0.0.1:4181/protected', { maxRedirects: 0 });
  await denied.page.request.get('http://127.0.0.1:4181/terminal', {
    maxRedirects: 0, headers: { Connection: 'Upgrade', Upgrade: 'websocket' },
  });
  assert.equal(await count(), before, 'denied requests must never reach upstream');
  await denied.context.close();
  const other = await login(4182, 'other@example.test');
  assert.equal((await other.page.request.get('http://127.0.0.1:4182/oauth2/auth')).status(), 202);
  await other.context.close();
  console.log('PASS: owner, non-owner 403, refresh, cross-workspace cookies, forged header denial, denied upstream/upgrade probes');
} finally {
  await browser.close();
}
