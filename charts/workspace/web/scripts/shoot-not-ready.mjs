#!/usr/bin/env node
/**
 * Browser check for a listed-but-not-ready assistant (#702): DeepSeek Harness
 * installed, no DeepSeek API key. Drives New build and Chat against a mocked
 * API, ASSERTS the behaviour (marker, reason, disabled start, Settings link),
 * then screenshots desktop + phone in dark + light.
 *
 * Needs no real key and no workspace server — any static server for dist/:
 *   yarn build && yarn preview --port 7070 --strictPort
 *   SHOT_BASE=http://127.0.0.1:7070 node scripts/shoot-not-ready.mjs [out]
 * Exits non-zero on the first failed assertion.
 */
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromiumPath } from './chromium-path.mjs';

const out = resolve(process.argv[2] || '/home/dev/screenshots/not-ready-702');
mkdirSync(out, { recursive: true });
const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:7070';

const REASON =
  'DeepSeek Harness needs a DeepSeek API key. Add it in Settings → Provider API keys.';
const ASSISTANTS = [
  { id: 'claude', label: 'Claude Code', default: true, ready: true, models: [], efforts: [] },
  { id: 'codex', label: 'Codex', ready: true, models: [], efforts: [] },
  {
    id: 'deepseek-harness', label: 'DeepSeek Harness', model: 'deepseek-v4-flash',
    ready: false, needs: ['DEEPSEEK_API_KEY'], notReadyReason: REASON,
    models: ['deepseek-v4-flash', 'deepseek-v4-pro'], efforts: [],
  },
];
const CONFIG = {
  enabled: true, ctoEnabled: false, defaultAssistant: 'claude', workdir: '/home/dev',
  readOnly: false, assistants: ASSISTANTS,
};

