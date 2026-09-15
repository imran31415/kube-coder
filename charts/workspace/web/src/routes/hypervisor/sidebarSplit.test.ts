import { describe, expect, it } from 'vitest';
import {
  SIDEBAR_W_DEFAULT,
  SIDEBAR_W_MAX,
  SIDEBAR_W_MIN,
  BRIEF_COLLAPSED_W,
  BRIEF_W,
  chatGridTemplate,
  clampSidebarW,
  initialSidebarW,
  readPaneCollapsed,
  resolvePaneCollapsed,
  writePaneCollapsed,
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

describe('chatGridTemplate (#683)', () => {
  it('is the two-column layout Chat has always had when no brief shows', () => {
    expect(chatGridTemplate({ sidebarW: 264, brief: 'none' })).toBe(
      '264px 6px minmax(0, 1fr)',
    );
  });

  it('adds a brief track at its full width when expanded', () => {
    expect(chatGridTemplate({ sidebarW: 264, brief: 'expanded' })).toBe(
      `264px 6px minmax(0, 1fr) ${BRIEF_W}px`,
    );
  });

  it('shrinks the brief track to the edge tab when collapsed', () => {
    expect(chatGridTemplate({ sidebarW: 264, brief: 'collapsed' })).toBe(
      `264px 6px minmax(0, 1fr) ${BRIEF_COLLAPSED_W}px`,
    );
  });

  it('rounds a dragged sidebar width to whole pixels', () => {
    expect(chatGridTemplate({ sidebarW: 271.4, brief: 'none' })).toContain('271px');
  });
});

describe('pane collapse choice', () => {
  it('round-trips an explicit choice', () => {
    expect(readPaneCollapsed(writePaneCollapsed(true))).toBe(true);
    expect(readPaneCollapsed(writePaneCollapsed(false))).toBe(false);
  });

  it('reads "never chosen" from an absent or junk value', () => {
    expect(readPaneCollapsed(null)).toBeNull();
    expect(readPaneCollapsed('yes')).toBeNull();
  });

  it('lets an explicit choice beat the auto-collapse heuristic both ways', () => {
    expect(resolvePaneCollapsed(false, true)).toBe(false);
    expect(resolvePaneCollapsed(true, false)).toBe(true);
    expect(resolvePaneCollapsed(null, true)).toBe(true);
    expect(resolvePaneCollapsed(null, false)).toBe(false);
  });
});
