/**
 * Screenshot the Chat screen's header + the Chat settings sheet it opens (the
 * Agent/Mode/Project/Model/Folder pickers, speak toggle and past chats moved
 * there from the header and the stack above the composer).
 *
 * Prereq:  node scripts/export-web.mjs            (mock web export → dist/)
 * Run:     node scripts/shoot-chat-settings.mjs
 * Env:     KC_DIST    export to serve (default dist/)
 *          KC_OUT     output folder  (default ../docs/screenshots)
 *          KC_PREFIX  file prefix    (default mobile-chat)
 *          KC_SHEET=0 skip opening the sheet (e.g. shooting a pre-change build)
 */
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { mkdirSync } from 'node:fs';
import handler from 'serve-handler';
import { chromium } from 'playwright';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, '..');
const distDir = path.resolve(process.env.KC_DIST || path.join(root, 'dist'));
const outDir = path.resolve(process.env.KC_OUT || path.join(root, '..', 'docs', 'screenshots'));
const prefix = process.env.KC_PREFIX || 'mobile-chat';
const openSheet = process.env.KC_SHEET !== '0';
mkdirSync(outDir, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Hidden screens stay mounted in the react-native-web DOM; click the copy that
// is actually topmost (same technique as screenshots.mjs / check-nav.mjs).
const isTopmost = (el) => {
  const r = el.getBoundingClientRect();
  if (!r.width || !r.height) return false;
  const t = document.elementFromPoint(
    Math.max(0, Math.min(r.x + r.width / 2, window.innerWidth - 1)),
    Math.max(0, Math.min(r.y + r.height / 2, window.innerHeight - 1)),
  );
  return t === el || el.contains(t) || (t && t.contains(el));
};
async function visible(loc, what, timeout = 15000) {
  const start = performance.now();
  for (;;) {
    const n = await loc.count();
    for (let i = 0; i < n; i++) {
      if (await loc.nth(i).evaluate(isTopmost).catch(() => false)) return loc.nth(i);
    }
    if (performance.now() - start > timeout) throw new Error(`timed out waiting for: ${what}`);
    await sleep(150);
  }
}

const server = http.createServer((req, res) =>
  handler(req, res, { public: distDir, rewrites: [{ source: '**', destination: '/index.html' }] }));
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const url = `http://127.0.0.1:${server.address().port}/`;

const browser = await chromium.launch({ executablePath: process.env.KC_CHROMIUM || undefined });
try {
  const ctx = await browser.newContext({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 2,
    isMobile: true,
    hasTouch: true,
    colorScheme: 'dark',
  });
  const page = await ctx.newPage();
  await page.goto(url, { waitUntil: 'networkidle' });
  await visible(page.getByLabel('Open menu'), 'menu button', 30000);
  await (await visible(page.getByLabel('Open menu'), 'menu button')).click();
  await sleep(450);
  const entry = page.getByLabel('Go to Chat');
  await entry.first().scrollIntoViewIfNeeded();
  await (await visible(entry, 'Chat entry')).click();
  await visible(page.getByPlaceholder('Message Kube-Coder…'), 'chat composer');
  await sleep(900);
  await page.screenshot({ path: path.join(outDir, `${prefix}.png`) });
  console.log(`✓ ${prefix}.png`);

  if (openSheet) {
    await (await visible(page.getByLabel(/^Chat settings/), 'settings button')).click();
    await visible(page.getByText('Speak replies'), 'settings sheet');
    await sleep(700);
    await page.screenshot({ path: path.join(outDir, `${prefix}-settings.png`) });
    console.log(`✓ ${prefix}-settings.png`);
  }
  await ctx.close();
} finally {
  await browser.close();
  server.close();
}
