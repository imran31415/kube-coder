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

export function startRunPolling(boardId: string): void {
  stopRunPolling();
  runPollTimer = setInterval(() => {
    if (!hasLiveRun.value && !activeRun.value) return;
    void refreshRuns(boardId);
    const open = activeRun.value;
    if (open && open.status === 'running') void openRun(boardId, open.id);
  }, RUN_POLL_MS);
}

export function stopRunPolling(): void {
  if (runPollTimer != null) {
    clearInterval(runPollTimer);
    runPollTimer = null;
  }
}

// ── review ─────────────────────────────────────────────────────────────────

export const reviewGroups = signal<ReviewGroup[]>([]);
export const reviewError = signal<string | null>(null);
/** Item the user arrived to review (from a feed link or the waiting badge). */
export const reviewFocusItemId = signal<string | null>(null);

export const openReviewCount = computed(
  () => reviewGroups.value.flatMap((g) => g.items).filter((r) => r.open).length,
);

export async function refreshReview(boardId: string): Promise<void> {
  try {
    const res = await getBoardReview(boardId);
    reviewGroups.value = res.groups ?? [];
    reviewError.value = null;
  } catch (err) {
    reviewError.value = err instanceof Error ? err.message : String(err);
  }
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
  try {
    await approveStagedActions(boardId, record.item_id, {
      // The hash the CARD was drawn from, not a freshly-read one: that is what
      // makes "you are approving something you have not read" detectable.
      content_hash: record.content_hash,
      approval_id: approvalId,
    });
    await refreshReview(boardId);
    return null;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    reviewError.value = message;
    // A 409 means the queue is out of date — reload it so the reviewer sees
    // what actually changed rather than an error over a stale card.
    await refreshReview(boardId);
    return message;
  }
}

export async function rejectStaged(
  boardId: string,
  itemId: string,
  reason: string,
  approvalId = newApprovalId(),
): Promise<string | null> {
  try {
    await rejectStagedActions(boardId, itemId, {
      approval_id: approvalId,
      reason,
    });
    await refreshReview(boardId);
    return null;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    reviewError.value = message;
    return message;
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
  try {
    const res = await sendBackStagedActions(boardId, itemId, {
      approval_id: approvalId,
      note,
    });
    lastResumeOutcome.value = res.resume ?? null;
    await refreshReview(boardId);
    return null;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    reviewError.value = message;
    return message;
  }
}

export async function editStaged(
  boardId: string,
  itemId: string,
  actionId: string,
  params: Record<string, unknown>,
): Promise<string | null> {
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
  }
}

// ── real-time (boards.changed, debounced; no timer poll — see module note) ──

let eventUnsub: (() => void) | null = null;
let eventRefreshTimer: ReturnType<typeof setTimeout> | null = null;

function onDashboardEvent(ev: DashboardEvent): void {
  if (ev.type === 'boards.review') {
    const boardId = String(ev.data.board_id ?? '');
    if (boardId && boardId === selectedBoardId.value) void refreshReview(boardId);
    return;
  }
  if (ev.type === 'boards.run') {
    // Run progress is LOCAL state, so it refreshes immediately rather than
    // riding the debounce that exists to protect the vendor's rate limit.
    const boardId = String(ev.data.board_id ?? '');
    if (boardId && boardId === selectedBoardId.value) {
      void refreshRuns(boardId);
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
  reviewFocusItemId.value = null;
  boardsInFlight = null;
  itemsInFlight.clear();
  strategies.value = {};
  strategyOrders.value = [];
  strategyPreview.value = null;
  boardMetrics.value = null;
  lastResumeOutcome.value = null;
  stopRunPolling();
  stopBoardsEvents();
}
