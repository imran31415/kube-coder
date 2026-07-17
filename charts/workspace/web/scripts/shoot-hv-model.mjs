#!/usr/bin/env node
/**
 * Screenshots for the Hypervisor in-chat model switcher (issue #308). Mocks the
 * /api/hypervisor facade with a realistic multi-provider config (Claude + the
 * two OpenCode providers, each carrying a `models` list, incl. the free DeepSeek
 * model on OpenRouter) and captures the topbar switcher on desktop + mobile in
 * both themes. To make the native <select> options visible in a still, we
 * momentarily set `size` so the browser renders it as an inline listbox.
 *
 * Usage: node scripts/shoot-hv-model.mjs [output-dir]
 */
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromiumPath } from './chromium-path.mjs';

const out = resolve(process.argv[2] || '/home/dev/screenshots');
mkdirSync(out, { recursive: true });

const CHROMIUM = chromiumPath();
const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:7070';

const now = 1_752_300_000; // fixed epoch (deterministic)
const CONFIG = {
  enabled: true,
  defaultAssistant: 'claude',
  workdir: '/home/dev',
  readOnly: false,
  commands: [],
  assistants: [
    { id: 'claude', label: 'Claude Code', default: true, models: ['default', 'opus', 'sonnet', 'haiku'] },
    { id: 'opencode-openrouter', label: 'OpenRouter', model: 'anthropic/claude-sonnet-4',
      models: ['anthropic/claude-sonnet-4', 'deepseek/deepseek-chat-v3-0324:free'] },
    { id: 'opencode-deepseek', label: 'DeepSeek', model: 'deepseek-chat',
      models: ['deepseek-chat', 'deepseek-reasoner'] },
    { id: 'ante', label: 'Ante CLI', models: [] },
  ],
};
const THREADS = [
  { id: 't1', title: 'Refactor the auth middleware', assistant: 'claude', model: 'opus', status: 'idle', created_at: now - 9000, updated_at: now - 600 },
  { id: 't2', title: 'Summarize the release notes', assistant: 'opencode-openrouter', model: 'deepseek/deepseek-chat-v3-0324:free', status: 'idle', created_at: now - 40000, updated_at: now - 5400 },
];

async function mockHv(page) {
  await page.route('**/api/hypervisor/config', (r) =>
    r.fulfill({ contentType: 'application/json', body: JSON.stringify(CONFIG) }),
  );
  await page.route('**/api/hypervisor/threads', (r) =>
    r.fulfill({ contentType: 'application/json', body: JSON.stringify({ threads: THREADS }) }),
  );
  await page.route('**/api/hypervisor/threads/**', (r) =>
    r.fulfill({ contentType: 'application/json', body: JSON.stringify({ thread: THREADS[0], events: [], source: 'capture' }) }),
  );
  // Other dashboard endpoints the shell may touch — keep them quiet.
  await page.route('**/api/tasks', (r) =>
    r.fulfill({ contentType: 'application/json', body: JSON.stringify({ tasks: [] }) }),
  );
}

async function gotoHypervisor(page, theme) {
  await page.goto(`${BASE}/`, { waitUntil: 'networkidle', timeout: 15000 });
  await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme);
  await page.evaluate(() => {
    window.history.pushState({}, '', '/hypervisor');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  await page.waitForSelector('.hv-topbar', { timeout: 5000 });
  await page.waitForTimeout(500);
}

// Render a <select> as an inline listbox so its options show in a still.
async function expand(page, sel) {
  await page.evaluate((s) => {
    const el = document.querySelector(s);
    if (el) {
      el.size = el.options.length;
      el.style.position = 'relative';
      el.style.zIndex = '50';
      el.style.maxWidth = '260px';
    }
  }, sel);
  await page.waitForTimeout(200);
}

const browser = await chromium.launch({ executablePath: CHROMIUM, headless: true });
try {
  for (const theme of ['dark', 'light']) {
    // ── Desktop ──────────────────────────────────────────────────────────
    {
      const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1, colorScheme: theme });
      await ctx.addInitScript((t) => {
        localStorage.setItem('kc.onboardingDone', 'true');
        localStorage.setItem('kc.theme', t);
      }, theme);
      const page = await ctx.newPage();
      await mockHv(page);
      await gotoHypervisor(page, theme);

      // New chat, Claude selected → topbar model switcher shows Claude aliases.
      await page.screenshot({ path: `${out}/hv-model-desktop-${theme}.png` });
      console.log(`✓ hv-model-desktop-${theme}.png`);

      // Switch the sidebar Agent to OpenRouter, then expand the topbar model
      // <select> so the free DeepSeek option is visible in the still.
      await page.selectOption('.hv-agent-select', 'opencode-openrouter');
      await page.waitForTimeout(300);
      await expand(page, '.hv-model-select');
      await page.screenshot({ path: `${out}/hv-model-openrouter-desktop-${theme}.png` });
      console.log(`✓ hv-model-openrouter-desktop-${theme}.png`);
      await ctx.close();
    }

    // ── Mobile ───────────────────────────────────────────────────────────
    {
      const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, colorScheme: theme });
      await ctx.addInitScript((t) => {
        localStorage.setItem('kc.onboardingDone', 'true');
        localStorage.setItem('kc.theme', t);
      }, theme);
      const page = await ctx.newPage();
      await mockHv(page);
      await gotoHypervisor(page, theme);
      await page.selectOption('.hv-agent-select', 'opencode-openrouter').catch(() => {});
      await page.waitForTimeout(300);
      await page.screenshot({ path: `${out}/hv-model-mobile-${theme}.png` });
      console.log(`✓ hv-model-mobile-${theme}.png`);
      await ctx.close();
    }
  }
} finally {
  await browser.close();
}
console.log(`\nSaved to ${out}`);
