#!/usr/bin/env node
/**
 * Focused screenshots of the AI CTO page (#466) — the projects rail with pulse
 * counts, the reused CTO chat with its deterministic welcome + starter chips,
 * and the deterministic brief panel (north star, goals, decision log, tasks,
 * git). Mocks the projects/brief/hypervisor endpoints so the page renders its
 * full active-workbench state with zero backend.
 *
 * Usage: node scripts/shoot-cto.mjs [output-dir]
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
    pulse: { running: 0, waiting: 0, last_activity_at: now - 60000 },
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
      { task_id: 't_rail', status: 'running', prompt: 'Build the projects rail component', workdir: '/home/dev/kube-coder', assistant: 'claude', last_activity_at: now - 200 },
      { task_id: 't_brief', status: 'running', prompt: 'Wire the brief panel to /brief', workdir: '/home/dev/kube-coder', assistant: 'claude', last_activity_at: now - 900 },
      { task_id: 't_mcp', status: 'waiting-for-input', prompt: 'Add the update_project MCP tool — confirm scope', workdir: '/home/dev/kube-coder', assistant: 'codex', last_activity_at: now - 1800 },
      { task_id: 't_tests', status: 'completed', prompt: 'Vitest for the CTO store', workdir: '/home/dev/kube-coder', assistant: 'claude', last_activity_at: now - 5400 },
    ],
  },
  goals: [
    { namespace: 'project.kube-coder.goals', key: 'ga', value: 'Reach GA with the /cto page + feed', tags: ['goal'], importance: 0.9, updated_at: now - 8000 },
    { namespace: 'project.kube-coder.goals', key: 'mobile', value: 'Native Expo parity for CTO + Feed', tags: ['goal'], importance: 0.7, updated_at: now - 90000 },
  ],
  decisions: [
    { namespace: 'project.kube-coder.decisions', key: 'sse', value: 'SSE over websockets — matches the rest of the stack', tags: ['decision'], importance: 0.8, updated_at: now - 40 },
    { namespace: 'project.kube-coder.decisions', key: 'no-fork', value: 'Reuse the hypervisor Chat via a store context, do not fork it', tags: ['decision'], importance: 0.8, updated_at: now - 120000 },
    { namespace: 'project.kube-coder.decisions', key: 'zero-form', value: 'Zero-form onboarding: discovery auto-provisions, no New-project button', tags: ['decision'], importance: 0.7, updated_at: now - 300000 },
  ],
  memories: [
    { namespace: 'project.kube-coder', key: 'ci', value: 'Preflight: helm + python unittest + both SPA builds + vitest', tags: [], importance: 0.5, updated_at: now - 500000 },
  ],
  git: [{ workdir: '/home/dev/kube-coder', branch: 'feat/466-cto-page', exists: true }],
  triggers: [{ kind: 'cron', id: 'weekly-review', workdir: '/home/dev/kube-coder', schedule: '0 9 * * 1' }],
  counts: { goals: 2, decisions: 3, memories: 1, tasks: 7 },
  brief_markdown: '# kube-coder — project brief',
};

const CONFIG = {
  enabled: true,
  assistants: [{ id: 'claude', label: 'Claude', model: 'opus-4.8', models: ['opus-4.8'] }],
  defaultAssistant: 'claude',
  workdir: '/home/dev',
  readOnly: false,
};

async function mockCto(page) {
  const json = (r, body) => r.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
  await page.route('**/api/projects/_discover', (r) => json(r, { candidates: [], registered: [] }));
  await page.route('**/api/projects/kube-coder/brief', (r) => json(r, BRIEF));
  await page.route(/\/api\/projects\/[^/]+$/, (r) => json(r, PROJECTS[0]));
  await page.route('**/api/projects', (r) => json(r, { projects: PROJECTS }));
  await page.route('**/api/hypervisor/config', (r) => json(r, CONFIG));
  await page.route('**/api/hypervisor/threads**', (r) => json(r, { threads: [] }));
  await page.route('**/api/workspace/dirs', (r) => json(r, { dirs: [] }));
  await page.route('**/api/mode', (r) => json(r, { readOnly: false, authed: true, authMode: 'basic' }));
  // Stub the SSE stream shut so a retrying EventSource doesn't stall load.
  await page.route('**/api/events', (r) =>
    r.fulfill({ contentType: 'text/event-stream', body: 'event: ready\ndata: {}\n\n' }),
  );
  await page.addInitScript(`Date.now = () => ${(now + 5) * 1000};`);
  // Seed the last-project so the returning-visit delta strip renders.
  await page.addInitScript(() => {
    localStorage.setItem('kc.cto.lastProject', 'kube-coder');
    localStorage.setItem('kc.onboardingDone', 'true');
  });
}

const browser = await chromium.launch({ executablePath: CHROMIUM, headless: true });
try {
  const shots = [
    { name: 'cto-desktop-dark', viewport: { width: 1280, height: 800 }, theme: 'dark' },
    { name: 'cto-desktop-light', viewport: { width: 1280, height: 800 }, theme: 'light' },
    { name: 'cto-mobile-dark', viewport: { width: 390, height: 844 }, theme: 'dark' },
  ];
  for (const s of shots) {
    const ctx = await browser.newContext({
      viewport: s.viewport, deviceScaleFactor: 2, colorScheme: s.theme,
    });
    const page = await ctx.newPage();
    await mockCto(page);
    await page.goto(`${BASE}/cto`, { waitUntil: 'load' });
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
