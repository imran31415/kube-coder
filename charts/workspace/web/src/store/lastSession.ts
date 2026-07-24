/**
 * Last-open session memory for the Hypervisor and Build tabs.
 *
 * Both tabs drive their active session through the URL (`/hypervisor/<id>`,
 * `/tasks/<id>`), so a refresh of a deep link already restores it. But coming
 * back to the app — reopening the tab, or clicking Hypervisor/Build in the
 * nav — lands on the bare route, which used to mean "nothing open". These
 * helpers remember the last session the user had open (per browser, via
 * localStorage) so the bare-route mount can redirect back to it, falling back
 * to the newest session when the remembered one is gone.
 */

export type SessionKind = 'hypervisor' | 'build';

const KEYS: Record<SessionKind, string> = {
  hypervisor: 'kc.hv.lastThread',
  build: 'kc.tasks.lastTask',
};

export function rememberLastSession(kind: SessionKind, id: string): void {
  try {
    localStorage.setItem(KEYS[kind], id);
  } catch {
    /* private mode / quota — restore just falls back to newest */
  }
}

export function lastSessionId(kind: SessionKind): string | null {
  try {
    return localStorage.getItem(KEYS[kind]);
  } catch {
    return null;
  }
}

/** Drop the remembered id if it matches `id` (e.g. that session was deleted). */
export function forgetLastSession(kind: SessionKind, id: string): void {
  try {
    if (localStorage.getItem(KEYS[kind]) === id) localStorage.removeItem(KEYS[kind]);
  } catch {
    /* noop */
  }
}

/**
 * Which session a bare-route visit should reopen: the remembered one while it
 * still exists, else the newest (`ids` comes newest-first from both list
 * endpoints), else none.
 */
export function restoreTarget(kind: SessionKind, ids: readonly string[]): string | null {
  const last = lastSessionId(kind);
  if (last && ids.includes(last)) return last;
  return ids[0] ?? null;
}
