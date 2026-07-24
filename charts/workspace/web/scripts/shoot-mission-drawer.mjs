#!/usr/bin/env node
/**
 * Focused screenshots of the Mission Control detail drawer (#425 phase 3) —
 * verifies the per-card timeline (tone dots, sub-agent cross-links), the
 * bounded output tail, and the follow-up composer. Mocks the queue plus the
 * new /api/missioncontrol/cards/{id} detail endpoint, then clicks a card so
 * the drawer opens exactly as it does for a user.
 *
 * Usage: node scripts/shoot-mission-drawer.mjs [output-dir]
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

// The card the drawer opens on: a live build with spawned sub-agents, same
// shape as shoot-mission.mjs's fixture.
const BUILD = {
  id: 'build:t_trigger91', ref_id: 't_trigger91', kind: 'build', state: 'running',
  title: 'Trigger run-history & audit log (#91)',
  headline: 'Editing web/src/routes/triggers/RunHistory.tsx — rendering the runs table',
  assistant: 'claude', model: '', workdir: '/home/dev/kube-coder',
  repo: 'kube-coder', branch: 'issue-91-trigger-history',
  created_at: now - 1920, updated_at: now - 12, finished_at: null,
  waiting_since: null, waiting_prompt: null, outcome: null, evidence: [], parent_id: null,
  children: [
    { id: 'subagent:t_testwriter', title: 'test-writer', state: 'running' },
    { id: 'subagent:t_docs', title: 'docs', state: 'done' },
  ],
};

const OTHERS = [
  {
    id: 'subagent:t_testwriter', ref_id: 't_testwriter', kind: 'subagent', state: 'running',
    title: 'test-writer',
    headline: 'Adding vitest coverage for webhook signature verification — 3 specs green',
    assistant: 'codex', model: '', workdir: '/home/dev/kube-coder',
    repo: 'kube-coder', branch: 'issue-91-trigger-history',
    created_at: now - 560, updated_at: now - 30, finished_at: null,
    waiting_since: null, waiting_prompt: null, outcome: null, evidence: [],
    parent_id: 'build:t_trigger91', children: [],
  },
  {
    id: 'subagent:t_docs', ref_id: 't_docs', kind: 'subagent', state: 'done',
    title: 'docs',
    headline: 'Wrote docs/triggers.md covering the run-history endpoints',
    assistant: 'ante', model: '', workdir: '/home/dev/kube-coder',
    repo: 'kube-coder', branch: 'issue-91-trigger-history',
    created_at: now - 1100, updated_at: now - 700, finished_at: now - 700,
    waiting_since: null, waiting_prompt: null,
    outcome: { ok: true, detail: 'completed' },
    evidence: [
      { label: 'vitest 214', ok: true, link: null },
      { label: 'PR #431', ok: null, link: 'https://github.com/imran31415/kube-coder/pull/431' },
    ],
    parent_id: 'build:t_trigger91', children: [],
  },
];

const QUEUE = {
  cards: [BUILD, ...OTHERS],
  pulse: { running: 2, waiting: 0, done_today: 1, oldest_wait_s: 0, generated_at: now },
};

const DETAIL = {
  card: BUILD,
  timeline: [
    {
      at: now - 1920, kind: 'start', text: 'Started',
      detail: 'Implement trigger run-history & audit log (#91): persist per-trigger runs and render them in the dashboard',
      link: null, status: 'ok',
    },
    {
      at: now - 1100, kind: 'subagent', text: 'Spawned sub-agent — docs',
      detail: 'Document the run-history endpoints in docs/triggers.md',
      link: 'subagent:t_docs', status: 'ok',
    },
    {
      at: now - 560, kind: 'subagent', text: 'Spawned sub-agent — test-writer',
      detail: 'Write vitest coverage for webhook signature verification',
      link: 'subagent:t_testwriter', status: 'ok',
    },
  ],
  output_tail: [
    '$ yarn vitest run src/routes/triggers',
    ' ✓ src/routes/triggers/RunHistory.test.tsx (6 tests) 212ms',
    '',
    ' Test Files  1 passed (1)',
    '      Tests  6 passed (6)',
    '',
    'Now wiring the runs table into the trigger detail pane —',
    'editing web/src/routes/triggers/RunHistory.tsx',
  ].join('\n'),
};

async function mockMission(page) {
  await page.route('**/api/missioncontrol/queue', (r) =>
    r.fulfill({ contentType: 'application/json', body: JSON.stringify(QUEUE) }),
  );
  await page.route('**/api/missioncontrol/cards/**', (r) =>
    r.fulfill({ contentType: 'application/json', body: JSON.stringify(DETAIL) }),
  );
  // The SSE stream never settles — stub it shut (same as shoot-mission.mjs).
  await page.route('**/api/events', (r) =>
    r.fulfill({ contentType: 'text/event-stream', body: 'event: ready\ndata: {}\n\n' }),
  );
  await page.addInitScript(`Date.now = () => ${(now + 5) * 1000};`);
}

const browser = await chromium.launch({ executablePath: CHROMIUM, headless: true });
try {
  const shots = [
    { name: 'mission-drawer-desktop-dark', viewport: { width: 1280, height: 800 }, theme: 'dark' },
    { name: 'mission-drawer-desktop-light', viewport: { width: 1280, height: 800 }, theme: 'light' },
    { name: 'mission-drawer-mobile-dark', viewport: { width: 390, height: 844 }, theme: 'dark' },
    { name: 'mission-drawer-mobile-light', viewport: { width: 390, height: 844 }, theme: 'light' },
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
    // Open the drawer the way a user does: click the live build's card body.
    await page.click('[data-card-id="build:t_trigger91"]');
    await page.waitForSelector('.mission-timeline', { timeout: 15000 });
    await page.waitForTimeout(500); // let the slide-in transition finish
    await page.screenshot({ path: `${out}/${s.name}.png` });
    await ctx.close();
    console.log(`${s.name}.png`);
  }
} finally {
  await browser.close();
}
