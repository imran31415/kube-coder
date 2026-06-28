/**
 * Capture store-ready screenshots of the app (mock/demo build) at the exact
 * pixel dimensions Apple App Store Connect and Google Play require.
 *
 * Pipeline: serve the exported web build (mobile/dist) → drive it with
 * Playwright/Chromium at each device's CSS viewport × deviceScaleFactor so the
 * PNG lands at the precise store pixel size → walk the app's screens.
 *
 * Prereq:  EXPO_PUBLIC_MOCK=1 npx expo export --platform web   (writes dist/)
 * Run:     node scripts/screenshots.mjs
 * Output:  ../ios-assets/<device>/*.png  and  ../android-assets/<device>/*.png
 */
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { mkdir, rm } from 'node:fs/promises';
import handler from 'serve-handler';
import { chromium } from 'playwright';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, '..');
const distDir = path.join(root, 'dist');
const iosDir = path.join(root, '..', 'ios-assets');
const androidDir = path.join(root, '..', 'android-assets');

// device CSS viewport × scale = final PNG pixels (the store-required size)
const DEVICES = [
  { platform: 'ios', name: 'iphone-6.7', label: 'iPhone 6.7" (1290×2796)', w: 430, h: 932, scale: 3 },
  { platform: 'ios', name: 'iphone-6.5', label: 'iPhone 6.5" (1242×2688)', w: 414, h: 896, scale: 3 },
  { platform: 'ios', name: 'ipad-12.9', label: 'iPad 12.9" (2048×2732)', w: 1024, h: 1366, scale: 2 },
  { platform: 'android', name: 'phone', label: 'Android phone (1080×1920)', w: 360, h: 640, scale: 3 },
  { platform: 'android', name: 'tablet-10', label: 'Android 10" tablet (1600×2560)', w: 800, h: 1280, scale: 2 },
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function startServer() {
  const server = http.createServer((req, res) =>
    handler(req, res, { public: distDir, rewrites: [{ source: '**', destination: '/index.html' }] }),
  );
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => resolve({ server, port: server.address().port }));
  });
}

/** Click a bottom-tab by its label (tab bar is the last match in the DOM). */
async function tab(page, name) {
  await page.getByText(name, { exact: true }).last().click();
  await sleep(700);
}

async function shots(page, device, outDir) {
  const shot = async (file) => {
    await sleep(450);
    await page.screenshot({ path: path.join(outDir, file) });
    console.log(`    ✓ ${device.name}/${file}`);
  };

  // 1) Tasks list
  await page.getByText('Tasks', { exact: true }).first().waitFor({ timeout: 15000 });
  await sleep(600);
  await shot('01-tasks.png');

  // 2) Task detail (open the first, running task)
  await page.getByText('Add a /healthz endpoint to server.py and a unit test for it').click();
  await sleep(900);
  await shot('02-task-detail.png');

  // back to the list via the Tasks tab (resets the stack)
  await tab(page, 'Tasks');

  // 3) New task form
  await page.getByText('+ New', { exact: true }).click();
  await sleep(800);
  await shot('03-new-task.png');
  await tab(page, 'Tasks');

  // 4) Memory
  await tab(page, 'Memory');
  await shot('04-memory.png');

  // 5) Metrics
  await tab(page, 'Metrics');
  await shot('05-metrics.png');

  await tab(page, 'Tasks');
}

async function main() {
  const { server, port } = await startServer();
  const url = `http://127.0.0.1:${port}/`;
  console.log(`Serving ${distDir} at ${url}`);
  const browser = await chromium.launch();
  try {
    for (const device of DEVICES) {
      const base = device.platform === 'ios' ? iosDir : androidDir;
      const outDir = path.join(base, device.name);
      await rm(outDir, { recursive: true, force: true });
      await mkdir(outDir, { recursive: true });
      console.log(`\n${device.label}  →  ${path.relative(root, outDir)}`);
      const ctx = await browser.newContext({
        viewport: { width: device.w, height: device.h },
        deviceScaleFactor: device.scale,
        isMobile: device.platform === 'android' || device.name.startsWith('iphone'),
        colorScheme: 'dark',
      });
      const page = await ctx.newPage();
      await page.goto(url, { waitUntil: 'networkidle' });
      await shots(page, device, outDir);
      await ctx.close();
    }
  } finally {
    await browser.close();
    server.close();
  }
  console.log('\nAll screenshots captured.');
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
