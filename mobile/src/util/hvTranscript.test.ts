import { describe, it, expect } from 'vitest';
import { turnWindow, TURN_WINDOW, TURN_WINDOW_STEP } from './hvTranscript';

describe('turnWindow (mobile)', () => {
  it('hides nothing when the thread fits in the window', () => {
    expect(turnWindow(0, TURN_WINDOW)).toEqual({ start: 0, hidden: 0 });
    expect(turnWindow(TURN_WINDOW, TURN_WINDOW)).toEqual({ start: 0, hidden: 0 });
  });

  it('renders only the tail once the thread exceeds the window', () => {
    const total = TURN_WINDOW + 9;
    expect(turnWindow(total, TURN_WINDOW)).toEqual({ start: 9, hidden: 9 });
  });

  it('reveals another page when visible grows by a step', () => {
    const total = TURN_WINDOW * 3;
    const next = turnWindow(total, TURN_WINDOW + TURN_WINDOW_STEP);
    expect(next.hidden).toBe(total - TURN_WINDOW - TURN_WINDOW_STEP);
  });

  it('never overshoots the total and clamps a tiny visible up to the floor', () => {
    expect(turnWindow(10, 999)).toEqual({ start: 0, hidden: 0 });
    expect(turnWindow(TURN_WINDOW + 4, 1)).toEqual({ start: 4, hidden: 4 });
  });
});
