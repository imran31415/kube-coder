import { describe, expect, it } from 'vitest';
import type { CronRecord, WebhookRecord, PageWatchRecord } from '../api/types';
import {
  isValidWatchUrl,
  isValidSelector,
  describeSchedule,
  filterTriggers,
  isValidSchedule,
  isValidTimezone,
  isValidTriggerId,
  mergeTriggers,
} from './triggers';

describe('trigger id validation (mirrors server.py)', () => {
  it('accepts what WebhookManager._ID_RE accepts', () => {
    expect(isValidTriggerId('webhook', 'github_CI-1')).toBe(true);
    expect(isValidTriggerId('webhook', 'a'.repeat(64))).toBe(true);
    expect(isValidTriggerId('webhook', 'a'.repeat(65))).toBe(false);
    expect(isValidTriggerId('webhook', 'has spaces')).toBe(false);
    expect(isValidTriggerId('webhook', '')).toBe(false);
  });

  it('holds cron ids to the tighter k8s-name rule', () => {
    expect(isValidTriggerId('cron', 'nightly-tests')).toBe(true);
    // Uppercase and underscores are fine for a webhook but not a CronJob name.
    expect(isValidTriggerId('cron', 'Nightly')).toBe(false);
    expect(isValidTriggerId('cron', 'nightly_tests')).toBe(false);
    expect(isValidTriggerId('cron', 'a'.repeat(41))).toBe(false);
  });
});

describe('schedule + timezone validation', () => {
  it('accepts five restricted fields and @shorthands', () => {
    expect(isValidSchedule('0 9 * * *')).toBe(true);
    expect(isValidSchedule('*/15 * * * *')).toBe(true);
    expect(isValidSchedule('0 9 1,15 * 1-5')).toBe(true);
    expect(isValidSchedule('@daily')).toBe(true);
  });

  it('rejects short, over-long and injection-shaped expressions', () => {
    expect(isValidSchedule('0 9 * *')).toBe(false);
    expect(isValidSchedule('0 9 * * * *')).toBe(false);
    expect(isValidSchedule('@every-other-tuesday')).toBe(false);
    // The server restricts the character class to keep the schedule safe to
    // interpolate into the CronJob manifest.
    expect(isValidSchedule('0 9 * * *"\n  foo: bar')).toBe(false);
  });

  it('accepts IANA-shaped timezones only', () => {
    expect(isValidTimezone('UTC')).toBe(true);
    expect(isValidTimezone('America/Los_Angeles')).toBe(true);
    expect(isValidTimezone('Etc/GMT+5')).toBe(true);
    expect(isValidTimezone('America/Los Angeles')).toBe(false);
    expect(isValidTimezone("UTC'")).toBe(false);
  });
});

describe('describeSchedule', () => {
  it('glosses the common shapes', () => {
    expect(describeSchedule('@daily')).toBe('Every day at midnight');
    expect(describeSchedule('* * * * *')).toBe('Every minute');
    expect(describeSchedule('0 * * * *')).toBe('Every hour, on the hour');
    expect(describeSchedule('30 * * * *')).toBe('Every hour at :30');
    expect(describeSchedule('*/15 * * * *')).toBe('Every 15 minutes');
    expect(describeSchedule('0 */6 * * *')).toBe('Every 6 hours at :00');
    expect(describeSchedule('30 2 * * *')).toBe('Every day at 02:30');
    expect(describeSchedule('0 9 * * 1')).toBe('Every Mon at 09:00');
    expect(describeSchedule('0 9 1 * *')).toBe('Monthly on the 1st at 09:00');
    expect(describeSchedule('0 9 3 * *')).toBe('Monthly on the 3rd at 09:00');
  });

  it('falls back to the raw expression rather than guessing', () => {
    expect(describeSchedule('0 9 1,15 * 1-5')).toBe('0 9 1,15 * 1-5');
    expect(describeSchedule('nonsense')).toBe('nonsense');
  });
});

const webhook = (over: Partial<WebhookRecord> = {}): WebhookRecord => ({
  id: 'wh',
  prompt_template: 'do the thing',
  workdir: '/home/dev',
  created_at: 100,
  ...over,
});

const cron = (over: Partial<CronRecord> = {}): CronRecord => ({
  id: 'cr',
  schedule: '0 9 * * *',
  prompt_template: 'run the suite',
  workdir: '/home/dev',
  created_at: 200,
  ...over,
});

describe('mergeTriggers', () => {
  it('folds both kinds into one newest-first list', () => {
    const merged = mergeTriggers(
      [webhook({ id: 'old-hook', created_at: 10 }), webhook({ id: 'new-hook', created_at: 300 })],
      [cron({ id: 'mid-cron', created_at: 200 })],
    );
    expect(merged.map((t) => t.id)).toEqual(['new-hook', 'mid-cron', 'old-hook']);
    expect(merged.map((t) => t.kind)).toEqual(['webhook', 'cron', 'webhook']);
  });

  it('carries the kind-specific fields across', () => {
    const [c] = mergeTriggers([], [cron({ suspended: true, timezone: 'UTC' })]);
    expect(c).toMatchObject({ schedule: '0 9 * * *', timezone: 'UTC', suspended: true });
    const [w] = mergeTriggers([webhook({ unsigned: true })], []);
    expect(w).toMatchObject({ kind: 'webhook', unsigned: true, prompt: 'do the thing' });
    expect(w.schedule).toBeUndefined();
  });

  it('treats a missing created_at as oldest instead of dropping the row', () => {
    const merged = mergeTriggers([webhook({ id: 'undated', created_at: undefined })], [cron()]);
    expect(merged.map((t) => t.id)).toEqual(['cr', 'undated']);
  });
});

