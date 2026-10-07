/** Real-server regression check. Requires isolated, aged fixtures and a logged-in
 * Claude agent. KC750_FIXTURES names a JSON file mapping other/cto/background/
 * phone/plain to {id,title}; KC750_URL and KC750_OUTPUT select server/artifacts.
 * This script sends real prompts. Only the explicit 503 fault-injection step
 * substitutes a response; all successful flows use the real server/agent.
 * KC750_RESILIENCE_ONLY=1 reruns the final checks without more agent turns.
 */
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';

const base = process.env.KC750_URL || 'http://127.0.0.1:6175';
const fixtures = JSON.parse(await fs.readFile(process.env.KC750_FIXTURES, 'utf8'));
const output = process.env.KC750_OUTPUT || 'issue-750-evidence';
await fs.mkdir(output, { recursive: true });
const resilienceOnly = process.env.KC750_RESILIENCE_ONLY === '1';
const results = resilienceOnly
  ? JSON.parse(await fs.readFile(path.join(output, 'results.json'), 'utf8')).passed : [];
function record(message) { results.push(message); console.log(message); }
const prompt = 'Read the local file sample.txt using a tool. This is a read-only verification, not a build request. Do not dispatch agents or change files. Reply with KC750_REAL_OK followed by the three words in the file.';

async function api(route, body) {
  const response = await fetch(`${base}/api/hypervisor/threads${route}`, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  assert(response.ok, `API ${route}: ${response.status}`);
  return response.json();
}

async function until(check, label, timeout = 90000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    if (await check()) return;
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  throw new Error(`Timed out: ${label}`);
}

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, colorScheme: 'light' });
context.setDefaultTimeout(15000);
await context.addInitScript(() => {
  localStorage.setItem('kc.onboardingDone', 'true');
  localStorage.setItem('kube-coder.ui', JSON.stringify({ theme: 'light', density: 'comfortable', railCollapsed: false }));
});
const page = await context.newPage();
const pageErrors = [];
page.on('pageerror', error => pageErrors.push(error.message));
const row = fixture => page.getByTitle(fixture.title, { exact: true });
const tab = name => page.getByRole('tab', { name: new RegExp(`^${name}`) });

async function screenshot(name) {
  await page.screenshot({ path: path.join(output, `${name}.png`), animations: 'disabled' });
}

async function finish(fixture) {
  await until(async () => (await api(`/${fixture.id}`)).thread.status !== 'running', 'real turn completion');
  const detail = await api(`/${fixture.id}`);
  assert(detail.events.some(e => e.role === 'assistant' && e.text?.includes('KC750_REAL_OK')), 'Real agent must return the expected marker');
  assert(detail.events.some(e => e.type === 'tool_call'), 'Real agent must use a tool');
  return detail;
}

