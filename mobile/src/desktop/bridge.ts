/**
 * The desktop app's IPC bridge (#833) — the ONE place the mobile code learns
 * it is running inside the Tauri shell in `desktop/`.
 *
 * The desktop app bundles this app's web export and injects Tauri's global
 * API (`withGlobalTauri`), so detection is a runtime check rather than a
 * build flag: the same `expo export --platform web` output runs in a browser
 * (no bridge, nothing changes) and in the desktop shell (bridge present).
 *
 * Pure (no react-native imports) so the node-side vitest can fake the global.
 * Commands are implemented in desktop/src-tauri/src/lib.rs.
 */

type Unlisten = () => void;

interface TauriGlobal {
  core: { invoke: <T>(cmd: string, args?: Record<string, unknown>) => Promise<T> };
  event: {
    listen: <T>(event: string, cb: (e: { payload: T }) => void) => Promise<Unlisten>;
  };
}

function tauri(): TauriGlobal | null {
  const g = globalThis as { __TAURI__?: TauriGlobal };
  const t = g.__TAURI__;
  return t && typeof t.core?.invoke === 'function' ? t : null;
}

/** True inside the desktop app. */
export function isDesktop(): boolean {
  return tauri() !== null;
}

/** Call a desktop command. Throws when not on desktop — callers check
 *  `isDesktop()` first, so reaching this on a phone is a bug worth seeing. */
export function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  const t = tauri();
  if (!t) return Promise.reject(new Error(`desktop command ${cmd} called outside the desktop app`));
  return t.core.invoke<T>(cmd, args);
}

/** Listen to an event from the desktop core. Resolves to an unlisten
 *  function; a no-op outside the desktop app. */
export async function listen<T>(event: string, cb: (payload: T) => void): Promise<Unlisten> {
  const t = tauri();
  if (!t) return () => {};
  return t.event.listen<T>(event, (e) => cb(e.payload));
}

/** What the core reports about its link to the workspace
 *  (desktop/src-tauri/src/core.rs `LiveStatus`). */
export interface LiveStatus {
  mode: 'off' | 'connecting' | 'stream' | 'polling' | 'paused' | 'asleep' | 'unauthorized';
  keeper: boolean | null;
  needsYou: number;
  feedFetches: number;
}

/** A deep link or a notification's target, queued by the core. */
export interface DesktopTarget {
  url?: string;
  ref?: string;
  feedId?: string;
}
