#!/usr/bin/env node
/**
 * Overflow/clipping screenshots for the Walkie-Talkie screen (issue #409):
 * feeds the mocked /api/gateway/internal transcript adversarially long
 * content — a multi-paragraph agent reply with an unbroken URL, a long user
 * utterance, a fat quick-reply set and a long history — and captures the
 * screen at the sizes where clipping bites: desktop, the ≤720px breakpoint,
 * and a short landscape viewport. Also captures the expanded "what you said"
 * line and the open transcript panel.
 *
 * Usage: node scripts/shoot-walkie-overflow.mjs [output-dir]
 */
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromiumPath } from './chromium-path.mjs';

const out = resolve(process.argv[2] || '/home/dev/screenshots/walkie-overflow');
mkdirSync(out, { recursive: true });
const CHROMIUM = chromiumPath();
const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:7070';
const PREFIX = process.env.SHOT_PREFIX || '';

const CONFIG = {
  enabled: true, defaultAssistant: 'claude', workdir: '/home/dev',
  readOnly: false,
  assistants: [{ id: 'claude', label: 'Claude', default: true, model: 'opus-4.8' }],
  stt: true,
};

const LONG_REPLY = [
  'Here is the full rundown you asked for. The nightly build finished clean about ten minutes ago — 383 tests passed, no flakes, and the container image was pushed to the registry as kube-coder/workspace:nightly-20260722.',
  '',
  'While it ran I also audited the three background tasks you left going. The database migration completed and applied 14 migrations; the log tail ends with "migration 0014_backfill_thread_index applied in 3.2s". The websocket soak test is still running at 41 minutes with zero dropped frames so far. The dependency upgrade task hit a peer-dependency conflict between @preact/signals and preact 10.24 — I held it rather than force-resolving, because the lockfile diff would have touched 212 packages.',
  '',
  'One thing needs your decision: the ingress chart bumps cert-manager to v1.15 which drops the deprecated Certificate v1alpha2 API. Staging still has two v1alpha2 resources: coder-tls and registry-tls. I can convert both automatically, but that re-issues the certs and there is a ~30 second window where new TLS handshakes would fail. Full diff and rollout plan: https://github.com/imran31415/kube-coder/compare/main...nightly-20260722-cert-manager-v1.15-migration-plan?expand=1&files=charts%2Fworkspace%2Ftemplates%2Fingress.yaml',
  '',
  'Everything else is green. Disk is at 61%, memory steady around 2.1Gi, and no pods restarted overnight.',
].join('\n');

const LONG_YOU = 'Okay so before you do anything else I need a complete status update on the nightly build, then check whether the database migration task I started before dinner actually finished or if it got stuck again like yesterday, and also look at the websocket soak test and the dependency upgrade — and if anything failed, do not restart it, just show me the logs and wait for me to decide what to do next.';

const MESSAGES = [
  { seq: 1, ts: 0, direction: 'out', kind: 'message', text: '✅ Linked! Press the orb and talk to your workspace agent.', quick_replies: [], wire: null, meta: {} },
  { seq: 2, ts: 0, direction: 'in', kind: 'message', text: "What's running right now?", quick_replies: [], wire: null, meta: {} },
  { seq: 3, ts: 0, direction: 'out', kind: 'message', text: 'Three tasks: the nightly build, a database migration, and a websocket soak test. All healthy as of thirty seconds ago.', quick_replies: [], wire: null, meta: {} },
  { seq: 4, ts: 0, direction: 'in', kind: 'message', text: 'Paste me the exact image tag from the last push, the full one with the registry host', quick_replies: [], wire: null, meta: {} },
  { seq: 5, ts: 0, direction: 'out', kind: 'message', text: 'registry.internal.example.com:5000/kube-coder/workspace@sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08 — pushed 6 minutes ago from the nightly job.', quick_replies: [], wire: null, meta: {} },
  { seq: 6, ts: 0, direction: 'in', kind: 'message', text: LONG_YOU, quick_replies: [], wire: null, meta: {} },
  {
    seq: 7, ts: 0, direction: 'out', kind: 'message',
    text: LONG_REPLY,
    quick_replies: [
      'Convert both certs now',
      'Hold the cert-manager bump',
      'Show the websocket soak logs',
      'Show the migration log tail',
      'Retry the dependency upgrade with --legacy-peer-deps',
      'Leave everything as is',
    ],
    wire: null, meta: {},
  },
];

const STATE = {
  available: true, messages: MESSAGES, cursor: 7, linked: true,
  simulate_out_of_window: false, provider: 'internal', identity: 'internal:local',
  busy: false, thread_id: 't-preview',
};

