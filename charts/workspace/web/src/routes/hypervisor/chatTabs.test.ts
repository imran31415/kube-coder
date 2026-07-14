import { describe, expect, it } from 'vitest';
import { isActiveThread, partitionThreads, ACTIVE_WINDOW_MS } from './chatTabs';
import type { HypervisorThread, ThreadStatus } from '../../api/hypervisor';

const NOW = 1_700_000_000_000; // fixed "now" in ms

function thread(over: Partial<HypervisorThread> = {}): HypervisorThread {
  return {
    id: 'a',
    title: 'chat',
    assistant: 'claude',
    status: 'idle' as ThreadStatus,
    created_at: NOW / 1000,
    updated_at: NOW / 1000,
    ...over,
  };
}

const secAgo = (ms: number) => (NOW - ms) / 1000;

describe('isActiveThread', () => {
  it('treats a running thread as active regardless of age', () => {
    const t = thread({ status: 'running', updated_at: secAgo(30 * ACTIVE_WINDOW_MS) });
    expect(isActiveThread(t, null, NOW)).toBe(true);
  });

  it('keeps the currently-open thread active even when old and idle', () => {
    const t = thread({ id: 'open', status: 'idle', updated_at: secAgo(30 * ACTIVE_WINDOW_MS) });
    expect(isActiveThread(t, 'open', NOW)).toBe(true);
  });

  it('is active when idle but updated within the window', () => {
    const t = thread({ status: 'idle', updated_at: secAgo(ACTIVE_WINDOW_MS - 60_000) });
    expect(isActiveThread(t, null, NOW)).toBe(true);
  });

  it('is past when idle and older than the window', () => {
    const t = thread({ status: 'idle', updated_at: secAgo(ACTIVE_WINDOW_MS + 60_000) });
    expect(isActiveThread(t, null, NOW)).toBe(false);
  });

  it('falls back to created_at when updated_at is null', () => {
    const recent = thread({ updated_at: null, created_at: secAgo(60_000) });
    const old = thread({ updated_at: null, created_at: secAgo(ACTIVE_WINDOW_MS + 60_000) });
    expect(isActiveThread(recent, null, NOW)).toBe(true);
    expect(isActiveThread(old, null, NOW)).toBe(false);
  });

  it('is past when both timestamps are null', () => {
    const t = thread({ updated_at: null, created_at: null });
    expect(isActiveThread(t, null, NOW)).toBe(false);
  });
});

describe('partitionThreads', () => {
  it('splits into active/past and preserves order', () => {
    const list = [
      thread({ id: 'running', status: 'running', updated_at: secAgo(10 * ACTIVE_WINDOW_MS) }),
      thread({ id: 'recent', updated_at: secAgo(60_000) }),
      thread({ id: 'old', updated_at: secAgo(ACTIVE_WINDOW_MS + 60_000) }),
      thread({ id: 'open-old', status: 'idle', updated_at: secAgo(10 * ACTIVE_WINDOW_MS) }),
    ];
    const { active, past } = partitionThreads(list, 'open-old', NOW);
    expect(active.map((t) => t.id)).toEqual(['running', 'recent', 'open-old']);
    expect(past.map((t) => t.id)).toEqual(['old']);
  });

  it('returns empty arrays for an empty list', () => {
    expect(partitionThreads([], null, NOW)).toEqual({ active: [], past: [] });
  });
});
