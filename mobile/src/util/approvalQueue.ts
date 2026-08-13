/**
 * Offline-tolerant queue for board review decisions (#588 Phase 6).
 *
 * Approving a staged comment from a phone on a train is the realistic
 * workflow, so a dropped connection has to be survivable — and survivable
 * WITHOUT double-firing, because the thing being sent is a comment on a
 * customer's ticket.
 *
 * Three facts about this app shaped the design rather than being worked around:
 *
 * 1. **There is no NetInfo dependency**, so nothing can react to reconnection.
 *    The queue therefore drains OPPORTUNISTICALLY — on screen focus, on
 *    foreground, and before any new decision — instead of on a connectivity
 *    event. Simpler, and it fails safe: the worst case is a decision that sits
 *    until the user next opens the screen.
 * 2. **There is no idempotency-key convention anywhere in this app.** So each
 *    entry mints an `approval_id` ONCE, at enqueue time, and every retry sends
 *    that same id. The server consumes it once and replays the stored result,
 *    which is what makes a retry safe. Minting per attempt would defeat the
 *    entire mechanism, which is why the id lives on the persisted entry and is
 *    never regenerated.
 * 3. `@react-native-async-storage/async-storage` is already a dependency, so
 *    persistence needs no new package.
 *
 * This module is deliberately PURE of react-native imports (storage is
 * injected) — mirroring `util/hvTranscript.ts`, which exists precisely so the
 * node-side vitest suite can cover it without a React Native renderer.
 */

export type Decision = 'approve' | 'reject' | 'send-back';

export interface QueuedApproval {
  /** Minted ONCE at enqueue. Reused by every retry — see note 2 above. */
  approval_id: string;
  board_id: string;
  item_id: string;
  decision: Decision;
  /** The hash the CARD carried. Only approve sends it. */
  content_hash: string;
  reason: string;
  queued_at: number;
  attempts: number;
  /** Last failure, kept so the UI can say why something is stuck. */
  last_error: string;
}

export interface DrainResult {
  sent: number;
  failed: number;
  /** Entries dropped because the server gave a definitive answer (2xx, or a
   *  4xx that retrying cannot fix). */
  dropped: number;
  remaining: number;
  errors: string[];
}

/** Minimal storage shape — `AsyncStorage` satisfies it, and so does a Map in a
 *  test. Injected rather than imported so this file stays node-testable. */
export interface QueueStorage {
  getItem(key: string): Promise<string | null>;
  setItem(key: string, value: string): Promise<void>;
}

/** One key holds the whole queue: it is short, and a single read/write keeps
 *  ordering and de-duplication trivially correct. */
export const QUEUE_KEY = 'kc.board.approvalQueue';

/** Beyond this, something is systematically wrong and retrying forever just
 *  drains the battery. The entry stays visible with its last error. */
export const MAX_ATTEMPTS = 8;

/**
 * Statuses where retrying can never succeed, so the entry is dropped rather
 * than retried forever:
 *
 *  401/403 — auth or read-only; a retry sends the same token
 *  404     — nothing is staged against that item any more
 *  409     — already decided, or the ticket changed. **Both are terminal for
 *            this queue**: a stale approval must NOT be re-attempted, because
 *            the whole point of the staleness guard is that the human has to
 *            look again.
 *  400/422 — malformed; the body will not improve on its own
 */
export const TERMINAL_STATUSES = [400, 401, 403, 404, 409, 422];