async function mock(page) {
  await page.route('**/api/hypervisor/config', (r) =>
    r.fulfill({ contentType: 'application/json', body: JSON.stringify(CONFIG) }));
  await page.route('**/api/hypervisor/threads', (r) =>
    r.fulfill({ contentType: 'application/json', body: JSON.stringify({ threads: [] }) }));
  await page.route('**/api/gateway/internal/transcript**', (r) =>
    r.fulfill({ contentType: 'application/json', body: JSON.stringify(STATE) }));
  await page.route('**/api/gateway/internal/control', (r) =>
    r.fulfill({ contentType: 'application/json', body: JSON.stringify({ ok: true, linked: true }) }));
  await page.route('**/api/events**', (r) => r.fulfill({ status: 204, body: '' }));
}

const STUB_VOICE = () => {
  class FakeRecognition {
    start() { window.__rec = this; }
    stop() { this.onend && this.onend(); }
    abort() { this.onend && this.onend(); }
  }
  window.SpeechRecognition = FakeRecognition;
  localStorage.setItem('kc.onboardingDone', 'true');
  localStorage.setItem('kc.guide.walkie', 'done');
};

async function capture(browser, { viewport, theme, file, history, expandYou, listening }) {
  const ctx = await browser.newContext({
    viewport,
    deviceScaleFactor: 2,
    colorScheme: theme,
  });
  await ctx.addInitScript(STUB_VOICE);
  await ctx.addInitScript((t) => localStorage.setItem('kc.theme', t), theme);
  const page = await ctx.newPage();
  await mock(page);

  await page.goto(`${BASE}/`, { waitUntil: 'networkidle', timeout: 15000 });
  await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme);
  await page.evaluate(() => {
    window.history.pushState({}, '', '/walkie');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  await page.waitForSelector('.wt-orb', { timeout: 8000 });
  await page.waitForTimeout(600); // reveal animation settles

  // DOM-level clicks: pre-fix, overflowing content can push these controls
  // outside the viewport (the very bug under test), which would fail
  // Playwright's actionability checks.
  if (history) {
    await page.evaluate(() => document.querySelector('.wt-history-toggle')?.click());
    await page.waitForTimeout(300);
  }
  if (expandYou) {
    // Post-fix the "you" line is a button with aria-expanded; before the fix
    // it's a plain div — click is a no-op there, which is the point.
    await page.evaluate(() => document.querySelector('.wt-you')?.click());
    await page.waitForTimeout(200);
  }
  if (listening) {
    // Start a capture and feed a long interim transcript: shows the capped,
    // scrollable interim line and the explicit "Stop & send" control.
    await page.evaluate(() => document.querySelector('.wt-orb')?.click());
    await page.waitForTimeout(150);
    await page.evaluate(() => {
      window.__rec?.onresult?.({
        resultIndex: 0,
        results: [
          { isFinal: true, 0: { transcript: 'Okay start the deploy to staging but first re-run the failed integration suite and tail the migration logs while it goes,' } },
          { isFinal: false, 0: { transcript: 'and if anything at all looks off pause everything and read me the last twenty lines out loud' } },
        ],
      });
    });
    await page.waitForTimeout(400);
  }

  await page.screenshot({ path: `${out}/${PREFIX}${file}` });
  console.log(`✓ ${PREFIX}${file}`);
  await ctx.close();
}

const browser = await chromium.launch({ executablePath: CHROMIUM, headless: true });
try {
  await capture(browser, {
    viewport: { width: 1280, height: 800 },
    theme: 'dark',
    file: 'desktop-dark.png',
  });
  await capture(browser, {
    viewport: { width: 390, height: 844 },
    theme: 'light',
    file: 'mobile-light.png',
  });
  await capture(browser, {
    viewport: { width: 844, height: 390 },
    theme: 'dark',
    file: 'landscape-short-dark.png',
  });
  await capture(browser, {
    viewport: { width: 1280, height: 800 },
    theme: 'dark',
    history: true,
    file: 'desktop-dark-history.png',
  });
  await capture(browser, {
    viewport: { width: 390, height: 844 },
    theme: 'light',
    expandYou: true,
    file: 'mobile-light-you-expanded.png',
  });
  await capture(browser, {
    viewport: { width: 1280, height: 800 },
    theme: 'dark',
    listening: true,
    file: 'desktop-dark-listening.png',
  });
} finally {
  await browser.close();
}
console.log(`\nSaved to ${out}`);
