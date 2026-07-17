#!/usr/bin/env node
/**
 * Focused screenshots of the Hypervisor composer's slash-command / skill picker
 * (issue #302): types `/` into the composer to open the popover, on desktop
 * (dark) and mobile (light). Mocks /api/hypervisor/config with a representative
 * `commands` list so the capture is deterministic regardless of what skills the
 * host pod has installed.
 *
 * Usage: node scripts/shoot-slash.mjs [output-dir]
 */
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromiumPath } from './chromium-path.mjs';

const out = resolve(process.argv[2] || '/home/dev/screenshots');
mkdirSync(out, { recursive: true });

const CHROMIUM = chromiumPath();
const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:7070';

const CONFIG = {
  enabled: true,
  defaultAssistant: 'claude',
  workdir: '/home/dev',
  readOnly: false,
  assistants: [
    { id: 'claude', label: 'Claude', default: true, model: 'opus-4.8' },
    { id: 'ante', label: 'Ante' },
  ],
  commands: [
    { name: 'kc-issue', kind: 'skill', description: 'Spin up an isolated agent to work a kube-coder issue', argument_hint: '<issue #>', scope: 'user' },
    { name: 'kc-preflight', kind: 'skill', description: 'Run the full CI suite locally before pushing', argument_hint: '', scope: 'user' },
    { name: 'kc-ship-pr', kind: 'skill', description: 'Commit local changes and open a pull request', argument_hint: '', scope: 'user' },
    { name: 'kc-screenshot', kind: 'skill', description: 'Capture desktop + mobile screenshots of the dashboard', argument_hint: '[output dir]', scope: 'user' },
    { name: 'deploy', kind: 'command', description: 'Ship the current branch to staging', argument_hint: '[env]', scope: 'project' },
  ],
};

async function mockHv(page) {
  await page.route('**/api/hypervisor/config', (r) =>
    r.fulfill({ contentType: 'application/json', body: JSON.stringify(CONFIG) }),
  );
  await page.route('**/api/hypervisor/threads', (r) =>
    r.fulfill({ contentType: 'application/json', body: JSON.stringify({ threads: [] }) }),
  );
  await page.route('**/api/hypervisor/threads/**', (r) =>
    r.fulfill({ contentType: 'application/json', body: JSON.stringify({ threads: [] }) }),
  );
}

async function drive(page, theme) {
  await page.goto(`${BASE}/`, { waitUntil: 'networkidle', timeout: 15000 });
  await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme);
  await page.evaluate(() => {
    window.history.pushState({}, '', '/hypervisor');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  await page.waitForSelector('.hv-composer-input', { timeout: 5000 });
  await page.click('.hv-composer-input');
  await page.type('.hv-composer-input', '/kc');
  await page.waitForSelector('.hv-slash-menu', { timeout: 3000 });
  await page.waitForTimeout(300);
}

const browser = await chromium.launch({ executablePath: CHROMIUM, headless: true });
try {
  // Desktop, dark.
  {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, colorScheme: 'dark' });
    await ctx.addInitScript(() => {
      localStorage.setItem('kc.onboardingDone', 'true');
      localStorage.setItem('kc.theme', 'dark');
    });
    const page = await ctx.newPage();
    await mockHv(page);
    await drive(page, 'dark');
    await page.screenshot({ path: `${out}/slash-desktop-dark.png` });
    console.log('✓ slash-desktop-dark.png');
    await ctx.close();
  }
  // Mobile, light.
  {
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, colorScheme: 'light' });
    await ctx.addInitScript(() => {
      localStorage.setItem('kc.onboardingDone', 'true');
      localStorage.setItem('kc.theme', 'light');
    });
    const page = await ctx.newPage();
    await mockHv(page);
    await drive(page, 'light');
    await page.screenshot({ path: `${out}/slash-mobile-light.png` });
    console.log('✓ slash-mobile-light.png');
    await ctx.close();
  }
} finally {
  await browser.close();
}
console.log(`\nSaved to ${out}`);
