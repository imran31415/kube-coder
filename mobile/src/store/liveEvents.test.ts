/**
 * Live events (#833): a screen that opts in stops interval polling while the
 * desktop core's stream is up, and is back on its interval the moment it is
 * not. Nothing changes for a screen that does not opt in, or off desktop.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  _liveListenerCounts,
  _resetLiveForTest,
  emitLiveEvent,
  EVENT_DEBOUNCE_MS,
  isLiveConnected,
  matchesEvent,
  onLiveEvent,
  onLiveEventDebounced,
  pollPlan,
  SAFETY_POLL_MS,
  setLiveConnected,
  subscribeLive,
} from './liveEvents';

afterEach(() => _resetLiveForTest());

describe('pollPlan', () => {
  it('polls on the interval while there is no stream', () => {
    expect(pollPlan(false, 4000, ['task.'])).toEqual({ intervalMs: 4000, eventDriven: false, paused: false });
  });

  it('goes event-driven with only a safety poll while the stream is up', () => {
    expect(pollPlan(true, 4000, ['task.'])).toEqual({ intervalMs: SAFETY_POLL_MS, eventDriven: true, paused: false });
  });

  it('leaves a screen that did not opt in alone, stream or not', () => {
    expect(pollPlan(true, 3000)).toEqual({ intervalMs: 3000, eventDriven: false, paused: false });
    expect(pollPlan(true, 3000, [])).toEqual({ intervalMs: 3000, eventDriven: false, paused: false });
  });

  it('does not poll at all while the window is hidden, stream or not (#833)', () => {
    expect(pollPlan(false, 4000, ['task.'], false)).toMatchObject({ paused: true, eventDriven: false });
    expect(pollPlan(true, 4000, ['task.'], false)).toMatchObject({ paused: true, eventDriven: false });
    expect(pollPlan(true, 3000, undefined, false)).toMatchObject({ paused: true });
    expect(pollPlan(true, 3000, undefined, true)).toMatchObject({ paused: false });
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

describe('onLiveEventDebounced (#833)', () => {
  afterEach(() => vi.useRealTimers());

  it('turns a burst of matching events into one call', () => {
    vi.useFakeTimers();
    const fn = vi.fn();
    onLiveEventDebounced(['feed.'], fn);
    for (let i = 0; i < 1000; i++) emitLiveEvent({ type: 'feed.item', data: { id: `f${i}` } });
    emitLiveEvent({ type: 'task.status', data: {} });
    expect(fn).not.toHaveBeenCalled();
    vi.advanceTimersByTime(EVENT_DEBOUNCE_MS);
    expect(fn).toHaveBeenCalledTimes(1);
    emitLiveEvent({ type: 'task.status', data: {} });
    vi.advanceTimersByTime(EVENT_DEBOUNCE_MS);
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('drops a pending call and its listener on unsubscribe', () => {
    vi.useFakeTimers();
    const fn = vi.fn();
    const off = onLiveEventDebounced(['feed.'], fn);
    emitLiveEvent({ type: 'feed.item', data: {} });
    off();
    vi.advanceTimersByTime(EVENT_DEBOUNCE_MS * 2);
    expect(fn).not.toHaveBeenCalled();
    expect(_liveListenerCounts().events).toBe(0);
  });

  it('keeps no event: the listener count is all that a long stream leaves behind', () => {
    vi.useFakeTimers();
    const offs = [onLiveEventDebounced(['feed.'], () => {}), onLiveEventDebounced(['task.'], () => {})];
    for (let i = 0; i < 10_000; i++) {
      emitLiveEvent({ type: i % 2 ? 'feed.item' : 'task.status', data: { blob: 'x'.repeat(100) } });
      if (i % 100 === 0) vi.advanceTimersByTime(EVENT_DEBOUNCE_MS);
    }
    expect(_liveListenerCounts().events).toBe(2);
    offs.forEach((off) => off());
    expect(_liveListenerCounts().events).toBe(0);
  });

  it('a screen focused and blurred many times leaves exactly its one listener', () => {
    let off = () => {};
    for (let i = 0; i < 25; i++) {
      off();
      off = onLiveEventDebounced(['boards.'], () => {});
    }
    expect(_liveListenerCounts().events).toBe(1);
    off();
    expect(_liveListenerCounts().events).toBe(0);
  });
});
