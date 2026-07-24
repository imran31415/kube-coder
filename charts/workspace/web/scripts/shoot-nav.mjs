#!/usr/bin/env node
/**
 * Screenshots for the categorized side nav (#267): desktop rail expanded /
 * group-collapsed / icon-only, grouped palette, and the mobile More sheet.
 *
 * Usage:  SHOT_BASE=http://127.0.0.1:3012 node scripts/shoot-nav.mjs [output-dir]
 */
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromiumPath } from './chromium-path.mjs';

const out = resolve(process.argv[2] || '/home/dev/screenshots/nav-267');
mkdirSync(out, { recursive: true });

const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:7070';
const browser = await chromium.launch({ executablePath: chromiumPath(), headless: true });

async function shoot({ name, viewport, theme, path, action, storage = {} }) {
  const ctx = await browser.newContext({
    viewport,
    deviceScaleFactor: 2,
    colorScheme: theme,
  });
  const page = await ctx.newPage();
  await page.addInitScript((items) => {
    localStorage.setItem('kc.onboardingDone', 'true');
    for (const [k, v] of Object.entries(items)) localStorage.setItem(k, v);
  }, storage);
  // Not networkidle — routes poll /api endpoints continuously.
  await page.goto(`${BASE}${path}`, { waitUntil: 'domcontentloaded', timeout: 15000 });
  // 'attached', not visible — the rail is display:none on mobile viewports.
  await page.waitForSelector('.rail', { state: 'attached', timeout: 8000 });
  await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme);
  if (action) await action(page);
  await page.waitForTimeout(350);
  await page.screenshot({ path: `${out}/${name}.png`, fullPage: false });
  console.log(`✓ ${name}`);
  await ctx.close();
}

const desktop = { width: 1280, height: 800 };
const mobile = { width: 390, height: 844 };

try {
  for (const theme of ['dark', 'light']) {
    await shoot({ name: `rail-grouped-desktop-${theme}`, viewport: desktop, theme, path: '/desktop' });
    await shoot({
      name: `moresheet-grouped-mobile-${theme}`,
      viewport: mobile,
      theme,
      path: '/desktop',
      action: async (p) => {
        await p.click('.bottomnav button[aria-label="More"]');
        await p.waitForSelector('.more-section', { timeout: 4000 });
      },
    });
  }
  await shoot({
    name: 'rail-group-collapsed-desktop-dark',
    viewport: desktop,
    theme: 'dark',
    path: '/desktop',
    storage: { 'kc.rail.groups.v1': JSON.stringify(['knowledge']) },
  });
  await shoot({
    name: 'rail-icon-mode-desktop-dark',
    viewport: desktop,
    theme: 'dark',
    path: '/desktop',
    storage: {
      'kube-coder.ui': JSON.stringify({ theme: 'system', density: 'comfortable', railCollapsed: true, masterCollapsed: false }),
    },
  });
  await shoot({
    name: 'palette-grouped-desktop-dark',
    viewport: desktop,
    theme: 'dark',
    path: '/desktop',
    action: async (p) => {
      await p.keyboard.press('Control+K');
      await p.waitForSelector('.palette', { timeout: 4000 });
    },
  });
} finally {
  await browser.close();
}
