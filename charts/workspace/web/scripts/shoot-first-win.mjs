#!/usr/bin/env node
/**
 * Screenshots for issue #500 — the AI CTO first-win landing, the single most
 * important screen a new user sees.
 *
 * This is the state that can't be faked with a deep-link: `justOnboarded` is an
 * in-memory signal set by the onboarding wizard's last step, so the only way to
 * render the build-first opener + build chips is to actually walk the wizard to
 * the end and let it route into /cto. We do exactly that, with a brand-new
 * workspace mocked underneath (no projects, no threads, Claude connected).
 *
 * Captures, per viewport × theme:
 *   firstwin-cto-<vp>-<theme>.png   opener + chips + composer as one hero
 *
 * Usage: node scripts/shoot-first-win.mjs [output-dir]
 */
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromiumPath } from './chromium-path.mjs';

const out = resolve(process.argv[2] || '/home/dev/screenshots');
mkdirSync(out, { recursive: true });

const CHROMIUM = chromiumPath();
const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:7070';

const viewports = [
  { name: 'desktop', width: 1280, height: 800, theme: 'dark' },
  { name: 'desktop', width: 1280, height: 800, theme: 'light' },
  { name: 'mobile', width: 390, height: 844, theme: 'dark' },
  { name: 'mobile', width: 390, height: 844, theme: 'light' },
];

const CONFIG = {
  enabled: true,
  assistants: [{ id: 'claude', label: 'Claude', model: 'opus-4.8', models: ['opus-4.8'] }],
  defaultAssistant: 'claude',
  workdir: '/home/dev',
  readOnly: false,
};

/** A brand-new workspace: Claude connected, nothing built yet. */
async function mockFreshWorkspace(ctx) {
  const json = (r, body) => r.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
  await ctx.route('**/api/subscriptions', (r) =>
    json(r, { subscriptions: { claude: { logged_in: true } }, claude_ready: true }));
  await ctx.route('**/api/projects/_discover', (r) => json(r, { candidates: [], registered: [] }));
  await ctx.route('**/api/projects', (r) => json(r, { projects: [] }));
  await ctx.route('**/api/hypervisor/config', (r) => json(r, CONFIG));
  await ctx.route('**/api/hypervisor/threads**', (r) => {
    if (r.request().method() !== 'GET') return r.continue();
    return json(r, { threads: [] });
  });
  await ctx.route('**/api/events', (r) =>
    r.fulfill({ contentType: 'text/event-stream', body: 'event: ready\ndata: {}\n\n' }));
}

const browser = await chromium.launch({ executablePath: CHROMIUM, headless: true });
try {
  for (const vp of viewports) {
    const ctx = await browser.newContext({
      viewport: { width: vp.width, height: vp.height },
      deviceScaleFactor: 2,
      colorScheme: vp.theme,
    });
    await mockFreshWorkspace(ctx);
    const page = await ctx.newPage();
    try {
      await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 15000 });
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), vp.theme);
      await page.waitForSelector('.ob', { timeout: 8000 });
      // Welcome → skip git / github / ssh → Continue past Claude (connected)
      // → "Meet your AI CTO", which is what sets the first-win signal.
      await page.getByRole('button', { name: 'Get started' }).click();
      for (let i = 0; i < 3; i++) {
        await page.locator('.ob-footer button', { hasText: /^Skip$/ }).first().click();
        await page.waitForTimeout(150);
      }
      await page.locator('.ob-footer button', { hasText: /^Continue$/ }).first().click();
      await page.locator('.ob-footer button', { hasText: /Meet your AI CTO/ }).first().click();

      await page.waitForSelector('.route-cto .cto-welcome', { timeout: 10000 });
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), vp.theme);
      await page.waitForTimeout(700);
      await page.screenshot({ path: `${out}/firstwin-cto-${vp.name}-${vp.theme}.png` });
      console.log(`✓ firstwin-cto-${vp.name}-${vp.theme}.png`);
    } catch (err) {
      console.error(`✗ firstwin ${vp.name} ${vp.theme}: ${err.message}`);
    }
    await ctx.close();
  }
} finally {
  await browser.close();
}
console.log(`\nSaved screenshots to: ${out}`);
