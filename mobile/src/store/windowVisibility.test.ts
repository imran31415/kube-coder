/** Window visibility (#833): what pauses polling and spinners. */
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  _resetWindowVisibleForTest,
  _windowVisibleListenerCount,
  isWindowVisible,
  setWindowVisible,
  subscribeWindowVisible,
  visibleFromAppState,
} from './windowVisibility';

afterEach(() => _resetWindowVisibleForTest());

describe('window visibility', () => {
  it('only a backgrounded app counts as hidden', () => {
    expect(visibleFromAppState('active')).toBe(true);
    expect(visibleFromAppState('inactive')).toBe(true); // iOS: notification centre over the app
    expect(visibleFromAppState('background')).toBe(false);
    expect(visibleFromAppState(null)).toBe(true);
  });

  it('starts visible and notifies on change only', () => {
    expect(isWindowVisible()).toBe(true);
    const l = vi.fn();
    subscribeWindowVisible(l);
    setWindowVisible(true);
    setWindowVisible(false);
    setWindowVisible(false);
    setWindowVisible(true);
    expect(l.mock.calls).toEqual([[false], [true]]);
  });

  it('unsubscribing leaves no listener behind', () => {
    const offs = Array.from({ length: 10 }, () => subscribeWindowVisible(() => {}));
    expect(_windowVisibleListenerCount()).toBe(10);
    offs.forEach((off) => off());
    expect(_windowVisibleListenerCount()).toBe(0);
  });
});
