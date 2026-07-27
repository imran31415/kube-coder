#!/usr/bin/env node
/**
 * Screenshots for issue #494 — the onboarding "Connect Claude" step and the AI
 * CTO first-win connect gate, both shown to a brand-new keyless user.
 *
 * This pod has a real Claude subscription login, so /api/subscriptions reports
 * claude_ready:true. We intercept it and force claude_ready:false to reproduce
 * the fresh-tester state these surfaces exist for.
 *
 * Usage: node scripts/shoot-onboarding.mjs [output-dir]
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

// Force the keyless-new-user state on the subscriptions endpoint.
const NO_CLAUDE = JSON.stringify({
  subscriptions: { claude: { logged_in: false }, codex: { logged_in: false } },
  claude_ready: false,
});

async function withKeylessRoute(ctx) {
  await ctx.route('**/api/subscriptions', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: NO_CLAUDE }));
}

// The CTO connect gate only renders when there's no active thread. The dev
// harness may have seeded threads that auto-open — stub the list empty so the
// welcome (and its gate) stays put for the shot.
async function withNoThreads(ctx) {
  await ctx.route('**/api/hypervisor/threads**', (route) => {
    if (route.request().method() !== 'GET') return route.continue();
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ threads: [] }) });
  });
}

const browser = await chromium.launch({ executablePath: CHROMIUM, headless: true });

try {
  for (const vp of viewports) {
    // --- Onboarding "Connect Claude" step ---
    {
      const ctx = await browser.newContext({
        viewport: { width: vp.width, height: vp.height },
        deviceScaleFactor: 2,
        colorScheme: vp.theme,
      });
      await withKeylessRoute(ctx);
      const page = await ctx.newPage();
      try {
        await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 15000 });
        await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), vp.theme);
        await page.waitForSelector('.ob', { timeout: 8000 });
        // Welcome → step through git / github / ssh via the footer "Skip".
        await page.getByRole('button', { name: 'Get started' }).click();
        for (let i = 0; i < 3; i++) {
          await page.locator('.ob-footer button', { hasText: /^Skip$/ }).first().click();
          await page.waitForTimeout(150);
        }
        await page.waitForSelector('.ccs', { timeout: 6000 });
        await page.waitForTimeout(400);
        await page.screenshot({ path: `${out}/onboarding-claude-${vp.name}-${vp.theme}.png` });
        console.log(`✓ onboarding-claude-${vp.name}-${vp.theme}.png`);
      } catch (err) {
        console.error(`✗ onboarding ${vp.name} ${vp.theme}: ${err.message}`);
      }
      await ctx.close();
    }

    // --- AI CTO first-win connect gate ---
    {
      const ctx = await browser.newContext({
        viewport: { width: vp.width, height: vp.height },
        deviceScaleFactor: 2,
        colorScheme: vp.theme,
      });
      await withKeylessRoute(ctx);
      await withNoThreads(ctx);
      const page = await ctx.newPage();
      try {
        // The dev server 404s deep-links — load the SPA at / and client-route to
        // /cto via the nav (under "More" on mobile).
        await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 15000 });
        await page.evaluate(() => localStorage.setItem('kc.onboardingDone', 'true'));
        await page.reload({ waitUntil: 'domcontentloaded' });
        await page.waitForTimeout(1200);
        if (vp.name === 'mobile') {
          // Mobile: /cto lives under the bottom-nav "More" page.
          await page.click('.bn-item[aria-label="More"]', { timeout: 4000 });
          await page.waitForTimeout(400);
          await page.locator('.more-item', { hasText: 'AI CTO' }).first().click({ timeout: 4000 });
        } else {
          await page.locator('.rail-item', { hasText: 'AI CTO' }).first().click({ timeout: 4000 });
        }
        await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), vp.theme);
        await page.waitForSelector('.cto-connect', { timeout: 8000 });
        await page.waitForTimeout(400);
        await page.screenshot({ path: `${out}/cto-connect-gate-${vp.name}-${vp.theme}.png` });
        console.log(`✓ cto-connect-gate-${vp.name}-${vp.theme}.png`);
      } catch (err) {
        console.error(`✗ cto gate ${vp.name} ${vp.theme}: ${err.message}`);
      }
      await ctx.close();
    }
  }
} finally {
  await browser.close();
}
console.log(`\nSaved screenshots to: ${out}`);
