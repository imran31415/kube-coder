import { describe, expect, it, afterEach, vi } from 'vitest';
import { getSubscriptions, logoutSubscription } from './subscriptions';

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

/** Capture the URL + method the api layer hits, returning `body` as JSON. */
function capture(body: unknown) {
  const calls: { url: string; method: string }[] = [];
  globalThis.fetch = vi.fn(async (u: string, init?: RequestInit) => {
    calls.push({ url: u, method: init?.method ?? 'GET' });
    return {
      ok: true,
      status: 200,
      statusText: '',
      headers: { get: (k: string) => (k.toLowerCase() === 'content-type' ? 'application/json' : null) },
      json: async () => body,
      text: async () => JSON.stringify(body),
    } as unknown as Response;
  }) as unknown as typeof fetch;
  return calls;
}

describe('subscriptions api (#251)', () => {
  it('getSubscriptions hits /api/subscriptions and unwraps the view', async () => {
    const calls = capture({
      subscriptions: {
        claude: { logged_in: true, kind: 'subscription', plan: 'max', expires_at: 123, expired: false, overridden_by_key: false },
        codex: { logged_in: false, available: false },
      },
    });
    const r = await getSubscriptions();
    expect(calls[0].url).toContain('/api/subscriptions');
    expect(calls[0].method).toBe('GET');
    expect(r.subscriptions.claude.logged_in).toBe(true);
    expect(r.subscriptions.claude.plan).toBe('max');
    expect(r.subscriptions.codex.available).toBe(false);
  });

  it('logoutSubscription DELETEs the per-provider path', async () => {
    const calls = capture({ ok: true });
    await logoutSubscription('codex');
    expect(calls[0].url).toContain('/api/subscriptions/codex');
    expect(calls[0].method).toBe('DELETE');
  });
});
