/**
 * Screenshot the task-detail composer with the issue #179 image-attach UI:
 * the attach-image button plus an uploaded thumbnail chip. Runs against the
 * mock web export (EXPO_PUBLIC_MOCK=1 expo export --platform web → dist/).
 *
 * On web, expo-image-picker opens a native <input type=file>; we answer the
 * file chooser with a small PNG, and the mock uploadTaskImage resolves a fake
 * path so the chip flips to "ready".
 *
 * Prereq:  EXPO_PUBLIC_MOCK=1 npx expo export --platform web
 * Run:     KC_CHROMIUM=/path/to/chrome node scripts/shoot-composer.mjs
 */
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { mkdirSync } from 'node:fs';
import handler from 'serve-handler';
import { chromium } from 'playwright';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, '..');
const distDir = path.join(root, 'dist');
const outDir = path.resolve(process.env.KC_OUT || path.join(root, '..', 'docs', 'screenshots'));
mkdirSync(outDir, { recursive: true });

const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAEklEQVR4nGP8z8Dwn4EIwDiqEAQAGAsE9wEK3wAAAABJRU5ErkJggg==',
  'base64',
);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const server = http.createServer((req, res) =>
  handler(req, res, { public: distDir, rewrites: [{ source: '**', destination: '/index.html' }] }));
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const port = server.address().port;
const url = `http://127.0.0.1:${port}/`;

const browser = await chromium.launch({
  executablePath: process.env.KC_CHROMIUM || undefined,
});
try {
  const ctx = await browser.newContext({ viewport: { width: 400, height: 860 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  await page.goto(url, { waitUntil: 'networkidle' });

  // The demo build boots on the Desktop home (#196); open a task straight from
  // its Activity list to land on the TaskDetail composer.
  await page.getByPlaceholder('Describe a build to run…').waitFor({ timeout: 20000 });
  await sleep(600);
  await page.getByText('Add a /healthz endpoint to server.py and a unit test for it').click();
  await sleep(900);

  await page.getByPlaceholder('Send a follow-up…').fill('Match this mockup — mind the spacing');

  // Answer the file chooser expo-image-picker opens on web.
  page.on('filechooser', (fc) => {
    void fc.setFiles({ name: 'mockup.png', mimeType: 'image/png', buffer: PNG });
  });
  await page.getByLabel('Attach image').click();
  await sleep(1200);

  await page.screenshot({ path: path.join(outDir, 'mobile-image-attach.png') });
  console.log('✓ mobile-image-attach.png');
  await ctx.close();
} finally {
  await browser.close();
  server.close();
}
