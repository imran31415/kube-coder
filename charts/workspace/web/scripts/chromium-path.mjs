/**
 * Resolve the Playwright Chromium binary for the screenshot scripts.
 *
 * Playwright pins the browser to a revision that changes with the package
 * version, and the install cache lives under whichever HOME ran
 * `playwright install` (here that's sometimes /home/dev, sometimes the
 * ephemeral /home/ubuntu). Hardcoding a path like
 * `/home/ubuntu/.cache/ms-playwright/chromium-1224/...` therefore rots the
 * moment the version or home dir drifts — which is exactly what broke
 * shoot.mjs. Discover it instead.
 *
 * Resolution order:
 *   1. $KC_CHROMIUM — explicit override (must exist).
 *   2. Highest-revision chrome binary under a "chromium-<rev>" cache dir,
 *      searched across every known ms-playwright cache root
 *      ($PLAYWRIGHT_BROWSERS_PATH, $HOME, and both /home/dev and /home/ubuntu).
 * Throws with an actionable install hint if none is found.
 */
import { readdirSync, existsSync } from 'node:fs';
import { join } from 'node:path';

export function chromiumPath() {
  const override = process.env.KC_CHROMIUM;
  if (override) {
    if (existsSync(override)) return override;
    throw new Error(`KC_CHROMIUM is set but does not exist: ${override}`);
  }

  const roots = [
    process.env.PLAYWRIGHT_BROWSERS_PATH,
    process.env.HOME && join(process.env.HOME, '.cache', 'ms-playwright'),
    '/home/dev/.cache/ms-playwright',
    '/home/ubuntu/.cache/ms-playwright',
  ].filter(Boolean);

  const found = [];
  for (const root of roots) {
    let entries;
    try { entries = readdirSync(root); } catch { continue; }
    for (const name of entries) {
      // Only the full browser (`chromium-<rev>`), never `chromium_headless_shell-*`.
      const m = /^chromium-(\d+)$/.exec(name);
      if (!m) continue;
      for (const sub of ['chrome-linux64', 'chrome-linux']) {
        const bin = join(root, name, sub, 'chrome');
        if (existsSync(bin)) found.push({ bin, rev: Number(m[1]) });
      }
    }
  }

  if (found.length === 0) {
    throw new Error(
      'No Playwright Chromium found. Install it with:\n' +
      '  yarn --cwd charts/workspace/web exec playwright install chromium\n' +
      'or point KC_CHROMIUM at an existing chrome binary.'
    );
  }

  // Prefer the newest revision (closest to what playwright-core drives).
  found.sort((a, b) => b.rev - a.rev);
  return found[0].bin;
}
