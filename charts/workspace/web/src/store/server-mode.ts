import { signal } from '@preact/signals';
import { getMode, type ServerMode } from '../api/system';

// Mode the SPA was launched into. Fetched once at boot from /api/mode.
// Defaults to a *writable, authed* shape so a network failure during the
// initial probe doesn't accidentally hide UI for a real (non-demo) user —
// the public demo flips this on intentionally.
export const serverMode = signal<ServerMode>({
  readOnly: false,
  authed: true,
  authMode: 'basic',
});

export const serverModeLoaded = signal<boolean>(false);

let _started = false;
export async function loadServerMode(): Promise<void> {
  if (_started) return;
  _started = true;
  try {
    serverMode.value = await getMode();
  } catch {
    // Keep the safe default. Tests + dev_server may not implement /api/mode.
  } finally {
    serverModeLoaded.value = true;
  }
}

/** True when this dashboard instance is the public read-only demo. */
export const useReadOnly = () => serverMode.value.readOnly;