export function newApprovalId(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  return `ap-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

export async function readQueue(storage: QueueStorage): Promise<QueuedApproval[]> {
  try {
    const raw = await storage.getItem(QUEUE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    // A malformed entry is dropped rather than crashing the drain: the queue
    // is best-effort local state, and one bad row must not strand the rest.
    return parsed.filter(
      (e): e is QueuedApproval =>
        !!e &&
        typeof e === 'object' &&
        typeof (e as QueuedApproval).approval_id === 'string' &&
        typeof (e as QueuedApproval).item_id === 'string',
    );
  } catch {
    return [];
  }
}

async function writeQueue(
  storage: QueueStorage,
  entries: QueuedApproval[],
): Promise<void> {
  await storage.setItem(QUEUE_KEY, JSON.stringify(entries));
}

/**
 * Add a decision. Returns the stored entry.
 *
 * A second decision on the same item REPLACES the first rather than stacking:
 * a user who taps Approve and then Reject before either has been sent means
 * the second one, and sending both would be a write followed by a
 * contradiction.
 */
export async function enqueue(
  storage: QueueStorage,
  input: Omit<QueuedApproval, 'approval_id' | 'queued_at' | 'attempts' | 'last_error'> &
    Partial<Pick<QueuedApproval, 'approval_id'>>,
): Promise<QueuedApproval> {
  const queue = await readQueue(storage);
  const entry: QueuedApproval = {
    approval_id: input.approval_id ?? newApprovalId(),
    board_id: input.board_id,
    item_id: input.item_id,
    decision: input.decision,
    content_hash: input.content_hash ?? '',
    reason: input.reason ?? '',
    queued_at: Date.now(),
    attempts: 0,
    last_error: '',
  };
  const next = queue.filter(
    (e) => !(e.board_id === entry.board_id && e.item_id === entry.item_id),
  );
  next.push(entry);
  await writeQueue(storage, next);
  return entry;
}

/** What `drain` calls per entry. Resolves with the HTTP status; throws for a
 *  transport failure (no status), which is treated as retryable. */
export type Sender = (entry: QueuedApproval) => Promise<{ status: number }>;

/**
 * Try to send everything, oldest first.
 *
 * Entries are removed only on a definitive answer — a 2xx, or a status in
 * `TERMINAL_STATUSES`. Anything else (a network error, a 5xx) leaves the entry
 * in place with its attempt count raised, so the next focus/foreground tries
 * again with the SAME `approval_id`.
 */
export async function drain(
  storage: QueueStorage,
  send: Sender,
): Promise<DrainResult> {
  const queue = await readQueue(storage);
  const result: DrainResult = {
    sent: 0, failed: 0, dropped: 0, remaining: 0, errors: [],
  };
  if (queue.length === 0) return result;

  const kept: QueuedApproval[] = [];
  for (const entry of queue) {
    let status = 0;
    let message = '';
    try {
      status = (await send(entry)).status;
    } catch (e) {
      message = e instanceof Error ? e.message : String(e);
    }

    if (status >= 200 && status < 300) {
      result.sent += 1;
      continue;
    }
    if (TERMINAL_STATUSES.includes(status)) {
      // Definitive. Dropping is correct even for 409: a stale approval must
      // not be retried — the human has to look at the item again.
      result.dropped += 1;
      result.errors.push(`${entry.item_id}: ${message || `HTTP ${status}`}`);
      continue;
    }

    const attempts = entry.attempts + 1;
    result.failed += 1;
    result.errors.push(`${entry.item_id}: ${message || `HTTP ${status}`}`);
    if (attempts >= MAX_ATTEMPTS) {
      result.dropped += 1;
      continue;
    }
    kept.push({
      ...entry,
      attempts,
      last_error: message || `HTTP ${status}`,
    });
  }

  await writeQueue(storage, kept);
  result.remaining = kept.length;
  return result;
}

/** Drop one entry — the "give up on this" affordance. */
export async function remove(
  storage: QueueStorage,
  boardId: string,
  itemId: string,
): Promise<void> {
  const queue = await readQueue(storage);
  await writeQueue(
    storage,
    queue.filter((e) => !(e.board_id === boardId && e.item_id === itemId)),
  );
}

/** Items with a decision still in flight — the UI greys these rather than
 *  offering the buttons again. */
export function pendingItemIds(queue: QueuedApproval[]): Set<string> {
  return new Set(queue.map((e) => e.item_id));
}
