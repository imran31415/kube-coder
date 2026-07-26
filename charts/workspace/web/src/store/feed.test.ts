import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';

vi.mock('./router', () => ({ navigate: vi.fn() }));

import {
  feedItems,
  unreadCount,
  ctoHandoff,
  refreshFeed,
  refreshUnread,
  markRead,
  dismiss,
  discussWithCto,
  dayLabel,
  groupByDay,
  _onDashboardEventForTest as onEvent,
  _resetFeedForTest,
} from './feed';
import { navigate } from './router';
import type { FeedItem } from '../api/feed';

const now = Math.floor(Date.now() / 1000);

function item(over: Partial<FeedItem>): FeedItem {
  return {
    id: 'fd_1', ts: now, kind: 'activity', title: 'Task finished', body_md: '',
    source: 'system:task', project_id: 'kc', links: [], waiting: false, read: false,
    ...over,
  };
}

function mockFetch(handler: (url: string) => unknown) {
  globalThis.fetch = vi.fn(async (url: string) => ({
    ok: true, status: 200,
    headers: { get: () => 'application/json' },
    json: async () => handler(String(url)),
  })) as unknown as typeof fetch;
}

const realFetch = globalThis.fetch;

beforeEach(() => {
  _resetFeedForTest();
  vi.mocked(navigate).mockReset();
});
afterEach(() => {
  _resetFeedForTest();
  globalThis.fetch = realFetch;
});

describe('store/feed', () => {
  it('dayLabel returns Today / Yesterday / a date', () => {
    const t = 1_700_000_000_000; // fixed "now" in ms
    const day = 86400;
    expect(dayLabel(t / 1000, t)).toBe('Today');
    expect(dayLabel(t / 1000 - day, t)).toBe('Yesterday');
    expect(dayLabel(t / 1000 - 5 * day, t)).not.toMatch(/Today|Yesterday/);
  });

  it('groupByDay buckets consecutive items by day, order preserved', () => {
    const t = 1_700_000_000_000;
    const day = 86400;
    const items = [
      item({ id: 'a', ts: t / 1000 }),
      item({ id: 'b', ts: t / 1000 - 10 }),
      item({ id: 'c', ts: t / 1000 - day }),
    ];
    const groups = groupByDay(items, t);
    expect(groups.map((g) => g.label)).toEqual(['Today', 'Yesterday']);
    expect(groups[0].items.map((i) => i.id)).toEqual(['a', 'b']);
    expect(groups[1].items.map((i) => i.id)).toEqual(['c']);
  });

  it('refreshFeed loads items and the unread count', async () => {
    mockFetch((url) =>
      url.includes('unread_count') ? { count: 2 } : { items: [item({ id: 'x' })] },
    );
    await refreshFeed();
    await refreshUnread(); // refreshFeed fires this un-awaited
    expect(feedItems.value.map((i) => i.id)).toEqual(['x']);
    expect(unreadCount.value).toBe(2);
  });

  it('markRead is optimistic and dismiss removes the item', async () => {
    mockFetch((url) => (url.includes('unread_count') ? { count: 0 } : { ok: true }));
    feedItems.value = [item({ id: 'a', read: false }), item({ id: 'b' })];
    await markRead('a');
    expect(feedItems.value.find((i) => i.id === 'a')?.read).toBe(true);
    await dismiss('b');
    expect(feedItems.value.map((i) => i.id)).toEqual(['a']);
  });

  it('a feed.item SSE event refreshes the stream (debounced)', async () => {
    vi.useFakeTimers();
    try {
      const urls: string[] = [];
      mockFetch((url) => {
        urls.push(url);
        return url.includes('unread_count') ? { count: 0 } : { items: [] };
      });
      onEvent({ type: 'feed.item', data: {} });
      onEvent({ type: 'task.status', data: {} }); // ignored
      await vi.advanceTimersByTimeAsync(300);
      expect(urls.some((u) => u.endsWith('/api/feed') || u.includes('/api/feed?'))).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it('discussWithCto sets a project-scoped handoff and navigates to /cto', () => {
    discussWithCto(item({
      title: 'Release is the bottleneck', project_id: 'kc',
      links: [{ label: 'Open task', ref: 'task:t1' }],
    }));
    expect(ctoHandoff.value?.projectId).toBe('kc');
    expect(ctoHandoff.value?.text).toContain('Release is the bottleneck');
    expect(ctoHandoff.value?.text).toContain('task:t1');
    expect(navigate).toHaveBeenCalledWith('/cto');
  });
});
