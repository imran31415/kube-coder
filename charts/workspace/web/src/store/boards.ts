import { signal, computed } from '@preact/signals';
import {
  subscribeEvents,
  eventStreamConnected,
  type DashboardEvent,
} from '../api/events';
import {
  listBoards,
  getBoardItems,
  listBoardCredentials,
  saveBoardCredential,
  deleteBoardCredential,
  listBoardRuns,
  getBoardRun,
  startBoardRun,
  stopBoardRun,
  getBoardReview,
  approveStagedActions,
  rejectStagedActions,
  sendBackStagedActions,
  editStagedAction,
  type Board,
  type BoardCredential,
  type BoardItem,
  type BoardItemsResult,
  type BoardRun,
  type BoardRunSummary,
  type RunItemState,
  listBoardTemplates,
  fillBoardTemplate,
  createBoard,
  testFetchBoard,
  type BoardTemplate,
  listBoardStrategies,
  saveBoardStrategy,
  deleteBoardStrategy,
  previewBoardStrategy,
  getBoardMetrics,
  type BoardMetrics,
  type BoardSelect,
  type ResumeOutcome,
  type ReviewGroup,
  type StagedRecord,
  type StartRunBody,
  type StrategyPreview,
} from '../api/boards';

/**
 * Board Processor state (#588/#589) — connectors on the left, items in the
 * middle, one item in detail.
 *
 * Fetching items hits a THIRD-PARTY API through the workspace, so unlike the
 * projects store this one never polls on a timer: a background poll would burn
 * the vendor's rate-limit budget (Zendesk allows as few as 200 requests/minute
 * account-wide) for a tab nobody is looking at. Refresh is explicit, plus a
 * debounced reaction to boards.changed so an action taken in chat shows up.
 */

export const boards = signal<Board[]>([]);
export const boardsLoading = signal(false);
export const boardsError = signal<string | null>(null);
export const boardsLastFetch = signal<number | null>(null);

export const selectedBoardId = signal<string | null>(null);
export const selectedItemId = signal<string | null>(null);

/** Per-board item listings, keyed by board id. */
export const boardItems = signal<Record<string, BoardItemsResult>>({});
export const itemsLoading = signal<Record<string, boolean>>({});
export const itemsError = signal<Record<string, string | null>>({});

export const selectedBoard = computed<Board | null>(
  () => boards.value.find((b) => b.id === selectedBoardId.value) ?? null,
);

export const selectedItems = computed<BoardItemsResult | null>(() =>
  selectedBoardId.value
    ? (boardItems.value[selectedBoardId.value] ?? null)
    : null,
);

export const selectedItem = computed<BoardItem | null>(() => {
  const listing = selectedItems.value;
  if (!listing || !selectedItemId.value) return null;
  return listing.items.find((i) => i.id === selectedItemId.value) ?? null;
});

/** Free-text filter over key / title / tags. */
export const itemFilter = signal('');

export const filteredItems = computed<BoardItem[]>(() => {
  const listing = selectedItems.value;
  if (!listing) return [];
  const needle = itemFilter.value.trim().toLowerCase();
  if (!needle) return listing.items;
  return listing.items.filter((i) =>
    `${i.key} ${i.title} ${i.tags.join(' ')}`.toLowerCase().includes(needle),
  );
});

let boardsInFlight: Promise<void> | null = null;

export async function refreshBoards(): Promise<void> {
  if (boardsInFlight) return boardsInFlight;
  boardsLoading.value = true;
  boardsInFlight = (async () => {
    try {
      const res = await listBoards();
      boards.value = res.boards ?? [];
      boardsError.value = null;
      boardsLastFetch.value = Date.now();
    } catch (err) {
      boardsError.value = err instanceof Error ? err.message : String(err);
    } finally {
      boardsLoading.value = false;
      boardsInFlight = null;
    }
  })();
  return boardsInFlight;
}

const itemsInFlight = new Map<string, Promise<void>>();

export async function loadItems(boardId: string, quiet = false): Promise<void> {
  const existing = itemsInFlight.get(boardId);
  if (existing) return existing;
  if (!quiet) itemsLoading.value = { ...itemsLoading.value, [boardId]: true };
  const p = (async () => {
    try {
      const res = await getBoardItems(boardId);
      boardItems.value = { ...boardItems.value, [boardId]: res };
      itemsError.value = { ...itemsError.value, [boardId]: null };
    } catch (err) {
      itemsError.value = {
        ...itemsError.value,
        [boardId]: err instanceof Error ? err.message : String(err),
      };
    } finally {
      itemsLoading.value = { ...itemsLoading.value, [boardId]: false };
      itemsInFlight.delete(boardId);
    }
  })();
  itemsInFlight.set(boardId, p);
  return p;
}

