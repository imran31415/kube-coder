/**
 * How tall a bottom sheet may be, given the keyboard (#662).
 *
 * Kept out of the components so it can be tested directly — this app's suite
 * is pure-logic, with no React Native render harness.
 *
 * The bug this exists to prevent: a sheet capped at a fraction of the *screen*
 * looks right with the keyboard down and hides most of its list once the
 * keyboard is up, because the keyboard eats ~40% of a phone screen and the
 * sheet is still sized against the full height. A `KeyboardAvoidingView`
 * alone does not fix that — it moves the sheet up, and the top of an
 * over-tall sheet then runs off the top of the screen instead.
 */

/** Share of the available space a sheet may occupy with the keyboard down. */
export const SHEET_MAX_FRACTION = 0.75;

/** Share it may occupy once the keyboard is up. Higher, because the space is
 *  scarce, but short of 1 so a strip of backdrop stays tappable to dismiss. */
export const SHEET_MAX_FRACTION_KEYBOARD = 0.92;

/** Never shrink below this — a sheet with no room for a row is not a sheet.
 *  Only applies while there is at least this much visible space. */
export const SHEET_MIN_HEIGHT = 220;

export function sheetMaxHeight(screenHeight: number, keyboardHeight = 0): number {
  const screen = Math.max(0, screenHeight || 0);
  const keyboard = Math.max(0, Math.min(keyboardHeight || 0, screen));
  const visible = screen - keyboard;
  if (keyboard === 0) return Math.round(screen * SHEET_MAX_FRACTION);
  const capped = Math.min(
    screen * SHEET_MAX_FRACTION,
    visible * SHEET_MAX_FRACTION_KEYBOARD,
  );
  // On a short device the fraction can fall under a usable sheet; give back
  // the space up to the floor, but never more than is actually visible.
  return Math.round(Math.max(Math.min(SHEET_MIN_HEIGHT, visible), capped));
}
