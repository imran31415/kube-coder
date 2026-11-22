/**
 * Capture the open NavDrawer from the mock web export: as it opens on Chat
 * (the home screen — Chat + a collapsed Workspace), then with Workspace open
 * (its Mission Control / Machine / Knowledge sections and the pinned tail).
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

const browser = await chromium.launch({ executablePath: process.env.KC_CHROMIUM || undefined });
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
await page.screenshot({ path: path.join(outDir, 'mobile-app-drawer-home.png') });
console.log(`✓ ${outDir}/mobile-app-drawer-home.png`);
await page.getByLabel('Show Workspace').first().click();
await sleep(400);
await page.screenshot({ path: path.join(outDir, 'mobile-app-drawer-workspace.png') });
console.log(`✓ ${outDir}/mobile-app-drawer-workspace.png`);
await browser.close();
server.close();
