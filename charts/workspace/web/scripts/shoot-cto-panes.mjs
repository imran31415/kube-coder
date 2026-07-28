#!/usr/bin/env node
/**
 * AI CTO collapsible side panes (#530) — the projects rail folded to an icon
 * strip and the brief folded to an edge tab, so the chat owns the width.
 * Captures expanded vs collapsed on a wide desktop (both themes) plus the
 * auto-collapsed state in the cramped ≤1200px band. Same mocked backend as
 * shoot-cto.mjs so the page renders a full workbench with zero services.
 *
 * Usage: node scripts/shoot-cto-panes.mjs [output-dir]
 */
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromiumPath } from './chromium-path.mjs';

const out = resolve(process.argv[2] || '/home/dev/screenshots');
mkdirSync(out, { recursive: true });

const CHROMIUM = chromiumPath();
const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:7070';
// Deep-link through /next — server.py's SPA_TOP_LEVEL allowlist doesn't carry
// /cto, so the bare path 404s on the dev harness.
const CTO_URL = `${BASE}/next/cto`;

const now = 1_752_300_000; // fixed epoch (script must be deterministic)

const PROJECTS = [
  {
    id: 'kube-coder', name: 'kube-coder', workdirs: ['/home/dev/kube-coder'],
    repo: 'imran31415/kube-coder', memory_namespace: 'project.kube-coder',
    status: 'active', north_star: 'Ship the AI CTO to every workspace',
    last_seen_at: now - 90000, created_at: now - 900000, updated_at: now - 200,
    pulse: { running: 2, waiting: 1, last_activity_at: now - 200 },
  },
  {
    id: 'hosted', name: 'hosted', workdirs: ['/home/dev/hosted'],
    repo: 'imran31415/kubecoder-hosted', memory_namespace: 'project.hosted',
    status: 'active', north_star: 'KubeCoder.com landing + waitlist',
    last_seen_at: now - 400000, created_at: now - 800000, updated_at: now - 60000,
    pulse: { running: 0, waiting: 1, last_activity_at: now - 60000 },
  },
  {
    id: 'pool-hall', name: 'pool-hall', workdirs: ['/home/dev/pool-hall'],
    repo: '', memory_namespace: 'project.pool-hall',
    status: 'active', north_star: '',
    last_seen_at: null, created_at: now - 300000, updated_at: now - 250000,
    pulse: { running: 0, waiting: 0, last_activity_at: now - 250000 },
  },
];

const BRIEF = {
  project: PROJECTS[0],
  tasks: {
    running: 2, waiting: 1, total: 7,
    recent: [
      { task_id: 't_rail', status: 'running', prompt: 'Collapse the projects rail to an icon strip', workdir: '/home/dev/kube-coder', assistant: 'claude', last_activity_at: now - 200 },
      { task_id: 't_brief', status: 'waiting-for-input', prompt: 'Brief edge tab — confirm the delta badge', workdir: '/home/dev/kube-coder', assistant: 'claude', last_activity_at: now - 1800 },
    ],
  },
  goals: [
    { namespace: 'project.kube-coder.goals', key: 'ga', value: 'Reach GA with the /cto page + feed', tags: ['goal'], importance: 0.9, updated_at: now - 8000 },
  ],
  decisions: [
    { namespace: 'project.kube-coder.decisions', key: 'panes', value: 'Side panes auto-collapse below 1200px; an explicit toggle always wins', tags: ['decision'], importance: 0.8, updated_at: now - 40 },
    { namespace: 'project.kube-coder.decisions', key: 'sse', value: 'SSE over websockets — matches the rest of the stack', tags: ['decision'], importance: 0.8, updated_at: now - 120000 },
  ],
  memories: [],
  git: [{ workdir: '/home/dev/kube-coder', branch: 'kc/issue-530', exists: true }],
  triggers: [],
  counts: { goals: 1, decisions: 2, memories: 0, tasks: 7 },
  brief_markdown: '# kube-coder — project brief',
};

