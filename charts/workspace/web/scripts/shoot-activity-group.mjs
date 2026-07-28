#!/usr/bin/env node
/**
 * Visual QA for grouped tool activity in the chat (#546): a turn with a long
 * run of consecutive tool calls collapses to one chip, and a run containing a
 * failure renders error-styled and starts expanded. Mocks /api/hypervisor so
 * the transcript is deterministic; shoots desktop + mobile in dark + light.
 *
 * Usage: node scripts/shoot-activity-group.mjs [output-dir]
 */
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromiumPath } from './chromium-path.mjs';

const out = resolve(process.argv[2] || '/home/dev/screenshots');
mkdirSync(out, { recursive: true });

const CHROMIUM = chromiumPath();
const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:7070';

const now = 1_752_300_000; // fixed epoch (script must be deterministic)
const CONFIG = {
  enabled: true,
  defaultAssistant: 'claude',
  workdir: '/home/dev',
  readOnly: false,
  assistants: [{ id: 'claude', label: 'Claude', default: true, model: 'opus-4.8' }],
};
const THREADS = [
  { id: 't1', title: 'Get the tests green', assistant: 'claude', status: 'idle', created_at: now - 9000, updated_at: now - 600 },
];

let seq = 0;
const ev = (o) => ({ seq: (seq += 1), ts: now + seq, ...o });
const say = (role, text) => ev({ role, type: 'message', text });
const call = (name, input) => ev({ role: 'assistant', type: 'tool_call', tool_id: `t${seq + 1}`, tool: { name, input } });
const res = (text, is_error) => ev({ role: 'system', type: 'tool_result', tool_use_id: `t${seq}`, text, is_error });
const ran = (cmd, err) => [call('Bash', { command: cmd }), res(err ? 'command failed' : 'ok', err)];
const read = (p) => [call('Read', { file_path: p }), res('…file contents…')];

const EVENTS = [
  say('user', 'The suite is red — figure out why and fix it.'),
  ...ran('git status'),
  ...ran('npm test'),
  ...ran('git log --oneline -5'),
  ...read('/home/dev/app/src/api.ts'),
  ...read('/home/dev/app/src/api.test.ts'),
  ...ran('rg "fetchUser" -n'),
  say('assistant', 'Two tests fail because `fetchUser` now returns a `Result` wrapper but the tests still expect the bare object. Patching the tests:'),
  ...ran('npm test -- api', true),
  ...ran('npx tsc --noEmit'),
  ...ran('npm test'),
  say('assistant', 'Green — 41 passing. The only change was the two assertions in `api.test.ts`.'),
];

async function mockHv(page) {
  await page.route('**/api/hypervisor/config', (r) =>
    r.fulfill({ contentType: 'application/json', body: JSON.stringify(CONFIG) }),
  );
  await page.route('**/api/hypervisor/threads', (r) =>
    r.fulfill({ contentType: 'application/json', body: JSON.stringify({ threads: THREADS }) }),
  );
  await page.route('**/api/hypervisor/threads/**', (r) =>
    r.fulfill({ contentType: 'application/json', body: JSON.stringify({ thread: THREADS[0], events: EVENTS }) }),
  );
}

async function shoot(browser, { name, viewport, theme }) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 2, colorScheme: theme });
  await ctx.addInitScript((t) => {
    localStorage.setItem('kc.onboardingDone', 'true');
    localStorage.setItem('kc.theme', t);
  }, theme);
  const page = await ctx.newPage();
  await mockHv(page);
  await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 20000 });
  await page.evaluate((t) => {
    document.documentElement.setAttribute('data-theme', t);
    window.history.pushState({}, '', '/hypervisor');
    window.dispatchEvent(new PopStateEvent('popstate'));
  }, theme);
  await page.waitForSelector('.hv-activity-group', { timeout: 15000 });
  await page.waitForTimeout(400);
  await page.evaluate(() => {
    document.querySelector('.hv-transcript')?.scrollTo(0, 0);
  });
  await page.waitForTimeout(200);
  await page.screenshot({ path: `${out}/${name}-collapsed.png`, fullPage: false });
  console.log(`✓ ${name}-collapsed.png`);

  // Expand the first (clean) group — its children come back as normal chips.
  await page.click('.hv-activity-group .hv-activity-head');
  await page.waitForTimeout(300);
  await page.screenshot({ path: `${out}/${name}-expanded.png`, fullPage: false });
  console.log(`✓ ${name}-expanded.png`);
  await ctx.close();
}

const browser = await chromium.launch({ executablePath: CHROMIUM, headless: true });
try {
  for (const theme of ['dark', 'light']) {
    await shoot(browser, { name: `actgroup-desktop-${theme}`, viewport: { width: 1280, height: 800 }, theme });
    await shoot(browser, { name: `actgroup-mobile-${theme}`, viewport: { width: 390, height: 844 }, theme });
  }
} finally {
  await browser.close();
}
console.log(`\nSaved to ${out}`);
