import { apiGet, apiPost, apiPut, apiDelete } from './client';

/**
 * Typed client for the Board Processor API (#588/#589) — external trackers
 * (Jira, GitHub, Linear, Zendesk) reached through a declarative connector.
 *
 * The server stays deterministic: it fetches, normalizes, stores and serves.
 * No connector ever carries a secret — it names one (`credential_ref`) and the
 * server resolves it at request time — so nothing here needs redacting.
 */

/** The closed normalized status set. Deliberately tiny: a four-value enum
 *  survives arbitrary vendor workflows because it is never the only copy of
 *  the truth — `raw` always carries the vendor's own value. */
export type BoardStatus = 'OPEN' | 'IN_PROGRESS' | 'ON_HOLD' | 'CLOSED';
export type BoardPriority = 'URGENT' | 'HIGH' | 'NORMAL' | 'LOW';

/** An enum'd field. `normalized` is null when the connector's mapping does not
 *  cover this vendor value — it passes through as raw rather than being
 *  coerced into a bucket it does not belong in. */
export interface NormalizedField<T> {
  normalized: T | null;
  raw: string | null;
}

export interface BoardParty {
  id?: string;
  name?: string;
  email?: string;
}

export interface BoardItem {
  /** Stable GLOBAL identity — idempotency markers key on this. */
  id: string;
  /** Human / URL reference (GitLab's iid, GitHub's number, Jira's key). */
  key: string;
  /** What action URLs interpolate. Distinct from id AND key on purpose. */
  ref: Record<string, unknown>;
  title: string;
  body: string;
  status: NormalizedField<BoardStatus>;
  priority: NormalizedField<BoardPriority>;
  assignee: BoardParty;
  /** The external customer who filed it — first-class, not a custom field. */
  contact: BoardParty;
  collection: BoardParty;
  tags: string[];
  /** Deep link, so a reviewer can see the ticket natively before approving. */
  url: string;
  created_at: string;
  updated_at: string;
  /** The FULL vendor object, always retained. */
  raw: Record<string, unknown>;
}

export interface BoardCredentialStatus {
  ref: string;
  set: boolean;
  detail: string;
}

export interface Board {
  id: string;
  vendor: string;
  display_name: string;
  base_url: string;
  credential_ref: string;
  credential_set: boolean;
  credential?: BoardCredentialStatus;
  actions_allowed?: string[];
  actions?: Record<string, unknown>;
  created_at?: number;
  updated_at?: number;
}

/**
 * Every listing carries `complete`. Absent pagination metadata NEVER means
 * complete: Jira silently truncates an over-large JQL result and GitLab omits
 * its pagination headers entirely under keyset pagination, so a full page with
 * no next-page signal is reported as incomplete rather than guessed at.
 */
export interface BoardItemsResult {
  items: BoardItem[];
  complete: boolean;
  truncation_reason: string;
  pages_fetched: number;
}

export interface BoardTestFetchResult extends BoardItemsResult {
  raw_count: number;
  map_errors: string[];
  truncated_for_display: boolean;
}

export interface BoardActionResult {
  ok: boolean;
  action: string;
  steps: { step: number; method: string; url: string; status: number }[];
  skipped?: string;
  error?: string;
  evidence?: Record<string, unknown>;
}

/**
 * A stored board credential, as the UI is allowed to see it: a name, a format,
 * and the last four characters. There is deliberately no endpoint that reads a
 * value back — the server resolves the reference at request time and hands the
 * value straight to an outbound Authorization header.
 */
export interface BoardCredential {
  name: string;
  format: 'token' | 'basic';
  /** Jira Cloud's Basic auth is `email:api_token`; this is the email half. */
  username: string;
  /** Last four characters — enough to tell two tokens apart, not to use one. */
  hint: string;
  created_at: number;
  updated_at: number;
}

/** Per-item progress inside a run. */
export type RunItemState =
  | 'pending' | 'claimed' | 'working' | 'done' | 'failed' | 'skipped';

export interface BoardRunItem {
  id: string;
  key: string;
  title: string;
  url: string;
  content_hash: string;
  state: RunItemState;
  lease_owner: string;
  task_id: string;
  disposition: string | null;
  reason: string;
  error: string;
  writes_used: number;
  updated_at: number;
}

