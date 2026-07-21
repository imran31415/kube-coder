import { describe, expect, it } from 'vitest';
import {
  SIDEBAR_W_DEFAULT,
  SIDEBAR_W_MAX,
  SIDEBAR_W_MIN,
  clampSidebarW,
  initialSidebarW,
} from './sidebarSplit';

describe('clampSidebarW', () => {
  it('passes through widths inside the range', () => {
    expect(clampSidebarW(300)).toBe(300);
  });

  it('clamps below the minimum', () => {
    expect(clampSidebarW(0)).toBe(SIDEBAR_W_MIN);
    expect(clampSidebarW(-50)).toBe(SIDEBAR_W_MIN);
  });

  it('clamps above the maximum', () => {
    expect(clampSidebarW(10_000)).toBe(SIDEBAR_W_MAX);
  });
});

describe('initialSidebarW', () => {
  it('restores a persisted in-range width', () => {
    expect(initialSidebarW('320')).toBe(320);
  });

  it('falls back to the default when nothing is stored', () => {
    expect(initialSidebarW(null)).toBe(SIDEBAR_W_DEFAULT);
  });

  it('falls back on garbage', () => {
    expect(initialSidebarW('not-a-number')).toBe(SIDEBAR_W_DEFAULT);
    expect(initialSidebarW('')).toBe(SIDEBAR_W_DEFAULT);
  });

  it('falls back on out-of-range values so a bad write cannot wedge the layout', () => {
    expect(initialSidebarW('10')).toBe(SIDEBAR_W_DEFAULT);
    expect(initialSidebarW('99999')).toBe(SIDEBAR_W_DEFAULT);
  });
});