let failures = 0;
function check(cond, what) {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${what}`);
  if (!cond) failures += 1;
}

async function mock(page, calls) {
  const j = (body, status = 200) => ({ status, contentType: 'application/json', body: JSON.stringify(body) });
  // Catch-all first: Playwright matches the most recently registered route.
  await page.route('**/api/**', (r) => r.fulfill(j({})));
  await page.route('**/api/claude/tasks**', (r) => {
    if (r.request().method() === 'POST') calls.push('POST /api/claude/tasks');
    return r.fulfill(j({ tasks: [] }));
  });
  await page.route('**/api/claude/assistants', (r) => r.fulfill(j({ assistants: ASSISTANTS })));
  await page.route('**/api/hypervisor/config', (r) => r.fulfill(j(CONFIG)));
  await page.route('**/api/hypervisor/threads**', (r) => {
    if (r.request().method() === 'POST') calls.push('POST /api/hypervisor/threads');
    return r.fulfill(j({ threads: [] }));
  });
  await page.route('**/api/workspace/dirs**', (r) => r.fulfill(j({ dirs: [] })));
  await page.route('**/api/projects**', (r) => r.fulfill(j({ projects: [] })));
  await page.route('**/api/provider-keys', (r) => r.fulfill(j({ providers: {} })));
  await page.route('**/api/subscriptions', (r) => r.fulfill(j({ subscriptions: {}, claude_ready: true })));
}

async function newContext(browser, viewport, theme) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 2, colorScheme: theme });
  await ctx.addInitScript((t) => {
    localStorage.setItem('kc.onboardingDone', 'true');
    localStorage.setItem('kc.theme', t);
  }, theme);
  return ctx;
}

async function newBuild(browser, name, viewport, theme, assertions) {
  const ctx = await newContext(browser, viewport, theme);
  const page = await ctx.newPage();
  const calls = [];
  await mock(page, calls);
  await page.goto(`${BASE}/tasks`, { waitUntil: 'domcontentloaded', timeout: 20000 });
  await page.getByRole('button', { name: /New build/ }).first().click();
  const select = page.locator('select.ntf-select').last();
  await page.waitForFunction(
    () => [...document.querySelectorAll('select.ntf-select option')].some((o) => o.value === 'deepseek-harness'),
    null, { timeout: 8000 },
  );
  if (assertions) {
    check(await select.inputValue() === 'claude', 'New build: a ready agent is pre-selected');
    const label = await select.locator('option[value="deepseek-harness"]').textContent();
    check(/needs API key/.test(label || ''), 'New build: DeepSeek Harness is listed with "needs API key"');
    check(await page.getByRole('alert').count() === 0, 'New build: no note while a ready agent is selected');
  }
  await select.selectOption('deepseek-harness');
  const alert = page.locator('.assistant-not-ready');
  await alert.waitFor({ timeout: 5000 });
  const start = page.getByRole('button', { name: /Start build/ });
  if (assertions) {
    check((await alert.textContent() || '').includes('needs a DeepSeek API key'), 'New build: the reason is shown');
    check(await start.isDisabled(), 'New build: Start build is disabled');
    await page.getByLabel('First prompt').fill('hello');
    await page.getByLabel('First prompt').press('Control+Enter');
    await page.waitForTimeout(300);
    check(!calls.includes('POST /api/claude/tasks'), 'New build: no build request was sent');
  }
  await page.waitForTimeout(250);
  await page.screenshot({ path: `${out}/newbuild-${name}.png` });
  if (assertions) {
    await page.getByRole('link', { name: 'Open Provider API keys' }).click();
    await page.waitForURL(/\/settings\/providers#providers$/, { timeout: 5000 }).catch(() => {});
    check(/\/settings\/providers#providers$/.test(page.url()), `New build: link opens Settings → Provider API keys (${page.url()})`);
    await select.selectOption('claude').catch(() => {});
  }
  await ctx.close();
}

async function chat(browser, name, viewport, theme, assertions) {
  const ctx = await newContext(browser, viewport, theme);
  const page = await ctx.newPage();
  const calls = [];
  await mock(page, calls);
  await page.goto(`${BASE}/hypervisor`, { waitUntil: 'domcontentloaded', timeout: 20000 });
  const trigger = page.locator('.hv-agent-select .ss-trigger').first();
  await trigger.waitFor({ state: 'attached', timeout: 10000 });
  if (!(await trigger.isVisible())) {
    // Phone layout keeps the sidebar in a sheet; open it if there's a toggle.
    const toggle = page.getByRole('button', { name: /chats|menu|sidebar/i }).first();
    if (await toggle.count()) await toggle.click().catch(() => {});
  }
  await trigger.click();
  const option = page.getByRole('option', { name: /DeepSeek Harness/ });
  if (assertions) {
    check(/needs API key/.test((await option.textContent()) || ''), 'Chat: DeepSeek Harness option shows "needs API key"');
  }
  await option.click();
  const note = page.locator('.hv-agent-not-ready');
  await note.waitFor({ timeout: 5000 });
  if (assertions) {
    check((await note.textContent() || '').includes('needs a DeepSeek API key'), 'Chat: the reason is shown under the Agent picker');
    const box = page.locator('textarea').last();
    await box.fill('hello');
    const send = page.getByRole('button', { name: /Send/ }).last();
    check(await send.isDisabled(), 'Chat: Send is disabled');
    await box.press('Enter');
    await page.waitForTimeout(300);
    check(!calls.includes('POST /api/hypervisor/threads'), 'Chat: no chat was created');
  }
  await page.screenshot({ path: `${out}/chat-${name}.png` });
  await ctx.close();
}

const browser = await chromium.launch({ executablePath: chromiumPath(), headless: true });
try {
  const desktop = { width: 1280, height: 800 };
  const phone = { width: 390, height: 844 };
  await newBuild(browser, 'desktop-dark', desktop, 'dark', true);
  await chat(browser, 'desktop-dark', desktop, 'dark', true);
  for (const [name, vp, theme] of [
    ['desktop-light', desktop, 'light'],
    ['mobile-dark', phone, 'dark'],
    ['mobile-light', phone, 'light'],
  ]) {
    await newBuild(browser, name, vp, theme, false);
    await chat(browser, name, vp, theme, false).catch((e) => console.log(`skip chat-${name}: ${e.message.split('\n')[0]}`));
  }
} finally {
  await browser.close();
}
console.log(failures ? `\n${failures} check(s) failed` : '\nall checks passed');
console.log(`screenshots: ${out}`);
process.exit(failures ? 1 : 0);
