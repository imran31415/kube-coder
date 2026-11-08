import { describe, expect, it } from 'vitest';

import { NAV_STATE_KEY, parseNavState, serializeNavState } from './navSession';

const state = {
  type: 'tab',
  key: 'tab-1',
  index: 1,
  routeNames: ['Desktop', 'Board', 'Tasks'],
  routes: [
    { key: 'Desktop-1', name: 'Desktop' },
    { key: 'Board-1', name: 'Board' },
    {
      key: 'Tasks-1',
      name: 'Tasks',
      state: { index: 1, routes: [{ name: 'TaskList' }, { name: 'TaskDetail', params: { id: 't1' } }] },
    },
  ],
  stale: false,
};

describe('the saved desktop screen', () => {
  it('is a memory-only core item', () => {
    // desktop/src-tauri/src/settings.rs keeps kc.session.* out of the file.
    expect(NAV_STATE_KEY.startsWith('kc.session.')).toBe(true);
  });

  it('round-trips a navigator state', () => {
    const s = serializeNavState(state);
    expect(s).not.toBeNull();
    expect(parseNavState(s)).toEqual(state);
  });

  it('refuses anything that is not a navigator state', () => {
    for (const bad of [undefined, null, 'x', {}, { index: 0, routes: [] }, { index: 2, routes: [{ name: 'A' }] }, { index: 0, routes: [{}] }]) {
      expect(serializeNavState(bad)).toBeNull();
    }
    expect(parseNavState(null)).toBeNull();
    expect(parseNavState('')).toBeNull();
    expect(parseNavState('{not json')).toBeNull();
    expect(parseNavState('{"index":0,"routes":[{"name":7}]}')).toBeNull();
  });

  it('does not save a state too large for the core', () => {
    const big = { index: 0, routes: [{ name: 'Feed', params: { blob: 'x'.repeat(40 * 1024) } }] };
    expect(serializeNavState(big)).toBeNull();
    expect(parseNavState(JSON.stringify(big))).toBeNull();
  });
});
