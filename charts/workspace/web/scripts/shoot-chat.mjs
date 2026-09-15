#!/usr/bin/env node
/**
 * Focused screenshots of Chat (#683) — the one chat surface, with the AI CTO
 * folded into it as a mode rather than a parallel page.
 *
 * Captures what the merge produced: a single thread list carrying CTO and
 * board threads beside plain chats (with mode badges and the All/Workspace/CTO
 * chip), the Mode picker in the sidebar, and the deterministic project brief
 * as the right-hand pane of any project-bound chat.
 *
 * Mocks the hypervisor/projects/brief endpoints so the surface renders its
 * full active state with zero backend.
 *
 * Usage: node scripts/shoot-chat.mjs [output-dir]
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
];

const BRIEF = {
  project: PROJECTS[0],
  tasks: {
    running: 2, waiting: 1, total: 7,
    recent: [
      { task_id: 't_merge', status: 'running', prompt: 'Fold the AI CTO into Chat', workdir: '/home/dev/kube-coder', assistant: 'claude', last_activity_at: now - 200 },
      { task_id: 't_brief', status: 'running', prompt: 'Mount the brief as a Chat pane', workdir: '/home/dev/kube-coder', assistant: 'claude', last_activity_at: now - 900 },
      { task_id: 't_mode', status: 'waiting-for-input', prompt: 'Mode picker — confirm the label', workdir: '/home/dev/kube-coder', assistant: 'codex', last_activity_at: now - 1800 },
    ],
  },
  goals: [
    { namespace: 'project.kube-coder.goals', key: 'one-surface', value: 'One chat surface, fewer routes', tags: ['goal'], importance: 0.9, updated_at: now - 8000 },
  ],
  decisions: [
    { namespace: 'project.kube-coder.decisions', key: 'creation-time', value: 'Mode is creation-time only — the preamble lands once, on turn 1', tags: ['decision'], importance: 0.8, updated_at: now - 40 },
    { namespace: 'project.kube-coder.decisions', key: 'any-project', value: 'The brief shows for ANY project-bound chat, not just CTO ones', tags: ['decision'], importance: 0.8, updated_at: now - 120000 },
  ],
  memories: [
    { namespace: 'project.kube-coder', key: 'ci', value: 'Preflight: helm + python unittest + both SPA builds + vitest', tags: [], importance: 0.5, updated_at: now - 500000 },
  ],
  git: [{ workdir: '/home/dev/kube-coder', branch: 'kc/issue-683-p3', exists: true }],
  counts: { goals: 1, decisions: 2, memories: 1, tasks: 7 },
  brief_markdown: '# kube-coder — project brief',
};

const THREADS = [
  { id: 'c_plan', title: 'Where should we spend Q3?', assistant: 'claude', model: 'opus-4.8', status: 'idle', persona: 'cto', project_id: 'kube-coder', workdir: '/home/dev/kube-coder', created_at: now - 7000, updated_at: now - 300 },
  { id: 'c_chart', title: 'Ship the chart change', assistant: 'claude', model: 'opus-4.8', status: 'running', persona: '', project_id: 'kube-coder', workdir: '/home/dev/kube-coder', created_at: now - 9000, updated_at: now - 120 },
  { id: 'c_board', title: 'Triage KC-214', assistant: 'claude', status: 'idle', persona: 'board', project_id: 'kube-coder', workdir: '/home/dev/kube-coder', created_at: now - 40000, updated_at: now - 20000 },
  { id: 'c_land', title: 'Landing page copy', assistant: 'codex', status: 'idle', persona: '', project_id: 'hosted', workdir: '/home/dev/hosted', created_at: now - 60000, updated_at: now - 50000 },
];

const EVENTS = [
  { seq: 1, ts: now - 320, role: 'user', type: 'message', text: 'Where should we spend Q3?' },
  { seq: 2, ts: now - 300, role: 'assistant', type: 'message', text: 'Three things are competing for the quarter. The AI CTO merge (#683) is the one that pays for itself twice — it removes a whole route and closes #663 by construction, so every later chat improvement lands once instead of twice.\n\nAfter that I would take the mobile parity work, then docs.' },
];

const CONFIG = {
  enabled: true,
  assistants: [
    { id: 'claude', label: 'Claude', model: 'opus-4.8', models: ['opus-4.8', 'sonnet-4.8'], efforts: ['low', 'medium', 'high', 'xhigh', 'max'], effort: 'high' },
    { id: 'codex', label: 'Codex', model: 'gpt-5', models: ['gpt-5'] },
  ],
  defaultAssistant: 'claude',
  workdir: '/home/dev',
  readOnly: false,
};

async function mockChat(page, opts = {}) {
  const json = (r, body) => r.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
  await page.route('**/api/projects/_discover', (r) => json(r, { candidates: [], registered: [] }));
  await page.route('**/api/projects/kube-coder/brief', (r) => json(r, BRIEF));
  await page.route(/\/api\/projects\/[^/]+$/, (r) => json(r, PROJECTS[0]));
  await page.route('**/api/projects', (r) => json(r, { projects: PROJECTS }));
  await page.route('**/api/hypervisor/config', (r) => json(r, CONFIG));
  await page.route(/\/api\/hypervisor\/threads\/[^/?]+/, (r) =>
    json(r, { thread: THREADS[0], events: EVENTS, source: 'session_log' }),
  );
  await page.route('**/api/hypervisor/threads**', (r) =>
    json(r, { threads: opts.threads ?? THREADS }),
  );
  await page.route('**/api/subscriptions', (r) =>
    json(r, { subscriptions: {}, claude_ready: true }),
  );
  await page.route('**/api/workspace/dirs', (r) => json(r, { dirs: [{ path: '/home/dev/kube-coder', label: 'kube-coder', is_git: true }] }));
  await page.route('**/api/tasks**', (r) => json(r, { tasks: [] }));
  await page.route('**/api/mode', (r) => json(r, { readOnly: false, authed: true, authMode: 'basic', ctoEnabled: true }));
  // Stub the SSE stream shut so a retrying EventSource doesn't stall load.
  await page.route('**/api/events', (r) =>
    r.fulfill({ contentType: 'text/event-stream', body: 'event: ready\ndata: {}\n\n' }),
  );
  await page.addInitScript(`Date.now = () => ${(now + 5) * 1000};`);
  await page.addInitScript(() => {
    localStorage.setItem('kc.onboardingDone', 'true');
    // The brief auto-collapses below 1200px; these shots are about the brief,
    // so record the explicit "expanded" choice a user on a wide screen makes.
    localStorage.setItem('kc.hvBriefCollapsed', '0');
    localStorage.setItem('kc.guide.hypervisor', 'collapsed');
  });
}

