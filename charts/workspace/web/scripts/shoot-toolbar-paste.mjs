#!/usr/bin/env node
/**
 * Screenshot the toolbar "Paste into message box" action handling a clipboard
 * IMAGE (issue #179 follow-up): copy a screenshot → task menu → Paste → the
 * image lands as an upload chip in the Send-message composer.
 *
 * Runs against the real dev_server backend (python3 dev_server.py 7070). We
 * seed the browser clipboard with a PNG (permissions granted), open the task
 * menu, click Paste, and confirm a ready chip appears. It never clicks Send,
 * so the live tmux session is untouched.
 *
 * Usage: KC_CHROMIUM=/path/to/chrome node scripts/shoot-toolbar-paste.mjs <task_id> [out-dir]
 */
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';

const TASK_ID = process.argv[2] || process.env.KC_TASK_ID;
const out = resolve(process.argv[3] || '/home/dev/kube-coder/docs/screenshots');
mkdirSync(out, { recursive: true });
if (!TASK_ID) { console.error('need a task id'); process.exit(1); }

const CHROMIUM = process.env.KC_CHROMIUM || '/home/ubuntu/.cache/ms-playwright/chromium-1224/chrome-linux64/chrome';
const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:7070';
const PNG_DATA_URL =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAEklEQVR4nGP8z8Dwn4EIwDiqEAQAGAsE9wEK3wAAAABJRU5ErkJggg==';

const browser = await chromium.launch({ executablePath: CHROMIUM, headless: true });
try {
  for (const theme of ['dark', 'light']) {
    const ctx = await browser.newContext({
      viewport: { width: 1180, height: 820 },
      deviceScaleFactor: 2,
      colorScheme: theme,
    });
    // Headless Chromium rejects a synthetic image on the real OS clipboard, so
    // stub navigator.clipboard.read() to return our PNG — this drives the exact
    // readClipboard() path the toolbar button uses.
    await ctx.addInitScript((dataUrl) => {
      try { localStorage.setItem('kc.onboardingDone', 'true'); } catch { /* noop */ }
      const read = async () => {
        const blob = await (await fetch(dataUrl)).blob();
        return [{ types: ['image/png'], getType: async () => blob }];
      };
      Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        get: () => ({ read, readText: async () => '' }),
      });
    }, PNG_DATA_URL);
    const page = await ctx.newPage();
    await page.route('**/prepare-terminal', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' }));
    await page.route('**/terminal/**', (r) =>
      r.fulfill({ status: 200, contentType: 'text/html', body: '<html><body></body></html>' }));

    await page.goto(`${BASE}/next/tasks/${TASK_ID}`, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme);
    await page.getByText('Send message', { exact: true }).first().click({ timeout: 10000 });
    await page.waitForSelector('.mc-composer', { timeout: 10000 });

    await page.getByLabel('Open task menu').click();
    await page.getByText('Paste into message box', { exact: true }).click();
    await page.waitForSelector('.mc-chip--ready', { timeout: 10000 });
    await page.waitForTimeout(300);

    const file = `toolbar-paste-image-${theme}.png`;
    await page.screenshot({ path: `${out}/${file}`, fullPage: false });
    console.log(`✓ ${file}`);
    await ctx.close();
  }
} finally {
  await browser.close();
}
