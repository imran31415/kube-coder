/**
 * The phone Board's three tabs (#709 Part 3) — Needs you · Working · Done.
 *
 * Pure, so the node-side vitest can walk every state and conclusion. The rule
 * is spelled the same way as the server's `boards/outcome.py`, which posts a
 * Done row's Feed item using its half of it; if one changes, change both.
 *
 * Where each tab's rows come from:
 * - **Needs you** is the review queue, exactly as before — `/review?open=1`,
 *   approve / reject / send back through the offline queue. Its COUNT comes
 *   from the Feed (`needsYouCount`), the same way the drawer badge does, so
 *   it never costs a vendor fetch.
 * - **Working** and **Done** come from `/recent`, one local read that joins
 *   each item's newest run row, its review state and its Feed read flag.
 */
import type { BoardRecentRow, FeedItem } from '../api/types';
import type { QueuedApproval } from './approvalQueue';
import { resolveFeedRef } from './feed';

export type BoardTab = 'needs_you' | 'working' | 'done';

export const BOARD_TABS: { key: BoardTab; label: string }[] = [
  { key: 'needs_you', label: 'Needs you' },
  { key: 'working', label: 'Working' },
  { key: 'done', label: 'Done' },
];

/** A review still open — an agent's write is waiting on a human. `partial`
 *  is an approval whose writes half-failed, left open on purpose. */
const OPEN_REVIEW = ['pending', 'partial'];
/** A review a human has ruled on. */
const DECIDED_REVIEW = ['approved', 'rejected', 'sent_back'];
/** Conclusions the agent hands to a human. */
const NEEDS_HUMAN = ['needs_review', 'needs_rescoping', 'blocked'];

/**
 * Which tab one item is on, or null for none.
 *
 * Order matters: an open review needs you whatever the run says, and a row
 * still being worked is Working even if an earlier review of it was decided —
 * an item sent back is being worked again, not finished. `pending` (queued,
 * not yet claimed) and `skipped` (the run stopped first) are on no tab.
 */
export function classifyBoardRow(
  row: Pick<BoardRecentRow, 'state' | 'disposition' | 'review_state'>,
): BoardTab | null {
  if (row.review_state && OPEN_REVIEW.includes(row.review_state)) return 'needs_you';
  if (row.state === 'claimed' || row.state === 'working') return 'working';
  if (row.review_state && DECIDED_REVIEW.includes(row.review_state)) return 'done';
  if (row.state === 'failed') return 'done';
  if (row.state === 'done') {
    return row.disposition && NEEDS_HUMAN.includes(row.disposition) ? 'needs_you' : 'done';
  }
  return null;
}

/**
 * Working and Done, newest first. Rows that classify as Needs you are dropped
 * here: that tab is the review queue, whose cards carry the writes to decide,
 * and listing the same item twice would invite deciding it from the wrong one.
 */
export function splitRecent(rows: BoardRecentRow[]): {
  working: BoardRecentRow[];
  done: BoardRecentRow[];
} {
  const byNewest = [...rows].sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
  return {
    working: byNewest.filter((r) => classifyBoardRow(r) === 'working'),
    done: byNewest.filter((r) => classifyBoardRow(r) === 'done'),
  };
}

/**
 * The Needs-you count, from the FEED — the rows still waiting on a decision
 * for THIS board. Same rule as the drawer's `countBoardsAwaitingReview`,
 * narrowed to one board: never the boards API, which on a timer would spend
 * someone else's rate limit on a number.
 */
export function needsYouCount(feed: FeedItem[], boardId: string): number {
  return feed.filter(
    (it) =>
      it.waiting &&
      !it.read &&
      (it.links ?? []).some((l) => {
        const t = resolveFeedRef(l);
        return t.kind === 'board' && t.boardId === boardId;
      }),
  ).length;
}

/** The rows after one Feed item was marked read — the dot clears at once,
 *  rather than on the next 15-second poll. */
export function markRowRead(rows: BoardRecentRow[], feedId: string): BoardRecentRow[] {
  return rows.map((r) => (r.feed_id === feedId && r.unread ? { ...r, unread: false } : r));
}

/**
 * Opening a Done row clears its dot through the Feed's existing read API.
 * Returns the feed id it marked, or null when there was nothing to clear. A
 * failed mark is swallowed: the dot simply comes back on the next poll, which
 * is the truth.
 */
export async function clearDot(
  row: BoardRecentRow,
  markRead: (feedId: string) => Promise<void>,
): Promise<string | null> {
  if (!row.unread || !row.feed_id) return null;
  try {
    await markRead(row.feed_id);
  } catch {
    // The next poll restores the dot.
  }
  return row.feed_id;
}

/** What a queued decision sends — unchanged by the tabs, and pinned by a test
 *  for that reason. Approve echoes the hash its card carried; send-back's
 *  text is a NOTE the server requires, reject's an optional reason. */
export function decisionRequest(entry: QueuedApproval): {
  approval_id: string;
  content_hash?: string;
  reason?: string;
  note?: string;
} {
  return {
    approval_id: entry.approval_id,
    ...(entry.decision === 'approve' ? { content_hash: entry.content_hash } : {}),
    ...(entry.reason
      ? entry.decision === 'send-back'
        ? { note: entry.reason }
        : { reason: entry.reason }
      : {}),
  };
}

/** What a screen reader hears on a tab: its name and its count, because a
 *  bare number beside a word reads as nothing at all. */
export function tabA11yLabel(tab: BoardTab, count: number | null, unread = 0): string {
  const name = BOARD_TABS.find((t) => t.key === tab)?.label ?? tab;
  if (count === null) return name;
  const noun =
    tab === 'needs_you'
      ? `${count} waiting`
      : `${count} ${count === 1 ? 'item' : 'items'}`;
  return unread > 0 ? `${name}, ${noun}, ${unread} unread` : `${name}, ${noun}`;
}
