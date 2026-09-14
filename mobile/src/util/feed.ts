/**
 * Pure Feed helpers (#470/#471) — ref resolution, day grouping, and the
 * "Discuss with CTO" context prefix. Kept RN-free so the node-side vitest can
 * exercise them (mirrors the web store/feed.ts logic).
 */
import type { FeedItem, FeedLink } from '../api/types';

/** Where a typed ref / external href should take the user on mobile. */
export type FeedRefTarget =
  | { kind: 'task'; id: string }
  | { kind: 'thread'; id: string }
  | { kind: 'memory' }
  | { kind: 'board'; boardId: string; itemId: string }
  | { kind: 'external'; url: string }
  | { kind: 'none' };

/** Resolve a feed link to a native navigation target. task: → TaskDetail,
 *  thread: → CtoScreen, memory: → MemoryScreen, board: → BoardScreen with the
 *  named item focused, href → in-app browser. */
export function resolveFeedRef(link: FeedLink): FeedRefTarget {
  if (link.href) return { kind: 'external', url: link.href };
  const ref = link.ref || '';
  // Split off the FIRST colon only. A board ref is three parts
  // ("board:<board_id>:<item_id>") and the item id can itself contain colons —
  // a GitHub GraphQL global id looks like `gid://…`. Slicing at every colon
  // would truncate the id and the card would never be found.
  const [kind, rest] = splitOnce(ref);
  if (kind === 'task' && rest) return { kind: 'task', id: rest };
  if (kind === 'thread' && rest) return { kind: 'thread', id: rest };
  if (kind === 'memory') return { kind: 'memory' };
  if (kind === 'board' && rest) {
    const [boardId, itemId] = splitOnce(rest);
    if (boardId && itemId) return { kind: 'board', boardId, itemId };
    return { kind: 'none' };
  }
  return { kind: 'none' };
}

/** `a:b:c` → `['a', 'b:c']`; no colon → `['a:b:c'...]` with an empty tail. */
function splitOnce(value: string): [string, string] {
  const idx = value.indexOf(':');
  if (idx < 0) return [value, ''];
  return [value.slice(0, idx), value.slice(idx + 1)];
}

/** True when this link points at a board review item. */
export function isBoardLink(link: FeedLink): boolean {
  return resolveFeedRef(link).kind === 'board';
}

/**
 * How many feed items are a board card still waiting on a decision.
 *
 * Deliberately derived from the FEED, not from the boards API: a board item
 * fetch is an outbound call against someone else's rate limit, and the drawer
 * badge must never be the thing that spends that budget. The web's
 * WaitingBadge makes the same trade for the same reason.
 */
export function countBoardsAwaitingReview(items: FeedItem[]): number {
  return items.filter(
    (it) => it.waiting && !it.read && (it.links ?? []).some(isBoardLink),
  ).length;
}

/** Short human label for the item's source. */
export function feedSourceLabel(source: string): string {
  if (source.startsWith('agent:')) return 'CTO';
  if (source.startsWith('cron:')) return `cron · ${source.slice(5)}`;
  if (source.startsWith('system:')) return source.slice(7);
  if (source.startsWith('board:')) return `board · ${source.slice(6)}`;
  return source || 'system';
}

/** Day label for a unix-seconds timestamp: Today / Yesterday / a short date. */
export function dayLabel(ts: number, now: number = Date.now()): string {
  const d = new Date(ts * 1000);
  const today = new Date(now);
  const startOf = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diffDays = Math.round((startOf(today) - startOf(d)) / 86400000);
  if (diffDays <= 0) return 'Today';
  if (diffDays === 1) return 'Yesterday';
  return d.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    year: d.getFullYear() === today.getFullYear() ? undefined : 'numeric',
  });
}

export interface FeedDayGroup {
  key: string;
  label: string;
  items: FeedItem[];
}

/** Group items (already newest-first) into consecutive day buckets. */
export function groupByDay(items: FeedItem[], now: number = Date.now()): FeedDayGroup[] {
  const groups: FeedDayGroup[] = [];
  let current: FeedDayGroup | null = null;
  for (const it of items) {
    const label = dayLabel(it.ts, now);
    if (!current || current.label !== label) {
      current = { key: label, label, items: [] };
      groups.push(current);
    }
    current.items.push(it);
  }
  return groups;
}

/** Deterministic "Discuss with CTO" context prefix — no LLM in the handoff. */
export function discussPrefix(item: FeedItem): string {
  const ref = item.links.find((l) => l.ref)?.ref;
  const suffix = ref ? ` (${ref})` : '';
  return `Re: ${item.title}${suffix}\n\nWhat should we do about this?`;
}
