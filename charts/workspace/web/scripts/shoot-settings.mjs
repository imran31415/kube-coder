#!/usr/bin/env node
/**
 * Screenshot the grouped settings sub-pages (issue #439).
 *
 * Usage:  SHOT_BASE=http://127.0.0.1:3001 node scripts/shoot-settings.mjs [output-dir]
 */
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromiumPath } from './chromium-path.mjs';

const out = resolve(process.argv[2] || '/home/dev/screenshots');
mkdirSync(out, { recursive: true });

const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:7070';

const ROUTES = [
  { path: '/next/settings', slug: 'settings-general' },
  { path: '/next/settings/account', slug: 'settings-account' },
  { path: '/next/settings/providers', slug: 'settings-providers' },
  { path: '/next/settings/integrations', slug: 'settings-integrations' },
  { path: '/next/settings/workspace', slug: 'settings-workspace' },
];

const VIEWPORTS = [
  { name: 'desktop', width: 1280, height: 800, theme: 'dark' },
  { name: 'desktop', width: 1280, height: 800, theme: 'light' },
  { name: 'mobile', width: 390, height: 844, theme: 'dark' },
];

const browser = await chromium.launch({ executablePath: chromiumPath(), headless: true });
try {
  for (const vp of VIEWPORTS) {
    const ctx = await browser.newContext({
      viewport: { width: vp.width, height: vp.height },
      deviceScaleFactor: 2,
      colorScheme: vp.theme,
    });
    await ctx.addInitScript(() => localStorage.setItem('kc.onboardingDone', 'true'));
    const page = await ctx.newPage();
    for (const r of ROUTES) {
      // 'load', not 'networkidle' — MetricsSection polls forever on /workspace.
      await page.goto(`${BASE}${r.path}`, { waitUntil: 'load', timeout: 15000 });
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), vp.theme);
      await page.waitForTimeout(400);
      await page.screenshot({ path: `${out}/${r.slug}-${vp.name}-${vp.theme}.png`, fullPage: false });
      console.log(`✓ ${r.slug}-${vp.name}-${vp.theme}.png`);
    }
    await ctx.close();
  }
} finally {
  await browser.close();
}
