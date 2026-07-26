import { describe, expect, it } from 'vitest';
import { resolveFeedRef, feedSourceLabel, dayLabel, groupByDay, discussPrefix } from './feed';
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