export async function selectBoard(id: string | null): Promise<void> {
  // The review queue belongs to one board. Keeping it across a switch showed
  // the previous board's count on the Review badge until something happened to
  // reload it (#704). The route reloads it for the new board.
  if (id !== selectedBoardId.value) {
    reviewGroups.value = [];
    reviewBoardId.value = null;
  }
  selectedBoardId.value = id;
  selectedItemId.value = null;
  itemFilter.value = '';
  if (id && !boardItems.value[id]) await loadItems(id);
}

export function selectItem(id: string | null): void {
  selectedItemId.value = id;
}

// ── credentials ────────────────────────────────────────────────────────────

export const boardCredentials = signal<BoardCredential[]>([]);
export const credentialsError = signal<string | null>(null);

export async function refreshCredentials(): Promise<void> {
  try {
    const res = await listBoardCredentials();
    boardCredentials.value = res.credentials ?? [];
    credentialsError.value = null;
  } catch (err) {
    credentialsError.value = err instanceof Error ? err.message : String(err);
  }
}

/** Save, then refresh the BOARD list too: a board that was "needs key" may
 *  have just become usable, and that state lives on the board, not the key. */
export async function saveCredential(
  name: string,
  body: { secret?: string; format?: 'token' | 'basic'; username?: string },
): Promise<string | null> {
  try {
    const res = await saveBoardCredential(name, body);
    boardCredentials.value = res.credentials ?? [];
    credentialsError.value = null;
    await refreshBoards();
    return null;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    credentialsError.value = message;
    return message;
  }
}

export async function removeCredential(name: string): Promise<string | null> {
  try {
    await deleteBoardCredential(name);
    boardCredentials.value = boardCredentials.value.filter(
      (c) => c.name !== name,
    );
    credentialsError.value = null;
    await refreshBoards();
    return null;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    credentialsError.value = message;
    return message;
  }
}

// ── runs ───────────────────────────────────────────────────────────────────

/** The Runs form's settings — everything the operator chooses before Start. */
export interface BoardRunForm {
  mode: 'propose' | 'autonomous';
  limit: number;
  concurrency: number;
  strategy: string;
}

export const RUN_FORM_DEFAULTS: Readonly<BoardRunForm> = {
  mode: 'propose',
  limit: 10,
  concurrency: 3,
  strategy: '',
};

const RUN_FORM_KEY = 'kc.boardRunForm';

/** The `<input min/max>` bounds, restated here because storage is untrusted:
 *  a hand-edited entry must not submit a run the form itself would reject. */
const LIMIT_MIN = 1;
const LIMIT_MAX = 500;
const CONCURRENCY_MIN = 1;
const CONCURRENCY_MAX = 8;

function clampInt(raw: unknown, min: number, max: number, fallback: number): number {
  // Absent or empty means "never chosen" — fall back rather than clamp, or
  // a missing field would read as 0 and land on the minimum.
  if (raw === null || raw === undefined || raw === '') return fallback;
  const n = Math.floor(Number(raw));
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}

function sanitizeRunForm(raw: unknown): BoardRunForm {
  const o = (raw ?? {}) as Partial<BoardRunForm>;
  return {
    mode: o.mode === 'autonomous' ? 'autonomous' : RUN_FORM_DEFAULTS.mode,
    limit: clampInt(o.limit, LIMIT_MIN, LIMIT_MAX, RUN_FORM_DEFAULTS.limit),
    concurrency: clampInt(
      o.concurrency, CONCURRENCY_MIN, CONCURRENCY_MAX, RUN_FORM_DEFAULTS.concurrency,
    ),
    strategy: typeof o.strategy === 'string' ? o.strategy : RUN_FORM_DEFAULTS.strategy,
  };
}

function readRunForms(): Record<string, BoardRunForm> {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(RUN_FORM_KEY) || '{}');
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    const out: Record<string, BoardRunForm> = {};
    for (const [id, form] of Object.entries(parsed as Record<string, unknown>)) {
      out[id] = sanitizeRunForm(form);
    }
    return out;
  } catch {
    /* private mode, or somebody hand-edited the entry into nonsense */
    return {};
  }
}

