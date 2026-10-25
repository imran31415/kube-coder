import { describe, it, expect } from 'vitest';
import {
  distanceFromBottom,
  isFollowing,
  isAtBottom,
  FOLLOW_FUZZ_PX,
  AT_BOTTOM_PX,
  type ScrollMetrics,
} from './scrollPin';

/** A transcript 2000px tall in a 500px viewport, scrolled `fromBottom` px up. */
function at(fromBottom: number): ScrollMetrics {
  return { scrollHeight: 2000, clientHeight: 500, scrollTop: 1500 - fromBottom };
}

describe('distanceFromBottom', () => {
  it('measures the unseen content below the viewport', () => {
    expect(distanceFromBottom(at(0))).toBe(0);
    expect(distanceFromBottom(at(250))).toBe(250);
  });

  it('is 0 when the content is shorter than the viewport', () => {
    expect(distanceFromBottom({ scrollTop: 0, scrollHeight: 120, clientHeight: 500 })).toBe(0);
  });
});

describe('isFollowing — should a new message pull the reader down?', () => {
  it('yes anywhere inside the fuzz zone', () => {
    expect(isFollowing(at(0))).toBe(true);
    expect(isFollowing(at(FOLLOW_FUZZ_PX - 1))).toBe(true);
  });

  it('no once the reader has scrolled clear of it', () => {
    expect(isFollowing(at(FOLLOW_FUZZ_PX))).toBe(false);
    expect(isFollowing(at(600))).toBe(false);
  });

  it('yes when the whole thread fits with no scrolling', () => {
    expect(isFollowing({ scrollTop: 0, scrollHeight: 120, clientHeight: 500 })).toBe(true);
  });
});

describe('isAtBottom — may a reflow correct the viewport? (#790)', () => {
  it('yes only at the literal bottom, give or take sub-pixel slack', () => {
    expect(isAtBottom(at(0))).toBe(true);
    expect(isAtBottom(at(AT_BOTTOM_PX))).toBe(true);
    expect(isAtBottom({ scrollTop: 1499.5, scrollHeight: 2000, clientHeight: 500 })).toBe(true);
  });

  it('no as soon as the reader has moved a real distance up', () => {
    expect(isAtBottom(at(AT_BOTTOM_PX + 1))).toBe(false);
    expect(isAtBottom(at(40))).toBe(false);
  });

  // The regression itself: a reader mid-gesture inside the 80px fuzz zone still
  // counts as "following", so new messages follow them — but an incidental
  // reflow must NOT yank them back, or they can never escape the zone.
  it('is strictly tighter than isFollowing inside the fuzz zone', () => {
    const mid = at(FOLLOW_FUZZ_PX - 10);
    expect(isFollowing(mid)).toBe(true);
    expect(isAtBottom(mid)).toBe(false);
  });

  it('yes when the thread is too short to scroll at all', () => {
    expect(isAtBottom({ scrollTop: 0, scrollHeight: 120, clientHeight: 500 })).toBe(true);
  });
});