const CONFIG = {
  enabled: true,
  assistants: [{ id: 'claude', label: 'Claude', model: 'opus-4.8', models: ['opus-4.8'] }],
  defaultAssistant: 'claude',
  workdir: '/home/dev',
  readOnly: false,
};

/** localStorage keys for the persisted pane choices (see railSplit.ts). */
const RAIL_KEY = 'kc.ctoRailCollapsed';
const BRIEF_KEY = 'kc.ctoBriefCollapsed';

async function mockCto(page, panes) {
  const json = (r, body) => r.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
  await page.route('**/api/projects/_discover', (r) => json(r, { candidates: [], registered: [] }));
  await page.route('**/api/projects/kube-coder/brief', (r) => json(r, BRIEF));
  await page.route(/\/api\/projects\/[^/]+$/, (r) => json(r, PROJECTS[0]));
  await page.route('**/api/projects', (r) => json(r, { projects: PROJECTS }));
  await page.route('**/api/hypervisor/config', (r) => json(r, CONFIG));
  await page.route('**/api/hypervisor/threads**', (r) => json(r, { threads: [] }));
  await page.route('**/api/workspace/dirs', (r) => json(r, { dirs: [] }));
  await page.route('**/api/mode', (r) => json(r, { readOnly: false, authed: true, authMode: 'basic' }));
  await page.route('**/api/events', (r) =>
    r.fulfill({ contentType: 'text/event-stream', body: 'event: ready\ndata: {}\n\n' }),
  );
  await page.addInitScript(`Date.now = () => ${(now + 5) * 1000};`);
  await page.addInitScript(
    ([railKey, briefKey, choice]) => {
      localStorage.setItem('kc.cto.lastProject', 'kube-coder');
      localStorage.setItem('kc.onboardingDone', 'true');
      if (choice !== null) {
        localStorage.setItem(railKey, choice);
        localStorage.setItem(briefKey, choice);
      }
    },
    [RAIL_KEY, BRIEF_KEY, panes],
  );
}

const WIDE = { width: 1440, height: 900 };
const CRAMPED = { width: 1100, height: 820 }; // inside the ≤1200px auto-collapse band

const browser = await chromium.launch({ executablePath: CHROMIUM, headless: true });
try {
  const shots = [
    // panes: '0' = pinned open, '1' = folded away, null = let the heuristic decide.
    { name: 'cto-panes-expanded-dark', viewport: WIDE, theme: 'dark', panes: '0' },
    { name: 'cto-panes-expanded-light', viewport: WIDE, theme: 'light', panes: '0' },
    { name: 'cto-panes-collapsed-dark', viewport: WIDE, theme: 'dark', panes: '1' },
    { name: 'cto-panes-collapsed-light', viewport: WIDE, theme: 'light', panes: '1' },
    { name: 'cto-panes-auto-collapsed-dark', viewport: CRAMPED, theme: 'dark', panes: null },
    // Stacked/mobile is untouched by the collapse work — rail strip on top,
    // brief in a bottom sheet.
    { name: 'cto-panes-mobile-dark', viewport: { width: 390, height: 844 }, theme: 'dark', panes: null },
  ];
  for (const s of shots) {
    const ctx = await browser.newContext({
      viewport: s.viewport, deviceScaleFactor: 2, colorScheme: s.theme,
    });
    const page = await ctx.newPage();
    await mockCto(page, s.panes);
    await page.goto(CTO_URL, { waitUntil: 'load' });
    await page.waitForSelector('.route-cto', { timeout: 15000 });
    await page.evaluate((theme) => {
      document.documentElement.setAttribute('data-theme', theme);
    }, s.theme);
    await page.waitForTimeout(700);
    await page.screenshot({ path: `${out}/${s.name}.png` });
    await ctx.close();
    console.log(`${s.name}.png`);
  }
} finally {
  await browser.close();
}