/**
 * Runs-form settings that outlive the panel, keyed by board (#643).
 *
 * RunsPanel is unmounted whenever the operator leaves the Runs tab — and
 * watching a run means bouncing to Review and back. Holding Items / At once
 * in component state meant every trip silently restored `10 @ 3`, so an
 * operator who had chosen `5 @ 2` and hit Start again dispatched a heavier
 * run than they had configured. On a 4 GiB pod that difference is enough to
 * OOM-kill the workspace, taking in-flight items and live tmux with it.
 *
 * Keyed by board id, never global: two boards can want very different
 * concurrency, and carrying one board's numbers to another silently would
 * be the same bug wearing a different hat.
 */
export const boardRunForms = signal<Record<string, BoardRunForm>>(readRunForms());

/** The stored form for a board, or the defaults if it has never been set. */
export function runFormFor(boardId: string | null): BoardRunForm {
  if (!boardId) return { ...RUN_FORM_DEFAULTS };
  return boardRunForms.value[boardId] ?? { ...RUN_FORM_DEFAULTS };
}

/** Merge a change into a board's form and mirror it to localStorage. */
export function setRunForm(boardId: string | null, patch: Partial<BoardRunForm>): void {
  if (!boardId) return;
  const next = sanitizeRunForm({ ...runFormFor(boardId), ...patch });
  boardRunForms.value = { ...boardRunForms.value, [boardId]: next };
  try {
    localStorage.setItem(RUN_FORM_KEY, JSON.stringify(boardRunForms.value));
  } catch {
    /* private mode — the choice still survives tab switches, just not reloads */
  }
}

/** Test seam: drop persisted forms and re-read storage. */
export function _resetRunFormsForTest(): void {
  boardRunForms.value = readRunForms();
}

/** Run summaries per board id. */
export const boardRuns = signal<Record<string, BoardRunSummary[]>>({});
export const activeRun = signal<BoardRun | null>(null);
export const runsError = signal<string | null>(null);

export const selectedBoardRuns = computed<BoardRunSummary[]>(() =>
  selectedBoardId.value ? (boardRuns.value[selectedBoardId.value] ?? []) : [],
);

/** A run in flight anywhere on the selected board — what drives the live poll
 *  below. Reading runs is a LOCAL call (the PVC), unlike reading items, so
 *  polling here costs the vendor nothing. */
export const hasLiveRun = computed(() =>
  selectedBoardRuns.value.some((r) => r.status === 'running'),
);

export async function refreshRuns(boardId: string): Promise<void> {
  try {
    const res = await listBoardRuns(boardId);
    boardRuns.value = { ...boardRuns.value, [boardId]: res.runs ?? [] };
    runsError.value = null;
  } catch (err) {
    runsError.value = err instanceof Error ? err.message : String(err);
  }
}

export async function openRun(boardId: string, runId: string): Promise<void> {
  try {
    activeRun.value = await getBoardRun(boardId, runId);
    runsError.value = null;
  } catch (err) {
    runsError.value = err instanceof Error ? err.message : String(err);
  }
}

export function closeRun(): void {
  activeRun.value = null;
}

export async function startRun(
  boardId: string,
  body: StartRunBody,
): Promise<string | null> {
  try {
    const run = await startBoardRun(boardId, body);
    activeRun.value = run;
    await refreshRuns(boardId);
    return null;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    runsError.value = message;
    return message;
  }
}

export async function stopRun(
  boardId: string,
  runId: string,
): Promise<string | null> {
  try {
    await stopBoardRun(boardId, runId);
    await refreshRuns(boardId);
    if (activeRun.value?.id === runId) await openRun(boardId, runId);
    return null;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    runsError.value = message;
    return message;
  }
}

/** Run progress polls on a timer — deliberately, and unlike items. A run
 *  record is a local file; reading it costs nothing outbound, and progress that
 *  only moved when you clicked would be worse than useless while 20 items are
 *  in flight. It stops the moment nothing is running. */
const RUN_POLL_MS = 3000;
let runPollTimer: ReturnType<typeof setInterval> | null = null;
let runPollBoard: string | null = null;

/**
 * When run state was last read back from the server.
 *
 * The UI uses this to say "live" versus "last updated 40s ago". Without it a
 * dropped event stream is indistinguishable from a quiet board: the numbers
 * simply stop moving, and the operator cannot tell whether the run stalled or
 * the page did.
 */
export const lastRunSync = signal<number | null>(null);

