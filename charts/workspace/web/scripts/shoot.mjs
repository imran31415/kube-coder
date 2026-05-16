#!/usr/bin/env node
/**
 * Screenshot a list of URLs at multiple viewport sizes using the playwright
 * Chromium downloaded for the MCP server.
 *
 * Usage:  node scripts/shoot.mjs [output-dir]
 */
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';

const out = resolve(process.argv[2] || '/home/dev/screenshots');
mkdirSync(out, { recursive: true });

const CHROMIUM = '/home/ubuntu/.cache/ms-playwright/chromium-1224/chrome-linux64/chrome';
const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:7070';

async function shootRoute(page, url, file, opts = {}) {
  await page.goto(url, { waitUntil: 'networkidle', timeout: 10000 });
  await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), opts.theme ?? 'dark');
  if (opts.waitFor) {
    try { await page.waitForSelector(opts.waitFor, { timeout: 4000 }); }
    catch { /* keep going */ }
  }
  if (opts.action) await opts.action(page);
  await page.waitForTimeout(opts.settle ?? 250);
  await page.screenshot({ path: `${out}/${file}`, fullPage: false });
  console.log(`✓ ${file}`);
}

const phase1Routes = [
  { path: '/next/',         slug: 'shell-tasks' },
  { path: '/next/memory',   slug: 'shell-memory' },
  { path: '/next/triggers', slug: 'shell-triggers' },
  { path: '/next/files',    slug: 'shell-files' },
  { path: '/next/settings', slug: 'shell-settings' },
];

const viewports = [
  { name: 'desktop', width: 1280, height: 800,  theme: 'dark'  },
  { name: 'desktop', width: 1280, height: 800,  theme: 'light' },
  { name: 'mobile',  width: 390,  height: 844,  theme: 'dark'  },
  { name: 'mobile',  width: 390,  height: 844,  theme: 'light' },
];

const browser = await chromium.launch({ executablePath: CHROMIUM, headless: true });

