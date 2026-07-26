import { signal, computed } from '@preact/signals';
import {
  subscribeEvents,
  eventStreamConnected,
  type DashboardEvent,
} from '../api/events';
import {
  listFeed,
  feedUnreadCount,
  markFeedRead,
  dismissFeedItem,
  type FeedItem,
  type FeedKind,
} from '../api/feed';
import { navigate } from './router';

/**
 * State for the Feed page (#470). Mirrors store/mission.ts — a `feed.item` SSE
 * subscription with a slow poll fallback. The server does all the sifting; this
 * only fetches, holds, and marks read/dismissed.
 */

export const feedItems = signal<FeedItem[]>([]);
export const feedLoading = signal(false);
export const feedError = signal<string | null>(null);
export const feedLastFetch = signal<number | null>(null);
export const unreadCount = signal(0);

export type FeedFilter = 'all' | FeedKind;
export const feedKindFilter = signal<FeedFilter>('all');
export const feedProjectFilter = signal<string | null>(null);

/** Items in view after the client-side project filter (kind is applied
 *  server-side on refresh; project is applied here so switching is instant). */
export const filteredFeed = computed(() => {
  const proj = feedProjectFilter.value;
  const items = feedItems.value;
  return proj ? items.filter((i) => i.project_id === proj) : items;
});

export interface FeedDayGroup {
  key: string;
  label: string;
  items: FeedItem[];
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

let inFlight: Promise<void> | null = null;

export async function refreshFeed(): Promise<void> {
  if (inFlight) return inFlight;
  feedLoading.value = true;
  inFlight = (async () => {
    try {
      const kind = feedKindFilter.value;
      feedItems.value = await listFeed({
        kinds: kind === 'all' ? undefined : [kind],
      });
      feedError.value = null;
      feedLastFetch.value = Date.now();
      void refreshUnread();
    } catch (err) {
      feedError.value = err instanceof Error ? err.message : String(err);
    } finally {
      feedLoading.value = false;
      inFlight = null;
    }
  })();
  return inFlight;
}

export async function refreshUnread(): Promise<void> {
  try {
    unreadCount.value = await feedUnreadCount();
  } catch {
    /* keep last-good */
  }
}

export function setKindFilter(kind: FeedFilter): void {
  feedKindFilter.value = kind;
  void refreshFeed(); // kind is a server-side filter
}

/** Mark one item read (optimistic), then confirm + refresh the unread count. */
export async function markRead(id: string): Promise<void> {
  const item = feedItems.value.find((i) => i.id === id);
  if (!item || item.read) return;
  feedItems.value = feedItems.value.map((i) => (i.id === id ? { ...i, read: true } : i));
  try {
    await markFeedRead(id);
  } catch {
    /* best effort — a failed mark just leaves it unread on the next refresh */
  }
  void refreshUnread();
}

export async function dismiss(id: string): Promise<void> {
  const prev = feedItems.value;
  feedItems.value = prev.filter((i) => i.id !== id);
  try {
    await dismissFeedItem(id);
  } catch {
    feedItems.value = prev; // roll back
  }
  void refreshUnread();
}

// ── "Discuss with CTO" handoff (the signature action) ──────────────────────
// Deterministic: builds a context prefix and hands it to the CTO page scoped to
// the item's project. No LLM in the handoff itself — the CtoRoute consumes this
// on mount, selects the project, and sends the prefix as the first message.

export interface CtoHandoff {
  projectId: string;
  text: string;
}
export const ctoHandoff = signal<CtoHandoff | null>(null);

export function discussWithCto(item: FeedItem): void {
  const ref = item.links.find((l) => l.ref)?.ref;
  const suffix = ref ? ` (${ref})` : '';
  ctoHandoff.value = {
    projectId: item.project_id || '',
    text: `Re: ${item.title}${suffix}\n\nWhat should we do about this?`,
  };
  navigate('/cto');
}

// ── real-time (feed.item SSE with poll fallback, mission.ts pattern) ───────

let pollHandle: ReturnType<typeof setInterval> | null = null;
let visibilityHandler: (() => void) | null = null;
let eventUnsub: (() => void) | null = null;
let eventRefreshTimer: ReturnType<typeof setTimeout> | null = null;
const FALLBACK_REFRESH_MS = 45000;

function onDashboardEvent(ev: DashboardEvent): void {
  if (ev.type !== 'feed.item') return;
  if (eventRefreshTimer != null) return;
  eventRefreshTimer = setTimeout(() => {
    eventRefreshTimer = null;
    void refreshFeed();
  }, 250);
}

export function startFeedPolling(intervalMs = 15000): void {
  void refreshFeed();
  if (!eventUnsub) eventUnsub = subscribeEvents(onDashboardEvent);
  if (pollHandle) clearInterval(pollHandle);
  pollHandle = setInterval(() => {
    if (typeof document !== 'undefined' && document.hidden) return;
    if (
      eventStreamConnected.value &&
      Date.now() - (feedLastFetch.value || 0) < FALLBACK_REFRESH_MS
    ) {
      return;
    }
    void refreshFeed();
  }, intervalMs);
  if (typeof document !== 'undefined') {
    if (visibilityHandler) document.removeEventListener('visibilitychange', visibilityHandler);
    visibilityHandler = () => {
      if (!document.hidden) void refreshFeed();
    };
    document.addEventListener('visibilitychange', visibilityHandler);
  }
}

export function stopFeedPolling(): void {
  if (pollHandle) clearInterval(pollHandle);
  pollHandle = null;
  if (eventUnsub) {
    eventUnsub();
    eventUnsub = null;
  }
  if (eventRefreshTimer != null) {
    clearTimeout(eventRefreshTimer);
    eventRefreshTimer = null;
  }
  if (visibilityHandler && typeof document !== 'undefined') {
    document.removeEventListener('visibilitychange', visibilityHandler);
    visibilityHandler = null;
  }
}

/** Reset for tests. */
export function _resetFeedForTest(): void {
  feedItems.value = [];
  unreadCount.value = 0;
  feedKindFilter.value = 'all';
  feedProjectFilter.value = null;
  feedLastFetch.value = null;
  ctoHandoff.value = null;
  stopFeedPolling();
}

/** Exposed for tests — the SSE handler. */
export const _onDashboardEventForTest = onDashboardEvent;
