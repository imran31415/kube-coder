import { signal } from '@preact/signals';
import { getMode, type ServerMode } from '../api/system';

// Mode the SPA was launched into. Fetched once at boot from /api/mode.
// Default to read-only so the public demo doesn't briefly flash mutation
// UI before /api/mode lands (the SPA boots in the demo deploy *before*
// the auth+mode probe completes, and clicking a write button between
// frames silently 403s server-side — exactly the failure mode this gate
// exists to prevent). loadServerMode() flips this to the real value
// (probably writable) for real deploys. UX-only — the server enforces
// the actual gate.
export const serverMode = signal<ServerMode>({
  readOnly: true,
  authed: true,
  authMode: 'basic',
  demoShowAll: false,
});

export const serverModeLoaded = signal<boolean>(false);

let _started = false;
export async function loadServerMode(): Promise<void> {
  if (_started) return;
  _started = true;
  try {
    serverMode.value = await getMode();
  } catch {
    // If /api/mode is unreachable (tests, dev_server without the route,
    // network outage), fall back to the *writable* shape so a real user
    // isn't stuck behind a phantom read-only gate. The startup default
    // above only protects the brief window between first paint and the
    // probe completing.
    serverMode.value = { readOnly: false, authed: true, authMode: 'basic', demoShowAll: false };
  } finally {
    serverModeLoaded.value = true;
  }
}

/** True when this dashboard instance is the public read-only demo. */
export const useReadOnly = () => serverMode.value.readOnly;
