/**
 * The screen the desktop window was on, kept across a dropped webview (#833).
 *
 * The desktop app destroys its window — and the webview with it — when it is
 * closed to the tray (Linux, Windows) or has been hidden for a while (macOS,
 * desktop/src-tauri/src/unload.rs). Opening it again boots the UI from
 * scratch. To come back where it was, the UI saves React Navigation's state
 * (the documented state-persistence shape) in the core's memory-only
 * `kc.session.*` items, and resets the navigator to it once it is mounted. The
 * core never writes these to disk, so a fresh launch starts on the home
 * screen as before.
 *
 * Pure, for the node-side vitest.
 */

export const NAV_STATE_KEY = 'kc.session.navState';
/** Well under the core's 64 KiB value limit; a real state is a few KB. */
const MAX_LEN = 32 * 1024;

export interface SavedNavState {
  index: number;
  routes: { name: string }[];
}

/** The string to save, or null when there is nothing worth saving. */
export function serializeNavState(state: unknown): string | null {
  if (!isNavState(state)) return null;
  let s: string;
  try {
    s = JSON.stringify(state);
  } catch {
    return null;
  }
  return s.length <= MAX_LEN ? s : null;
}

/** A saved state the navigator can be reset to, or null. */
export function parseNavState(raw: string | null | undefined): SavedNavState | null {
  if (!raw || raw.length > MAX_LEN) return null;
  try {
    const v: unknown = JSON.parse(raw);
    return isNavState(v) ? v : null;
  } catch {
    return null;
  }
}

function isNavState(v: unknown): v is SavedNavState {
  if (!v || typeof v !== 'object') return false;
  const s = v as { index?: unknown; routes?: unknown };
  return (
    Array.isArray(s.routes) &&
    s.routes.length > 0 &&
    s.routes.every((r) => !!r && typeof (r as { name?: unknown }).name === 'string') &&
    typeof s.index === 'number' &&
    s.index >= 0 &&
    s.index < s.routes.length
  );
}
