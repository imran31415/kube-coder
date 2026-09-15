import type { HypervisorThread } from '../../api/hypervisor';

/**
 * Thread "mode" — the client-side reading of the server's `persona` field
 * (#683).
 *
 * Persona has always been a general server concept: a thread is created with
 * one, the preamble for it is delivered on turn 1, and the list endpoint can
 * filter on it. The client is the only place it got hard-coded into a page —
 * the AI CTO got its own route whose list was `persona=cto` while Chat's was
 * `persona=default`, so the two lists were disjoint and every chat-management
 * feature had to be built twice.
 *
 * There is now ONE list. A thread's persona becomes a read-only badge on its
 * row, and an optional chip filters the list down to one mode. Nothing here
 * talks to the server: the personas below are exactly the ones server.py
 * already creates (`cto` #465, `board`/`board-gen` #588/#589).
 */

/** The filter the sidebar chip row is set to. 'all' is the default — no
 *  thread is ever hidden unless the user asks for it. */
export type ThreadModeFilter = 'all' | 'default' | 'cto';

/** Human label for a thread's persona. '' (a plain workspace chat) has no
 *  badge — the overwhelmingly common case should stay visually quiet. */
export function personaLabel(persona: string | undefined | null): string {
  switch ((persona || '').toLowerCase()) {
    case 'cto':
      return 'CTO';
    // Board Processor threads (#588/#589) are machine-created, so they are
    // badge-only: never offered in the Mode picker, but honestly labelled.
    case 'board':
      return 'Board';
    case 'board-gen':
      return 'Board gen';
    default:
      return '';
  }
}

/** Longer text for the badge's tooltip, so a badge is self-explaining. */
export function personaHint(persona: string | undefined | null): string {
  switch ((persona || '').toLowerCase()) {
    case 'cto':
      return 'AI CTO chat — started with the CTO preamble';
    case 'board':
      return 'Board Processor — works one item on a connected board';
    case 'board-gen':
      return 'Board Processor — generates board items';
    default:
      return '';
  }
}

/** Narrow a thread list to one mode. 'all' is the identity. */
export function filterByMode(
  list: HypervisorThread[],
  mode: ThreadModeFilter,
): HypervisorThread[] {
  if (mode === 'all') return list;
  if (mode === 'cto') return list.filter((t) => (t.persona || '') === 'cto');
  // 'default' = the plain workspace chats. Board threads carry a persona too,
  // so this is "no persona", not "not cto".
  return list.filter((t) => !(t.persona || ''));
}

/**
 * Whether the chip row is worth rendering: only once the list actually holds
 * more than one mode. A workspace that has never started a CTO chat sees the
 * sidebar exactly as it looks today.
 */
export function hasMixedModes(list: HypervisorThread[]): boolean {
  let plain = false;
  let personal = false;
  for (const t of list) {
    if (t.persona) personal = true;
    else plain = true;
    if (plain && personal) return true;
  }
  return false;
}
