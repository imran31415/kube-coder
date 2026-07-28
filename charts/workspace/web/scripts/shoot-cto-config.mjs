#!/usr/bin/env node
/**
 * Screenshots for the AI CTO's per-project assistant configuration (#483).
 *
 * Drives /cto with a mocked multi-provider config and two projects — one with
 * stored defaults (claude/opus/xhigh), one inheriting the workspace default —
 * then opens the topbar gear so the whole control is visible in the still.
 * `claude` and `codex` carry the five canonical effort levels and `ante`
 * carries none, so the stills also document the hide-when-unsupported rule.
 *
 * Usage: node scripts/shoot-cto-config.mjs [output-dir]
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
const EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'];

const CONFIG = {
  enabled: true,
  ctoEnabled: true,
  defaultAssistant: 'claude',
  workdir: '/home/dev',
  readOnly: false,
  commands: [],
  assistants: [
    { id: 'claude', label: 'Claude Code', default: true,
      models: ['default', 'opus', 'sonnet', 'haiku'],
      efforts: EFFORTS, effort: 'high', effortCap: 'xhigh' },
    { id: 'codex', label: 'Codex', models: [],
      efforts: EFFORTS, effort: 'high', effortCap: 'xhigh' },
    { id: 'ante', label: 'Ante CLI', models: [], efforts: [] },
  ],
};

const PROJECTS = [
  {
    id: 'kube-coder', name: 'kube-coder', workdirs: ['/home/dev/kube-coder'],
    repo: 'imran31415/kube-coder', memory_namespace: 'project.kube-coder',
    status: 'active', north_star: 'Ship the AI CTO to every workspace',
    default_assistant: 'claude', default_model: 'opus', default_effort: 'xhigh',
    last_seen_at: now - 90000, created_at: now - 900000, updated_at: now - 200,
    pulse: { running: 2, waiting: 1, last_activity_at: now - 200 },
  },
  {
    id: 'hosted', name: 'hosted', workdirs: ['/home/dev/hosted'],
    repo: 'imran31415/kubecoder-hosted', memory_namespace: 'project.hosted',
    status: 'active', north_star: 'KubeCoder.com landing + waitlist',
    default_assistant: '', default_model: '', default_effort: '',
    last_seen_at: now - 400000, created_at: now - 800000, updated_at: now - 60000,
    pulse: { running: 0, waiting: 0, last_activity_at: now - 60000 },
  },
];

const BRIEF = {
  project: PROJECTS[0],
  tasks: { running: 2, waiting: 1, total: 7, recent: [] },
  goals: [
    { namespace: 'project.kube-coder.goals', key: 'ga',
      value: 'Reach GA with the /cto page + feed', tags: ['goal'],
      importance: 0.9, updated_at: now - 8000 },
  ],
  decisions: [
    { namespace: 'project.kube-coder.decisions', key: 'effort',
      value: 'Each project pins its own provider, model and effort',
      tags: ['decision'], importance: 0.8, updated_at: now - 40 },
  ],
  memories: [],
  git: [{ workdir: '/home/dev/kube-coder', branch: 'kc/issue-483', exists: true }],
  triggers: [],
  counts: { goals: 1, decisions: 1, memories: 0, tasks: 7 },
  brief_markdown: '# kube-coder — project brief',
};

async function mock(page, { threads = [] } = {}) {
  const json = (r, body) =>
    r.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
  await page.route('**/api/projects/_discover', (r) => json(r, { candidates: [], registered: [] }));
  await page.route('**/api/projects/kube-coder/brief', (r) => json(r, BRIEF));
  await page.route(/\/api\/projects\/[^/]+$/, (r) => json(r, PROJECTS[0]));
  await page.route('**/api/projects', (r) => json(r, { projects: PROJECTS }));
  await page.route('**/api/hypervisor/config', (r) => json(r, CONFIG));
  await page.route('**/api/hypervisor/threads**', (r) =>
    json(r, { threads, thread: threads[0], events: [], source: 'capture' }));
  await page.route('**/api/workspace/dirs', (r) => json(r, { dirs: [] }));
  await page.route('**/api/tasks', (r) => json(r, { tasks: [] }));
  // No live workspace tasks: their chip row is wide enough to overflow a phone
  // viewport on its own, which would hide the very control we're capturing.
  await page.route('**/api/claude/tasks', (r) => json(r, { tasks: [] }));
  await page.route('**/api/mode', (r) =>
    json(r, { readOnly: false, authed: true, authMode: 'basic', ctoEnabled: true }));
  await page.route('**/api/events', (r) =>
    r.fulfill({ contentType: 'text/event-stream', body: 'event: ready\ndata: {}\n\n' }));
  await page.addInitScript(`Date.now = () => ${(now + 5) * 1000};`);
  await page.addInitScript(() => {
    localStorage.setItem('kc.cto.lastProject', 'kube-coder');
    localStorage.setItem('kc.onboardingDone', 'true');
  });
}

/** Render a <select> as an inline listbox so its options show in a still. */
async function expand(page, sel) {
  await page.evaluate((s) => {
    const el = document.querySelector(s);
    if (el) {
      el.size = el.options.length;
      el.style.position = 'relative';
      el.style.zIndex = '50';
    }
  }, sel);
  await page.waitForTimeout(200);
}

const VIEWPORTS = [
  { name: 'desktop', width: 1280, height: 800 },
  { name: 'mobile', width: 390, height: 844 },
];

const browser = await chromium.launch({ executablePath: CHROMIUM, headless: true });
try {
  for (const theme of ['dark', 'light']) {
    for (const vp of VIEWPORTS) {
      // ── /cto: the per-project assistant config popover (#483) ───────────
      {
        const ctx = await browser.newContext({
          viewport: { width: vp.width, height: vp.height },
          deviceScaleFactor: 2, colorScheme: theme,
        });
        const page = await ctx.newPage();
        await mock(page);
        await page.goto(`${BASE}/next/cto`, { waitUntil: 'load' });
        await page.waitForSelector('.route-cto', { timeout: 15000 });
        await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme);
        await page.waitForTimeout(700);
        await page.screenshot({ path: `${out}/cto-config-closed-${vp.name}-${theme}.png` });
        console.log(`✓ cto-config-closed-${vp.name}-${theme}.png`);

        await page.click('.cto-config-trigger');
        await page.waitForSelector('.cto-config', { timeout: 5000 });
        await page.waitForTimeout(300);
        await page.screenshot({ path: `${out}/cto-config-open-${vp.name}-${theme}.png` });
        console.log(`✓ cto-config-open-${vp.name}-${theme}.png`);

        await expand(page, '#cto-effort');
        await page.screenshot({ path: `${out}/cto-config-effort-${vp.name}-${theme}.png` });
        console.log(`✓ cto-config-effort-${vp.name}-${theme}.png`);
        await ctx.close();
      }

    }
  }
} finally {
  await browser.close();
}
console.log(`\nSaved to ${out}`);
