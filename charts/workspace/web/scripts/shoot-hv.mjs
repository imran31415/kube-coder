#!/usr/bin/env node
/**
 * Focused screenshots of the Hypervisor chat on mobile — verifies the notch
 * clearance, the labeled "Chats" trigger + count, and the slide-over past-chats
 * panel with its close affordance. Mocks the /api/hypervisor facade and injects
 * a simulated safe-area inset so a headless (non-notched) Chromium still shows
 * the iOS notch treatment.
 *
 * Usage: node scripts/shoot-hv.mjs [output-dir]
 */
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';

const out = resolve(process.argv[2] || '/home/dev/screenshots');
mkdirSync(out, { recursive: true });

const CHROMIUM = '/home/dev/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome';
const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:7070';

const now = 1_752_300_000; // fixed epoch (script must be deterministic)
const CONFIG = {
  enabled: true,
  defaultAssistant: 'claude',
  workdir: '/home/dev',
  readOnly: false,
  assistants: [
    { id: 'claude', label: 'Claude', default: true, model: 'opus-4.8' },
    { id: 'ante', label: 'Ante' },
  ],
};
const THREADS = [
  { id: 't1', title: 'Fix the iOS notch on the topbar', assistant: 'claude', status: 'idle', created_at: now - 9000, updated_at: now - 600 },
  { id: 't2', title: 'Spin up a task to run the tests', assistant: 'claude', status: 'running', created_at: now - 40000, updated_at: now - 5400 },
  { id: 't3', title: 'What is running and how much CPU?', assistant: 'ante', status: 'idle', created_at: now - 90000, updated_at: now - 88000 },
];

async function mockHv(page) {
  await page.route('**/api/hypervisor/config', (r) =>
    r.fulfill({ contentType: 'application/json', body: JSON.stringify(CONFIG) }),
  );
  await page.route('**/api/hypervisor/threads', (r) =>
    r.fulfill({ contentType: 'application/json', body: JSON.stringify({ threads: THREADS }) }),
  );
  await page.route('**/api/hypervisor/threads/**', (r) =>
    r.fulfill({ contentType: 'application/json', body: JSON.stringify({ thread: THREADS[0], events: [] }) }),
  );
}

// Simulate a notched device: env(safe-area-inset-*) is 0 in headless Chromium,
// so override the tokens the layout reads.
const SAFE_AREA = ':root{--safe-top:47px!important;--safe-bottom:34px!important;}';

const browser = await chromium.launch({ executablePath: CHROMIUM, headless: true });
try {
  const ctx = await browser.newContext({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 2,
    colorScheme: 'dark',
  });
  // Dismiss onboarding + pin theme before any app code runs.
  await ctx.addInitScript(() => {
    localStorage.setItem('kc.onboardingDone', 'true');
    localStorage.setItem('kc.theme', 'dark');
  });
  const page = await ctx.newPage();
  await mockHv(page);

  // The dev server 404s on deep links, so load the SPA root then drive the
  // history-API router client-side to /hypervisor.
  await page.goto(`${BASE}/`, { waitUntil: 'networkidle', timeout: 15000 });
  await page.evaluate(() => {
    document.documentElement.setAttribute('data-theme', 'dark');
  });
  await page.evaluate(() => {
    window.history.pushState({}, '', '/hypervisor');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  await page.addStyleTag({ content: SAFE_AREA });
  await page.waitForSelector('.hv-topbar', { timeout: 5000 });
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${out}/hv-mobile-empty.png` });
  console.log('✓ hv-mobile-empty.png');

  // Open the past-chats slide-over via the new labeled trigger.
  await page.click('.hv-topbar-menu');
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${out}/hv-mobile-chats.png` });
  console.log('✓ hv-mobile-chats.png');

  await ctx.close();
} finally {
  await browser.close();
}
console.log(`\nSaved to ${out}`);
