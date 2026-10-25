/**
 * The two *different* bottom-proximity questions the Hypervisor chat transcript
 * asks (#530, #790) — kept pure so each has one definition and a unit test.
 *
 * They are not the same question, and conflating them is what broke scrolling:
 *
 *  - `isFollowing` is the loose one. "Is the reader still following along?"
 *    decides whether a *new* message should pull them down with it, so it wants
 *    a generous fuzz zone: someone parked a few dozen pixels off the bottom is
 *    still reading the newest message, not browsing history.
 *
 *  - `isAtBottom` is the strict one. "Is the view glued to the literal bottom
 *    right now?" gates the *corrective* write a ResizeObserver makes when
 *    content reflows under a reader (an image finishing load, a lazily-mounted
 *    embed, the composer auto-growing). That correction moves the viewport
 *    without the reader asking, so it may only fire when there is effectively
 *    nothing to correct — a few pixels of sub-pixel/rounding slack, no more.
 *    Gating it on `isFollowing` instead meant any incidental reflow snapped a
 *    reader scrolling up back to the bottom before they could clear the fuzz
 *    zone, so in a media-heavy thread scrolling up never progressed (#790).
 */

/** How far from the bottom still counts as "following the newest message". */
export const FOLLOW_FUZZ_PX = 80;

/** Slack allowed when asking whether the view sits *exactly* at the bottom —
 *  enough to absorb fractional device-pixel scroll offsets, nothing more. */
export const AT_BOTTOM_PX = 4;

/** The three scroll numbers both questions are answered from. Matches the
 *  shape of a real scroll container, so callers pass the element itself. */
export interface ScrollMetrics {
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
}

/** Pixels of unseen content below the viewport (0 when the content fits). */
export function distanceFromBottom(m: ScrollMetrics): number {
  return Math.max(0, m.scrollHeight - m.scrollTop - m.clientHeight);
}

/** Loose: the reader is close enough that a new message should pull them along. */
export function isFollowing(m: ScrollMetrics): boolean {
  return distanceFromBottom(m) < FOLLOW_FUZZ_PX;
}

/** Strict: the view is parked on the literal bottom, so a corrective scroll
 *  write would not actually take anything away from the reader. */
export function isAtBottom(m: ScrollMetrics): boolean {
  return distanceFromBottom(m) <= AT_BOTTOM_PX;
}
