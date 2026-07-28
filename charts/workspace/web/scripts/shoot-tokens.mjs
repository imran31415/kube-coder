#!/usr/bin/env node
/**
 * Token-sweep visual QA: capture the surfaces most exposed to a change in
 * tokens.css, across desktop + mobile and dark + light.
 *
 * Usage:  node scripts/shoot-tokens.mjs <output-dir>
 */
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromiumPath } from './chromium-path.mjs';

const out = resolve(process.argv[2] || '/home/dev/screenshots/tokens');
mkdirSync(out, { recursive: true });

const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:7070';

const routes = [
  { path: '/next/cto', slug: 'cto' },
  { path: '/next/hypervisor', slug: 'chat' },
  { path: '/next/desktop', slug: 'desktop' },
  { path: '/next/settings', slug: 'settings' },
  { path: '/next/', slug: 'tasks' },
  { path: '/next/docs', slug: 'docs' },
];

const viewports = [
  { name: 'desktop', width: 1280, height: 800, theme: 'dark' },
  { name: 'desktop', width: 1280, height: 800, theme: 'light' },
  { name: 'mobile', width: 390, height: 844, theme: 'dark' },
  { name: 'mobile', width: 390, height: 844, theme: 'light' },
];

const browser = await chromium.launch({ executablePath: chromiumPath(), headless: true });

try {
  for (const vp of viewports) {
    const ctx = await browser.newContext({
      viewport: { width: vp.width, height: vp.height },
      deviceScaleFactor: 2,
      colorScheme: vp.theme,
    });
    // First-run overlay eats the page and would mask every surface below it.
    await ctx.addInitScript(() => localStorage.setItem('kc.onboardingDone', 'true'));
    const page = await ctx.newPage();
    for (const r of routes) {
      const file = `${r.slug}-${vp.name}-${vp.theme}.png`;
      try {
        // Not networkidle: CTO/Desktop/Tasks hold open polling + stream
        // connections, so the network never goes idle and goto() times out.
        await page.goto(`${BASE}${r.path}`, { waitUntil: 'domcontentloaded', timeout: 15000 });
        await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), vp.theme);
        // Freeze the pulse/bounce animations so before/after diffs compare
        // layout and type, not which frame the keyframes happened to be on.
        await page.addStyleTag({
          content: '*,*::before,*::after{animation:none!important;transition:none!important}',
        });
        await page.waitForTimeout(1800);
        await page.screenshot({ path: `${out}/${file}`, fullPage: false });
        console.log(`✓ ${file}`);
      } catch (e) {
        console.log(`✗ ${file}: ${e.message.split('\n')[0]}`);
      }
    }
    await ctx.close();
  }
} finally {
  await browser.close();
}
