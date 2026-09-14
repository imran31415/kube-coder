/**
 * `expo export --platform web`, with EXPO_PUBLIC_MOCK=1 actually set.
 *
 * The npm script used to inline the variable POSIX-style —
 * `EXPO_PUBLIC_MOCK=1 expo export …` — and npm runs scripts through cmd.exe on
 * Windows, where a leading `NAME=value` is not an assignment. The flag never
 * reached the bundler, `src/store/config.ts` saw `undefined`, and the export
 * booted to onboarding instead of the demo data: `npm run screenshots` and
 * `npm run check:nav` were both unrunnable for a Windows contributor, silently
 * and with a green exit code.
 *
 * Setting it here works on every platform, and needs no devDependency —
 * `cross-env` would be a new package in the supply chain of a repo that pins
 * `overrides` and runs `audit-ci`, in exchange for one assignment.
 *
 * `--clear` is not optional. Metro caches transformed modules, and the inlined
 * `process.env.EXPO_PUBLIC_MOCK` is baked in at transform time — so an export
 * that ran once without the flag keeps serving the cached, flagless module
 * however many times it is re-run with the flag set.
 */
import { spawnSync } from 'node:child_process';

const result = spawnSync(
  'npx',
  ['expo', 'export', '--platform', 'web', '--clear', ...process.argv.slice(2)],
  {
    stdio: 'inherit',
    env: { ...process.env, EXPO_PUBLIC_MOCK: '1' },
    // npx is a shell script on Windows; without this the spawn fails outright.
    shell: process.platform === 'win32',
  },
);

if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
