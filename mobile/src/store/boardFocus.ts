/**
 * A pending "open this board item" request (#692).
 *
 * A board review push and a Feed link chip both name one item on one board
 * ("board:<board_id>:<item_id>"), and both have to land on the Board screen
 * with that item in view. Route params cannot carry it: Board is a TAB screen
 * that stays mounted, so a param set once goes stale and the next tap on a
 * different item re-renders with the old value. So the request lives here —
 * the same tiny pub/sub the nav drawer uses — and the screen consumes it.
 *
 * `seq` is what makes two taps in a row work. Without it, tapping the same
 * notification twice would set an identical object and the screen's effect,
 * keyed on the value, would never re-run.
 */
import { useEffect, useState } from 'react';

export interface BoardFocusRequest {
  boardId: string;
  itemId: string;
  /** Monotonic. Two requests for the same item are still two requests. */
  seq: number;
}

let pending: BoardFocusRequest | null = null;
let seq = 0;
const listeners = new Set<(v: BoardFocusRequest | null) => void>();

/** Ask the Board screen to select `boardId` and bring `itemId` into view.
 *  A blank id is ignored — the caller has already fallen back to the Feed. */
export function requestBoardFocus(boardId: string, itemId: string): BoardFocusRequest | null {
  if (!boardId || !itemId) return null;
  seq += 1;
  pending = { boardId, itemId, seq };
  listeners.forEach((l) => l(pending));
  return pending;
}

/** The outstanding request, if any. */
export function peekBoardFocus(): BoardFocusRequest | null {
  return pending;
}

/**
 * Retire a request once the screen has acted on it.
 *
 * Pass the `seq` that was acted on. If a NEWER request arrived while the screen
 * was still resolving this one — two pushes in quick succession — the clear is
 * a no-op and the newer request survives. Clearing blind would drop it, and the
 * second tap is the one the user is actually waiting on.
 */
export function clearBoardFocus(forSeq?: number): void {
  if (pending === null) return;
  if (forSeq !== undefined && forSeq !== pending.seq) return;
  pending = null;
  listeners.forEach((l) => l(null));
}

/** Subscribe a component to the outstanding request. */
export function useBoardFocus(): BoardFocusRequest | null {
  const [v, set] = useState(pending);
  useEffect(() => {
    listeners.add(set);
    // A request can land between render and subscribe (a cold tap resolves
    // while the screen is still mounting), so re-read rather than trust the
    // initial state.
    set(pending);
    return () => {
      listeners.delete(set);
    };
  }, []);
  return v;
}

/** Test seam — module state outlives a vitest file otherwise. */
export function _resetBoardFocusForTest(): void {
  pending = null;
  seq = 0;
  listeners.clear();
}
