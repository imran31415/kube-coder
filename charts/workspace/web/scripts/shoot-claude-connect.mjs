#!/usr/bin/env node
/**
 * Screenshot the "Connect Claude account" flow in Settings.
 *
 * The connect UI only renders when the Claude subscription is absent, so
 * /api/subscriptions (and the login/start endpoint) are mocked via route
 * interception — everything else hits the real dev server.
 *
 * Usage:  node scripts/shoot-claude-connect.mjs [output-dir]
 */
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromiumPath } from './chromium-path.mjs';

const out = resolve(process.argv[2] || '/home/dev/screenshots');
mkdirSync(out, { recursive: true });

const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:7070';
const OAUTH_URL =
  'https://claude.com/cai/oauth/authorize?code=true&client_id=example&response_type=code'
  + '&scope=user%3Ainference&code_challenge=demo&code_challenge_method=S256&state=demo';

const shots = [
  { name: 'desktop', width: 1280, height: 800, theme: 'dark' },
  { name: 'desktop', width: 1280, height: 800, theme: 'light' },
  { name: 'mobile', width: 390, height: 844, theme: 'dark' },
];

const browser = await chromium.launch({ executablePath: chromiumPath(), headless: true });

try {
  for (const vp of shots) {
    const ctx = await browser.newContext({
      viewport: { width: vp.width, height: vp.height },
      deviceScaleFactor: 2,
      colorScheme: vp.theme,
    });
    const page = await ctx.newPage();
    await page.addInitScript(() => localStorage.setItem('kc.onboardingDone', 'true'));

    await page.route('**/api/subscriptions', (route) => route.fulfill({
      json: {
        subscriptions: {
          claude: { logged_in: false },
          codex: { logged_in: false, available: false },
        },
      },
    }));
    await page.route('**/api/subscriptions/claude/login/start', (route) => route.fulfill({
      json: { url: OAUTH_URL, in_progress: true },
    }));

    // domcontentloaded, not networkidle — the shell keeps an SSE stream open.
    await page.goto(`${BASE}/next/settings`, { waitUntil: 'domcontentloaded', timeout: 15000 });
    await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), vp.theme);

    // State 1: the subscription row offering the Connect button.
    const btn = page.getByText('Connect Claude account');
    await btn.scrollIntoViewIfNeeded();
    await page.waitForTimeout(300);
    await page.screenshot({ path: `${out}/claude-connect-idle-${vp.name}-${vp.theme}.png` });
    console.log(`✓ claude-connect-idle-${vp.name}-${vp.theme}.png`);

    // State 2: after clicking Connect — sign-in link + paste-code input.
    await btn.click();
    const codeInput = page.getByPlaceholder('Paste authorization code');
    await codeInput.waitFor({ timeout: 5000 });
    await codeInput.scrollIntoViewIfNeeded();
    await page.mouse.wheel(0, 120); // breathing room below the paste-code row
    await page.waitForTimeout(300);
    await page.screenshot({ path: `${out}/claude-connect-awaiting-${vp.name}-${vp.theme}.png` });
    console.log(`✓ claude-connect-awaiting-${vp.name}-${vp.theme}.png`);

    await ctx.close();
  }
} finally {
  await browser.close();
}