/**
 * Poll run progress for a board.
 *
 * Idempotent per board, because ownership moved up to the route. This used to
 * be started and stopped by RunsPanel's own mount effect, and the tabs render
 * conditionally — so stepping over to Review to approve something silently
 * killed the poll tracking the run you went there to act on. Progress then
 * rode entirely on the event stream, with nothing on screen to say so when
 * that stream had dropped.
 */
export function startRunPolling(boardId: string): void {
  if (runPollTimer != null && runPollBoard === boardId) return;
  stopRunPolling();
  runPollBoard = boardId;
  runPollTimer = setInterval(() => {
    if (!hasLiveRun.value && !activeRun.value) return;
    void refreshRuns(boardId).then(() => {
      lastRunSync.value = Date.now();
    });
    const open = activeRun.value;
    if (open && open.status === 'running') void openRun(boardId, open.id);
    // Reports and decisions reach the Review badge through `boards.review`
    // events. With the stream down, a live run would otherwise add cards the
    // badge never counts, so ride this tick. Only while the stream is down:
    // with it up, the event already did this and a second read is waste.
    if (hasLiveRun.value && !boardsLive.value) void refreshReview(boardId);
  }, RUN_POLL_MS);
}

export function stopRunPolling(): void {
  if (runPollTimer != null) {
    clearInterval(runPollTimer);
    runPollTimer = null;
  }
  runPollBoard = null;
}

// ── review ─────────────────────────────────────────────────────────────────

export const reviewGroups = signal<ReviewGroup[]>([]);
export const reviewError = signal<string | null>(null);
/** Which board `reviewGroups` was read for, or null before the first read. A
 *  queue on screen for one board must never be counted, or linked to, as if it
 *  belonged to another. */
export const reviewBoardId = signal<string | null>(null);
/** Item the user arrived to review (from a feed link, the waiting badge, or a
 *  run item's outcome). */
export const reviewFocusItemId = signal<string | null>(null);

/**
 * Which tab of /board is showing.
 *
 * A store signal rather than the route's own state for two reasons (#704). The
 * Runs table has to be able to send someone to one card on the Review tab. And
 * following a run item's "View session" link leaves the route: coming back
 * should land on the Runs table that link was clicked from, not reset to Items.
 */
export const boardTab = signal<BoardTab>('items');

/** Open the Review tab on one item's card. */
export function openReviewFor(itemId: string): void {
  reviewFocusItemId.value = String(itemId);
  boardTab.value = 'review';
}

/** Open review cards on the selected board — the Review badge. Zero while the
 *  queue on hand was read for some other board. */
export const openReviewCount = computed(() => {
  if (reviewBoardId.value !== selectedBoardId.value) return 0;
  return reviewGroups.value.flatMap((g) => g.items).filter((r) => r.open).length;
});

/** Open cards per disposition, for the badge's spoken and hover label. */
export const openReviewBreakdown = computed<{ disposition: string; open: number }[]>(
  () => {
    if (reviewBoardId.value !== selectedBoardId.value) return [];
    return reviewGroups.value
      .map((g) => ({
        disposition: g.disposition,
        open: g.items.filter((r) => r.open).length,
      }))
      .filter((g) => g.open > 0);
  },
);

/** Bumped per read, so only the newest read may land. */
let reviewSeq = 0;

/**
 * Read the review queue for a board.
 *
 * Reads overlap routinely: a run whose agents report together publishes a
 * burst of `boards.review` events, each starting a read, and nothing makes the
 * responses come back in order. Letting whichever answer arrived last win meant
 * an older queue could overwrite a newer one — a count that went backwards. A
 * read for a board that is no longer selected is dropped too, so switching
 * boards mid-read cannot put one board's queue under another's name.
 */
export async function refreshReview(boardId: string): Promise<void> {
  const seq = ++reviewSeq;
  const stale = () =>
    seq !== reviewSeq ||
    (selectedBoardId.value !== null && selectedBoardId.value !== boardId);
  try {
    const res = await getBoardReview(boardId);
    if (stale()) return;
    reviewGroups.value = res.groups ?? [];
    reviewBoardId.value = boardId;
    reviewError.value = null;
  } catch (err) {
    if (stale()) return;
    reviewError.value = err instanceof Error ? err.message : String(err);
  }
}

/**
 * Which decision is in flight on which item.
 *
 * This used to be a `busy` boolean private to ReviewPanel, and a boolean is
 * exactly one bit short of what the button needs: it could grey the card out
 * but could not say *what* was happening, so an approve waiting on a real
 * GitHub write looked identical to a UI that had wedged. Holding the KIND here
 * lets every surface — the card, the header, the mobile list — label the wait
 * with the verb the operator actually clicked.
 */
