/**
 * Memory soak for the desktop UI (#833): does the web process's heap stay put
 * under a busy workspace's workload?
 *
 * Loads the desktop app's UI (desktop/dist, from `node scripts/build-ui.mjs`)
 * in headless Chromium with a stand-in for the Tauri bridge — the same
 * commands desktop/src-tauri answers — so it needs no workspace and no Rust
 * build. The stand-in answers `kc_request` from synthetic fixtures built in the
 * page, and delivers core events the way Tauri does: one fresh `eval` per
 * event.
 *
 * The workload is what grew the maintainer's WebContent process to 654 MB in
 * 11 minutes: an open Hypervisor chat with a long, idle transcript (2,000
 * events, ~2.5 MB, the size of a real one), polled every 2 s, while the event
 * stream carries Builds, Feed and Board events. The recorded rate on a real
 * workspace with 2 running and 3 waiting Builds was zero events in 5 minutes
 * (the server publishes on status transitions only), so the default here,
 * 2 events/s, is a stress rate.
 *
 * Phases:
 *   1. Chat open and focused, events replayed, for --minutes. The JS heap is
 *      read after a forced GC every 15 s; growth from the first sample after
 *      warm-up must stay under --max-growth-mb. Before the fix it grew by
 *      ~1 MB/s here.
 *   2. The Builds list with running Builds (spinners), then the window hidden:
 *      no workspace request and no running animation for 20 s. Shown again,
 *      it refreshes.
 *
 *   node desktop/scripts/soak.mjs [--minutes 1.5] [--rate 2] [--max-growth-mb 8] [--dist desktop/dist]
 *                                 [--snapshots DIR]
 *
 * --snapshots writes a V8 heap snapshot (open in Chrome DevTools → Memory)
 * after warm-up, at the halfway sample and at the end of phase 1.
 *
 * Needs mobile/'s dev dependencies (playwright, serve-handler) and Chromium
 * (`npx playwright install chromium` in mobile/). KC_CHROMIUM names a browser
 * explicitly, as in the mobile checks.
 */
import fs from 'node:fs';
import http from 'node:http';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(path.resolve(here, '../../mobile/package.json'));
const handler = require('serve-handler');
const { chromium } = require('playwright');

const arg = (name, dflt) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 ? process.argv[i + 1] : dflt;
};
const MINUTES = Number(arg('minutes', '1.5'));
const RATE = Number(arg('rate', '2'));
const MAX_GROWTH_MB = Number(arg('max-growth-mb', '8'));
const DIST = path.resolve(arg('dist', path.resolve(here, '../dist')));
const SNAPSHOTS = arg('snapshots', '');
const WARMUP_MS = 30_000;
const SAMPLE_MS = 15_000;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

if (!fs.existsSync(path.join(DIST, 'index.html'))) {
  console.error(`no UI build at ${DIST}; run: node desktop/scripts/build-ui.mjs`);
  process.exit(2);
}

