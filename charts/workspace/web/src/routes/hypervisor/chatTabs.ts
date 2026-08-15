import type { HypervisorThread } from '../../api/hypervisor';

/**
 * Splits the Hypervisor thread list into "active" vs "past" so the sidebar can
 * default to just the chats you're currently working with and tuck the rest
 * behind a Past tab. Everything here is derived from data the list endpoint
 * already returns (`status` + `updated_at`) — no backend/archive flag needed.
 */

export type ChatTab = 'active' | 'past';

/** Rolling window: an idle chat touched within this long still counts active. */
export const ACTIVE_WINDOW_MS = 24 * 60 * 60 * 1000;

/**
 * A thread is "active" when it's live (`running`) or was updated within the
 * recency window. Timestamps are stored in seconds; a missing `updated_at`
 * falls back to `created_at`, then to 0 (→ past).
 *
 * NOTE (#620): this deliberately does NOT consider which thread is open.
 * It used to, so that the chat you were in never got hidden — but the effect
 * was that opening an old chat from Past *reclassified* it as active, so it
 * vanished from the list you were looking at the instant you clicked it. You
 * could not tell which chat was open, could not rename it, and since the tab
 * only auto-switches Active → Past it could look lost entirely.
 *
 * Bucketing is now purely a property of the thread. Keeping the open chat
 * visible is a separate concern, handled in `partitionThreads` by PINNING it
 * into the other bucket rather than moving it out of its own.
 */
export function isActiveThread(t: HypervisorThread, now: number): boolean {
  if (t.status === 'running') return true;
  const updatedSec = t.updated_at ?? t.created_at ?? 0;
  return now - updatedSec * 1000 <= ACTIVE_WINDOW_MS;
}

export interface PartitionedThreads {
  active: HypervisorThread[];
  past: HypervisorThread[];
}

/**
 * Partition a thread list into active/past, preserving input order.
 *
 * The open thread stays in its OWN bucket and is additionally pinned into the
 * other one, so it is visible on whichever tab the user happens to be on and
 * never disappears by being clicked (#620). It is present twice across the two
 * lists, but only ever rendered once, because only one tab shows at a time.
 */
export function partitionThreads(
  list: HypervisorThread[],
  openId: string | null,
  now: number,
): PartitionedThreads {
  const active: HypervisorThread[] = [];
  const past: HypervisorThread[] = [];
  for (const t of list) {
    (isActiveThread(t, now) ? active : past).push(t);
  }
  if (openId) {
    const open = list.find((t) => t.id === openId);
    if (open) {
      // Pin into whichever list does not already hold it. Position mirrors the
      // natural bucket's convention: newest-first lists put it at the top.
      if (!active.some((t) => t.id === openId)) active.unshift(open);
      else if (!past.some((t) => t.id === openId)) past.unshift(open);
    }
  }
  return { active, past };
}