export interface BoardRunSummary {
  id: string;
  board_id: string;
  mode: 'propose' | 'autonomous';
  status: 'running' | 'done' | 'stopped' | 'interrupted';
  concurrency: number;
  /** What was asked for. Differs from `concurrency` when the pod was busy. */
  requested_concurrency: number;
  /** Why they differ, in words — never left for the user to infer. */
  clamp_reason: string;
  created_at: number;
  updated_at: number;
  finished_at: number | null;
  error: string;
  /** False when the board's listing itself was truncated: "we worked every
   *  open ticket" and "every one we could see" are different claims. */
  listing_complete: boolean;
  truncation_reason: string;
  total: number;
  counts: Record<RunItemState, number>;
  done: number;
  failed: number;
  skipped: number;
}

export interface BoardRun extends BoardRunSummary {
  select: Record<string, unknown>;
  stop_on: Record<string, number>;
  stop_requested: boolean;
  consecutive_failures: number;
  skipped_already_processed?: number;
  items: Record<string, BoardRunItem>;
}

// ── review ─────────────────────────────────────────────────────────────────

export type Disposition =
  | 'completed' | 'needs_review' | 'needs_rescoping' | 'blocked' | 'rejected'
  | 'failed';

export interface StagedAction {
  id: string;
  action: string;
  params: Record<string, unknown>;
  /** What the reviewer reads — rendered verbatim, never as markup. */
  preview: string;
  writes: number;
  state: 'pending' | 'done' | 'failed' | 'discarded';
  edited?: boolean;
  result?: Record<string, unknown> | null;
}

export interface StagedRecord {
  board_id: string;
  item_id: string;
  item_key: string;
  item_title: string;
  item_url: string;
  /** The hash at staging time. Echoed back on approve — an approver holding a
   *  stale card cannot approve the newer thing that replaced it. */
  content_hash: string;
  run_id: string;
  state: 'pending' | 'approved' | 'rejected' | 'sent_back' | 'partial';
  disposition: Disposition | null;
  reason: string;
  evidence: Record<string, unknown>;
  actions: StagedAction[];
  pending_actions: StagedAction[];
  open: boolean;
  decided_by: string;
  result: Record<string, unknown> | null;
  created_at: number;
  updated_at: number;
}

export interface ReviewGroup {
  disposition: string;
  count: number;
  items: StagedRecord[];
}

export const getBoardReview = (boardId: string, openOnly = false) =>
  apiGet<{ groups: ReviewGroup[]; total: number; open: number }>(
    `/api/boards/${encodeURIComponent(boardId)}/review${openOnly ? '?open=1' : ''}`,
  );

const staged = (boardId: string, itemId: string, verb: string) =>
  `/api/boards/${encodeURIComponent(boardId)}/staged/${encodeURIComponent(itemId)}/${verb}`;

/**
 * Fire the staged writes.
 *
 * `content_hash` is the one the card was drawn from and `approval_id` is
 * minted by the client. Together they make approval safe to retry over a
 * dropped connection: the server consumes the id once and returns the stored
 * result to every replay, so the customer never gets two comments.
 */
export const approveStagedActions = (
  boardId: string,
  itemId: string,
  body: { content_hash: string; approval_id: string },
) => apiPost<{ replayed: boolean; result: Record<string, unknown> }>(
  staged(boardId, itemId, 'approve'), body);

export const rejectStagedActions = (
  boardId: string,
  itemId: string,
  body: { approval_id: string; reason?: string },
) => apiPost<{ replayed: boolean }>(staged(boardId, itemId, 'reject'), body);

/** How much of the earlier agent's context a sent-back item got back. */
export type ResumeTier = 'followup' | 'session' | 'fresh';

export interface ResumeOutcome {
  dispatched: boolean;
  run_id?: string;
  detail: string;
}

/**
 * Send an item back with a question (#588 Phase 6).
 *
 * `note` is REQUIRED by the server, and rightly so: the agent is about to work
 * this again and the note is the only thing telling it what to change. The
 * response says whether it was re-dispatched and how much context survived —
 * never assume it did.
 */
export const sendBackStagedActions = (
  boardId: string,
  itemId: string,
  body: { approval_id: string; note: string },
) => apiPost<{ replayed: boolean; resume?: ResumeOutcome }>(
  staged(boardId, itemId, 'send-back'), body);

