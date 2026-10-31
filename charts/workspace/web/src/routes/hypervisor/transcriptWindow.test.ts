import { describe, it, expect } from 'vitest';
import {
  turnWindow,
  shouldRevealEarlier,
  scrollTopAfterReveal,
  REVEAL_NEAR_TOP_PX,
  TURN_WINDOW,
  TURN_WINDOW_STEP,
} from './transcriptWindow';

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

describe('shouldRevealEarlier (#814)', () => {
  it('loads older turns only inside the runway near the top', () => {
    expect(shouldRevealEarlier(5, 0)).toBe(true);
    expect(shouldRevealEarlier(5, REVEAL_NEAR_TOP_PX - 1)).toBe(true);
    expect(shouldRevealEarlier(5, REVEAL_NEAR_TOP_PX)).toBe(false);
  });

  it('never loads when nothing is hidden', () => {
    expect(shouldRevealEarlier(0, 0)).toBe(false);
  });
});

describe('scrollTopAfterReveal (#814)', () => {
  it('shifts scrollTop by exactly the height added above the reader', () => {
    expect(scrollTopAfterReveal({ height: 3000, top: 0 }, 8400)).toBe(5400);
    expect(scrollTopAfterReveal({ height: 3000, top: 300 }, 6000)).toBe(3300);
  });

  it('leaves the reader alone when the content did not grow', () => {
    expect(scrollTopAfterReveal({ height: 3000, top: 300 }, 3000)).toBeNull();
    // A source swap can shrink the flow in the same frame — not a reveal.
    expect(scrollTopAfterReveal({ height: 3000, top: 300 }, 2500)).toBeNull();
  });
});
