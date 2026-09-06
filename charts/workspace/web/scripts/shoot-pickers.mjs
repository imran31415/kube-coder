/**
 * Screenshots of the searchable pickers (SearchSelect) in the Chat toolbar,
 * open and filtered, at desktop and phone widths.
 *
 * The phone width is the one that matters: the toolbar sets `overflow-x: auto`
 * at <=390px, which clipped the first version's absolutely-positioned popover
 * down to its search box alone. Unit tests could not see it — this is how it
 * was found, and how a regression would be.
 *
 * Usage: node scripts/shoot-pickers.mjs [output-dir]
 */
import { chromium } from 'playwright-core';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromiumPath } from './chromium-path.mjs';

const out = resolve(process.argv[2] || '/home/dev/screenshots');
mkdirSync(out, { recursive: true });
const BASE = 'http://127.0.0.1:7070';

const CONFIG = {
  enabled: true, defaultAssistant: 'claude', workdir: '/home/dev', readOnly: false,
  assistants: [
    { id: 'claude', label: 'Claude', default: true, model: 'opus-4.8' },
    { id: 'codex', label: 'Codex', model: 'gpt-5.2' },
    { id: 'ante', label: 'Ante', free: true },
    { id: 'opencode-deepseek', label: 'OpenCode · DeepSeek' },
  ],
  models: ['opus-4.8', 'sonnet-4.8', 'haiku-4.5'],
};
// Enough of each to show why a rail/select stopped working.
const PROJECTS = [
  'kube-coder', 'Pool Hall', 'smush', 'kubecoder-hosted', 'LibreFang',
  'openai-agents-python', 'Nagme', 'strix', 'Board Processor', 'Mission Control',
].map((n) => ({ id: n.toLowerCase().replace(/\s+/g, '-'), name: n, status: 'active' }));
const DIRS = [
  '/home/dev/kube-coder', '/home/dev/smush', '/home/dev/hosted', '/home/dev/strix',
  '/home/dev/openai-agents-python', '/home/dev/librefang', '/home/dev/notes',
].map((p) => ({ path: p, label: p.split('/').pop(), is_git: true }));

async function mock(page) {
  const j = (body) => ({ contentType: 'application/json', body: JSON.stringify(body) });
  await page.route('**/api/hypervisor/config', (r) => r.fulfill(j(CONFIG)));
  await page.route('**/api/hypervisor/threads', (r) => r.fulfill(j({ threads: [] })));
  await page.route('**/api/projects**', (r) => r.fulfill(j({ projects: PROJECTS })));
  await page.route('**/api/workspace/dirs**', (r) => r.fulfill(j({ dirs: DIRS })));
}

const browser = await chromium.launch({ executablePath: chromiumPath(), headless: true });
try {
  for (const [name, viewport] of [['desktop', { width: 1280, height: 800 }], ['mobile', { width: 390, height: 844 }]]) {
    const ctx = await browser.newContext({ viewport, deviceScaleFactor: 2, colorScheme: 'dark' });
    await ctx.addInitScript(() => {
      localStorage.setItem('kc.onboardingDone', 'true');
      localStorage.setItem('kc.theme', 'dark');
    });
    const page = await ctx.newPage();
    await mock(page);
    await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.evaluate(() => {
      document.documentElement.setAttribute('data-theme', 'dark');
      window.history.pushState({}, '', '/hypervisor');
      window.dispatchEvent(new PopStateEvent('popstate'));
    });
    await page.waitForSelector('.hv-topbar', { timeout: 8000 });
    await page.waitForTimeout(700);
    await page.screenshot({ path: `${out}/pickers-${name}-closed.png` });
    console.log(`✓ pickers-${name}-closed.png`);

    const project = await page.$('[aria-label="Project for this chat"]');
    if (project) {
      await project.click();
      await page.waitForTimeout(300);
      await page.screenshot({ path: `${out}/pickers-${name}-open.png` });
      console.log(`✓ pickers-${name}-open.png`);
      await page.fill('[aria-label="Search Project for this chat"]', 'ho');
      await page.waitForTimeout(300);
      await page.screenshot({ path: `${out}/pickers-${name}-filtered.png` });
      console.log(`✓ pickers-${name}-filtered.png`);
    } else {
      console.log(`  ! project picker not found on ${name}`);
    }
    await ctx.close();
  }
} finally { await browser.close(); }
