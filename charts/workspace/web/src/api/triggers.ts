import { apiGet, apiPost, apiDelete } from './client';

export type TriggerKind = 'webhook' | 'cron' | 'page-watch';

/** What a cron or webhook does when it fires (#709 part 2). A config saved
 *  before this existed has no `action`, and means `build`. */
export type TriggerAction = 'build' | 'board_run';

/** A Board-run trigger's settings. Always propose mode: the server refuses
 *  any other, so there is no mode field to send. */
export interface TriggerBoardRun {
  board_id: string;
  select?: { tags?: string[]; limit?: number } & Record<string, unknown>;
  concurrency?: number;
}

/** A webhook's delivery filter: exact strings at payload dot-paths. */
export interface WebhookMatch {
  /** Compared with the delivery's X-GitHub-Event header. */
  event?: string;
  payload?: Record<string, string>;
}

export interface WebhookRecord {
  id: string;
  prompt_template: string;
  action?: TriggerAction;
  board_run?: TriggerBoardRun;
  match?: WebhookMatch;
  item_id_from?: string;
  workdir?: string;
  interpolate_mode?: 'attach' | 'interpolate';
  source?: string;
  created_at?: number;
  secret_set?: boolean;
}

export interface CronRecord {
  id: string;
  schedule: string;
  prompt_template: string;
  action?: TriggerAction;
  board_run?: TriggerBoardRun;
  workdir?: string;
  payload?: Record<string, unknown>;
  interpolate_mode?: 'attach' | 'interpolate';
  timezone?: string;
  suspended?: boolean;
  created_at?: number;
  fire_token_set?: boolean;
  last_fire_at?: number;
}

/** A page-watch (#681): a cron whose fire is conditional on a URL's content
 *  changing. `last_hash` is server-side state, surfaced only so the UI can
 *  show whether a baseline has been taken yet. */
export interface PageWatchRecord {
  id: string;
  url: string;
  selector?: string | null;
  schedule: string;
  prompt_template: string;
  workdir?: string;
  timezone?: string;
  interpolate_mode?: 'attach' | 'interpolate';
  include_content?: boolean;
  /** Reserved for a future Playwright path — always false in this version. */
  render?: boolean;
  suspended?: boolean;
  created_at?: number;
  fire_token_set?: boolean;
  redirected_from?: string;
  last_hash?: string | null;
  last_checked_at?: number | null;
  last_changed_at?: number | null;
  last_error?: string | null;
  consecutive_failures?: number;
}

export interface Trigger {
  kind: TriggerKind;
  id: string;
  name: string;
  schedule?: string;       // cron + page-watch
  prompt: string;
  /** cron + webhook. Always set by listTriggers; absent on the wire means build. */
  action?: TriggerAction;
  board_run?: TriggerBoardRun;
  // webhook only
  match?: WebhookMatch;
  item_id_from?: string;
  workdir?: string;
  timezone?: string;
  suspended?: boolean;
  created_at?: number;
  // page-watch only
  url?: string;
  selector?: string | null;
  last_checked_at?: number | null;
  last_changed_at?: number | null;
  last_error?: string | null;
  /** null until the first successful check has taken a baseline. */
  last_hash?: string | null;
}

export async function listTriggers(): Promise<Trigger[]> {
  // Each list is caught independently so one failing endpoint blanks only its
  // own kind rather than emptying the whole Triggers tab.
  const [wh, cr, pw] = await Promise.all([
    apiGet<{ webhooks: WebhookRecord[] }>('/api/webhooks').catch(() => ({ webhooks: [] as WebhookRecord[] })),
    apiGet<{ crons: CronRecord[] }>('/api/crons').catch(() => ({ crons: [] as CronRecord[] })),
    apiGet<{ page_watches: PageWatchRecord[] }>('/api/page-watches').catch(() => ({ page_watches: [] as PageWatchRecord[] })),
  ]);
  const triggers: Trigger[] = [];
  for (const w of wh.webhooks) {
    triggers.push({
      kind: 'webhook',
      id: w.id,
      name: w.id,
      prompt: w.prompt_template,
      workdir: w.workdir,
      created_at: w.created_at,
      action: w.action ?? 'build',
      board_run: w.board_run,
      match: w.match,
      item_id_from: w.item_id_from,
    });
  }
  for (const c of cr.crons) {
    triggers.push({
      kind: 'cron',
      id: c.id,
      name: c.id,
      schedule: c.schedule,
      prompt: c.prompt_template,
      workdir: c.workdir,
      timezone: c.timezone,
      suspended: c.suspended,
      created_at: c.created_at,
      action: c.action ?? 'build',
      board_run: c.board_run,
    });
  }
  for (const p of pw.page_watches) {
    triggers.push({
      kind: 'page-watch',
      id: p.id,
      name: p.id,
      schedule: p.schedule,
      prompt: p.prompt_template,
      workdir: p.workdir,
      timezone: p.timezone,
      suspended: p.suspended,
      created_at: p.created_at,
      url: p.url,
      selector: p.selector,
      last_checked_at: p.last_checked_at,
      last_changed_at: p.last_changed_at,
      last_error: p.last_error,
      last_hash: p.last_hash,
    });
  }
  triggers.sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0));
  return triggers;
}

