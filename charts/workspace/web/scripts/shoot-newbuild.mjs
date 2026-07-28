#!/usr/bin/env node
/**
 * Screenshots for the New Build seed prompt + saved prompt templates (#94)
 * and the consistent "Chat" nav label (#346).
 *
 * Usage:  SHOT_BASE=http://127.0.0.1:7094 node scripts/shoot-newbuild.mjs [out]
 */
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromiumPath } from './chromium-path.mjs';

const out = resolve(process.argv[2] || '/home/dev/screenshots/newbuild-94');
mkdirSync(out, { recursive: true });

const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:7070';
const browser = await chromium.launch({ executablePath: chromiumPath(), headless: true });

// Pre-seeded templates so the chips are visible without driving the save flow
// in every shot (the save flow gets its own capture below).
const TEMPLATES = JSON.stringify([
  { id: 'tpl-a', name: 'Fix failing tests', prompt: 'Run the test suite in ./api, fix whatever fails, and open a PR.', created_at: 1 },
  { id: 'tpl-b', name: 'Review my branch', prompt: 'Review the diff on my current branch and list anything risky.', created_at: 2 },
  { id: 'tpl-c', name: 'Bump deps', prompt: 'Update dependencies to the latest minor versions and run the tests.', created_at: 3 },
]);

async function shoot({ name, viewport, theme, path = '/tasks', action, storage = {} }) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 2, colorScheme: theme });
  const page = await ctx.newPage();
  await page.addInitScript((items) => {
    localStorage.setItem('kc.onboardingDone', 'true');
    for (const [k, v] of Object.entries(items)) localStorage.setItem(k, v);
  }, storage);
  // Not networkidle — the routes poll /api endpoints continuously.
  await page.goto(`${BASE}${path}`, { waitUntil: 'domcontentloaded', timeout: 15000 });
  await page.waitForSelector('.rail', { state: 'attached', timeout: 8000 });
  await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme);
  await page.waitForTimeout(300);
  if (action) await action(page);
  await page.waitForTimeout(350);
  await page.screenshot({ path: `${out}/${name}.png`, fullPage: false });
  console.log(`✓ ${name}`);
  await ctx.close();
}

const desktop = { width: 1280, height: 800 };
const mobile = { width: 390, height: 844 };

/** Open the New Build drawer (desktop) / sheet (mobile). */
async function openNewBuild(p) {
  // /tasks restores the last-open build on mount, which on mobile replaces the
  // list (and its New build button) with the full-screen detail. Tapping
  // "Builds" in the bottom nav clears the selection and brings the list back.
  // (The bar stays in the DOM on desktop — display:none — so test visibility.)
  const bnBuilds = p.locator('.bottomnav button:has-text("Builds")');
  if (await bnBuilds.isVisible()) {
    await bnBuilds.click();
    await p.waitForTimeout(300);
  }
  await p.click('button:has-text("New build")');
  await p.waitForSelector('.ntf', { timeout: 5000 });
}

try {
  for (const theme of ['dark', 'light']) {
    // #94 — the prompt field + saved template chips.
    await shoot({
      name: `newbuild-prompt-templates-desktop-${theme}`,
      viewport: desktop,
      theme,
      storage: { 'kc.prompt.templates.v1': TEMPLATES },
      action: async (p) => {
        await openNewBuild(p);
        await p.fill('.ntf-textarea', 'Run the test suite in ./api, fix whatever fails, and open a PR.');
      },
    });
    await shoot({
      name: `newbuild-prompt-templates-mobile-${theme}`,
      viewport: mobile,
      theme,
      storage: { 'kc.prompt.templates.v1': TEMPLATES },
      action: async (p) => {
        await openNewBuild(p);
        await p.fill('.ntf-textarea', 'Run the test suite in ./api, fix whatever fails, and open a PR.');
      },
    });

    // #346 — "Chat" in the rail (desktop) and the bottom nav (mobile).
    await shoot({ name: `nav-chat-label-desktop-${theme}`, viewport: desktop, theme, path: '/hypervisor' });
    await shoot({ name: `nav-chat-label-mobile-${theme}`, viewport: mobile, theme, path: '/hypervisor' });
  }

  // The save-as-template flow, mid-naming.
  await shoot({
    name: 'newbuild-save-template-desktop-dark',
    viewport: desktop,
    theme: 'dark',
    action: async (p) => {
      await openNewBuild(p);
      await p.fill('.ntf-textarea', 'Review the diff on my current branch and list anything risky.');
      await p.click('.ntf-tpl-save');
      await p.waitForSelector('.ntf-tpl-namerow', { timeout: 4000 });
    },
  });

  // Empty state — no templates saved yet.
  await shoot({
    name: 'newbuild-empty-templates-desktop-dark',
    viewport: desktop,
    theme: 'dark',
    action: openNewBuild,
  });

  // The "Chat vs Builds" doc page (#346). Needs the dev server started with
  // DOCS_DIR pointing at this checkout's docs/ so the new page is served.
  for (const theme of ['dark', 'light']) {
    await shoot({
      name: `docs-chat-vs-builds-desktop-${theme}`,
      viewport: desktop,
      theme,
      path: '/docs/chat-vs-builds',
      action: async (p) => p.waitForSelector('.docs-article-wrap', { timeout: 6000 }),
    });
  }
} finally {
  await browser.close();
}
