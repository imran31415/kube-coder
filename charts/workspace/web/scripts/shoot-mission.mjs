#!/usr/bin/env node
/**
 * Focused screenshots of the Mission Control board (#425) — verifies the
 * four-column desktop grid, the waiting-card quick-reply block, lineage
 * lines, the pulse strip, and the mobile swimlane collapse. Mocks
 * /api/missioncontrol/queue with a fixture covering every card state.
 *
 * Usage: node scripts/shoot-mission.mjs [output-dir]
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
const CARDS = [
  {
    id: 'build:t_memgc', ref_id: 't_memgc', kind: 'build', state: 'waiting',
    title: 'Memory GC defaults (#359)',
    headline: 'Purging soft-deleted tombstones needs a destructive sqlite pass',
    assistant: 'claude', model: '', workdir: '/home/dev/kube-coder',
    repo: 'kube-coder', branch: 'issue-359-memory-gc',
    created_at: now - 3200, updated_at: now - 840, finished_at: null,
    waiting_since: now - 840,
    waiting_prompt: {
      kind: 'choice',
      question: 'Run sqlite3 memory.db "DELETE FROM tombstones…" to purge 412 soft-deleted rows?',
      options: [
        { index: 1, label: 'Yes' },
        { index: 2, label: "Yes, and don't ask again" },
        { index: 3, label: 'No, and tell Claude what to do differently' },
      ],
    },
    outcome: null, parent_id: null, children: [],
  },
  {
    id: 'build:t_trigger91', ref_id: 't_trigger91', kind: 'build', state: 'running',
    title: 'Trigger run-history & audit log (#91)',
    headline: 'Editing web/src/routes/triggers/RunHistory.tsx — rendering the runs table',
    assistant: 'claude', model: '', workdir: '/home/dev/kube-coder',
    repo: 'kube-coder', branch: 'issue-91-trigger-history',
    created_at: now - 1920, updated_at: now - 12, finished_at: null,
    waiting_since: null, waiting_prompt: null, outcome: null, parent_id: null,
    children: [
      { id: 'subagent:t_testwriter', title: 'test-writer', state: 'running' },
      { id: 'subagent:t_docs', title: 'docs', state: 'review' },
    ],
  },
  {
    id: 'subagent:t_testwriter', ref_id: 't_testwriter', kind: 'subagent', state: 'running',
    title: 'test-writer',
    headline: 'Adding vitest coverage for webhook signature verification — 3 specs green',
    assistant: 'codex', model: '', workdir: '/home/dev/kube-coder',
    repo: 'kube-coder', branch: 'issue-91-trigger-history',
    created_at: now - 560, updated_at: now - 30, finished_at: null,
    waiting_since: null, waiting_prompt: null, outcome: null,
    parent_id: 'build:t_trigger91', children: [],
  },
  {
    id: 'chat:h_landing', ref_id: 'h_landing', kind: 'chat', state: 'running',
    title: 'Landing page copy refresh',
    headline: 'Iterating on pricing section wording — waiting on a slow Vite build',
    assistant: 'claude', model: 'opus-4.8', workdir: '', repo: '', branch: '',
    created_at: now - 7400, updated_at: now - 95, finished_at: null,
    waiting_since: null, waiting_prompt: null, outcome: null, parent_id: null,
    children: [],
  },
  {
    id: 'build:t_sidebar', ref_id: 't_sidebar', kind: 'build', state: 'review',
    title: 'Sidebar reorganization (#267)',
    headline: 'Grouped rail into Work / Knowledge / System sections; PR opened',
    assistant: 'claude', model: '', workdir: '/home/dev/kube-coder',
    repo: 'kube-coder', branch: 'issue-267-sidebar',
    created_at: now - 9800, updated_at: now - 1560, finished_at: now - 1560,
    waiting_since: null, waiting_prompt: null,
    outcome: { ok: true, detail: 'completed' }, parent_id: null, children: [],
  },
  {
    id: 'subagent:t_docs', ref_id: 't_docs', kind: 'subagent', state: 'review',
    title: 'docs',
    headline: 'Wrote docs/triggers.md covering the run-history endpoints',
    assistant: 'ante', model: '', workdir: '/home/dev/kube-coder',
    repo: 'kube-coder', branch: 'issue-91-trigger-history',
    created_at: now - 1100, updated_at: now - 700, finished_at: now - 700,
    waiting_since: null, waiting_prompt: null,
    outcome: { ok: true, detail: 'completed' },
    parent_id: 'build:t_trigger91', children: [],
  },
  {
    id: 'build:t_deps', ref_id: 't_deps', kind: 'build', state: 'done',
    title: 'Bump controller SPA deps',
    headline: 'npm install failed with ENOSPC while refreshing the lockfile',
    assistant: 'codex', model: '', workdir: '/home/dev/kube-coder',
    repo: 'kube-coder', branch: 'main',
    created_at: now - 16000, updated_at: now - 14200, finished_at: now - 14200,
    waiting_since: null, waiting_prompt: null,
    outcome: { ok: false, detail: 'error · exit 1' }, parent_id: null, children: [],
  },
  {
    id: 'chat:h_dind', ref_id: 'h_dind', kind: 'chat', state: 'done',
    title: 'Debug DinD TLS handshake',
    headline: 'Root cause was the unix→TLS bridge half-close; fix documented',
    assistant: 'claude', model: '', workdir: '', repo: '', branch: '',
    created_at: now - 30000, updated_at: now - 18500, finished_at: null,
    waiting_since: null, waiting_prompt: null,
    outcome: { ok: true, detail: 'idle — resumable' }, parent_id: null,
    children: [],
  },
];
const QUEUE = {
  cards: CARDS,
  pulse: {
    running: 3, waiting: 1, review: 2, done_today: 4,
    oldest_wait_s: 840, generated_at: now,
  },
};

async function mockMission(page) {
  await page.route('**/api/missioncontrol/queue', (r) =>
    r.fulfill({ contentType: 'application/json', body: JSON.stringify(QUEUE) }),
  );
  // The SSE stream never settles, which would stall a networkidle wait and
  // keeps a retrying EventSource noisy in logs — stub it shut.
  await page.route('**/api/events', (r) =>
    r.fulfill({ contentType: 'text/event-stream', body: 'event: ready\ndata: {}\n\n' }),
  );
  // Keep the board's clocks deterministic: relative "Xm ago" labels are
  // computed against Date.now(), so pin it just past the fixture epoch.
  await page.addInitScript(`Date.now = () => ${(now + 5) * 1000};`);
}

