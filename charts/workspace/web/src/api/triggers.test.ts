/**
 * Triggers API — the three-way fan-out and the page-watch mapping (#681).
 *
 * The triggers API had no test coverage before page-watch was added. These
 * stub globalThis.fetch rather than the client module, matching the pattern in
 * src/api/client.test.ts.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { listTriggers, type CronRecord, type PageWatchRecord, type WebhookRecord } from './triggers';

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

type Bodies = Record<string, unknown>;

/** Serve a canned JSON body per URL suffix; anything unlisted 500s. */
function serve(bodies: Bodies) {
  const calls: string[] = [];
  globalThis.fetch = vi.fn(async (u: string) => {
    calls.push(String(u));
    const key = Object.keys(bodies).find((k) => String(u).includes(k));
    const ok = key !== undefined;
    return {
      ok,
      status: ok ? 200 : 500,
      statusText: ok ? 'OK' : 'Server Error',
      headers: { get: (h: string) => (h.toLowerCase() === 'content-type' ? 'application/json' : null) },
      json: async () => (ok ? bodies[key as string] : { error: 'boom' }),
      text: async () => JSON.stringify(ok ? bodies[key as string] : { error: 'boom' }),
    } as unknown as Response;
  }) as unknown as typeof fetch;
  return calls;
}

const watch = (over: Partial<PageWatchRecord> = {}): PageWatchRecord => ({
  id: 'ci',
  url: 'https://example.test/badge.svg',
  schedule: '*/5 * * * *',
  prompt_template: 'CI changed',
  workdir: '/home/dev',
  created_at: 300,
  last_hash: 'sha256:abc',
  last_checked_at: 1000,
  ...over,
});

describe('listTriggers', () => {
  it('fans out to all three endpoints', async () => {
    const calls = serve({
      '/api/webhooks': { webhooks: [] },
      '/api/crons': { crons: [] },
      '/api/page-watches': { page_watches: [] },
    });
    await listTriggers();
    expect(calls.some((c) => c.includes('/api/webhooks'))).toBe(true);
    expect(calls.some((c) => c.includes('/api/crons'))).toBe(true);
    expect(calls.some((c) => c.includes('/api/page-watches'))).toBe(true);
  });

  it('maps a page-watch into the unified Trigger shape', async () => {
    serve({
      '/api/webhooks': { webhooks: [] },
      '/api/crons': { crons: [] },
      '/api/page-watches': { page_watches: [watch({ selector: '.status' })] },
    });
    const [t] = await listTriggers();
    expect(t.kind).toBe('page-watch');
    expect(t.id).toBe('ci');
    expect(t.url).toBe('https://example.test/badge.svg');
    expect(t.selector).toBe('.status');
    expect(t.prompt).toBe('CI changed');
    expect(t.last_checked_at).toBe(1000);
  });

  it('keeps the other kinds when page-watches fails', async () => {
    // One endpoint failing must blank only its own kind, never the whole tab.
    serve({
      '/api/webhooks': { webhooks: [{ id: 'wh', prompt_template: 'p' } as WebhookRecord] },
      '/api/crons': { crons: [{ id: 'cr', schedule: '0 * * * *', prompt_template: 'p' } as CronRecord] },
    });
    const list = await listTriggers();
    expect(list.map((t) => t.kind).sort()).toEqual(['cron', 'webhook']);
  });

  it('keeps page-watches when the other two fail', async () => {
    serve({ '/api/page-watches': { page_watches: [watch()] } });
    const list = await listTriggers();
    expect(list).toHaveLength(1);
    expect(list[0].kind).toBe('page-watch');
  });

  it('sorts all three kinds together, newest first', async () => {
    serve({
      '/api/webhooks': { webhooks: [{ id: 'wh', prompt_template: 'p', created_at: 200 } as WebhookRecord] },
      '/api/crons': { crons: [{ id: 'cr', schedule: '0 * * * *', prompt_template: 'p', created_at: 100 } as CronRecord] },
      '/api/page-watches': { page_watches: [watch({ created_at: 300 })] },
    });
    const list = await listTriggers();
    expect(list.map((t) => t.id)).toEqual(['ci', 'wh', 'cr']);
  });

  it('surfaces a watch that has not taken a baseline yet', async () => {
    serve({
      '/api/webhooks': { webhooks: [] },
      '/api/crons': { crons: [] },
      '/api/page-watches': { page_watches: [watch({ last_hash: null, last_checked_at: null })] },
    });
    const [t] = await listTriggers();
    // The row renders "waiting for first check" off exactly this.
    expect(t.last_hash).toBeNull();
  });
});
