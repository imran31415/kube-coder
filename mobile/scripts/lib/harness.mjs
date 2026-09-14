/**
 * Shared harness for the local Playwright gates (`check-nav`, `check-board`).
 *
 * Both gates need the same four things: a static server over `dist/`, a phone-
 * sized page, a way to ask whether an element is genuinely hit-testable, and a
 * way to wait for one. They had divergent copies — `check-nav.mjs` had no
 * `waitVisible` at all and leaned on fixed sleeps instead, which is how a slow
 * machine turns a passing gate into a flaky one.
 *
 * `screenshots.mjs` deliberately keeps its own copy: it writes store assets,
 * and there is no reason to put that on the same blast radius as a check.
 *
 * WHY hit-testing at all: react-native-web leaves hidden tab screens in the
 * DOM. `toBeVisible()` is satisfied by an element that is painted underneath
 * another screen, so every assertion here goes through elementFromPoint.
 */
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import handler from 'serve-handler';
import { chromium } from 'playwright';

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** iOS HIG's minimum touch target, in CSS px (== pt at dsf 2). */
export const MIN_TOUCH = 44;

/** iPhone 14-ish. Matches check-nav's viewport so the two gates agree. */
export const PHONE = {
  viewport: { width: 390, height: 844 },
  deviceScaleFactor: 2,
  isMobile: true,
  hasTouch: true,
};

/** Serve the Expo web export. Returns { port, close }. */
export async function serveDist(scriptUrl) {
  const distDir = path.resolve(path.dirname(fileURLToPath(scriptUrl)), '..', 'dist');
  const server = http.createServer((req, res) =>
    handler(req, res, {
      public: distDir,
      rewrites: [{ source: '**', destination: '/index.html' }],
    }),
  );
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  return { port: server.address().port, distDir, close: () => server.close() };
}

/** Playwright does not always find a browser on Windows; KC_CHROMIUM names one
 *  explicitly, the same escape hatch shoot-composer.mjs uses. */
export function launchBrowser() {
  return chromium.launch({ executablePath: process.env.KC_CHROMIUM || undefined });
}

/** Runs IN THE PAGE. True when this element is what a tap at its centre would
 *  actually reach. */
export const isTopmost = (el) => {
  const r = el.getBoundingClientRect();
  if (!r.width || !r.height) return false;
  const t = document.elementFromPoint(
    Math.max(0, Math.min(r.x + r.width / 2, window.innerWidth - 1)),
    Math.max(0, Math.min(r.y + r.height / 2, window.innerHeight - 1)),
  );
  return t === el || el.contains(t) || (t && t.contains(el));
};

/** Index of the first hit-testable match, or -1. */
export async function visibleNth(loc) {
  const n = await loc.count();
  for (let i = 0; i < n; i++) {
    if (await loc.nth(i).evaluate(isTopmost).catch(() => false)) return i;
  }
  return -1;
}

/** Poll until one match is hit-testable. Returns that locator. */
export async function waitVisible(loc, what, timeout = 15000) {
  const start = Date.now();
  for (;;) {
    const i = await visibleNth(loc);
    if (i >= 0) return loc.nth(i);
    if (Date.now() - start > timeout) throw new Error(`timed out waiting for: ${what}`);
    await sleep(150);
  }
}

export async function clickVisible(loc, what) {
  const el = await waitVisible(loc, what);
  await el.click();
}

/** A fresh phone-sized page on the app's home route. */
export async function freshPage(browser, port, settleMs = 1400) {
  const page = await browser.newPage(PHONE);
  await page.goto(`http://127.0.0.1:${port}/`);
  await sleep(settleMs);
  return page;
}

/** Open the drawer and jump to a destination by its aria-label. The list
 *  scrolls, so the entry is brought into view before the hit-test. */
export async function openDrawerAndGo(page, item) {
  await clickVisible(page.getByLabel('Open menu'), 'menu button');
  await sleep(450);
  const entry = page.getByLabel(`Go to ${item}`);
  await entry.first().scrollIntoViewIfNeeded();
  await clickVisible(entry, `drawer item ${item}`);
  await sleep(900);
}

/** A tiny PASS/FAIL tally, so each gate's `main` stays about the assertions. */
export function makeReport() {
  let failures = 0;
  return {
    check(ok, label, detail = '') {
      if (!ok) failures++;
      console.log(`${ok ? 'PASS' : 'FAIL'} ${label}${detail ? `  ${detail}` : ''}`);
      return ok;
    },
    fail(label, detail = '') {
      failures++;
      console.log(`FAIL ${label}${detail ? `  ${detail}` : ''}`);
    },
    get failures() {
      return failures;
    },
    finish() {
      console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILURES`);
      return failures === 0 ? 0 : 1;
    },
  };
}