try {
  await page.goto(`${base}/hypervisor/${fixtures.other.id}`);
  await row(fixtures.other).waitFor();
  if (!resilienceOnly) {
    assert.equal(await row(fixtures.cto).count(), 0, 'Old CTO starts outside Active');
    await tab('Past').click();
    await row(fixtures.cto).waitFor();
    await screenshot('01-old-cto-in-past');
    await row(fixtures.cto).click();
    await page.getByRole('textbox', { name: 'Message Kube-Coder' }).fill(prompt);
    await page.getByRole('button', { name: 'Send', exact: true }).click();
    await until(async () => (await api(`/${fixtures.cto.id}`)).thread.status === 'running', 'CTO running');
    await tab('Active').click();
    await row(fixtures.other).click();
    await row(fixtures.cto).waitFor();
    assert.equal((await api(`/${fixtures.cto.id}`)).thread.status, 'running', 'Check visibility while the real turn is still running');
    await screenshot('02-running-cto-after-switch');
    await finish(fixtures.cto);
    await until(async () => (await row(fixtures.cto).locator('..').innerText()).includes('idle'), 'sidebar completion');
    await screenshot('03-completed-cto-active');
    record('Old CTO resumed through UI; stays Active after switching and after real tool-use completion');

    // An independent browser client starts work while the first remains on another chat.
    const second = await context.newPage();
    await second.goto(`${base}/hypervisor/${fixtures.background.id}`);
    await second.getByRole('textbox', { name: 'Message Kube-Coder' }).fill(prompt);
    const started = Date.now();
    await second.getByRole('button', { name: 'Send', exact: true }).click();
    await row(fixtures.background).waitFor({ timeout: 10000 });
    record(`Background CTO discovered without reload in ${Date.now() - started} ms`);
    await finish(fixtures.background);
    await second.close();

    await row(fixtures.cto).click();
    await page.getByRole('textbox', { name: 'Message Kube-Coder' }).fill(prompt);
    await page.getByRole('button', { name: 'Send', exact: true }).click();
    await until(async () => (await api(`/${fixtures.cto.id}`)).thread.status === 'running', 'turn to stop');
    await page.getByRole('button', { name: 'Stop', exact: true }).click();
    await until(async () => (await api(`/${fixtures.cto.id}`)).thread.status === 'idle', 'stopped turn');
    await row(fixtures.other).click();
    await row(fixtures.cto).waitFor();
    record('Stopped CTO remains Active after using the Stop button');

    await page.reload();
    await row(fixtures.cto).waitFor();
    await page.getByRole('button', { name: 'CTO', exact: true }).click();
    await until(async () => await row(fixtures.other).count() === 0, 'CTO filter');
    await row(fixtures.cto).waitFor();
    await page.getByRole('button', { name: 'All', exact: true }).click();
    record('Reload and CTO/All filters preserve correct visibility');

    // Repeat the actual resume/switch journey on a phone and for ordinary chats.
    for (const [key, width] of [['phone', 390], ['plain', 1440]]) {
      const fixture = fixtures[key];
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      const openChats = async () => {
        if (width === 390 && await page.locator('.route-hypervisor').getAttribute('data-sidebar-open') !== 'true') {
          await page.getByRole('button', { name: /^Chats \(/ }).click();
        }
      };
      await openChats();
      await tab('Past').click();
      await row(fixture).click();
      await page.getByRole('textbox', { name: 'Message Kube-Coder' }).fill(prompt);
      await page.getByRole('button', { name: 'Send', exact: true }).click();
      await until(async () => (await api(`/${fixture.id}`)).thread.status === 'running', `${key} running`);
      await openChats();
      await tab('Active').click();
      await row(fixtures.other).click();
      await openChats();
      await row(fixture).waitFor();
      assert.equal((await api(`/${fixture.id}`)).thread.status, 'running');
      await screenshot(`04-${key}-running-after-switch`);
      await finish(fixture);
      await row(fixture).waitFor();
      record(`${key}: resumed through UI, switched chats, completed real tool use, stayed Active`);
    }
  }

  // Temporary HTTP failure. A transport abort instead triggers the existing
  // global API client's sign-in redirect, independently of chat polling.
  const listUrl = `${base}/api/hypervisor/threads`;
  let failed = false;
  await page.route(listUrl, async route => {
    failed = true;
    await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: 'Injected temporary outage' }) });
  }, { times: 1 });
  await until(() => failed, 'injected list failure', 15000);
  await row(fixtures.cto).waitFor();
  const recovered = await page.waitForResponse(response => response.url() === listUrl && response.ok(), { timeout: 15000 });
  assert(recovered.ok());
  await row(fixtures.cto).waitFor();
  record('Browser retained rows through an injected HTTP 503 and recovered against the real server');

  // Rename/delete/restore after polling is active; only our isolated fixture.
  const threadRow = row(fixtures.plain).locator('..');
  await threadRow.getByRole('button', { name: 'Rename chat' }).click();
  await page.getByRole('textbox', { name: 'Chat name' }).fill('KC750 renamed');
  await page.getByRole('textbox', { name: 'Chat name' }).press('Enter');
  fixtures.plain.title = 'KC750 renamed';
  await row(fixtures.plain).waitFor();
  await row(fixtures.plain).locator('..').getByRole('button', { name: 'Delete chat' }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: 'Delete', exact: true }).click();
  await row(fixtures.plain).waitFor({ state: 'detached' });
  await page.getByRole('button', { name: 'Recently deleted' }).click();
  const trashRow = page.locator('.hv-thread-deleted').filter({ hasText: fixtures.plain.title });
  await trashRow.getByRole('button', { name: 'Restore' }).click();
  await row(fixtures.plain).waitFor();
  record('Rename, delete, and restore work with polling enabled');
  assert.deepEqual(pageErrors, [], 'No browser runtime errors');
  await fs.writeFile(path.join(output, 'results.json'), JSON.stringify({ passed: results, pageErrors }, null, 2));
  console.log(JSON.stringify({ passed: results, pageErrors }, null, 2));
} catch (error) {
  await fs.writeFile(path.join(output, 'results.json'), JSON.stringify({ passed: results, error: error.message, pageErrors }, null, 2));
  await screenshot('failure');
  await fs.writeFile(path.join(output, 'failure.txt'), `${error.stack}\n${await page.locator('body').ariaSnapshot()}`);
  throw error;
} finally {
  await browser.close();
}
