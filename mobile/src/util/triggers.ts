/**
 * Pure helpers for the Triggers screen (#250) — validation, merging and
 * human-readable schedule labels.
 *
 * The validation rules deliberately mirror server.py's WebhookManager/
 * CronManager regexes so the form rejects a bad id or schedule before the
 * round-trip, and the user sees the same rule the backend enforces. Keeping
 * them here (no react-native imports) also lets the node-side vitest suite
 * cover them without a RN runtime.
 */
import type { CronRecord, Trigger, TriggerKind, WebhookRecord } from '../api/types';

/** WebhookManager._ID_RE — 1-64 chars of [A-Za-z0-9_-]. */
const WEBHOOK_ID_RE = /^[a-zA-Z0-9_-]{1,64}$/;
/** CronManager._ID_RE — tighter, because it becomes part of a k8s object name. */
const CRON_ID_RE = /^[a-z0-9-]{1,40}$/;
/** CronManager._SCHEDULE_RE — 5 restricted fields, or an @shorthand. */
const CRON_FIELD = '[0-9*/,-]+';
const SCHEDULE_RE = new RegExp(
  `^@(yearly|annually|monthly|weekly|daily|hourly)$|^${Array(5).fill(CRON_FIELD).join('\\s+')}$`,
);
/** CronManager._TIMEZONE_RE — IANA-ish name. */
const TIMEZONE_RE = /^[A-Za-z0-9_/+-]{1,64}$/;

export function isValidTriggerId(kind: TriggerKind, id: string): boolean {
  return kind === 'cron' ? CRON_ID_RE.test(id) : WEBHOOK_ID_RE.test(id);
}

export function triggerIdHint(kind: TriggerKind): string {
  return kind === 'cron'
    ? 'Lowercase letters, digits and hyphens (max 40) — it becomes a Kubernetes CronJob name.'
    : 'Letters, digits, hyphens and underscores (max 64).';
}

export function isValidSchedule(schedule: string): boolean {
  return SCHEDULE_RE.test(schedule.trim());
}

export function isValidTimezone(tz: string): boolean {
  return TIMEZONE_RE.test(tz.trim());
}

const SHORTHAND: Record<string, string> = {
  '@hourly': 'Every hour',
  '@daily': 'Every day at midnight',
  '@weekly': 'Every week (Sunday, midnight)',
  '@monthly': 'Every month (1st, midnight)',
  '@yearly': 'Every year (Jan 1, midnight)',
  '@annually': 'Every year (Jan 1, midnight)',
};

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

/**
 * A short, friendly gloss of a cron expression — a phone screen has no room
 * for a five-field crontab primer. Covers the shapes people actually write;
 * anything more exotic falls back to the raw expression (which is still shown
 * next to it), so this never lies about a schedule it doesn't understand.
 */
export function describeSchedule(schedule: string): string {
  const s = schedule.trim();
  if (SHORTHAND[s]) return SHORTHAND[s];
  const f = s.split(/\s+/);
  if (f.length !== 5) return s;
  const [min, hour, dom, mon, dow] = f;
  const everyDate = dom === '*' && mon === '*';
  const at = (h: string, m: string) => `${h.padStart(2, '0')}:${m.padStart(2, '0')}`;
  const numeric = (v: string) => /^\d+$/.test(v);

  if (min === '*' && hour === '*' && everyDate && dow === '*') return 'Every minute';
  if (numeric(min) && hour === '*' && everyDate && dow === '*') {
    return min === '0' ? 'Every hour, on the hour' : `Every hour at :${min.padStart(2, '0')}`;
  }
  if (/^\*\/\d+$/.test(min) && hour === '*' && everyDate && dow === '*') {
    return `Every ${min.slice(2)} minutes`;
  }
  if (numeric(min) && /^\*\/\d+$/.test(hour) && everyDate && dow === '*') {
    return `Every ${hour.slice(2)} hours at :${min.padStart(2, '0')}`;
  }
  if (numeric(min) && numeric(hour) && everyDate) {
    if (dow === '*') return `Every day at ${at(hour, min)}`;
    if (numeric(dow) && Number(dow) <= 6) return `Every ${DAYS[Number(dow)]} at ${at(hour, min)}`;
  }
  if (numeric(min) && numeric(hour) && numeric(dom) && mon === '*' && dow === '*') {
    return `Monthly on the ${dom}${ordinal(Number(dom))} at ${at(hour, min)}`;
  }
  return s;
}

function ordinal(n: number): string {
  if (n % 100 >= 11 && n % 100 <= 13) return 'th';
  return ['th', 'st', 'nd', 'rd'][n % 10] ?? 'th';
}

/** Fold the two backend lists into one newest-first stream (matches the web
 *  dashboard's Triggers route, which presents both kinds in a single list). */
export function mergeTriggers(webhooks: WebhookRecord[], crons: CronRecord[]): Trigger[] {
  const out: Trigger[] = [
    ...webhooks.map(
      (w): Trigger => ({
        kind: 'webhook',
        id: w.id,
        prompt: w.prompt_template,
        workdir: w.workdir,
        created_at: w.created_at,
        unsigned: w.unsigned,
        receive_url: w.receive_url,
      }),
    ),
    ...crons.map(
      (c): Trigger => ({
        kind: 'cron',
        id: c.id,
        prompt: c.prompt_template,
        workdir: c.workdir,
        created_at: c.created_at,
        schedule: c.schedule,
        timezone: c.timezone,
        suspended: c.suspended,
      }),
    ),
  ];
  out.sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0));
  return out;
}

/** Free-text filter over id, prompt and schedule (same fields as the web
 *  dashboard's filter box). */
export function filterTriggers(list: Trigger[], query: string): Trigger[] {
  const q = query.trim().toLowerCase();
  if (!q) return list;
  return list.filter((t) =>
    `${t.kind} ${t.id} ${t.prompt} ${t.schedule ?? ''} ${t.workdir ?? ''}`.toLowerCase().includes(q),
  );
}
