/**
 * Capture the open NavDrawer (categorized nav, #267) from the mock web export.
 *
 * Prereq:  EXPO_PUBLIC_MOCK=1 npx expo export --platform web   (writes dist/)
 * Run:     node scripts/shoot-drawer.mjs [output-dir]
 */
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { mkdir } from 'node:fs/promises';
import handler from 'serve-handler';
import { chromium } from 'playwright';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const distDir = path.resolve(__dirname, '..', 'dist');
const outDir = path.resolve(process.argv[2] || '/tmp/drawer-shots');
await mkdir(outDir, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const server = http.createServer((req, res) =>
  handler(req, res, { public: distDir, rewrites: [{ source: '**', destination: '/index.html' }] }),
);
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const port = server.address().port;

const browser = await chromium.launch();
const page = await browser.newPage({
  viewport: { width: 390, height: 844 },
  deviceScaleFactor: 2,
  isMobile: true,
  hasTouch: true,
});
await page.goto(`http://127.0.0.1:${port}/`);
await sleep(1600);
await page.getByLabel('Open menu').first().click();
await sleep(600);
await page.screenshot({ path: path.join(outDir, 'mobile-app-drawer-grouped.png') });
console.log(`✓ ${outDir}/mobile-app-drawer-grouped.png`);
await browser.close();
server.close();
