import { describe, expect, it } from 'vitest';
import {
  clampCtoRailW,
  initialCtoRailW,
  ctoGridTemplate,
  readPaneCollapsed,
  writePaneCollapsed,
  resolvePaneCollapsed,
  CTO_RAIL_W_DEFAULT,
  CTO_RAIL_W_MIN,
  CTO_RAIL_W_MAX,
} from './railSplit';

describe('cto rail width', () => {
  it('clamps a dragged width to the track range', () => {
    expect(clampCtoRailW(0)).toBe(CTO_RAIL_W_MIN);
    expect(clampCtoRailW(9999)).toBe(CTO_RAIL_W_MAX);
    expect(clampCtoRailW(300)).toBe(300);
  });

  it('falls back to the default for a missing or out-of-range persisted width', () => {
    expect(initialCtoRailW(null)).toBe(CTO_RAIL_W_DEFAULT);
    expect(initialCtoRailW('nonsense')).toBe(CTO_RAIL_W_DEFAULT);
    expect(initialCtoRailW('10')).toBe(CTO_RAIL_W_DEFAULT);
    expect(initialCtoRailW('300')).toBe(300);
  });
});

describe('cto pane collapse (#530)', () => {
  it('round-trips a persisted choice, and treats anything else as "not chosen"', () => {
    expect(readPaneCollapsed(writePaneCollapsed(true))).toBe(true);
    expect(readPaneCollapsed(writePaneCollapsed(false))).toBe(false);
    expect(readPaneCollapsed(null)).toBeNull();
    expect(readPaneCollapsed('true')).toBeNull();
  });

  it('lets an explicit choice win over the auto-collapse heuristic', () => {
    expect(resolvePaneCollapsed(null, true)).toBe(true); // heuristic decides
    expect(resolvePaneCollapsed(null, false)).toBe(false);
    expect(resolvePaneCollapsed(false, true)).toBe(false); // user pinned it open
    expect(resolvePaneCollapsed(true, false)).toBe(true); // user folded it away
  });

  it('drops the splitter track when the rail is collapsed', () => {
    expect(ctoGridTemplate({ railW: 248, railCollapsed: false, briefCollapsed: false })).toBe(
      '248px 6px minmax(0, 1fr) 320px',
    );
    expect(ctoGridTemplate({ railW: 248, railCollapsed: true, briefCollapsed: false })).toBe(
      '48px minmax(0, 1fr) 320px',
    );
    expect(ctoGridTemplate({ railW: 248, railCollapsed: true, briefCollapsed: true })).toBe(
      '48px minmax(0, 1fr) 34px',
    );
  });

  it('rounds a dragged sub-pixel rail width into the template', () => {
    expect(ctoGridTemplate({ railW: 248.4, railCollapsed: false, briefCollapsed: true })).toBe(
      '248px 6px minmax(0, 1fr) 34px',
    );
  });
});
