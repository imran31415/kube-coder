/**
 * The board the phone was last looking at (#712).
 *
 * The Board screen used to open on whichever board happened to sort first,
 * which on a workspace with two connected trackers meant the queue you were
 * working yesterday was one tap away every single time. Remembering the choice
 * is the whole of it.
 *
 * Storage is INJECTED rather than imported, the same as `approvalQueue` — that
 * is what lets the node-side vitest suite cover this without a React Native
 * renderer.
 */

/** Minimal storage shape — `AsyncStorage` satisfies it, and so does a Map. */
export interface LastBoardStorage {
  getItem(key: string): Promise<string | null>;
  setItem(key: string, value: string): Promise<void>;
}

export const LAST_BOARD_KEY = 'kc.board.lastId';

/** Remember the board in view. Failures are swallowed: a preference that did
 *  not persist must never break the screen that set it. */
export async function rememberBoard(
  storage: LastBoardStorage,
  boardId: string,
): Promise<void> {
  if (!boardId) return;
  try {
    await storage.setItem(LAST_BOARD_KEY, boardId);
  } catch {
    /* preference only */
  }
}

/**
 * Which board to open, given what is connected now.
 *
 * The remembered board wins; a board that has since been disconnected falls
 * back to the first one, because showing nothing is never the better answer.
 * Returns null only when there are no boards at all.
 */
export async function pickBoard(
  storage: LastBoardStorage,
  boards: { id: string }[],
): Promise<string | null> {
  if (boards.length === 0) return null;
  let remembered: string | null = null;
  try {
    remembered = await storage.getItem(LAST_BOARD_KEY);
  } catch {
    remembered = null;
  }
  if (remembered && boards.some((b) => b.id === remembered)) return remembered;
  return boards[0].id;
}
