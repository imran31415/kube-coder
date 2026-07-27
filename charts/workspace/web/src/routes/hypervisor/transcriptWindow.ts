/**
 * How much of a long chat transcript to keep mounted (#525). The Hypervisor /
 * AI CTO chat rendered every turn ever exchanged, so a long founder/ops session
 * accumulated unbounded DOM — plus live `show_app_preview` iframes, `show_media`
 * players and `show_file` frames that were never unmounted — until the tab went
 * sluggish and crashed. We render only the most recent `visible` turns from the
 * tail (bounded), with a "Show earlier" affordance that reveals older ones a
 * page at a time. This is pure so it can be unit-tested and shared in spirit
 * with the mobile port (see mobile/src/util/hvTranscript.ts turnWindow()).
 */

/** Turns rendered from the tail by default — enough that a normal session never
 *  hides anything, small enough that a thousand-turn thread stays light. */
export const TURN_WINDOW = 30;

/** How many more turns each "Show earlier messages" tap reveals. */
export const TURN_WINDOW_STEP = 30;

export interface TurnWindow {
  /** Index of the first rendered turn — turns before it are hidden. */
  start: number;
  /** How many turns are hidden above the window (0 ⇒ the whole thread fits). */
  hidden: number;
}

/**
 * Given the total turn count and how many the user has chosen to reveal, return
 * the tail slice to render. `visible` is clamped up to at least TURN_WINDOW and
 * down to the total, so callers can seed it at TURN_WINDOW and grow it by
 * TURN_WINDOW_STEP without any bounds bookkeeping of their own.
 */
export function turnWindow(total: number, visible: number): TurnWindow {
  const shown = Math.min(Math.max(0, total), Math.max(TURN_WINDOW, visible));
  const start = Math.max(0, total - shown);
  return { start, hidden: start };
}
