/**
 * Live events (#833): a screen that opts in stops interval polling while the
 * desktop core's stream is up, and is back on its interval the moment it is
 * not. Nothing changes for a screen that does not opt in, or off desktop.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  _resetLiveForTest,
  emitLiveEvent,
  isLiveConnected,
  matchesEvent,
  onLiveEvent,
  pollPlan,
  SAFETY_POLL_MS,
  setLiveConnected,
  subscribeLive,
} from './liveEvents';

afterEach(() => _resetLiveForTest());

describe('pollPlan', () => {
  it('polls on the interval while there is no stream', () => {
    expect(pollPlan(false, 4000, ['task.'])).toEqual({ intervalMs: 4000, eventDriven: false });
  });

  it('goes event-driven with only a safety poll while the stream is up', () => {
    expect(pollPlan(true, 4000, ['task.'])).toEqual({ intervalMs: SAFETY_POLL_MS, eventDriven: true });
  });

  it('leaves a screen that did not opt in alone, stream or not', () => {
    expect(pollPlan(true, 3000)).toEqual({ intervalMs: 3000, eventDriven: false });
    expect(pollPlan(true, 3000, [])).toEqual({ intervalMs: 3000, eventDriven: false });
  });

  it('cuts a 4 s screen to at most one request per safety window', () => {
    const minute = 60_000;
    const before = (10 * minute) / pollPlan(false, 4000, ['task.']).intervalMs;
    const after = (10 * minute) / pollPlan(true, 4000, ['task.']).intervalMs;
    expect(before).toBe(150);
    expect(after).toBe(2);
  });
});

describe('the stream status', () => {
  it('starts down and notifies on change only', () => {
    expect(isLiveConnected()).toBe(false);
    const l = vi.fn();
    subscribeLive(l);
    setLiveConnected(true);
    setLiveConnected(true);
    setLiveConnected(false);
    expect(l.mock.calls).toEqual([[true], [false]]);
  });
});

describe('events', () => {
  it('match by exact type or prefix', () => {
    expect(matchesEvent('feed.item', ['feed.'])).toBe(true);
    expect(matchesEvent('boards.review', ['task.', 'boards.'])).toBe(true);
    expect(matchesEvent('task.status', ['feed.'])).toBe(false);
    expect(matchesEvent('memory.changed', ['memory.changed'])).toBe(true);
  });

  it('reach every subscriber even when one throws', () => {
    const bad = vi.fn(() => {
      throw new Error('boom');
    });
    const good = vi.fn();
    onLiveEvent(bad);
    const off = onLiveEvent(good);
    emitLiveEvent({ type: 'feed.item', data: { id: 'f1' } });
    expect(good).toHaveBeenCalledWith({ type: 'feed.item', data: { id: 'f1' } });
    off();
    emitLiveEvent({ type: 'feed.item', data: {} });
    expect(good).toHaveBeenCalledTimes(1);
  });
});