/** Runs IN THE PAGE before the app: the stand-in Tauri bridge. */
function standInBridge() {
  const THREAD = 'soak-thread';
  const now = Date.now() / 1000;
  // ~2.5 MB of transcript, shaped like a long Claude chat: prose, tool calls
  // with a command, tool results with output.
  const filler = (n, seed) => `${seed} `.repeat(Math.ceil(n / (seed.length + 1))).slice(0, n);
  const events = [];
  for (let seq = 1; seq <= 2000; seq++) {
    const k = seq % 4;
    events.push(
      k === 1
        ? { seq, ts: now - 4000 + seq, role: 'user', type: 'message', text: filler(200, `ask ${seq}`) }
        : k === 2
          ? { seq, ts: now - 4000 + seq, role: 'assistant', type: 'tool_call', tool_id: `t${seq}`, tool: { name: 'Bash', input: { command: filler(600, `cmd${seq}`) } } }
          : k === 3
            ? { seq, ts: now - 4000 + seq, role: 'system', type: 'tool_result', tool_use_id: `t${seq - 1}`, is_error: false, text: filler(2400, `out${seq}`) }
            : { seq, ts: now - 4000 + seq, role: 'assistant', type: 'message', text: filler(1500, `reply ${seq}`) },
    );
  }
  const thread = { id: THREAD, title: 'Soak chat', assistant: 'claude', model: 'default', status: 'idle', created_at: now - 4000, updated_at: now - 60, persona: '', project_id: '', workdir: '/home/dev' };
  const tasks = Array.from({ length: 30 }, (_, i) => ({
    task_id: `soak-${i}`,
    prompt: `Build ${i}: ${filler(120, 'work')}`,
    status: i < 4 ? 'running' : i < 7 ? 'waiting-for-input' : 'completed',
    created_at: now - 3600 + i,
    workdir: '/home/dev',
    assistant: 'claude',
  }));
  const feed = Array.from({ length: 60 }, (_, i) => ({
    id: `f${i}`, kind: i % 3 ? 'task' : 'board', title: `Feed ${i}`, body: filler(200, 'feed'), ts: now - i * 60,
    waiting: i < 5, read: i > 10, ref: i % 3 ? `task:soak-${i % 30}` : `board:soak:item-${i}`,
  }));
  const routes = [
    [/^\/api\/hypervisor\/threads\/[^/?]+(\?|$)/, (q) => {
      const since = Number(new URLSearchParams(q).get('since') || 0);
      return { thread, events: events.filter((e) => e.seq > since), source: 'session_log' };
    }],
    [/^\/api\/hypervisor\/threads(\?|$)/, () => ({ threads: [thread] })],
    [/^\/api\/hypervisor\/config/, () => ({ enabled: true, defaultAssistant: 'claude', workdir: '/home/dev', readOnly: false, assistants: [{ id: 'claude', label: 'Claude Code', ready: true, default: true, models: ['default'] }] })],
    [/^\/api\/claude\/tasks(\?|$)/, () => ({ tasks })],
    [/^\/api\/feed/, () => ({ items: feed })],
    [/^\/api\/missioncontrol\/queue/, () => ({ cards: [], pulse: { running: 4, waiting: 3, review: 0, done_today: 9, oldest_wait_s: 60, generated_at: now } })],
  ];
  const stats = { requests: 0, bytes: 0 };
  const items = new Map([['kc.apiBase', 'https://soak.invalid']]);
  let targets = [{ url: `kubecoder://thread/${THREAD}` }];
  const listeners = {};
  let nextId = 1;
  window.__soak = {
    stats,
    listenerCount: () => Object.values(listeners).reduce((n, m) => n + Object.keys(m).length, 0),
    emit: (event, payload) => {
      for (const cb of Object.values(listeners[event] || {})) cb({ event, payload });
    },
    setHidden: (hidden) => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => (hidden ? 'hidden' : 'visible') });
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
      document.dispatchEvent(new Event('visibilitychange'));
    },
  };
  window.__TAURI__ = {
    core: {
      invoke: async (cmd, args = {}) => {
        switch (cmd) {
          case 'kc_request': {
            stats.requests++;
            const [p, q = ''] = args.req.path.split('?');
            const hit = routes.find(([re]) => re.test(args.req.path));
            // A fresh string per answer, like the core's IPC response.
            const body = JSON.stringify(hit ? hit[1](q) : {});
            stats.bytes += body.length;
            return { status: 200, statusText: 'OK', contentType: 'application/json', body };
          }
          case 'kc_item_get':
            return items.get(args.key) ?? null;
          case 'kc_item_set':
            items.set(args.key, args.value);
            return null;
          case 'kc_secret_get':
            return args.key === 'kc.devToken' ? 'kc-desktop-token' : null;
          case 'kc_live_status':
            return { mode: 'stream', keeper: false, needsYou: 0, feedFetches: 0 };
          case 'kc_take_targets': {
            const t = targets;
            targets = [];
            return t;
          }
          default:
            return null;
        }
      },
    },
    event: {
      listen: async (event, cb) => {
        const id = nextId++;
        (listeners[event] ||= {})[id] = cb;
        return () => {
          delete listeners[event][id];
        };
      },
    },
  };
}

const server = http.createServer((req, res) =>
  handler(req, res, { public: DIST, rewrites: [{ source: '**', destination: '/index.html' }] }),
);
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const browser = await chromium.launch({ executablePath: process.env.KC_CHROMIUM || undefined });
const page = await browser.newPage({ viewport: { width: 1280, height: 820 } });
const errors = [];
page.on('pageerror', (e) => errors.push(e.message));
await page.addInitScript(standInBridge);
await page.goto(`http://127.0.0.1:${server.address().port}/`);
const cdp = await page.context().newCDPSession(page);
await cdp.send('HeapProfiler.enable');

