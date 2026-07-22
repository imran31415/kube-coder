#!/usr/bin/env node
/**
 * Focused screenshots of the Hypervisor voice interface (issue #396):
 * the push-to-talk mic in the composer (hot, pulsing) and the speak-replies
 * toggle in the topbar (on), over a small mocked transcript. SpeechRecognition
 * is stubbed via addInitScript so the "listening" state renders
 * deterministically in headless Chromium (no real mic/permission involved).
 *
 * Usage: node scripts/shoot-voice.mjs [output-dir]
 */
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromiumPath } from './chromium-path.mjs';

const out = resolve(process.argv[2] || '/home/dev/screenshots');
mkdirSync(out, { recursive: true });

const CHROMIUM = chromiumPath();
const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:7070';

const now = 1_752_300_000; // fixed epoch (script must be deterministic)
const CONFIG = {
  enabled: true,
  defaultAssistant: 'claude',
  workdir: '/home/dev',
  readOnly: false,
  assistants: [{ id: 'claude', label: 'Claude', default: true, model: 'opus-4.8' }],
  commands: [],
  stt: true,
};
const THREAD = {
  id: 't1',
  title: 'Deploy the dashboard',
  assistant: 'claude',
  status: 'idle',
  created_at: now - 9000,
  updated_at: now - 600,
};
const EVENTS = [
  {
    seq: 1,
    ts: now - 700,
    role: 'user',
    type: 'message',
    text: 'Deploy the dashboard and read me the summary',
  },
  {
    seq: 2,
    ts: now - 620,
    role: 'assistant',
    type: 'message',
    text:
      'Deployed the dashboard to port **3000**. The build finished clean and both ' +
      'health checks pass — I will keep an eye on the logs while you review.',
  },
];

async function mockHv(page) {
  // Catch-all first (registered routes are matched newest-first, so the
  // specific mocks below win) — keeps unrelated /api calls from 404-noise.
  await page.route('**/api/**', (r) =>
    r.fulfill({ contentType: 'application/json', body: '{}' }),
  );
  await page.route('**/api/workspace/dirs', (r) =>
    r.fulfill({ contentType: 'application/json', body: JSON.stringify({ dirs: [] }) }),
  );
  await page.route('**/api/hypervisor/config', (r) =>
    r.fulfill({ contentType: 'application/json', body: JSON.stringify(CONFIG) }),
  );
  await page.route('**/api/hypervisor/threads', (r) =>
    r.fulfill({ contentType: 'application/json', body: JSON.stringify({ threads: [THREAD] }) }),
  );
  await page.route('**/api/hypervisor/threads/t1?*', (r) =>
    r.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ thread: THREAD, events: EVENTS, source: 'capture' }),
    }),
  );
  await page.route('**/api/hypervisor/threads/t1/activity*', (r) =>
    r.fulfill({ contentType: 'application/json', body: JSON.stringify({ activity: [] }) }),
  );
}

// A do-nothing SpeechRecognition so sttSupported() is true and clicking the
// mic flips the UI into its listening state without touching real audio.
const STUB_RECOGNITION = () => {
  class FakeRecognition {
    start() {}
    stop() {
      this.onend && this.onend();
    }
    abort() {
      this.onend && this.onend();
    }
  }
  window.SpeechRecognition = FakeRecognition;
  localStorage.setItem('kc.onboardingDone', 'true');
  localStorage.setItem('kc.guide.hypervisor', 'done');
  localStorage.setItem('kc.hv.speak', '0');
};

const SAFE_AREA = ':root{--safe-top:47px!important;--safe-bottom:34px!important;}';

async function capture(browser, { viewport, theme, safeArea, file }) {
  const ctx = await browser.newContext({
    viewport,
    deviceScaleFactor: 2,
    colorScheme: theme,
  });
  await ctx.addInitScript(STUB_RECOGNITION);
  await ctx.addInitScript((t) => localStorage.setItem('kc.theme', t), theme);
  const page = await ctx.newPage();
  await mockHv(page);

  // The dev server 404s on deep links, so load the SPA root then drive the
  // history-API router client-side into the open thread.
  await page.goto(`${BASE}/`, { waitUntil: 'networkidle', timeout: 15000 });
  await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme);
  await page.evaluate(() => {
    window.history.pushState({}, '', '/hypervisor/t1');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  if (safeArea) await page.addStyleTag({ content: SAFE_AREA });
  await page.waitForSelector('.hv-turn', { timeout: 8000 });

  // Speak-replies ON + mic LISTENING — the two states this change adds.
  await page.click('.hv-voice-toggle');
  await page.click('.hv-mic-btn');
  await page.waitForTimeout(600);

  await page.screenshot({ path: `${out}/${file}` });
  console.log(`✓ ${file}`);
  await ctx.close();
}

const browser = await chromium.launch({ executablePath: CHROMIUM, headless: true });
try {
  await capture(browser, {
    viewport: { width: 1280, height: 800 },
    theme: 'dark',
    safeArea: false,
    file: 'hypervisor-voice-desktop-dark.png',
  });
  await capture(browser, {
    viewport: { width: 390, height: 844 },
    theme: 'light',
    safeArea: true,
    file: 'hypervisor-voice-mobile-light.png',
  });
} finally {
  await browser.close();
}
console.log(`\nSaved to ${out}`);