const browser = await chromium.launch({ executablePath: CHROMIUM, headless: true });
try {
  const shots = [
    { name: 'mission-desktop-dark', viewport: { width: 1280, height: 800 }, theme: 'dark' },
    { name: 'mission-desktop-light', viewport: { width: 1280, height: 800 }, theme: 'light' },
    { name: 'mission-mobile-dark', viewport: { width: 390, height: 844 }, theme: 'dark' },
    { name: 'mission-mobile-light', viewport: { width: 390, height: 844 }, theme: 'light' },
  ];
  for (const s of shots) {
    const ctx = await browser.newContext({
      viewport: s.viewport, deviceScaleFactor: 2, colorScheme: s.theme,
    });
    const page = await ctx.newPage();
    await page.addInitScript(() => localStorage.setItem('kc.onboardingDone', 'true'));
    await mockMission(page);
    await page.goto(`${BASE}/mission`, { waitUntil: 'load' });
    await page.waitForSelector('.route-mission', { timeout: 15000 });
    await page.evaluate((theme) => {
      document.documentElement.setAttribute('data-theme', theme);
    }, s.theme);
    await page.waitForTimeout(500);
    // Viewport-only: fullPage would smear fixed chrome (BottomNav, sheets)
    // down the stitched capture.
    await page.screenshot({ path: `${out}/${s.name}.png` });
    await ctx.close();
    console.log(`${s.name}.png`);
  }
} finally {
  await browser.close();
}