export type DecisionKind = 'approve' | 'reject' | 'send_back' | 'edit';

export const decisionPending = signal<Record<string, DecisionKind>>({});

function setPending(itemId: string, kind: DecisionKind | null): void {
  const next = { ...decisionPending.value };
  if (kind) next[itemId] = kind;
  else delete next[itemId];
  decisionPending.value = next;
}

/**
 * Patch one staged record in place, without re-reading the queue.
 *
 * A decision used to cost two serialized round trips before anything moved on
 * screen: the write itself (a real call to the vendor, seconds long) and then
 * a full re-read of the queue. The card only stopped looking busy after both.
 * Applying the outcome locally the moment the write returns settles the card
 * on the first round trip; the re-read still happens, but it confirms rather
 * than blocks.
 */
function patchRecord(itemId: string, patch: Partial<StagedRecord>): void {
  reviewGroups.value = reviewGroups.value.map((group) => ({
    ...group,
    items: group.items.map((r) =>
      r.item_id === itemId ? { ...r, ...patch } : r,
    ),
  }));
}

/**
 * Re-read the board's items after a decision, at most once per burst.
 *
 * Unlike the rest of the fan-out, this is an outbound call to somebody else's
 * API, and the module note above is emphatic about not spending the vendor's
 * rate-limit budget. Approving six cards in a row is one intent, not six, so
 * the listing refreshes once after the flurry stops rather than per click.
 */
const ITEMS_REFRESH_DEBOUNCE_MS = 1200;
let itemsRefreshTimer: ReturnType<typeof setTimeout> | null = null;

function scheduleItemsRefresh(boardId: string): void {
  if (itemsRefreshTimer != null) clearTimeout(itemsRefreshTimer);
  itemsRefreshTimer = setTimeout(() => {
    itemsRefreshTimer = null;
    if (selectedBoardId.value === boardId) void loadItems(boardId, true);
  }, ITEMS_REFRESH_DEBOUNCE_MS);
}

/**
 * Everything a decision changes, refreshed together.
 *
 * A decision is not confined to the review queue: approving a write closes the
 * real ticket, which changes the Items listing; it settles the run item, which
 * changes the Runs table; and it lands in the decision ledger, which changes
 * the approval rate. Refreshing only the queue meant an operator could approve
 * a card, switch to Items, and still see the issue they had just closed listed
 * as open — the staleness the maintainer described as "hard to tell the state
 * of the board and items".
 *
 * The reads are issued together rather than in sequence because they are
 * independent, and `allSettled` because a failing metrics read must not stop
 * the queue from refreshing.
 */
export async function syncAfterDecision(boardId: string): Promise<void> {
  scheduleItemsRefresh(boardId);
  await Promise.allSettled([
    refreshReview(boardId),
    refreshRuns(boardId),
    refreshBoardMetrics(boardId),
  ]);
}

/**
 * A client-minted id for one decision.
 *
 * The point is that it is generated ONCE per decision and reused by every
 * retry, so the server can consume it once and replay the stored result. A
 * fresh id per attempt would defeat the whole mechanism, which is why minting
 * lives here rather than inline at each call site.
 */