const browser = await chromium.launch({ executablePath: CHROMIUM, headless: true });
try {
  const shots = [
    { name: 'chat-brief-desktop-dark', path: '/hypervisor/c_plan', viewport: { width: 1680, height: 960 }, theme: 'dark' },
    { name: 'chat-brief-desktop-light', path: '/hypervisor/c_plan', viewport: { width: 1680, height: 960 }, theme: 'light' },
    { name: 'chat-brief-mobile-dark', path: '/hypervisor/c_plan', viewport: { width: 390, height: 844 }, theme: 'dark' },
    // The brief is a bottom sheet on a phone rather than a third column, so
    // open it — a shot of the closed sheet shows nothing about the brief.
    { name: 'chat-brief-sheet-mobile-dark', path: '/hypervisor/c_plan', viewport: { width: 390, height: 844 }, theme: 'dark', openBrief: true },
    // The AI CTO's front door, now reached by the /cto redirect rather than by
    // a page of its own: Chat, CTO mode pre-selected, its welcome in the
    // transcript's centring slot.
    { name: 'chat-cto-welcome-desktop-dark', path: '/cto', viewport: { width: 1680, height: 960 }, theme: 'dark', threads: [] },
    { name: 'chat-cto-welcome-desktop-light', path: '/cto', viewport: { width: 1680, height: 960 }, theme: 'light', threads: [] },
  ];
  for (const s of shots) {
    const ctx = await browser.newContext({
      viewport: s.viewport, deviceScaleFactor: 2, colorScheme: s.theme,
    });
    const page = await ctx.newPage();
    await mockChat(page, { threads: s.threads });
    await page.goto(`${BASE}${s.path}`, { waitUntil: 'load' });
    await page.waitForSelector('.route-hypervisor', { timeout: 15000 });
    await page.evaluate((theme) => {
      document.documentElement.setAttribute('data-theme', theme);
    }, s.theme);
    await page.waitForTimeout(900);
    if (s.openBrief) {
      await page.click('.hv-brief-toggle');
      await page.waitForSelector('.cto-brief', { timeout: 10000 });
      await page.waitForTimeout(600);
    }
    await page.screenshot({ path: `${out}/${s.name}.png` });
    await ctx.close();
    console.log(`${s.name}.png`);
  }
} finally {
  await browser.close();
}
