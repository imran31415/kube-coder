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
    expect(isActiveThread(t, NOW)).toBe(true);
  });

  /** #620: bucketing is a property of the thread alone. Being open must not
   *  change which bucket a thread belongs to — that is what made a chat vanish
   *  from Past the moment it was clicked. Visibility of the open chat is
   *  handled by pinning in partitionThreads, not by reclassifying. */
  it('does not treat an old idle thread as active just because it is open', () => {
    const t = thread({ id: 'open', status: 'idle', updated_at: secAgo(30 * ACTIVE_WINDOW_MS) });
    expect(isActiveThread(t, NOW)).toBe(false);
  });

  it('is active when idle but updated within the window', () => {
    const t = thread({ status: 'idle', updated_at: secAgo(ACTIVE_WINDOW_MS - 60_000) });
    expect(isActiveThread(t, NOW)).toBe(true);
  });

  it('is past when idle and older than the window', () => {
    const t = thread({ status: 'idle', updated_at: secAgo(ACTIVE_WINDOW_MS + 60_000) });
    expect(isActiveThread(t, NOW)).toBe(false);
  });

  it('falls back to created_at when updated_at is null', () => {
    const recent = thread({ updated_at: null, created_at: secAgo(60_000) });
    const old = thread({ updated_at: null, created_at: secAgo(ACTIVE_WINDOW_MS + 60_000) });
    expect(isActiveThread(recent, NOW)).toBe(true);
    expect(isActiveThread(old, NOW)).toBe(false);
  });

  it('is past when both timestamps are null', () => {
    const t = thread({ updated_at: null, created_at: null });
    expect(isActiveThread(t, NOW)).toBe(false);
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
    // 'open-old' stays in past (its natural bucket) and is pinned into active
    // so it is visible whichever tab is showing — it is not moved out of past.
    expect(active.map((t) => t.id)).toEqual(['open-old', 'running', 'recent']);
    // past keeps input order; 'open-old' is there naturally, after 'old'.
    expect(past.map((t) => t.id)).toEqual(['old', 'open-old']);
  });

  it('returns empty arrays for an empty list', () => {
    expect(partitionThreads([], null, NOW)).toEqual({ active: [], past: [] });
  });
});

/**
 * Regression cover for #620 — "clicking a task in the sidebar makes it
 * disappear from the list". Reported as blocking: the entry vanished on click,
 * so the user could not tell which chat was open or rename it, and a chat that
 * had moved to the tab they were not on looked lost entirely.
 */
describe('#620 — opening a chat never removes it from the list you are on', () => {
  const oldIdle = () =>
    thread({ id: 'old-chat', status: 'idle', updated_at: secAgo(30 * ACTIVE_WINDOW_MS) });

  it('keeps an old chat in Past after it is opened', () => {
    const list = [oldIdle()];
    const before = partitionThreads(list, null, NOW);
    expect(before.past.map((t) => t.id)).toEqual(['old-chat']);

    // …the user clicks it. It must still be in the Past list they are looking at.
    const after = partitionThreads(list, 'old-chat', NOW);
    expect(after.past.map((t) => t.id)).toEqual(['old-chat']);
  });

  it('shows the open chat on the Active tab too, so it is never hidden', () => {
    const { active } = partitionThreads([oldIdle()], 'old-chat', NOW);
    expect(active.map((t) => t.id)).toEqual(['old-chat']);
  });

  it('never drops the open chat from BOTH lists', () => {
    const list = [
      oldIdle(),
      thread({ id: 'recent', updated_at: secAgo(60_000) }),
    ];
    for (const openId of [null, 'old-chat', 'recent']) {
      const { active, past } = partitionThreads(list, openId, NOW);
      for (const t of list) {
        const seen = active.some((a) => a.id === t.id) || past.some((p) => p.id === t.id);
        expect(seen, `${t.id} vanished with openId=${openId}`).toBe(true);
      }
    }
  });

  it('does not duplicate an already-active open chat within one list', () => {
    const list = [thread({ id: 'recent', updated_at: secAgo(60_000) })];
    const { active, past } = partitionThreads(list, 'recent', NOW);
    expect(active.map((t) => t.id)).toEqual(['recent']);
    // Pinned into the other list so the Past tab still shows what is open.
    expect(past.map((t) => t.id)).toEqual(['recent']);
  });

  it('ignores an openId that is not in the list', () => {
    const list = [thread({ id: 'recent', updated_at: secAgo(60_000) })];
    expect(partitionThreads(list, 'gone', NOW)).toEqual({
      active: [list[0]],
      past: [],
    });
  });
});