try {
  // Phase 1 shell screenshots
  for (const vp of viewports) {
    const ctx = await browser.newContext({
      viewport: { width: vp.width, height: vp.height },
      deviceScaleFactor: 2,
      colorScheme: vp.theme,
    });
    const page = await ctx.newPage();
    for (const r of phase1Routes) {
      try {
        await shootRoute(page, `${BASE}${r.path}`, `phase1-${r.slug}-${vp.name}-${vp.theme}.png`, { theme: vp.theme });
      } catch (err) {
        console.error(`✗ ${r.slug}: ${err.message}`);
      }
    }
    if (vp.name === 'desktop' && vp.theme === 'dark') {
      try {
        await shootRoute(page, `${BASE}/next/`, `phase1-palette-open-desktop-dark.png`, {
          theme: 'dark',
          action: async (p) => { await p.keyboard.press('Control+K'); },
          waitFor: '.palette',
        });
      } catch (err) {
        console.error('✗ palette:', err.message);
      }
    }
    await ctx.close();
  }

  // Phases 3–5 screenshots: memory, triggers, files
  const dataRoutes = [
    { path: '/next/memory',    slug: 'memory-list' },
    { path: '/next/triggers',  slug: 'triggers-list' },
    { path: '/next/files',     slug: 'files-list' },
    { path: '/next/settings',  slug: 'settings-full' },
  ];
  for (const vp of viewports) {
    const ctx = await browser.newContext({
      viewport: { width: vp.width, height: vp.height },
      deviceScaleFactor: 2,
      colorScheme: vp.theme,
    });
    const page = await ctx.newPage();
    for (const r of dataRoutes) {
      try {
        await shootRoute(page, `${BASE}${r.path}`, `phase35-${r.slug}-${vp.name}-${vp.theme}.png`, {
          theme: vp.theme,
          settle: 500,
        });
      } catch (err) {
        console.error(`✗ ${r.slug} ${vp.name} ${vp.theme}: ${err.message}`);
      }
    }
    await ctx.close();
  }

  // Phase 2 task screenshots: list and detail. Light + dark + mobile.
  for (const vp of viewports) {
    const ctx = await browser.newContext({
      viewport: { width: vp.width, height: vp.height },
      deviceScaleFactor: 2,
      colorScheme: vp.theme,
    });
    const page = await ctx.newPage();

    // Plain task list
    await shootRoute(page, `${BASE}/next/tasks`, `phase2-tasks-list-${vp.name}-${vp.theme}.png`, {
      theme: vp.theme,
      waitFor: '.tl-row',
      settle: 400,
    });

    // Pick the first task row and open the detail
    try {
      await page.goto(`${BASE}/next/tasks`, { waitUntil: 'networkidle' });
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), vp.theme);
      await page.waitForSelector('.tl-row', { timeout: 4000 });
      await page.click('.tl-row:first-child');
      await page.waitForTimeout(800);
      await page.screenshot({ path: `${out}/phase2-tasks-detail-${vp.name}-${vp.theme}.png` });
      console.log(`✓ phase2-tasks-detail-${vp.name}-${vp.theme}.png`);
    } catch (err) {
      console.error(`✗ tasks detail ${vp.name}: ${err.message}`);
    }

    // New-task form (desktop drawer / mobile sheet)
    try {
      await page.goto(`${BASE}/next/tasks`, { waitUntil: 'networkidle' });
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), vp.theme);
      await page.waitForTimeout(400);
      await page.click('button:has-text("New task")');
      await page.waitForSelector('.ntf', { timeout: 4000 });
      await page.waitForTimeout(300);
      await page.screenshot({ path: `${out}/phase2-new-task-${vp.name}-${vp.theme}.png` });
      console.log(`✓ phase2-new-task-${vp.name}-${vp.theme}.png`);
    } catch (err) {
      console.error(`✗ new task ${vp.name}: ${err.message}`);
    }
    await ctx.close();
  }
  // Phase 6 cutover: confirm SPA renders at / (no /next prefix) and
  // capture Onboarding, Subagents tab, Memory history/relations.
  for (const vp of viewports) {
    if (vp.theme !== 'dark') continue; // dark only for these
    const ctx = await browser.newContext({
      viewport: { width: vp.width, height: vp.height },
      deviceScaleFactor: 2,
      colorScheme: vp.theme,
    });
    const page = await ctx.newPage();

    // Cutover root.
    try {
      await page.goto(`${BASE}/`, { waitUntil: 'networkidle', timeout: 10000 });
      await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
      await page.evaluate(() => localStorage.setItem('kc.onboardingDone', 'true'));
      await page.reload({ waitUntil: 'networkidle' });
      await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
      await page.waitForSelector('.tl-row', { timeout: 4000 });
      await page.waitForTimeout(300);
      await page.screenshot({ path: `${out}/phase6-root-${vp.name}-dark.png` });
      console.log(`✓ phase6-root-${vp.name}-dark.png`);
    } catch (err) {
      console.error(`✗ root cutover ${vp.name}: ${err.message}`);
    }

    // Subagents tab inside task detail (desktop only).
    if (vp.name === 'desktop') {
      try {
        await page.goto(`${BASE}/tasks`, { waitUntil: 'networkidle' });
        await page.evaluate(() => {
          document.documentElement.setAttribute('data-theme', 'dark');
          localStorage.setItem('kc.onboardingDone', 'true');
        });
        await page.waitForSelector('.tl-row', { timeout: 4000 });
        await page.click('.tl-row:first-child');
        await page.waitForTimeout(500);
        await page.click('button[role="tab"]:has-text("Subagents")');
        await page.waitForTimeout(800);
        await page.screenshot({ path: `${out}/phase6-task-subagents-${vp.name}-dark.png` });
        console.log(`✓ phase6-task-subagents-${vp.name}-dark.png`);
      } catch (err) {
        console.error(`✗ subagents ${vp.name}: ${err.message}`);
      }
    }

    // Memory detail with relations/history tabs (desktop only).
    if (vp.name === 'desktop') {
      try {
        await page.goto(`${BASE}/memory`, { waitUntil: 'networkidle' });
        await page.evaluate(() => {
          document.documentElement.setAttribute('data-theme', 'dark');
          localStorage.setItem('kc.onboardingDone', 'true');
        });
        await page.waitForSelector('.mem-row', { timeout: 4000 });
        await page.click('.mem-row:first-child');
        await page.waitForTimeout(500);
        await page.click('button[role="tab"]:has-text("History")');
        await page.waitForTimeout(800);
        await page.screenshot({ path: `${out}/phase6-memory-history-${vp.name}-dark.png` });
        console.log(`✓ phase6-memory-history-${vp.name}-dark.png`);
      } catch (err) {
        console.error(`✗ memory history ${vp.name}: ${err.message}`);
      }
    }

    // Onboarding (cleared localStorage to force it).
    try {
      const onboardCtx = await browser.newContext({
        viewport: { width: vp.width, height: vp.height },
        deviceScaleFactor: 2,
        colorScheme: 'dark',
      });
      const op = await onboardCtx.newPage();
      await op.goto(`${BASE}/`, { waitUntil: 'networkidle' });
      await op.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
      await op.waitForSelector('.ob', { timeout: 4000 });
      await op.waitForTimeout(400);
      await op.screenshot({ path: `${out}/phase6-onboarding-${vp.name}-dark.png` });
      console.log(`✓ phase6-onboarding-${vp.name}-dark.png`);
      await onboardCtx.close();
    } catch (err) {
      console.error(`✗ onboarding ${vp.name}: ${err.message}`);
    }

    await ctx.close();
  }
} finally {
  await browser.close();
}
console.log(`\nSaved screenshots to: ${out}`);