export function newApprovalId(): string {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  return `ap-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

/** Approve the writes staged against one item. Returns an error string, or
 *  null on success (including a replay, which is a success). */
export async function approveStaged(
  boardId: string,
  record: StagedRecord,
  approvalId = newApprovalId(),
): Promise<string | null> {
  setPending(record.item_id, 'approve');
  try {
    await approveStagedActions(boardId, record.item_id, {
      // The hash the CARD was drawn from, not a freshly-read one: that is what
      // makes "you are approving something you have not read" detectable.
      content_hash: record.content_hash,
      approval_id: approvalId,
    });
    // Settle the card now; the fan-out below only confirms it.
    patchRecord(record.item_id, {
      state: 'approved',
      open: false,
      pending_actions: [],
    });
    void syncAfterDecision(boardId);
    return null;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    reviewError.value = message;
    // A 409 means the queue is out of date — reload it so the reviewer sees
    // what actually changed rather than an error over a stale card.
    await refreshReview(boardId);
    return message;
  } finally {
    setPending(record.item_id, null);
  }
}

export async function rejectStaged(
  boardId: string,
  itemId: string,
  reason: string,
  approvalId = newApprovalId(),
): Promise<string | null> {
  setPending(itemId, 'reject');
  try {
    await rejectStagedActions(boardId, itemId, {
      approval_id: approvalId,
      reason,
    });
    patchRecord(itemId, { state: 'rejected', open: false, pending_actions: [] });
    void syncAfterDecision(boardId);
    return null;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    reviewError.value = message;
    await refreshReview(boardId);
    return message;
  } finally {
    setPending(itemId, null);
  }
}

/**
 * The re-scoping round trip (#588 Phase 6).
 *
 * Returns the resume outcome on success so the caller can say what actually
 * happened — a reviewer told their context was preserved when it was not would
 * trust the next answer more than it deserves. `null` detail means the request
 * itself failed.
 */
export const lastResumeOutcome = signal<ResumeOutcome | null>(null);

export async function sendBackStaged(
  boardId: string,
  itemId: string,
  note: string,
  approvalId = newApprovalId(),
): Promise<string | null> {
  setPending(itemId, 'send_back');
  try {
    const res = await sendBackStagedActions(boardId, itemId, {
      approval_id: approvalId,
      note,
    });
    lastResumeOutcome.value = res.resume ?? null;
    patchRecord(itemId, { state: 'sent_back', open: false, pending_actions: [] });
    void syncAfterDecision(boardId);
    return null;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    reviewError.value = message;
    await refreshReview(boardId);
    return message;
  } finally {
    setPending(itemId, null);
  }
}

/**
 * Edit is NOT a decision — the item stays open, and nothing has been written to
 * the board yet — so it refreshes the queue only and deliberately skips the
 * fan-out that approve, reject and send-back perform.
 */
export async function editStaged(
  boardId: string,
  itemId: string,
  actionId: string,
  params: Record<string, unknown>,
): Promise<string | null> {
  setPending(itemId, 'edit');
  try {
    await editStagedAction(boardId, itemId, {
      action_id: actionId,
      params,
    });
    await refreshReview(boardId);
    return null;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    reviewError.value = message;
    return message;
  } finally {
    setPending(itemId, null);
  }
}

// ── real-time (boards.changed, debounced; no timer poll — see module note) ──

let eventUnsub: (() => void) | null = null;
let eventRefreshTimer: ReturnType<typeof setTimeout> | null = null;

function onDashboardEvent(ev: DashboardEvent): void {
  if (ev.type === 'boards.review') {
    const boardId = String(ev.data.board_id ?? '');
    if (boardId && boardId === selectedBoardId.value) {
      void refreshReview(boardId);
      // An agent staging work, or another reviewer deciding, moves the
      // approval rate too — refreshing the queue alone left the strip under
      // it quoting a figure from before the thing that just happened.
      void refreshBoardMetrics(boardId);
    }
    return;
  }
  if (ev.type === 'boards.run') {
    // Run progress is LOCAL state, so it refreshes immediately rather than
    // riding the debounce that exists to protect the vendor's rate limit.
    const boardId = String(ev.data.board_id ?? '');
    if (boardId && boardId === selectedBoardId.value) {
      void refreshRuns(boardId).then(() => {
        lastRunSync.value = Date.now();
      });
      const open = activeRun.value;
      if (open && open.id === ev.data.id) void openRun(boardId, open.id);
    }
    return;
  }
  if (ev.type !== 'boards.changed') return;
  if (eventRefreshTimer != null) return;
  // Coalesce bursts (an action publishes one event per write) into a single
  // refresh, the same 250ms debounce the projects store uses.
  eventRefreshTimer = setTimeout(() => {
    eventRefreshTimer = null;
    void refreshBoards();
    const id = selectedBoardId.value;
    // Reload only the board being looked at: every item fetch is an outbound
    // call to someone else's API.
    if (id) void loadItems(id, true);
  }, 250);
}

export function startBoardsEvents(): void {
  if (!eventUnsub) eventUnsub = subscribeEvents(onDashboardEvent);
}

export function stopBoardsEvents(): void {
  if (eventUnsub) {
    eventUnsub();
    eventUnsub = null;
  }
  if (eventRefreshTimer != null) {
    clearTimeout(eventRefreshTimer);
    eventRefreshTimer = null;
  }
}

/** True when SSE is up, so the UI can say "live" instead of implying a poll. */
export const boardsLive = computed(() => eventStreamConnected.value);

// ── where the board stands, and what to do next ────────────────────────────

/**
 * One line's worth of "where am I".
 *
 * The four tabs each answer a different question and none of them answers the
 * first one an operator actually has, which is *what is happening and what
 * should I do now*. Finding that out meant visiting Items to count, Runs to
 * see whether anything was moving, and Review to see whether anything was
 * waiting — the maintainer's "hard to tell the state of the board and items
 * and next steps easily".
 *
 * `next` is deliberately one sentence rather than a status dump: a summary
 * that lists five facts and recommends nothing has moved the work of deciding
 * back onto the reader. The tab that sentence points at is returned separately
 * as `nextTab`, so the UI can offer a button that goes there rather than
 * printing "open Review" at somebody already reading the review queue.
 */
export type BoardTab = 'items' | 'runs' | 'review' | 'credentials';

export interface BoardStanding {
  items: number;
  /** A run is in flight on this board. */
  live: boolean;
  working: number;
  queued: number;
  settled: number;
  runTotal: number;
  /** Open review cards — the count the Review badge shows. */
  awaiting: number;
  /** What is going on, in one sentence. Empty when there is no board. */
  next: string;
  /** Where acting on `next` would take you, or null when nothing is owed. */
  nextTab: BoardTab | null;
}

export const boardStanding = computed<BoardStanding>(() => {
  const board = selectedBoard.value;
  const listing = selectedItems.value;
  const runs = selectedBoardRuns.value;
  const awaiting = openReviewCount.value;
  const live = runs.find((r) => r.status === 'running') ?? null;

  const counts: Partial<Record<RunItemState, number>> = live?.counts ?? {};
  const working = (counts.working ?? 0) + (counts.claimed ?? 0);
  const queued = counts.pending ?? 0;
  const settled = live ? live.done + live.failed + live.skipped : 0;

  const n = (count: number, one: string, many: string) =>
    count === 1 ? one : many;

  let next = '';
  let nextTab: BoardTab | null = null;
  if (!board) {
    next = '';
  } else if (board.credential_set === false) {
    next = 'This board has no credential yet, so nothing can authenticate.';
    nextTab = 'credentials';
  } else if (live && awaiting > 0) {
    next = `A run is in flight, and ${awaiting} ${n(awaiting, 'item', 'items')} already ${n(awaiting, 'needs', 'need')} your decision.`;
    nextTab = 'review';
  } else if (live) {
    next = 'A run is in flight. Nothing needs you yet.';
  } else if (awaiting > 0) {
    next = `${awaiting} ${n(awaiting, 'item is', 'items are')} waiting on your decision.`;
    nextTab = 'review';
  } else if (!listing || listing.items.length === 0) {
    next = 'No items were read from this board. Check the connector before running it.';
  } else if (runs.length === 0) {
    next = 'Nothing has been worked here yet.';
    nextTab = 'runs';
  } else {
    next = 'Everything worked so far has been decided.';
    nextTab = 'runs';
  }

  return {
    items: listing?.items.length ?? 0,
    live: !!live,
    working,
    queued,
    settled,
    runTotal: live?.total ?? 0,
    awaiting,
    next,
    nextTab,
  };
});

// ── strategies and metrics (#588 Phase 7) ──────────────────────────────────

export const strategies = signal<Record<string, BoardSelect>>({});
export const strategyOrders = signal<string[]>([]);
export const strategyPreview = signal<StrategyPreview | null>(null);
export const boardMetrics = signal<BoardMetrics | null>(null);

export async function refreshStrategies(boardId: string): Promise<void> {
  try {
    const res = await listBoardStrategies(boardId);
    strategies.value = res.strategies ?? {};
    strategyOrders.value = res.orders ?? [];
  } catch (err) {
    runsError.value = err instanceof Error ? err.message : String(err);
  }
}

export async function saveStrategy(
  boardId: string,
  name: string,
  select: BoardSelect,
): Promise<string | null> {
  try {
    const res = await saveBoardStrategy(boardId, { name, select });
    strategies.value = res.strategies ?? {};
    return null;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    runsError.value = message;
    return message;
  }
}

export async function deleteStrategy(
  boardId: string,
  name: string,
): Promise<void> {
  try {
    const res = await deleteBoardStrategy(boardId, name);
    strategies.value = res.strategies ?? {};
  } catch (err) {
    runsError.value = err instanceof Error ? err.message : String(err);
  }
}

/**
 * What a run with this selection would work.
 *
 * A preview costs one vendor listing, so it is explicit rather than reactive —
 * a preview firing on every keystroke would spend the board's rate-limit
 * budget on a form nobody has submitted.
 */
export async function previewStrategy(
  boardId: string,
  select: BoardSelect,
): Promise<string | null> {
  try {
    strategyPreview.value = await previewBoardStrategy(boardId, select);
    return null;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    strategyPreview.value = null;
    runsError.value = message;
    return message;
  }
}

export async function refreshBoardMetrics(boardId: string): Promise<void> {
  try {
    boardMetrics.value = await getBoardMetrics(boardId);
  } catch {
    // Metrics are decoration on the review queue. Failing to load them must
    // not put an error banner over the thing the reviewer came here to do.
    boardMetrics.value = null;
  }
}

// ── connecting a board ─────────────────────────────────────────────────────

export const boardTemplates = signal<BoardTemplate[]>([]);
export const templatesError = signal<string | null>(null);

export async function refreshTemplates(): Promise<void> {
  try {
    const res = await listBoardTemplates();
    boardTemplates.value = res.templates ?? [];
    templatesError.value = null;
  } catch (err) {
    templatesError.value = err instanceof Error ? err.message : String(err);
  }
}

export interface ConnectOutcome {
  boardId: string;
  /** What test-fetch actually got back. The board exists either way — this is
   *  the difference between "saved" and "demonstrated". */
  fetched: number;
  complete: boolean;
  /** Set when the board was created but its first fetch failed. Deliberately
   *  NOT a reason to delete it: the connector is usually right and the
   *  credential wrong, and throwing the answers away makes the operator type
   *  them all again to find out. */
  fetchError: string | null;
}

/**
 * The whole connect flow: credential, then fill, then create, then prove it.
 *
 * The order is the only one that works. The credential has to exist before
 * `test-fetch` runs or the verification step fails for a reason that has
 * nothing to do with the connector, and `test-fetch` has to run at all,
 * because the entire discipline of this feature is that a connector which has
 * not fetched has demonstrated nothing. Saving a board and calling it
 * connected would be the lie the docs spend two paragraphs warning about.
 */
export async function connectBoard(opts: {
  templateId: string;
  values: Record<string, string>;
  boardId?: string;
  displayName?: string;
  credential?: { name: string; secret: string; format: 'token' | 'basic'; username?: string };
}): Promise<{ outcome: ConnectOutcome | null; error: string | null }> {
  try {
    if (opts.credential?.secret) {
      const err = await saveCredential(opts.credential.name, {
        secret: opts.credential.secret,
        format: opts.credential.format,
        username: opts.credential.username ?? '',
      });
      if (err) return { outcome: null, error: err };
    }

    const filled = await fillBoardTemplate(opts.templateId, {
      values: opts.values,
      id: opts.boardId ?? '',
      display_name: opts.displayName ?? '',
    });
    const created = await createBoard(filled.connector);
    await refreshBoards();

    let fetched = 0;
    let complete = false;
    let fetchError: string | null = null;
    try {
      const probe = await testFetchBoard(created.id);
      fetched = probe.items?.length ?? 0;
      complete = probe.complete;
    } catch (err) {
      fetchError = err instanceof Error ? err.message : String(err);
    }
    return {
      outcome: { boardId: created.id, fetched, complete, fetchError },
      error: null,
    };
  } catch (err) {
    return { outcome: null, error: err instanceof Error ? err.message : String(err) };
  }
}

export function _resetBoardsForTest(): void {
  boardTemplates.value = [];
  templatesError.value = null;
  boards.value = [];
  boardsLoading.value = false;
  boardsError.value = null;
  boardsLastFetch.value = null;
  selectedBoardId.value = null;
  selectedItemId.value = null;
  boardItems.value = {};
  itemsLoading.value = {};
  itemsError.value = {};
  itemFilter.value = '';
  boardCredentials.value = [];
  credentialsError.value = null;
  boardRuns.value = {};
  activeRun.value = null;
  runsError.value = null;
  reviewGroups.value = [];
  reviewError.value = null;
  reviewBoardId.value = null;
  reviewFocusItemId.value = null;
  // Bumped, never rewound: a read still in flight from before the reset must
  // not be able to land in the next test.
  reviewSeq++;
  boardTab.value = 'items';
  boardsInFlight = null;
  itemsInFlight.clear();
  strategies.value = {};
  strategyOrders.value = [];
  strategyPreview.value = null;
  boardMetrics.value = null;
  lastResumeOutcome.value = null;
  decisionPending.value = {};
  lastRunSync.value = null;
  if (itemsRefreshTimer != null) {
    clearTimeout(itemsRefreshTimer);
    itemsRefreshTimer = null;
  }
  stopRunPolling();
  stopBoardsEvents();
}
