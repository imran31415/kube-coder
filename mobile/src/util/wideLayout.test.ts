/**
 * The wide layout's boundary (#833). The phone layout must be untouched: on a
 * phone, a tablet, in a browser at any width (the store screenshots render an
 * iPad at 1024px from the web export) and in a desktop window below the
 * breakpoint.
 */
import { describe, expect, it } from 'vitest';
import { isWideLayout, SIDEBAR_WIDTH, WIDE_BREAKPOINT } from './wideLayout';

describe('isWideLayout', () => {
  it('is the phone layout everywhere outside the desktop app, at any width', () => {
    for (const w of [320, 390, 430, 800, 1024, 1366, 2560]) {
      expect(isWideLayout(false, w)).toBe(false);
    }
  });

  it('is the phone layout in a narrow desktop window', () => {
    expect(isWideLayout(true, 380)).toBe(false);
    expect(isWideLayout(true, WIDE_BREAKPOINT - 1)).toBe(false);
  });

  it('switches to the sidebar at the breakpoint', () => {
    expect(isWideLayout(true, WIDE_BREAKPOINT)).toBe(true);
    expect(isWideLayout(true, 1280)).toBe(true);
  });

  it('leaves the content pane wider than a phone', () => {
    expect(WIDE_BREAKPOINT - SIDEBAR_WIDTH).toBeGreaterThan(430);
  });
});
