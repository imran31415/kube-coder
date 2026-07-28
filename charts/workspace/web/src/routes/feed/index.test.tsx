import { render, screen } from '@testing-library/preact';
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { FeedRoute } from './index';
import { feedItems, _resetFeedForTest } from '../../store/feed';
import { serverMode } from '../../store/server-mode';
import type { FeedItem } from '../../api/feed';

const now = Math.floor(Date.now() / 1000);
function item(over: Partial<FeedItem>): FeedItem {
  return {
    id: 'fd_1', ts: now, kind: 'briefing', title: 'Morning briefing', body_md: '',
    source: 'agent:th1', project_id: 'kc', links: [], waiting: false, read: true,
    ...over,
  };
}

const realFetch = globalThis.fetch;

beforeEach(() => {
  _resetFeedForTest();
  serverMode.value = { readOnly: false, authed: true, authMode: 'basic', ctoEnabled: true };
  // The route starts polling on mount; feed it benign JSON.
  globalThis.fetch = vi.fn(async (url: string) => ({
    ok: true, status: 200,
    headers: { get: () => 'application/json' },
    json: async () => (String(url).includes('unread_count') ? { count: 0 } : { items: feedItems.value }),
  })) as unknown as typeof fetch;
});
afterEach(() => {
  _resetFeedForTest();
  globalThis.fetch = realFetch;
});

describe('FeedRoute', () => {
  it('renders the masthead and filter chips', () => {
    render(<FeedRoute />);
    expect(screen.getByText('Feed')).toBeTruthy();
    expect(screen.getByRole('tab', { name: 'Briefings' })).toBeTruthy();
    expect(screen.getByRole('tab', { name: 'All' })).toBeTruthy();
  });

  it('shows the quiet empty state when there is nothing', async () => {
    feedItems.value = [];
    render(<FeedRoute />);
    // Mount kicks off refreshFeed (skeletons); it resolves to the empty line.
    expect(await screen.findByText(/Nothing yet/)).toBeTruthy();
  });

  it('wears the shared route masthead so CTO/Feed/Mission match (#510)', () => {
    render(<FeedRoute />);
    expect(screen.getByText('Feed').classList.contains('route-title')).toBe(true);
  });

  it('shimmers skeleton rows on first load instead of a bare "Loading…" (#510)', async () => {
    feedItems.value = [];
    const { container } = render(<FeedRoute />);
    expect(container.querySelectorAll('.feed-item-skeleton').length).toBe(4);
    expect(screen.queryByText('Loading…')).toBeNull();
    // They give way to the real stream (here, the empty line) once it lands.
    expect(await screen.findByText(/Nothing yet/)).toBeTruthy();
    expect(container.querySelectorAll('.feed-item-skeleton').length).toBe(0);
  });

  it('renders day-grouped items', () => {
    feedItems.value = [item({ id: 'a', title: 'Release is the bottleneck' })];
    render(<FeedRoute />);
    expect(screen.getByText('Release is the bottleneck')).toBeTruthy();
    expect(screen.getByText('Today')).toBeTruthy();
  });

  it('shows the disabled state when the CTO feature is off', () => {
    serverMode.value = { readOnly: false, authed: true, authMode: 'basic', ctoEnabled: false };
    render(<FeedRoute />);
    expect(screen.getByText('Feed is disabled')).toBeTruthy();
  });
});
