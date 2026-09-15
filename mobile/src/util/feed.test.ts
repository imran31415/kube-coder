import { describe, expect, it } from 'vitest';
import {
  resolveFeedRef,
  feedSourceLabel,
  dayLabel,
  groupByDay,
  discussPrefix,
  countBoardsAwaitingReview,
} from './feed';
import type { FeedItem } from '../api/types';

const now = 1_700_000_000_000; // fixed ms
function item(over: Partial<FeedItem>): FeedItem {
  return {
    id: 'fd_1', ts: now / 1000, kind: 'activity', title: 'A thing', body_md: '',
    source: 'system:task', project_id: 'kc', links: [], waiting: false, read: false,
    ...over,
  };
}

describe('resolveFeedRef', () => {
  it('resolves typed internal refs', () => {
    expect(resolveFeedRef({ label: '', ref: 'task:t1' })).toEqual({ kind: 'task', id: 't1' });
    expect(resolveFeedRef({ label: '', ref: 'thread:th9' })).toEqual({ kind: 'thread', id: 'th9' });
    expect(resolveFeedRef({ label: '', ref: 'memory:project.kc/x' })).toEqual({ kind: 'memory' });
  });
  it('resolves an external href', () => {
    expect(resolveFeedRef({ label: '', href: 'https://x/y' })).toEqual({ kind: 'external', url: 'https://x/y' });
  });
  it('returns none for an unknown or empty ref', () => {
    expect(resolveFeedRef({ label: '' })).toEqual({ kind: 'none' });
    expect(resolveFeedRef({ label: '', ref: 'weird' })).toEqual({ kind: 'none' });
  });
});

describe('feedSourceLabel', () => {
  it('humanizes sources', () => {
    expect(feedSourceLabel('agent:th1')).toBe('CTO');
    expect(feedSourceLabel('system:task')).toBe('task');
    expect(feedSourceLabel('cron:dep-scout')).toBe('cron · dep-scout');
    // The server sources a board review item as "board:<board_id>", which used
    // to render as the shouty raw ref in the Feed meta line.
    expect(feedSourceLabel('board:kube-coder-gh')).toBe('board · kube-coder-gh');
  });
});

describe('dayLabel + groupByDay', () => {
  const day = 86400;
  it('labels Today / Yesterday / a date', () => {
    expect(dayLabel(now / 1000, now)).toBe('Today');
    expect(dayLabel(now / 1000 - day, now)).toBe('Yesterday');
    expect(dayLabel(now / 1000 - 5 * day, now)).not.toMatch(/Today|Yesterday/);
  });
  it('buckets consecutive items by day, order preserved', () => {
    const items = [
      item({ id: 'a', ts: now / 1000 }),
      item({ id: 'b', ts: now / 1000 - 10 }),
      item({ id: 'c', ts: now / 1000 - day }),
    ];
    const groups = groupByDay(items, now);
    expect(groups.map((g) => g.label)).toEqual(['Today', 'Yesterday']);
    expect(groups[0].items.map((i) => i.id)).toEqual(['a', 'b']);
    expect(groups[1].items.map((i) => i.id)).toEqual(['c']);
  });
});

describe('discussPrefix', () => {
  it('builds a deterministic context prefix with the ref', () => {
    const text = discussPrefix(item({ title: 'Release is blocked', links: [{ label: 'Open', ref: 'task:t1' }] }));
    expect(text).toContain('Release is blocked');
    expect(text).toContain('task:t1');
  });
  it('omits the ref suffix when there is none', () => {
    const text = discussPrefix(item({ title: 'No refs here', links: [] }));
    expect(text).toContain('No refs here');
    expect(text).not.toContain('(');
  });
});

describe('resolveFeedRef · board refs (#692)', () => {
  it('resolves a board review ref to the board and the item', () => {
    expect(resolveFeedRef({ label: '', ref: 'board:acme:412' })).toEqual({
      kind: 'board', boardId: 'acme', itemId: '412',
    });
  });

  it('keeps an item id that itself contains colons intact', () => {
    /* GitHub GraphQL global ids look like `gid://…`, and the server puts them
       in the ref verbatim. Splitting on every colon would hand the screen a
       truncated id that matches no card in the queue. */
    expect(resolveFeedRef({ label: '', ref: 'board:kube-coder-gh:I_kwDOA:4102' })).toEqual({
      kind: 'board', boardId: 'kube-coder-gh', itemId: 'I_kwDOA:4102',
    });
    expect(resolveFeedRef({ label: '', ref: 'board:gh:gid://issue/46' })).toEqual({
      kind: 'board', boardId: 'gh', itemId: 'gid://issue/46',
    });
  });

  it('returns none for a malformed board ref rather than a half target', () => {
    // Each of these would otherwise navigate to a board that cannot be found,
    // leaving the screen spinning on a card that will never arrive.
    expect(resolveFeedRef({ label: '', ref: 'board' })).toEqual({ kind: 'none' });
    expect(resolveFeedRef({ label: '', ref: 'board:' })).toEqual({ kind: 'none' });
    expect(resolveFeedRef({ label: '', ref: 'board:acme' })).toEqual({ kind: 'none' });
    expect(resolveFeedRef({ label: '', ref: 'board:acme:' })).toEqual({ kind: 'none' });
    expect(resolveFeedRef({ label: '', ref: 'board::412' })).toEqual({ kind: 'none' });
  });

  it('still prefers an explicit href over the ref', () => {
    expect(resolveFeedRef({ label: '', ref: 'board:acme:412', href: 'https://x/y' })).toEqual({
      kind: 'external', url: 'https://x/y',
    });
  });
});

describe('countBoardsAwaitingReview', () => {
  const boardLink = { label: 'Open item', ref: 'board:acme:412' };

  it('counts only unread board items that are waiting on a decision', () => {
    const items = [
      item({ id: 'a', waiting: true, read: false, links: [boardLink] }),
      item({ id: 'b', waiting: true, read: true, links: [boardLink] }),   // already seen
      item({ id: 'c', waiting: false, read: false, links: [boardLink] }), // not waiting
      item({ id: 'd', waiting: true, read: false, links: [{ label: '', ref: 'task:t1' }] }),
      item({ id: 'e', waiting: true, read: false, links: [] }),
      item({ id: 'f', waiting: true, read: false, links: [boardLink] }),
    ];
    expect(countBoardsAwaitingReview(items)).toBe(2);
  });

  it('is zero for an empty feed, so a quiet board shows no badge', () => {
    expect(countBoardsAwaitingReview([])).toBe(0);
  });

  it('does not count a malformed board ref it could not route to', () => {
    const items = [item({ waiting: true, read: false, links: [{ label: '', ref: 'board:acme' }] })];
    expect(countBoardsAwaitingReview(items)).toBe(0);
  });
});