/** One recorded fire (#91). A `spawned` run has a `task_id`; `rejected` and
 *  `error` carry a `reason` slug and usually an `error` gloss. `skipped` is a
 *  page-watch check that arrived and correctly chose to do nothing. */
export interface TriggerRun {
  ts: number;
  type: TriggerKind;
  trigger_id: string;
  outcome: 'spawned' | 'skipped' | 'rejected' | 'error';
  /** Short slug: bad_signature, replay, at_capacity, suspended, unchanged, … */
  reason?: string;
  task_id?: string;
  /** A Board-run trigger (#709) starts a run on this board, not a task. */
  run_id?: string;
  board_id?: string;
  error?: string;
  /** The socket peer — behind the ingress, usually the ingress controller. */
  source_ip?: string;
  /** Leftmost X-Forwarded-For hop. Caller-asserted, hence a separate field. */
  forwarded_for?: string;
  /** Present only where a proof of authorisation applied: an HMAC for a
   *  webhook, the fire token for a cron/page-watch. Absent on the dashboard's
   *  own Test / Check-now buttons, which authenticate as the workspace owner. */
  signature_verified?: boolean;
  provider?: string;
  /** Fired from the dashboard rather than by a timer or an external sender. */
  manual?: boolean;
}

export interface TriggerRunPage {
  runs: TriggerRun[];
  /** Entries still on disk, which is what pagination needs — not the number of
   *  times the trigger has ever fired. The ledger is capped, so those differ. */
  total: number;
  limit: number;
  offset: number;
}

/** Each kind's REST collection. The ledger path is derived from it rather than
 *  from `kind` directly, because the server's collections are plurals
 *  ('page-watches') and the kind is not. */
const RUNS_COLLECTION: Record<TriggerKind, string> = {
  webhook: 'webhooks',
  cron: 'crons',
  'page-watch': 'page-watches',
};

export const listTriggerRuns = (
  kind: TriggerKind,
  id: string,
  opts: { limit?: number; offset?: number } = {},
) =>
  apiGet<TriggerRunPage>(
    `/api/${RUNS_COLLECTION[kind]}/${encodeURIComponent(id)}/runs`,
    { limit: opts.limit, offset: opts.offset },
  );

export interface CreateCronInput {
  id: string;
  schedule: string;
  /** Required for a Build; ignored for a Board run. */
  prompt_template?: string;
  workdir?: string;
  timezone?: string;
  action?: TriggerAction;
  board_run?: TriggerBoardRun;
}

export const createCron = (input: CreateCronInput) =>
  apiPost<CronRecord>('/api/crons', input);

export const fireCron = (id: string) => apiPost<{ ok: true }>(`/api/crons/${id}/run`, {});
export const suspendCron = (id: string) => apiPost<{ ok: true }>(`/api/crons/${id}/suspend`, {});
export const resumeCron = (id: string) => apiPost<{ ok: true }>(`/api/crons/${id}/resume`, {});
export const deleteCron = (id: string) => apiDelete<{ ok: true }>(`/api/crons/${id}`);

export interface CreateWebhookInput {
  id: string;
  /** Required for a Build; ignored for a Board run. */
  prompt_template?: string;
  workdir?: string;
  interpolate_mode?: 'attach' | 'interpolate';
  action?: TriggerAction;
  board_run?: TriggerBoardRun;
  match?: WebhookMatch;
  item_id_from?: string;
}

export const createWebhook = (input: CreateWebhookInput) =>
  apiPost<WebhookRecord & { receive_url?: string; hmac_secret_once?: string }>(
    '/api/webhooks', input);

/** What a fire answers: a Build's task, a Board run, or a skip (#709). */
export interface WebhookFireResult {
  task_id?: string;
  run_id?: string;
  board_id?: string;
  skipped?: boolean;
  reason?: string;
}

/** The dashboard's Test button. `event` stands in for X-GitHub-Event, which
 *  this request does not carry, so a webhook with `match.event` can be
 *  exercised from here. */
export const testWebhook = (
  id: string,
  body: { event?: string; payload?: unknown } = {},
) => apiPost<WebhookFireResult>(`/api/webhooks/${id}/test`, body);
export const deleteWebhook = (id: string) => apiDelete<{ ok: true }>(`/api/webhooks/${id}`);

export interface CreatePageWatchInput {
  id: string;
  url: string;
  schedule: string;
  prompt_template: string;
  selector?: string;
  workdir?: string;
  timezone?: string;
  include_content?: boolean;
}

/** The server may answer 202 with a `warning` when the config saved but the
 *  CronJob did not apply — a watch with no timer never fires, so the caller
 *  must surface it rather than treat 202 as success. */
export const createPageWatch = (input: CreatePageWatchInput) =>
  apiPost<PageWatchRecord & { warning?: string }>('/api/page-watches', input);

/** Runs the same code path as the scheduled check, so what the button does and
 *  what the CronJob does cannot drift apart. */
export const checkPageWatch = (id: string) =>
  apiPost<{ outcome: string; error?: string }>(`/api/page-watches/${id}/check`, {});
export const suspendPageWatch = (id: string) => apiPost<{ ok: true }>(`/api/page-watches/${id}/suspend`, {});
export const resumePageWatch = (id: string) => apiPost<{ ok: true }>(`/api/page-watches/${id}/resume`, {});
export const deletePageWatch = (id: string) => apiDelete<{ ok: true }>(`/api/page-watches/${id}`);
