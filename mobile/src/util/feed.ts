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
  | { kind: 'external'; url: string }
  | { kind: 'none' };

/** Resolve a feed link to a native navigation target. task: → TaskDetail,
 *  thread: → CtoScreen, memory: → MemoryScreen, href → in-app browser. */
export function resolveFeedRef(link: FeedLink): FeedRefTarget {
  if (link.href) return { kind: 'external', url: link.href };
  const ref = link.ref || '';
  const idx = ref.indexOf(':');
  if (idx < 0) return { kind: 'none' };
  const kind = ref.slice(0, idx);
  const rest = ref.slice(idx + 1);
  if (kind === 'task' && rest) return { kind: 'task', id: rest };
  if (kind === 'thread' && rest) return { kind: 'thread', id: rest };
  if (kind === 'memory') return { kind: 'memory' };
  return { kind: 'none' };
}

/** Short human label for the item's source. */
export function feedSourceLabel(source: string): string {
  if (source.startsWith('agent:')) return 'CTO';
  if (source.startsWith('cron:')) return `cron · ${source.slice(5)}`;
  if (source.startsWith('system:')) return source.slice(7);
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
