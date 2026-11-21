#!/usr/bin/env node
/**
 * Screenshots for the side nav: the rail's two top-level entries (Chat +
 * Workspace) with Workspace collapsed on Chat and auto-revealed on one of its
 * routes, the icon-only rail, the narrow-screen bottom bar and its Workspace
 * sheet. (First written for the categorized rail, #267.)
 *
 * Every /api, /metrics and /health call is answered here with an empty
 * object, so no real workspace data reaches a screenshot; the server only has
 * to serve the built SPA (dist/), e.g.:
 *
 *   yarn build && npx vite preview --port 3113 --strictPort &
 *   SHOT_BASE=http://127.0.0.1:3113/next node scripts/shoot-nav.mjs [output-dir]
 *
 * Selectors tolerate the pre-Chat+Workspace rail too ("More" bottom slot), so
 * the same script shoots a before/after pair from two builds.
 */
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromiumPath } from './chromium-path.mjs';

const out = resolve(process.argv[2] || '/home/dev/screenshots/nav');
mkdirSync(out, { recursive: true });

const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:7070';
const browser = await chromium.launch({ executablePath: chromiumPath(), headless: true });

async function shoot({ name, viewport, theme, path, action, storage = {} }) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 2, colorScheme: theme });
  // Catch-all fixture: empty answers keep every surface in its empty state.
  // The shell's own polls get just enough shape not to throw (a render error
  // stops later signal updates — e.g. the sheet never opening). Later routes
  // win in Playwright, so the specific ones come after the catch-all.
  const json = (r, body) => r.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
  await ctx.route(/\/api\//, (r) => json(r, {}));
  await ctx.route(/\/(oauth\/)?metrics(\?|$)/, (r) => json(r, METRICS));
  await ctx.route(/\/(oauth\/)?health(\?|$)/, (r) => json(r, HEALTH));
  await ctx.route(/\/api\/events/, (r) => r.fulfill({ contentType: 'text/event-stream', body: 'event: ready\ndata: {}\n\n' }));
  await ctx.route(/\/api\/mode/, (r) => json(r, { readOnly: false, authed: true, authMode: 'oauth2', ctoEnabled: true, boardEnabled: true }));
  await ctx.route(/\/api\/claude\/tasks(\?|$)/, (r) => json(r, { tasks: [] }));
  await ctx.route(/\/api\/hypervisor\/threads(\?|$)/, (r) => json(r, { threads: [] }));
  await ctx.route(/\/api\/memory(\?|$)/, (r) => json(r, { memories: [] }));
  await ctx.route(/\/api\/workspace\/dirs/, (r) => json(r, { dirs: [] }));
  await ctx.route(/\/api\/projects(\?|$)/, (r) => json(r, { projects: [] }));
  await ctx.route(/\/api\/projects\/_discover/, (r) => json(r, { candidates: [], registered: [] }));
  await ctx.route(/\/api\/subscriptions/, (r) => json(r, { subscriptions: {}, claude_ready: true }));
  await ctx.route(/\/api\/missioncontrol\/queue/, (r) => json(r, { cards: [], pulse: { running: 0, waiting: 0, review: 0, done_today: 0 } }));
  const page = await ctx.newPage();
  page.on('pageerror', (e) => console.warn(`  [${name}] page error: ${e.message}`));
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
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${out}/${name}.png`, fullPage: false });
  console.log(`✓ ${name}`);
  await ctx.close();
}

const METRICS = {
  cpu: { usage_percent: 12, cores: 4 },
  memory: { total_mb: 16384, used_mb: 4100, available_mb: 12284, percent: 25 },
  disk: { total_gb: 100, used_gb: 30, available_gb: 70, percent: 30, path: '/home/dev' },
  alerts: [],
  timestamp: 0,
};
const HEALTH = {
  vscode: true, terminal: true, browser: true, status: 'healthy',
  services: { vscode: { status: 'up', port: 8080 }, terminal: { status: 'up', port: 7681 } },
  timestamp: 0,
};
const desktop = { width: 1280, height: 800 };
const mobile = { width: 390, height: 844 };

try {
  for (const theme of ['dark', 'light']) {
    // Bare visit: lands on the default route with the default rail state.
    await shoot({ name: `rail-home-desktop-${theme}`, viewport: desktop, theme, path: '/' });
  }
  // A route inside Workspace: the disclosure reveals itself.
  await shoot({ name: 'rail-workspace-route-desktop-dark', viewport: desktop, theme: 'dark', path: '/memory' });
  await shoot({
    name: 'rail-icon-mode-desktop-dark',
    viewport: desktop,
    theme: 'dark',
    path: '/memory',
    storage: {
      'kube-coder.ui': JSON.stringify({ theme: 'system', density: 'comfortable', railCollapsed: true, masterCollapsed: false }),
    },
  });
  await shoot({ name: 'bottomnav-home-mobile-dark', viewport: mobile, theme: 'dark', path: '/' });
  await shoot({
    name: 'workspace-sheet-mobile-dark',
    viewport: mobile,
    theme: 'dark',
    path: '/',
    action: async (p) => {
      await p.click('.bottomnav button[aria-label="Workspace"], .bottomnav button[aria-label="More"]');
      await p.waitForSelector('.sheet-open .more-section', { timeout: 4000 });
    },
  });
} finally {
  await browser.close();
}
