/**
 * Build the desktop app's UI: the mobile app's web export, into desktop/dist.
 *
 * The desktop app has no UI source of its own. It bundles `expo export
 * --platform web` from ../mobile, the same export `npm run screenshots` uses,
 * and the mobile code switches to the desktop core when it finds Tauri's
 * bridge at runtime (mobile/src/desktop/bridge.ts). So this script only runs
 * that export with the right flags; it never copies or forks mobile source.
 *
 *   KC_DESKTOP_DEMO=1     the demo data build (EXPO_PUBLIC_MOCK=1): no
 *                         workspace needed. Used for screenshots and the
 *                         memory measurement.
 *   KC_ALLOW_CLEARTEXT=1  allow http://localhost workspaces (a development
 *                         build). The Rust core reads the same variable at
 *                         compile time (src-tauri/src/scope.rs), so the two
 *                         halves always agree.
 *
 * `--clear` matters for the same reason as in mobile/scripts/export-web.mjs:
 * Metro bakes EXPO_PUBLIC_* in at transform time and caches the result.
 */
import { spawnSync } from 'node:child_process';
import { existsSync, rmSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const mobile = resolve(here, '../../mobile');
const out = resolve(here, '../dist');
const win = process.platform === 'win32';

function run(cmd, args, env) {
  const r = spawnSync(cmd, args, { cwd: mobile, stdio: 'inherit', env, shell: win });
  if (r.error) throw r.error;
  if (r.status !== 0) process.exit(r.status ?? 1);
}

const flag = (name) => ['1', 'true', 'yes'].includes((process.env[name] ?? '').trim().toLowerCase());

if (!existsSync(join(mobile, 'node_modules'))) {
  run('npm', ['ci', '--no-audit', '--no-fund'], process.env);
}

const env = { ...process.env };
// Never inherit these from the shell: the flags above are the only switches.
delete env.EXPO_PUBLIC_MOCK;
delete env.EXPO_PUBLIC_ALLOW_CLEARTEXT;
delete env.EXPO_PUBLIC_HOST;
delete env.EXPO_PUBLIC_TOKEN;
if (flag('KC_DESKTOP_DEMO')) env.EXPO_PUBLIC_MOCK = '1';
if (flag('KC_ALLOW_CLEARTEXT')) env.EXPO_PUBLIC_ALLOW_CLEARTEXT = '1';

rmSync(out, { recursive: true, force: true });
run('npx', ['expo', 'export', '--platform', 'web', '--clear', '--output-dir', out], env);
