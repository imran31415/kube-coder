import { describe, it, expect } from 'vitest';
import { turnWindow, TURN_WINDOW, TURN_WINDOW_STEP } from './transcriptWindow';

describe('turnWindow', () => {
  it('hides nothing when the thread fits in the window', () => {
    expect(turnWindow(0, TURN_WINDOW)).toEqual({ start: 0, hidden: 0 });
    expect(turnWindow(5, TURN_WINDOW)).toEqual({ start: 0, hidden: 0 });
    expect(turnWindow(TURN_WINDOW, TURN_WINDOW)).toEqual({ start: 0, hidden: 0 });
  });

  it('renders only the tail once the thread exceeds the window', () => {
    const total = TURN_WINDOW + 12;
    expect(turnWindow(total, TURN_WINDOW)).toEqual({ start: 12, hidden: 12 });
  });

  it('reveals another page when visible grows by a step', () => {
    const total = TURN_WINDOW * 4;
    const first = turnWindow(total, TURN_WINDOW);
    expect(first.hidden).toBe(total - TURN_WINDOW);
    const next = turnWindow(total, TURN_WINDOW + TURN_WINDOW_STEP);
    expect(next.hidden).toBe(total - TURN_WINDOW - TURN_WINDOW_STEP);
    expect(next.start).toBeLessThan(first.start);
  });

  it('never overshoots the total, even when visible exceeds it', () => {
    expect(turnWindow(10, 999)).toEqual({ start: 0, hidden: 0 });
  });

  it('clamps a below-minimum visible up to the window floor', () => {
    const total = TURN_WINDOW + 5;
    // visible smaller than TURN_WINDOW still renders a full window.
    expect(turnWindow(total, 1)).toEqual({ start: 5, hidden: 5 });
  });

  it('treats a negative total as empty', () => {
    expect(turnWindow(-3, TURN_WINDOW)).toEqual({ start: 0, hidden: 0 });
  });
});