let failures = 0;
const check = (ok, label) => {
  if (!ok) failures++;
  console.log(`${ok ? 'PASS' : 'FAIL'} ${label}`);
};
const heapMB = async () => {
  await cdp.send('HeapProfiler.collectGarbage');
  const { usedSize } = await cdp.send('Runtime.getHeapUsage');
  return usedSize / 1048576;
};
async function snapshot(tag) {
  if (!SNAPSHOTS) return;
  fs.mkdirSync(SNAPSHOTS, { recursive: true });
  const file = path.join(SNAPSHOTS, `soak-${tag}.heapsnapshot`);
  const out = fs.createWriteStream(file);
  const onChunk = (e) => out.write(e.chunk);
  cdp.on('HeapProfiler.addHeapSnapshotChunk', onChunk);
  await cdp.send('HeapProfiler.takeHeapSnapshot', { reportProgress: false });
  cdp.off('HeapProfiler.addHeapSnapshotChunk', onChunk);
  await new Promise((r) => out.end(r));
  console.log(`snapshot ${file}`);
}
const stats = () => page.evaluate(() => ({ ...window.__soak.stats, listeners: window.__soak.listenerCount() }));

// ---- 1. The chat open, the event stream busy.
const types = ['task.status', 'feed.item', 'boards.review', 'task.created', 'boards.run'];
let n = 0;
const replay = setInterval(() => {
  const type = types[n++ % types.length];
  const payload = { type, data: { id: `soak-${n % 30}`, status: 'running', op: 'progress', n } };
  // Tauri's emit_js_script: a fresh script per event.
  page.evaluate(`(function () { window.__soak.emit('kc://event', ${JSON.stringify(payload)}) })()`).catch(() => {});
}, 1000 / Math.max(RATE, 0.01));

const t0 = Date.now();
await sleep(WARMUP_MS);
const base = await heapMB();
const baseStats = await stats();
const samples = [{ s: Math.round((Date.now() - t0) / 1000), heapMB: base }];
console.log(`t=${samples[0].s}s heap ${base.toFixed(1)} MB (baseline)`);
await snapshot('start');
let midDone = false;
while (Date.now() - t0 < MINUTES * 60_000) {
  await sleep(SAMPLE_MS);
  const h = await heapMB();
  samples.push({ s: Math.round((Date.now() - t0) / 1000), heapMB: h });
  console.log(`t=${samples.at(-1).s}s heap ${h.toFixed(1)} MB`);
  if (!midDone && Date.now() - t0 >= (MINUTES * 60_000) / 2) {
    midDone = true;
    await snapshot('mid');
  }
}
await snapshot('end');
clearInterval(replay);
const end = await stats();
const growth = samples.at(-1).heapMB - base;
const minutes = (samples.at(-1).s - samples[0].s) / 60;
const kbPerMin = (end.bytes - baseStats.bytes) / 1024 / Math.max(minutes, 0.01);
console.log(`events replayed: ${n} (${RATE}/s); requests ${end.requests - baseStats.requests}, ${kbPerMin.toFixed(0)} KB/min of responses after warm-up`);
check(growth < MAX_GROWTH_MB, `JS heap after GC grew ${growth.toFixed(1)} MB over ${minutes.toFixed(1)} min (bound ${MAX_GROWTH_MB} MB)`);
check(end.listeners === baseStats.listeners, `core event listeners unchanged (${baseStats.listeners} → ${end.listeners})`);
// An idle chat reads its one overlap event per poll, not the transcript.
check(kbPerMin < 1024, `an idle open chat reads ${kbPerMin.toFixed(0)} KB/min (< 1 MB/min)`);

// ---- 2. Running Builds, then the window hidden.
await page.getByLabel('Go to Builds').first().click();
await sleep(3000);
const spinners = await page.evaluate(() => document.getAnimations().filter((a) => a.playState === 'running').length);
check(spinners > 0, `running Builds animate while shown (${spinners} animations)`);
await page.evaluate(() => window.__soak.setHidden(true));
await sleep(1000);
const hiddenFrom = (await stats()).requests;
await sleep(20_000);
const hiddenReqs = (await stats()).requests - hiddenFrom;
const runningHidden = await page.evaluate(() => document.getAnimations().filter((a) => a.playState === 'running').length);
check(hiddenReqs === 0, `no workspace request in 20 s hidden (${hiddenReqs})`);
check(runningHidden === 0, `no animation runs while hidden (${runningHidden} running)`);
await page.evaluate(() => window.__soak.setHidden(false));
await sleep(2000);
const shownReqs = (await stats()).requests - hiddenFrom - hiddenReqs;
const runningShown = await page.evaluate(() => document.getAnimations().filter((a) => a.playState === 'running').length);
check(shownReqs > 0, `shown again, it refreshes at once (${shownReqs} requests)`);
check(runningShown === spinners, `shown again, the spinners resume (${runningShown})`);
check(errors.length === 0, `no page errors${errors.length ? `: ${errors.slice(0, 3).join(' | ')}` : ''}`);

console.log(JSON.stringify({ samples }));
console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILURES`);
await browser.close();
server.close();
process.exit(failures === 0 ? 0 : 1);
