#!/usr/bin/env node
/**
 * Screenshot the Send-message composer with the issue #179 image-paste UI:
 * the attach-image button plus an uploaded thumbnail chip.
 *
 * Runs against the real dev_server backend (python3 dev_server.py 7070, which
 * bypasses auth) so the task detail renders normally. Points at a live running
 * task, switches to the Send-message tab, then drives the hidden file input to
 * upload a small PNG and produce a ready chip. It NEVER clicks Send, so the
 * live tmux session is untouched. The only side effect is a tiny PNG written to
 * the task's attachments dir by the real upload endpoint.
 *
 * Usage: KC_CHROMIUM=/path/to/chrome node scripts/shoot-msgchat.mjs [task_id] [output-dir]
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

// A tiny PNG handed to the file input to exercise the upload → chip flow.
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAEklEQVR4nGP8z8Dwn4EIwDiqEAQAGAsE9wEK3wAAAABJRU5ErkJggg==',
  'base64',
);

const browser = await chromium.launch({ executablePath: CHROMIUM, headless: true });
try {
  for (const theme of ['dark', 'light']) {
    const ctx = await browser.newContext({
      viewport: { width: 1180, height: 820 },
      deviceScaleFactor: 2,
      colorScheme: theme,
    });
    await ctx.addInitScript(() => {
      try { localStorage.setItem('kc.onboardingDone', 'true'); } catch { /* noop */ }
    });
    const page = await ctx.newPage();

    // Don't let the composer's TerminalPane re-point the real ttyd, and give the
    // ttyd iframe a blank body instead of a 404 so the shot stays clean.
    await page.route('**/prepare-terminal', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' }));
    await page.route('**/terminal/**', (r) =>
      r.fulfill({ status: 200, contentType: 'text/html', body: '<html><body></body></html>' }));

    await page.goto(`${BASE}/next/tasks/${TASK_ID}`, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme);

    await page.getByText('Send message', { exact: true }).first().click({ timeout: 10000 });
    await page.waitForSelector('.mc-composer', { timeout: 10000 });

    await page.fill('.mc-input', 'Here is the mockup — what do you think of the spacing?');
    await page.setInputFiles('.mc-file-input', { name: 'mockup.png', mimeType: 'image/png', buffer: PNG });
    await page.waitForSelector('.mc-chip--ready', { timeout: 10000 });
    await page.waitForTimeout(300);

    const file = `msgchat-image-paste-${theme}.png`;
    await page.screenshot({ path: `${out}/${file}`, fullPage: false });
    console.log(`✓ ${file}`);
    await ctx.close();
  }
} finally {
  await browser.close();
}
