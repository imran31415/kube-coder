#!/usr/bin/env node
/**
 * Ad-hoc manual verification for the CTO/hypervisor chat scroll pin (#530).
 * Drives a real Chromium against the dev harness with a mocked hypervisor
 * thread, sends a multi-line message (so the composer has grown), streams a
 * reply in, and reports how far the transcript sits from the bottom.
 *
 * Usage: SHOT_BASE=http://127.0.0.1:3010 node scripts/_verify-scroll.mjs
 */
import { chromium } from 'playwright-core';
import { chromiumPath } from './chromium-path.mjs';

const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:7070';
const now = 1_752_300_000;

const CONFIG = {
  enabled: true,
  assistants: [{ id: 'claude', label: 'Claude', model: 'opus-4.8', models: ['opus-4.8'] }],
  defaultAssistant: 'claude',
  workdir: '/home/dev',
  readOnly: false,
};
const THREAD = {
  id: 'th1', title: 'Working session', assistant: 'claude', model: 'opus-4.8',
  workdir: '/home/dev', status: 'idle', created_at: now - 5000, updated_at: now - 10,
};

// A transcript long enough to scroll, then grown by the send + reply.
const events = [];
for (let i = 1; i <= 12; i++) {
  events.push({
    seq: i, ts: now - 400 + i, role: i % 2 ? 'user' : 'assistant', type: 'message',
    text: i % 2
      ? `Question number ${i} about the workspace layout`
      : `Answer number ${i}. ${'Detail line that wraps across the panel. '.repeat(4)}`,
  });
}
let running = false;

const browser = await chromium.launch({ executablePath: chromiumPath(), headless: true });
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: 'dark' });
const page = await ctx.newPage();
const json = (r, body) => r.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });

await page.route('**/api/hypervisor/config', (r) => json(r, CONFIG));
await page.route(/\/api\/hypervisor\/threads\?/, (r) => json(r, { threads: [THREAD] }));
await page.route('**/api/hypervisor/threads', (r) => json(r, { threads: [THREAD] }));
await page.route('**/api/hypervisor/threads/th1/messages', async (r) => {
  const body = JSON.parse(r.request().postData() || '{}');
  events.push({ seq: events.length + 1, ts: now, role: 'user', type: 'message', text: body.message });
  // The runner takes a beat to report `running`, so the "Working…" placeholder
  // appears on a *later* poll — with no new event to trigger a re-pin.
  setTimeout(() => { running = true; }, 1200);
  // The reply streams in over the next couple of polls.
  setTimeout(() => {
    events.push({
      seq: events.length + 1, ts: now + 1, role: 'assistant', type: 'message',
      text: `Here is the reply. ${'It keeps going for several lines so the turn is tall. '.repeat(6)}`,
    });
  }, 3000);
  setTimeout(() => {
    events.push({
      seq: events.length + 1, ts: now + 2, role: 'assistant', type: 'message',
      text: `And a second block that lands later. ${'More text still. '.repeat(8)}`,
    });
    running = false;
  }, 4200);
  await json(r, { ok: true });
});
await page.route('**/api/hypervisor/threads/th1/activity**', (r) => json(r, { events: [] }));
// Regex, not a glob: Playwright treats `?` in a glob as a single-char wildcard,
// so `threads/th1?**` would also swallow POSTs to `threads/th1/messages`.
await page.route(/\/api\/hypervisor\/threads\/th1\?since=/, (r) =>
  json(r, { thread: { ...THREAD, status: running ? 'running' : 'idle' }, events, source: 'events' }),
);
await page.route('**/api/workspace/dirs', (r) => json(r, { dirs: [] }));
await page.route('**/api/mode', (r) => json(r, { readOnly: false, authed: true, authMode: 'basic' }));
await page.route('**/api/events', (r) =>
  r.fulfill({ contentType: 'text/event-stream', body: 'event: ready\ndata: {}\n\n' }),
);
await page.addInitScript(() => localStorage.setItem('kc.onboardingDone', 'true'));

page.on('request', (r) => { if (r.url().includes('/hypervisor/threads')) console.log('REQ', r.method(), r.url().replace(/.*\/api/, '/api')); });
await page.goto(`${BASE}/next/hypervisor`, { waitUntil: 'load' });
await page.waitForSelector('.hv-transcript', { timeout: 15000 });
await page.waitForTimeout(1500);

page.on('console', (m) => { if (m.type() === 'error') console.log('CONSOLE', m.text().slice(0, 200)); });
page.on('request', (r) => { if (r.method() === 'POST') console.log('POST', r.url()); });

const measure = () =>
  page.evaluate(() => {
    const el = document.querySelector('.hv-transcript');
    return {
      blocks: el.querySelectorAll('.hv-msg, .hv-turn').length,
      fromBottom: Math.round(el.scrollHeight - el.scrollTop - el.clientHeight),
      scrollHeight: el.scrollHeight,
      clientHeight: el.clientHeight,
      composer: Math.round(document.querySelector('.hv-composer textarea')?.getBoundingClientRect().height || 0),
    };
  });

console.log('on load            ', await measure());

// Type a multi-line draft so the composer auto-grows before the send.
const ta = page.locator('.hv-composer textarea');
await ta.click();
// Shift+Enter for newlines — a bare Enter submits.
for (const line of ['line one of a long message', 'line two', 'line three', 'line four']) {
  await ta.type(line);
  await page.keyboard.press('Shift+Enter');
}
await ta.type('line five');
await page.waitForTimeout(400);
console.log('draft state', await page.evaluate(() => {
  const t = document.querySelector('.hv-composer-input');
  return { value: t?.value, disabled: t?.disabled, cls: t?.className, active: document.activeElement?.className };
}));
console.log('composer grown     ', await measure());

await ta.press('Enter');
await page.waitForTimeout(700);
console.log('right after send   ', await measure());
await page.waitForTimeout(2000);
console.log('agent went running ', await measure());
await page.waitForTimeout(2500);
console.log('reply streaming    ', await measure());
await page.waitForTimeout(2500);
console.log('reply complete     ', await measure());
console.log('last bubble', await page.evaluate(() => {
  const n = document.querySelectorAll('.hv-msg, .hv-turn');
  return n.length ? n[n.length - 1].textContent.slice(0, 80) : null;
}));

// Scrolling up must stay put across the next poll.
await page.evaluate(() => {
  document.querySelector('.hv-transcript').scrollTop = 0;
});
await page.waitForTimeout(2600);
console.log('scrolled up + poll ', await measure());

await page.screenshot({ path: process.env.SHOT_OUT || '/home/dev/screenshots/issue-530/chat-scroll-after-send.png' });
await browser.close();
