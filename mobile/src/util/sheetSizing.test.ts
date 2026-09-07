import { describe, expect, it } from 'vitest';
import {
  sheetMaxHeight,
  SHEET_MAX_FRACTION,
  SHEET_MIN_HEIGHT,
} from './sheetSizing';

/**
 * #662 — with the keyboard up, a 75%-of-screen sheet put most of its option
 * list underneath the keys. These pin the sizing decision; the visual result
 * still needs a simulator.
 */
describe('sheetMaxHeight', () => {
  it('keeps the old 75% behaviour while the keyboard is down', () => {
    expect(sheetMaxHeight(844)).toBe(Math.round(844 * SHEET_MAX_FRACTION));
    expect(sheetMaxHeight(844, 0)).toBe(Math.round(844 * SHEET_MAX_FRACTION));
  });

  it('fits inside the space the keyboard leaves', () => {
    // iPhone 14-class: 844pt tall, ~336pt keyboard → 508pt visible.
    const h = sheetMaxHeight(844, 336);
    expect(h).toBeLessThanOrEqual(844 - 336);
    // …and the old value would not have.
    expect(Math.round(844 * SHEET_MAX_FRACTION)).toBeGreaterThan(844 - 336);
  });

  it('leaves a strip of backdrop so the sheet stays dismissable', () => {
    expect(sheetMaxHeight(844, 336)).toBeLessThan(844 - 336);
  });

  it('still fits on a short device where the keyboard takes the most', () => {
    // iPhone SE-class: 667pt tall, ~300pt keyboard.
    const h = sheetMaxHeight(667, 300);
    expect(h).toBeLessThanOrEqual(667 - 300);
    expect(h).toBeGreaterThan(0);
  });

  it('never returns more than is visible, even below the minimum height', () => {
    const visible = 120; // absurdly small, e.g. a landscape phone
    const h = sheetMaxHeight(600, 600 - visible);
    expect(h).toBeLessThanOrEqual(visible);
    expect(SHEET_MIN_HEIGHT).toBeGreaterThan(visible);
  });

  it('gives back room up to the floor when the fraction is too mean', () => {
    // 500 visible → 0.92 × 500 = 460, above the floor, so the floor is moot.
    expect(sheetMaxHeight(900, 400)).toBeGreaterThanOrEqual(SHEET_MIN_HEIGHT);
  });

  it('shrugs off garbage input rather than returning NaN', () => {
    expect(sheetMaxHeight(0)).toBe(0);
    expect(sheetMaxHeight(844, -50)).toBe(Math.round(844 * SHEET_MAX_FRACTION));
    expect(sheetMaxHeight(844, 99999)).toBeGreaterThanOrEqual(0);
    expect(Number.isNaN(sheetMaxHeight(NaN, NaN))).toBe(false);
  });
});