describe('filterTriggers', () => {
  const list = mergeTriggers([webhook({ id: 'github-ci' })], [cron({ id: 'nightly-tests' })]);

  it('matches id, prompt, schedule and kind', () => {
    expect(filterTriggers(list, 'github').map((t) => t.id)).toEqual(['github-ci']);
    expect(filterTriggers(list, 'suite').map((t) => t.id)).toEqual(['nightly-tests']);
    expect(filterTriggers(list, '0 9').map((t) => t.id)).toEqual(['nightly-tests']);
    expect(filterTriggers(list, 'webhook').map((t) => t.id)).toEqual(['github-ci']);
  });

  it('is case-insensitive and returns everything for an empty query', () => {
    expect(filterTriggers(list, 'GITHUB').map((t) => t.id)).toEqual(['github-ci']);
    expect(filterTriggers(list, '   ')).toHaveLength(2);
  });
});

describe('page-watch validation (mirrors server.py)', () => {
  it('holds page-watch ids to the tight k8s-name rule, like crons', () => {
    // PageWatchManager reuses CronManager._ID_RE — a watch is backed by a
    // CronJob too, so it inherits the stricter rule, not the webhook one.
    expect(isValidTriggerId('page-watch', 'ci-green')).toBe(true);
    expect(isValidTriggerId('page-watch', 'CI-Green')).toBe(false);
    expect(isValidTriggerId('page-watch', 'has_underscore')).toBe(false);
    expect(isValidTriggerId('page-watch', 'a'.repeat(41))).toBe(false);
    // ...and the webhook rule really is looser, so this is a real distinction.
    expect(isValidTriggerId('webhook', 'has_underscore')).toBe(true);
  });

  it('accepts http(s) urls and rejects everything else', () => {
    expect(isValidWatchUrl('https://example.com/build')).toBe(true);
    expect(isValidWatchUrl('http://example.com')).toBe(true);
    expect(isValidWatchUrl('example.com')).toBe(false);
    expect(isValidWatchUrl('file:///etc/passwd')).toBe(false);
    expect(isValidWatchUrl('javascript:alert(1)')).toBe(false);
    expect(isValidWatchUrl('')).toBe(false);
    expect(isValidWatchUrl(`https://e.test/${'x'.repeat(2100)}`)).toBe(false);
  });

  it('accepts the selector subset page_watch.parse_selector supports', () => {
    for (const ok of ['div', '#main', '.status', 'div.card', 'span#id.a.b',
      '[data-state="green"]', '.build .status']) {
      expect(isValidSelector(ok)).toBe(true);
    }
  });

  it('rejects selector syntax the server would refuse', () => {
    // Rejecting here means the user learns the limit while typing, rather
    // than from a failed save.
    for (const bad of ['.a > .b', '.a + .b', '.a ~ .b', '.a, .b', 'a:hover']) {
      expect(isValidSelector(bad)).toBe(false);
    }
  });

  it('treats an empty selector as "watch the whole page"', () => {
    expect(isValidSelector('')).toBe(true);
    expect(isValidSelector('   ')).toBe(true);
  });
});

describe('mergeTriggers with page watches', () => {
  const pw = (over: Partial<PageWatchRecord> = {}): PageWatchRecord => ({
    id: 'ci',
    url: 'https://example.test/badge.svg',
    schedule: '*/5 * * * *',
    prompt_template: 'CI changed',
    created_at: 500,
    ...over,
  });

  it('folds all three kinds into one newest-first stream', () => {
    const out = mergeTriggers(
      [webhook({ created_at: 100 })],
      [cron({ created_at: 200 })],
      [pw({ created_at: 300 })],
    );
    expect(out.map((t) => t.kind)).toEqual(['page-watch', 'cron', 'webhook']);
  });

  it('carries the page-watch fields the row renders', () => {
    const [t] = mergeTriggers([], [], [pw({ selector: '.status', last_error: 'boom' })]);
    expect(t.url).toBe('https://example.test/badge.svg');
    expect(t.selector).toBe('.status');
    expect(t.last_error).toBe('boom');
  });

  it('defaults to no page watches so existing callers still work', () => {
    expect(mergeTriggers([webhook()], [cron()])).toHaveLength(2);
  });

  it('filters on the url and selector, not just id and prompt', () => {
    const list = mergeTriggers([], [], [pw({ selector: '.build-status' })]);
    expect(filterTriggers(list, 'badge.svg')).toHaveLength(1);
    expect(filterTriggers(list, 'build-status')).toHaveLength(1);
    expect(filterTriggers(list, 'nothing-here')).toHaveLength(0);
  });
});
