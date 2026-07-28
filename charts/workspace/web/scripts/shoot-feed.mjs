#!/usr/bin/env node
/**
 * Focused screenshots of the Feed page (#470) — the day-grouped single column,
 * kind rules, filter chips, waiting item, and action chips incl. "Discuss with
 * CTO". Mocks /api/feed so the page renders its full active state with no
 * backend.
 *
 * Usage: node scripts/shoot-feed.mjs [output-dir]
 */
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromiumPath } from './chromium-path.mjs';

const out = resolve(process.argv[2] || '/home/dev/screenshots');
mkdirSync(out, { recursive: true });

const CHROMIUM = chromiumPath();
const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:7070';
const now = 1_752_300_000;
const day = 86400;

const ITEMS = [
  {
    id: 'fd_1', ts: now - 200, kind: 'briefing', title: 'Morning briefing — the release is the bottleneck',
    body_md: 'Two PRs are green and waiting on review; the feed backend is the critical path. Suggest reviewing #480 first.',
    source: 'agent:th_cto', project_id: 'kube-coder',
    links: [{ label: 'Open thread', ref: 'thread:th_cto' }], waiting: false, read: false,
  },
  {
    id: 'fd_2', ts: now - 1800, kind: 'news', title: 'brace-expansion 5.0.8 advisory affects minimatch < 10',
    body_md: 'A dep-scout run flagged `GHSA-mh99-v99m-4gvg`. Your lockfile pins minimatch ^10, so **no action needed** — noted for the record.',
    source: 'cron:dep-scout', project_id: 'kube-coder',
    links: [{ label: 'Advisory', href: 'https://github.com/advisories/GHSA-mh99-v99m-4gvg' }],
    waiting: false, read: false,
  },
  {
    id: 'fd_3', ts: now - 5400, kind: 'activity', title: 'Task waiting on you: add the update_project MCP tool',
    body_md: '', source: 'system:task', project_id: 'kube-coder',
    links: [{ label: 'Open task', ref: 'task:t_mcp' }], waiting: true, read: true,
  },
  {
    id: 'fd_4', ts: now - 9000, kind: 'decision', title: 'SSE over websockets — matches the rest of the stack',
    body_md: '', source: 'system:memory', project_id: 'kube-coder',
    links: [{ label: 'View decision', ref: 'memory:project.kube-coder.decisions/sse' }],
    waiting: false, read: true,
  },
  {
    id: 'fd_5', ts: now - day - 1000, kind: 'activity', title: 'Task finished: ship the projects rail component',
    body_md: '', source: 'system:task', project_id: 'kube-coder',
    links: [{ label: 'Open task', ref: 'task:t_rail' }], waiting: false, read: true,
  },
];

const PROJECTS = [
  { id: 'kube-coder', name: 'kube-coder', workdirs: ['/home/dev/kube-coder'], repo: 'o/kc',
    memory_namespace: 'project.kube-coder', status: 'active', north_star: '', last_seen_at: null,
    created_at: now - 9e5, updated_at: now, pulse: { running: 2, waiting: 1, last_activity_at: now } },
];

async function mockFeed(page) {
  const json = (r, body) => r.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
  await page.route('**/api/feed/unread_count', (r) => json(r, { count: 2 }));
  await page.route(/\/api\/feed(\?.*)?$/, (r) => json(r, { items: ITEMS }));
  await page.route('**/api/projects', (r) => json(r, { projects: PROJECTS }));
  await page.route('**/api/mode', (r) => json(r, { readOnly: false, authed: true, authMode: 'basic', ctoEnabled: true }));
  await page.route('**/api/events', (r) =>
    r.fulfill({ contentType: 'text/event-stream', body: 'event: ready\ndata: {}\n\n' }));
  await page.addInitScript(`Date.now = () => ${(now + 5) * 1000};`);
  await page.addInitScript(() => localStorage.setItem('kc.onboardingDone', 'true'));
}

/** Same mocks, but /api/feed never answers — the first-load skeletons (#510)
 *  stay on screen for the shot instead of flashing past. */
async function mockFeedStalled(page) {
  await mockFeed(page);
  await page.route(/\/api\/feed(\?.*)?$/, () => { /* hang */ });
}

const browser = await chromium.launch({ executablePath: CHROMIUM, headless: true });
try {
  const shots = [
    { name: 'feed-desktop-dark', viewport: { width: 1280, height: 900 }, theme: 'dark' },
    { name: 'feed-desktop-light', viewport: { width: 1280, height: 900 }, theme: 'light' },
    { name: 'feed-mobile-dark', viewport: { width: 390, height: 844 }, theme: 'dark' },
    { name: 'feed-mobile-light', viewport: { width: 390, height: 844 }, theme: 'light' },
    { name: 'feed-loading-desktop-dark', viewport: { width: 1280, height: 900 }, theme: 'dark', stalled: true },
    { name: 'feed-loading-desktop-light', viewport: { width: 1280, height: 900 }, theme: 'light', stalled: true },
    { name: 'feed-loading-mobile-dark', viewport: { width: 390, height: 844 }, theme: 'dark', stalled: true },
  ];
  for (const s of shots) {
    const ctx = await browser.newContext({ viewport: s.viewport, deviceScaleFactor: 2, colorScheme: s.theme });
    const page = await ctx.newPage();
    if (s.stalled) await mockFeedStalled(page);
    else await mockFeed(page);
    await page.goto(`${BASE}/feed`, { waitUntil: 'load' });
    await page.waitForSelector('.route-feed', { timeout: 15000 });
    await page.evaluate((theme) => document.documentElement.setAttribute('data-theme', theme), s.theme);
    await page.waitForTimeout(600);
    await page.screenshot({ path: `${out}/${s.name}.png` });
    await ctx.close();
    console.log(`${s.name}.png`);
  }
} finally {
  await browser.close();
}
