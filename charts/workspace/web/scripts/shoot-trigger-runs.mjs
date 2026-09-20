#!/usr/bin/env node
/**
 * Screenshot the per-trigger run-history panel (#91).
 *
 * The panel is only interesting with entries in it, and the interesting entries
 * are the rejections — a bad HMAC, a replay, a full pod. Seeding those into a
 * live workspace would mean sending deliberately-malformed webhooks at it, so
 * the trigger list and the three ledgers are stubbed at the network layer with
 * `ctx.route` instead. Everything below the fetch — routing, the toggle, the
 * table, the outcome vocabulary — is the real SPA.
 *
 * Usage:  node scripts/shoot-trigger-runs.mjs [output-dir]
 */
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromiumPath } from './chromium-path.mjs';

const out = resolve(process.argv[2] || '/home/dev/screenshots');
mkdirSync(out, { recursive: true });
const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:7070';

const NOW = Math.floor(Date.now() / 1000);
const WEBHOOKS = [{
  id: 'github-pr-review', prompt_template: 'Review the PR titled {{ payload.pull_request.title }}',
  workdir: '/home/dev/kube-coder', created_at: NOW - 86400 * 9, secret_set: true,
}];
const CRONS = [{
  id: 'nightly-digest', schedule: '0 2 * * *', prompt_template: 'Summarise what changed today',
  workdir: '/home/dev/kube-coder', timezone: 'America/Los_Angeles', created_at: NOW - 86400 * 20,
}];

const WEBHOOK_RUNS = [
  { ts: NOW - 240, type: 'webhook', trigger_id: 'github-pr-review', outcome: 'spawned',
    task_id: '1789921572-3d2c0458', signature_verified: true, provider: 'github',
    source_ip: '10.244.0.9', forwarded_for: '140.82.115.4' },
  { ts: NOW - 1100, type: 'webhook', trigger_id: 'github-pr-review', outcome: 'rejected',
    reason: 'bad_signature', signature_verified: false, provider: 'github',
    source_ip: '10.244.0.9', forwarded_for: '203.0.113.77' },
  { ts: NOW - 2400, type: 'webhook', trigger_id: 'github-pr-review', outcome: 'rejected',
    reason: 'replay', signature_verified: true, provider: 'github',
    error: 'identical signed body already seen in the 5-minute window',
    source_ip: '10.244.0.9', forwarded_for: '140.82.115.4' },
  { ts: NOW - 5400, type: 'webhook', trigger_id: 'github-pr-review', outcome: 'rejected',
    reason: 'at_capacity', signature_verified: true, provider: 'github',
    error: 'too many running tasks (12/12)', source_ip: '10.244.0.9', forwarded_for: '140.82.115.4' },
  { ts: NOW - 9000, type: 'webhook', trigger_id: 'github-pr-review', outcome: 'spawned',
    task_id: '1789910011-7c1a4e02', manual: true, provider: 'github', source_ip: '127.0.0.1' },
];
const CRON_RUNS = [
  { ts: NOW - 3600 * 5, type: 'cron', trigger_id: 'nightly-digest', outcome: 'spawned',
    task_id: '1789900112-b41d9f77', signature_verified: true, source_ip: '10.244.1.31' },
  { ts: NOW - 3600 * 29, type: 'cron', trigger_id: 'nightly-digest', outcome: 'rejected',
    reason: 'bad_token', signature_verified: false, source_ip: '10.244.1.28' },
];

function json(route, body) {
  return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
}

async function stub(ctx) {
  await ctx.route('**/api/webhooks/*/runs*', (route) => {
    const url = new URL(route.request().url());
    const limit = Number(url.searchParams.get('limit') || 20);
    const offset = Number(url.searchParams.get('offset') || 0);
    return json(route, { runs: WEBHOOK_RUNS.slice(offset, offset + limit),
                         total: WEBHOOK_RUNS.length, limit, offset });
  });
  await ctx.route('**/api/crons/*/runs*', (route) =>
    json(route, { runs: CRON_RUNS, total: CRON_RUNS.length, limit: 20, offset: 0 }));
  await ctx.route('**/api/page-watches/*/runs*', (route) =>
    json(route, { runs: [], total: 0, limit: 20, offset: 0 }));
  await ctx.route('**/api/webhooks', (route) => json(route, { webhooks: WEBHOOKS }));
  await ctx.route('**/api/crons', (route) => json(route, { crons: CRONS }));
  await ctx.route('**/api/page-watches', (route) => json(route, { page_watches: [] }));
}

const viewports = [
  { name: 'desktop', width: 1280, height: 900, theme: 'dark' },
  { name: 'desktop', width: 1280, height: 900, theme: 'light' },
  { name: 'mobile', width: 390, height: 900, theme: 'dark' },
  { name: 'mobile', width: 390, height: 900, theme: 'light' },
];

const browser = await chromium.launch({ executablePath: chromiumPath(), headless: true });
try {
  for (const vp of viewports) {
    const ctx = await browser.newContext({
      viewport: { width: vp.width, height: vp.height },
      deviceScaleFactor: 2,
      colorScheme: vp.theme,
    });
    await ctx.addInitScript(() => localStorage.setItem('kc.onboardingDone', 'true'));
    await stub(ctx);
    const page = await ctx.newPage();
    await page.goto(`${BASE}/next/triggers`, { waitUntil: 'domcontentloaded', timeout: 15000 });
    await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), vp.theme);
    await page.waitForSelector('.trig-row', { timeout: 8000 });

    // Collapsed: the Runs affordance on every row, nothing fetched yet.
    await page.waitForTimeout(300);
    await page.screenshot({ path: `${out}/runs-collapsed-${vp.name}-${vp.theme}.png` });

    // Open the webhook's ledger — the rejection rows are the point.
    await page.getByRole('button', { name: 'Runs' }).first().click();
    await page.waitForSelector('.trig-runs-table', { timeout: 8000 });
    await page.waitForTimeout(350);
    await page.screenshot({ path: `${out}/runs-webhook-${vp.name}-${vp.theme}.png` });

    if (vp.name === 'desktop') {
      // And the cron's, showing a rotated fire token as the receiver sees it.
      await page.getByRole('button', { name: 'Runs' }).first().click();  // collapse
      await page.getByRole('button', { name: 'Runs' }).last().click();
      await page.waitForSelector('.trig-runs-table', { timeout: 8000 });
      await page.waitForTimeout(350);
      await page.screenshot({ path: `${out}/runs-cron-${vp.name}-${vp.theme}.png` });
    }
    console.log(`done ${vp.name}-${vp.theme}`);
    await ctx.close();
  }
} finally {
  await browser.close();
}