/** Edit is the escape hatch between approve-as-written and reject. It is NOT
 *  itself a decision — the item stays open and still has to be approved. */
export const editStagedAction = (
  boardId: string,
  itemId: string,
  body: { action_id: string; params: Record<string, unknown> },
) => apiPost<StagedRecord>(staged(boardId, itemId, 'edit'), body);

/** A named, already-validated `select` (#588 Phase 7). Stored cleaned, so a
 *  saved strategy can never be one the run route would reject. */
export type BoardSelect = Record<string, unknown>;

export interface StrategiesResult {
  strategies: Record<string, BoardSelect>;
  orders: string[];
}

export interface StrategyPreview {
  select: BoardSelect;
  matched: number;
  would_work: number;
  skipped_already_processed: number;
  held_by_another_run: number;
  listing_complete: boolean;
  truncation_reason: string;
  sample: {
    id: string;
    key: string;
    title: string;
    status: string | null;
    priority: string | null;
  }[];
}

export interface BoardMetrics {
  board_id: string;
  dispositions: Record<string, number>;
  decisions: Record<string, number>;
  decided: number;
  approved: number;
  /** NULL when nothing has been decided. Not zero — a board nobody has
   *  reviewed and a board where everything was rejected are different facts,
   *  and rendering both as 0% would say the wrong one. */
  approval_rate: number | null;
  edited_before_approval: number;
  open: number;
}

export const listBoardStrategies = (boardId: string) =>
  apiGet<StrategiesResult>(`/api/boards/${encodeURIComponent(boardId)}/strategies`);

export const saveBoardStrategy = (
  boardId: string,
  body: { name: string; select: BoardSelect },
) => apiPost<{ strategies: Record<string, BoardSelect> }>(
  `/api/boards/${encodeURIComponent(boardId)}/strategies`, body);

export const deleteBoardStrategy = (boardId: string, name: string) =>
  apiDelete<{ strategies: Record<string, BoardSelect> }>(
    `/api/boards/${encodeURIComponent(boardId)}/strategies/${encodeURIComponent(name)}`);

/** What a run with this selection WOULD work — before spending N agents to
 *  find out. Runs the same `select_items` a real run runs. */
export const previewBoardStrategy = (boardId: string, select: BoardSelect) =>
  apiPost<StrategyPreview>(
    `/api/boards/${encodeURIComponent(boardId)}/strategies/preview`, { select });

export const getBoardMetrics = (boardId: string) =>
  apiGet<BoardMetrics>(`/api/boards/${encodeURIComponent(boardId)}/metrics`);

export interface StartRunBody {
  mode?: 'propose' | 'autonomous';
  concurrency?: number;
  select?: Record<string, unknown>;
  stop_on?: Record<string, number>;
}

export const listBoards = () =>
  apiGet<{ boards: Board[] }>('/api/boards');

export const getBoard = (id: string) =>
  apiGet<Board>(`/api/boards/${encodeURIComponent(id)}`);

export const getBoardItems = (id: string) =>
  apiGet<BoardItemsResult>(`/api/boards/${encodeURIComponent(id)}/items`);

export const createBoard = (connector: Record<string, unknown>) =>
  apiPost<Board>('/api/boards', connector);

export const updateBoard = (id: string, connector: Record<string, unknown>) =>
  apiPut<Board>(`/api/boards/${encodeURIComponent(id)}`, connector);

export const deleteBoard = (id: string) =>
  apiDelete<{ ok: boolean }>(`/api/boards/${encodeURIComponent(id)}`);

/** The verification oracle: runs the SAME engine production uses, so a
 *  connector that passes here has demonstrably worked rather than been
 *  asserted to work. */
export const testFetchBoard = (id: string, maxPages?: number) =>
  apiPost<BoardTestFetchResult>(
    `/api/boards/${encodeURIComponent(id)}/test-fetch`,
    maxPages ? { max_pages: maxPages } : {},
  );

// ── templates ──────────────────────────────────────────────────────────────

/** One blank in a template, and enough to render a field for it. `token` is
 *  the literal ALL-CAPS text sitting in the connector; the server substitutes
 *  it — never the browser, because these land inside URL paths and Jira's JQL
 *  string, where a stray space is a valid query against a different board. */
export interface TemplatePlaceholder {
  token: string;
  label: string;
  help: string;
  example: string;
}

