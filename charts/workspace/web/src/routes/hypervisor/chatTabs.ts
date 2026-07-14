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
 * A thread is "active" when it's live (`running`), is the one currently open
 * (never hide the chat the user is in, even once it goes idle), or was updated
 * within the recency window. Timestamps are stored in seconds; a missing
 * `updated_at` falls back to `created_at`, then to 0 (→ past).
 */
export function isActiveThread(
  t: HypervisorThread,
  openId: string | null,
  now: number,
): boolean {
  if (t.status === 'running') return true;
  if (openId && t.id === openId) return true;
  const updatedSec = t.updated_at ?? t.created_at ?? 0;
  return now - updatedSec * 1000 <= ACTIVE_WINDOW_MS;
}

export interface PartitionedThreads {
  active: HypervisorThread[];
  past: HypervisorThread[];
}

/** Partition a thread list into active/past, preserving input order. */
export function partitionThreads(
  list: HypervisorThread[],
  openId: string | null,
  now: number,
): PartitionedThreads {
  const active: HypervisorThread[] = [];
  const past: HypervisorThread[] = [];
  for (const t of list) {
    (isActiveThread(t, openId, now) ? active : past).push(t);
  }
  return { active, past };
}
