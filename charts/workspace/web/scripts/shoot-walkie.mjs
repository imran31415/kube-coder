#!/usr/bin/env node
/**
 * Screenshots of the Walkie-Talkie WhatsApp-gateway preview (issue #306),
 * desktop light + dark. Mocks the hypervisor facade + the /api/gateway/internal
 * endpoints with a scripted transcript (ack → final with tap-buttons → an
 * out-of-window template) so the device render is deterministic and independent
 * of a live agent. Expands one "wire" disclosure so the provider payload shows.
 *
 * Usage: node scripts/shoot-walkie.mjs [output-dir]
 */
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromiumPath } from './chromium-path.mjs';

const out = resolve(process.argv[2] || '/home/dev/screenshots');
mkdirSync(out, { recursive: true });
const CHROMIUM = chromiumPath();
const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:7070';

const CONFIG = {
  enabled: true, defaultAssistant: 'claude', workdir: '/home/dev',
  readOnly: false,
  assistants: [{ id: 'claude', label: 'Claude', default: true, model: 'opus-4.8' }],
};

const WIRE_BUTTONS = {
  provider: 'meta',
  payloads: [{
    messaging_product: 'whatsapp', to: '+15550100', type: 'interactive',
    interactive: {
      type: 'button',
      body: { text: "The test task is still running (~40s in). It's run 3 commands so far." },
      action: {
        buttons: [
          { type: 'reply', reply: { id: '1', title: 'Restart it' } },
          { type: 'reply', reply: { id: '2', title: 'Show logs' } },
          { type: 'reply', reply: { id: '3', title: 'Leave it' } },
        ],
      },
    },
  }],
};

const MESSAGES = [
  { seq: 1, ts: 0, direction: 'out', kind: 'message', text: '✅ Linked! Send me a message and I’ll drive your workspace agent.', quick_replies: [], wire: null, meta: {} },
  { seq: 2, ts: 0, direction: 'in', kind: 'message', text: "what's running right now?", quick_replies: [], wire: { inbound: { from: 'whatsapp:+15550100', text: "what's running right now?" } }, meta: {} },
  { seq: 3, ts: 0, direction: 'out', kind: 'message', text: 'On it — working on that…', quick_replies: [], wire: null, meta: {} },
  { seq: 4, ts: 0, direction: 'out', kind: 'message', text: "The test task is still running (~40s in). It's run 3 commands so far.", quick_replies: ['Restart it', 'Show logs', 'Leave it'], wire: WIRE_BUTTONS, meta: {} },
  { seq: 5, ts: 0, direction: 'out', kind: 'template', text: "✅ Your task 'nightly build' finished — reply to see the result.", quick_replies: [], wire: { provider: 'meta', payloads: [{ messaging_product: 'whatsapp', to: '+15550100', type: 'template', template: { name: 'task_complete', language: { code: 'en' } } }] }, meta: {} },
];

const STATE = {
  available: true, messages: MESSAGES, cursor: 5, linked: true,
  simulate_out_of_window: false, provider: 'meta', identity: 'internal:local',
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

const browser = await chromium.launch({ executablePath: CHROMIUM, headless: true });
try {
  for (const theme of ['light', 'dark']) {
    const ctx = await browser.newContext({
      viewport: { width: 1180, height: 900 },
      deviceScaleFactor: 2,
      colorScheme: theme,
    });
    await ctx.addInitScript((t) => {
      localStorage.setItem('kc.onboardingDone', 'true');
      localStorage.setItem('kc.theme', t);
    }, theme);
    const page = await ctx.newPage();
    await mock(page);
    await page.goto(`${BASE}/`, { waitUntil: 'networkidle', timeout: 15000 });
    await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme);
    await page.evaluate(() => {
      window.history.pushState({}, '', '/hypervisor');
      window.dispatchEvent(new PopStateEvent('popstate'));
    });
    // Switch the Hypervisor main pane to Walkie-Talkie.
    await page.getByRole('tab', { name: /Walkie-Talkie/ }).click();
    await page.waitForSelector('.wt-device', { timeout: 8000 });
    // Reveal one wire payload so the provider JSON is visible.
    await page.locator('.wt-wire-toggle').first().click();
    await page.waitForTimeout(400);
    const file = resolve(out, `walkie-desktop-${theme}.png`);
    await page.screenshot({ path: file });
    console.log('wrote', file);
    await ctx.close();
  }
} finally {
  await browser.close();
}
