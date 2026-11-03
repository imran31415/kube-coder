/**
 * The desktop bridge's listeners (#833): however often the UI boots, tears
 * down and the core's stream drops and comes back, each core event has exactly
 * one live listener, and none is left behind — a stale one would keep its
 * closure, and everything it reaches, alive for as long as the window is.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../push/tapRouter', () => ({ handleNotificationTap: vi.fn() }));
vi.mock('../store/nav', () => ({ navigationRef: { isReady: () => false, resetRoot: vi.fn() } }));

import { _liveListenerCounts, _resetLiveForTest, isLiveConnected, onLiveEvent } from '../store/liveEvents';
import { initDesktop } from './desktopApp';

type Cb = (e: { payload: unknown }) => void;

/** A stand-in for Tauri's global: listen() resolves on a later tick, like the
 *  real IPC round trip, and the test can count what is still registered. */
function fakeTauri() {
  const live = new Map<number, { event: string; cb: Cb }>();
  let next = 1;
  const g = {
    core: {
      invoke: vi.fn(async (cmd: string) => {
        if (cmd === 'kc_take_targets') return [];
        if (cmd === 'kc_live_status') return { mode: 'off', keeper: null, needsYou: 0, feedFetches: 0 };
        return null;
      }),
    },
    event: {
      listen: (event: string, cb: Cb) =>
        new Promise<() => void>((resolve) =>
          setTimeout(() => {
            const id = next++;
            live.set(id, { event, cb });
            resolve(() => {
              live.delete(id);
            });
          }, 5),
        ),
    },
  };
  const emit = (event: string, payload: unknown) => {
    for (const l of [...live.values()]) if (l.event === event) l.cb({ payload });
  };
  const count = (event?: string) => [...live.values()].filter((l) => !event || l.event === event).length;
  return { g, emit, count };
}

const tick = () => new Promise((r) => setTimeout(r, 20));
let bridge: ReturnType<typeof fakeTauri>;

beforeEach(() => {
  bridge = fakeTauri();
  (globalThis as { __TAURI__?: unknown }).__TAURI__ = bridge.g;
});

afterEach(() => {
  delete (globalThis as { __TAURI__?: unknown }).__TAURI__;
  _resetLiveForTest();
});

describe('initDesktop listeners', () => {
  it('registers one listener per core event and removes them all on teardown', async () => {
    const off = initDesktop();
    await tick();
    expect(bridge.count('kc://live')).toBe(1);
    expect(bridge.count('kc://event')).toBe(1);
    expect(bridge.count('kc://targets')).toBe(1);
    off();
    expect(bridge.count()).toBe(0);
  });

  it('removes a listener whose registration lands after teardown', async () => {
    const off = initDesktop();
    off(); // before any listen() has resolved
    await tick();
    expect(bridge.count()).toBe(0);
  });

  it('leaves exactly one live listener per event after repeated mount and unmount', async () => {
    for (let i = 0; i < 5; i++) {
      const off = initDesktop();
      if (i % 2) await tick();
      off();
    }
    const off = initDesktop();
    await tick();
    expect(bridge.count()).toBe(3);
    off();
    await tick();
    expect(bridge.count()).toBe(0);
  });

  it('adds no listener when the stream drops and reconnects', async () => {
    const off = initDesktop();
    await tick();
    const seen = vi.fn();
    onLiveEvent(seen);
    const before = _liveListenerCounts();
    for (let i = 0; i < 50; i++) {
      bridge.emit('kc://live', { mode: 'stream', keeper: false, needsYou: 0, feedFetches: i });
      bridge.emit('kc://live', { mode: 'polling', keeper: false, needsYou: 0, feedFetches: i });
    }
    bridge.emit('kc://live', { mode: 'stream', keeper: false, needsYou: 0, feedFetches: 99 });
    expect(isLiveConnected()).toBe(true);
    expect(bridge.count()).toBe(3);
    expect(_liveListenerCounts()).toEqual(before);
    bridge.emit('kc://event', { type: 'feed.item', data: { id: 'f1' } });
    expect(seen).toHaveBeenCalledTimes(1);
    off();
  });
});
