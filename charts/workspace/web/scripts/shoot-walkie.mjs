#!/usr/bin/env node
/**
 * Screenshots of the voice-first Walkie-Talkie (issue #401): the PTT orb +
 * response card in idle (desktop dark + mobile light), and the LISTENING
 * state with live rings + interim transcript (desktop dark). Mocks the
 * /api/gateway/internal endpoints with a scripted transcript so the render is
 * deterministic, and stubs SpeechRecognition + getUserMedia/AudioContext (the
 * AnalyserNode feeds a synthetic waveform) so the listening visualizer runs in
 * headless Chromium with no real mic or permissions involved.
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
  stt: true,
};

const MESSAGES = [
  { seq: 1, ts: 0, direction: 'out', kind: 'message', text: '✅ Linked! Press the orb and talk to your workspace agent.', quick_replies: [], wire: null, meta: {} },
  { seq: 2, ts: 0, direction: 'in', kind: 'message', text: "What's running right now?", quick_replies: [], wire: null, meta: {} },
  { seq: 3, ts: 0, direction: 'out', kind: 'message', text: 'On it — working on that…', quick_replies: [], wire: null, meta: {} },
  { seq: 4, ts: 0, direction: 'in', kind: 'message', text: 'How is the nightly build doing?', quick_replies: [], wire: null, meta: {} },
  {
    seq: 5, ts: 0, direction: 'out', kind: 'message',
    text: 'The nightly build finished clean about ten minutes ago — 383 tests passed, no flakes. The test task you started is still running (~40s in); it has run 3 commands so far and nothing looks stuck.',
    quick_replies: ['Restart it', 'Show logs', 'Leave it'], wire: null, meta: {},
  },
];

const STATE = {
  available: true, messages: MESSAGES, cursor: 5, linked: true,
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

// SpeechRecognition that hands itself to the test (window.__rec) so the
// script can inject interim results, plus a getUserMedia + AudioContext pair
// whose AnalyserNode emits a synthetic speech-like waveform — the rings react
// exactly as they would to a real mic.
const STUB_VOICE = () => {
  class FakeRecognition {
    start() { window.__rec = this; }
    stop() { this.onend && this.onend(); }
    abort() { this.onend && this.onend(); }
  }
  window.SpeechRecognition = FakeRecognition;
  navigator.mediaDevices.getUserMedia = () =>
    Promise.resolve({ getTracks: () => [] });
  let t = 0;
  class FakeAnalyser {
    fftSize = 512;
    getByteTimeDomainData(buf) {
      t += 1;
      const amp = 40 + 30 * Math.sin(t / 3); // breathing speech envelope
      for (let i = 0; i < buf.length; i++) {
        buf[i] = 128 + Math.round(amp * Math.sin(i / 5) * Math.sin(t + i / 17));
      }
    }
  }
  window.AudioContext = class {
    createAnalyser() { return new FakeAnalyser(); }
    createMediaStreamSource() { return { connect() {} }; }
    close() { return Promise.resolve(); }
  };
  localStorage.setItem('kc.onboardingDone', 'true');
  localStorage.setItem('kc.guide.walkie', 'done');
  localStorage.setItem('kc.hv.speak', '1');
};

async function capture(browser, { viewport, theme, file, listening }) {
  const ctx = await browser.newContext({
    viewport,
    deviceScaleFactor: 2,
    colorScheme: theme,
  });
  await ctx.addInitScript(STUB_VOICE);
  await ctx.addInitScript((t) => localStorage.setItem('kc.theme', t), theme);
  const page = await ctx.newPage();
  await mock(page);

  // The dev server 404s on deep links, so load the SPA root then drive the
  // history-API router client-side into the walkie route.
  await page.goto(`${BASE}/`, { waitUntil: 'networkidle', timeout: 15000 });
  await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme);
  await page.evaluate(() => {
    window.history.pushState({}, '', '/walkie');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  await page.waitForSelector('.wt-orb', { timeout: 8000 });

  if (listening) {
    await page.click('.wt-orb');
    // Feed interim results through the stubbed recognition so the ghost
    // transcript renders above the orb.
    await page.evaluate(() => {
      window.__rec?.onresult?.({
        resultIndex: 0,
        results: [
          { isFinal: true, 0: { transcript: 'Restart the nightly build' } },
          { isFinal: false, 0: { transcript: 'and show me the logs' } },
        ],
      });
    });
    await page.waitForTimeout(700); // let the rAF level loop settle mid-swing
  } else {
    await page.waitForTimeout(500);
  }

  await page.screenshot({ path: `${out}/${file}` });
  console.log(`✓ ${file}`);
  await ctx.close();
}

const browser = await chromium.launch({ executablePath: CHROMIUM, headless: true });
try {
  await capture(browser, {
    viewport: { width: 1280, height: 800 },
    theme: 'dark',
    file: 'walkie-voice-desktop-dark.png',
  });
  await capture(browser, {
    viewport: { width: 390, height: 844 },
    theme: 'light',
    file: 'walkie-voice-mobile-light.png',
  });
  await capture(browser, {
    viewport: { width: 1280, height: 800 },
    theme: 'dark',
    listening: true,
    file: 'walkie-voice-listening-desktop-dark.png',
  });
} finally {
  await browser.close();
}
console.log(`\nSaved to ${out}`);
