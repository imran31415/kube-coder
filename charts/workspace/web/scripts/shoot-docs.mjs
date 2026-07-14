#!/usr/bin/env node
/**
 * Screenshot every docs page on desktop + mobile so we can audit
 * mobile overflow regressions while iterating on docs.css.
 *
 * Usage:
 *   node scripts/shoot-docs.mjs [output-dir]
 *
 * Env:
 *   SHOT_BASE — origin of the dashboard (default http://127.0.0.1:6080)
 *   KC_DEV_TOKEN — bearer token written to localStorage['kc.devToken']
 */
import { chromium } from 'playwright-core';
import { mkdirSync, readFileSync, existsSync } from 'node:fs';
import { resolve, basename, extname } from 'node:path';
import { chromiumPath } from './chromium-path.mjs';

const out = resolve(process.argv[2] || '/home/dev/screenshots/docs');
mkdirSync(out, { recursive: true });

const CHROMIUM = chromiumPath();

const BASE = process.env.SHOT_BASE || 'http://127.0.0.1:6080';
const TOKEN = process.env.KC_DEV_TOKEN || readFileSync('/home/dev/.claude-tasks/.api-token', 'utf8').trim();
// To verify CSS changes without rebuilding the image-baked /opt/dashboard-dist,
// serve fresh static assets from STATIC_DIR via a Playwright route handler
// while keeping API + HTML responses from BASE.
const STATIC_DIR = process.env.STATIC_DIR || '';

const pages = [
  'getting-started',
  'tasks-concepts',
  'tasks-assistants',
  'tasks-api',
  'memory-concepts',
  'memory-architecture',
  'triggers-webhooks',
  'files',
  'browser',
  'improvements-plan',
];

const viewports = [
  { name: 'desktop', width: 1280, height: 800 },
  { name: 'mobile',  width: 390,  height: 844 },
];

const browser = await chromium.launch({ executablePath: CHROMIUM, headless: true });

try {
  const MIME = {
    '.js': 'application/javascript',
    '.mjs': 'application/javascript',
    '.css': 'text/css',
    '.html': 'text/html',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.json': 'application/json',
  };

  for (const vp of viewports) {
    const ctx = await browser.newContext({
      viewport: { width: vp.width, height: vp.height },
      deviceScaleFactor: 1,
      colorScheme: 'dark',
    });
    const page = await ctx.newPage();
    if (STATIC_DIR) {
      // Intercept SPA assets + index.html so the fresh local build is served
      // instead of the image-baked one at /opt/dashboard-dist. API calls
      // still go to BASE so we don't have to wire a separate backend.
      const freshIndex = readFileSync(resolve(STATIC_DIR, 'index.html'), 'utf8');
      await page.route('**/*', async (route, req) => {
        const url = new URL(req.url());
        if (url.origin !== BASE) return route.continue();
        const path = url.pathname;
        if (path.startsWith('/api/') || path.startsWith('/oauth/api/')) {
          return route.continue();
        }
        if (path.startsWith('/next/assets/') || path === '/next/favicon.svg' || path === '/favicon.svg') {
          const rel = path.replace(/^\/next\//, '').replace(/^\//, '');
          const file = resolve(STATIC_DIR, rel);
          if (existsSync(file)) {
            const body = readFileSync(file);
            const type = MIME[extname(file)] || 'application/octet-stream';
            return route.fulfill({ status: 200, headers: { 'content-type': type }, body });
          }
        }
        // Anything else under the SPA — return the freshly built index.html
        // so the HTML references the new asset filenames.
        if (req.resourceType() === 'document') {
          return route.fulfill({ status: 200, headers: { 'content-type': 'text/html' }, body: freshIndex });
        }
        return route.continue();
      });
    }
    // Set token + dismiss onboarding before any docs page loads its data.
    await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' });
    await page.evaluate(([tok]) => {
      localStorage.setItem('kc.devToken', tok);
      localStorage.setItem('kc.onboardingDone', 'true');
      document.documentElement.setAttribute('data-theme', 'dark');
    }, [TOKEN]);

    for (const id of pages) {
      const url = `${BASE}/docs/${id}`;
      try {
        await page.goto(url, { waitUntil: 'networkidle', timeout: 15000 });
        await page.waitForSelector('.docs-article', { timeout: 8000 });
        await page.waitForTimeout(300);
        const file = `${id}-${vp.name}.png`;
        await page.screenshot({ path: `${out}/${file}`, fullPage: true });
        // Detect horizontal overflow on the body and on any element wider than viewport.
        const overflow = await page.evaluate((vw) => {
          const wide = [];
          document.querySelectorAll('.docs-article *').forEach((el) => {
            const w = el.scrollWidth;
            if (w > vw + 2) {
              wide.push({
                tag: el.tagName.toLowerCase(),
                cls: el.className,
                w,
                text: (el.textContent || '').slice(0, 60),
              });
            }
          });
          return {
            bodyScrollWidth: document.body.scrollWidth,
            viewportWidth: vw,
            wideElements: wide.slice(0, 10),
          };
        }, vp.width);
        const flag = overflow.bodyScrollWidth > vp.width + 2 ? '⚠ overflow' : 'ok';
        console.log(`✓ ${file} (${flag}, body=${overflow.bodyScrollWidth}px, ${overflow.wideElements.length} wide)`);
        if (overflow.wideElements.length) {
          for (const w of overflow.wideElements) {
            console.log(`    - <${w.tag}> ${w.w}px ${w.cls || ''}: ${w.text.replace(/\s+/g, ' ')}`);
          }
        }
      } catch (err) {
        console.error(`✗ ${id} ${vp.name}: ${err.message}`);
      }
    }
    await ctx.close();
  }
} finally {
  await browser.close();
}
console.log(`\nSaved to: ${out}`);