/** What the operator must paste, or null when the workspace already brokers
 *  it (GitHub). Comes from the server so the form and the connector's own
 *  `auth` block cannot drift into asking for the wrong shape. */
export interface TemplateCredential {
  name: string;
  format: 'token' | 'basic';
  username_label: string;
  secret_label: string;
  help: string;
}

export interface BoardTemplate {
  id: string;
  display_name: string;
  vendor: string;
  actions: string[];
  needs: string[];
  placeholders: TemplatePlaceholder[];
  credential: TemplateCredential | null;
  /** Always false. A template has demonstrated nothing; only test-fetch has. */
  verified: boolean;
  note: string;
}

export const listBoardTemplates = () =>
  apiGet<{ templates: BoardTemplate[] }>('/api/boards/templates');

/** Template + answers → a connector, unsaved. Deliberately separate from
 *  create so what gets persisted is the same validated payload any other
 *  author would POST. */
export const fillBoardTemplate = (
  templateId: string,
  body: { values: Record<string, string>; id?: string; display_name?: string },
) =>
  apiPost<{ connector: Record<string, unknown>; verified: boolean }>(
    `/api/boards/templates/${encodeURIComponent(templateId)}/fill`,
    body,
  );

// ── credentials ────────────────────────────────────────────────────────────

export const listBoardCredentials = () =>
  apiGet<{ credentials: BoardCredential[] }>('/api/boards/credentials');

/** Save one. An empty `secret` on an existing name keeps the stored value, so
 *  a username can be corrected without re-typing a token the UI never showed. */
export const saveBoardCredential = (
  name: string,
  body: { secret?: string; format?: 'token' | 'basic'; username?: string },
) =>
  apiPut<{ credentials: BoardCredential[] }>(
    `/api/boards/credentials/${encodeURIComponent(name)}`,
    body,
  );

export const deleteBoardCredential = (name: string) =>
  apiDelete<{ ok: boolean }>(
    `/api/boards/credentials/${encodeURIComponent(name)}`,
  );

// ── runs ───────────────────────────────────────────────────────────────────

export const listBoardRuns = (boardId: string) =>
  apiGet<{ runs: BoardRunSummary[] }>(
    `/api/boards/${encodeURIComponent(boardId)}/runs`,
  );

export const getBoardRun = (boardId: string, runId: string) =>
  apiGet<BoardRun>(
    `/api/boards/${encodeURIComponent(boardId)}/runs/${encodeURIComponent(runId)}`,
  );

export const startBoardRun = (boardId: string, body: StartRunBody) =>
  apiPost<BoardRun>(`/api/boards/${encodeURIComponent(boardId)}/runs`, body);

/** Stops CLAIMING; items already dispatched finish. Killing a build mid-write
 *  leaves a half-applied multi-step action, which is worse than one extra
 *  comment. */
export const stopBoardRun = (boardId: string, runId: string) =>
  apiPost<{ ok: boolean; detail: string }>(
    `/api/boards/${encodeURIComponent(boardId)}/runs/${encodeURIComponent(runId)}/stop`,
    {},
  );

/** Why a run's concurrency is lower than what was asked for, or ''. */
export function clampLabel(run: {
  concurrency: number;
  requested_concurrency: number;
  clamp_reason: string;
}): string {
  if (!run.clamp_reason || run.concurrency >= run.requested_concurrency) return '';
  return run.clamp_reason;
}

/** Human-readable reason a listing stopped short, for the UI banner. */
export function truncationLabel(result: {
  complete: boolean;
  truncation_reason: string;
}): string {
  if (result.complete) return '';
  switch (result.truncation_reason) {
    case 'full_page_no_pagination_metadata':
      return 'The board returned a full page with no next-page signal, so there may be more items we could not see.';
    case 'max_pages':
      return 'Stopped at the page limit — this board has more items than were read.';
    case 'cursor_expired':
      return 'The board’s pagination cursor expired mid-read, so this list is partial.';
    case 'items_path_not_found':
      return 'The connector’s items_path did not match the response shape.';
    default:
      if (result.truncation_reason.startsWith('http_')) {
        return `The board returned ${result.truncation_reason.replace('http_', 'HTTP ')}.`;
      }
      return 'This list may be incomplete.';
  }
}
