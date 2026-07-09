#!/usr/bin/env node
/**
 * Screenshot the "workspace is restarting…" holding page (issue #184) in light
 * and dark. This is the exact static HTML the always-up holding-<user> nginx
 * backend serves via the ingress custom-error path during a self-serve update's
 * Recreate downtime. The markup is extracted from the chart ConfigMap so the
 * screenshot can't drift from what actually ships.
 *
 * Usage: KC_CHROMIUM=/path/to/chrome node scripts/shoot-holding-page.mjs [out-dir]
 */
import { chromium } from 'playwright-core';
import { mkdirSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const out = resolve(process.argv[2] || '/home/dev/kube-coder/docs/screenshots');
mkdirSync(out, { recursive: true });

const CHROMIUM =
  process.env.KC_CHROMIUM ||
  '/home/dev/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome';

// Pull the holding.html body straight out of the chart ConfigMap (indented
// under `holding.html: |`) so this renders the shipped markup verbatim.
const cm = readFileSync(
  '/home/dev/kube-coder/charts/workspace/templates/holding-configmap.yaml',
  'utf8',
);
const marker = '  holding.html: |\n';
const start = cm.indexOf(marker) + marker.length;
const end = cm.indexOf('\n{{- end }}', start);
const html = cm
  .slice(start, end)
  .split('\n')
  .map((l) => l.replace(/^ {4}/, '')) // strip the 4-space ConfigMap indent
  .join('\n');

const browser = await chromium.launch({ executablePath: CHROMIUM, headless: true });
try {
  for (const theme of ['dark', 'light']) {
    const ctx = await browser.newContext({
      viewport: { width: 1100, height: 720 },
      deviceScaleFactor: 2,
      colorScheme: theme,
    });
    const page = await ctx.newPage();
    // The page polls /health; keep it "down" so the holding state stays put.
    await page.route('**/health', (r) => r.fulfill({ status: 503, body: 'down' }));
    await page.setContent(html, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(400);
    const file = `holding-restarting-${theme}.png`;
    await page.screenshot({ path: `${out}/${file}`, fullPage: false });
    console.log(`✓ ${file}`);
    await ctx.close();
  }
} finally {
  await browser.close();
}
