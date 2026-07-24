import { describe, expect, it, afterEach, vi } from 'vitest';
import {
  getSubscriptions,
  logoutSubscription,
  startClaudeConnect,
  submitClaudeConnectCode,
  pollClaudeConnect,
  cancelClaudeConnect,
} from './subscriptions';

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

describe('claude connect api', () => {
  it('startClaudeConnect POSTs and returns the sign-in URL', async () => {
    const calls = capture({ url: 'https://claude.com/cai/oauth/authorize?code=true', in_progress: true });
    const r = await startClaudeConnect();
    expect(calls[0].url).toContain('/api/subscriptions/claude/login/start');
    expect(calls[0].method).toBe('POST');
    expect(r.url).toContain('oauth/authorize');
  });

  it('submitClaudeConnectCode POSTs the pasted code', async () => {
    const calls = capture({ ok: true });
    await submitClaudeConnectCode('abc123#state');
    expect(calls[0].url).toContain('/api/subscriptions/claude/login/code');
    expect(calls[0].method).toBe('POST');
  });

  it('pollClaudeConnect surfaces connected + refreshed subscriptions', async () => {
    const calls = capture({
      connected: true,
      in_progress: false,
      subscriptions: { claude: { logged_in: true, kind: 'subscription', plan: 'max' }, codex: { logged_in: false } },
    });
    const r = await pollClaudeConnect();
    expect(calls[0].url).toContain('/api/subscriptions/claude/login/poll');
    expect(r.connected).toBe(true);
    expect(r.subscriptions?.claude.plan).toBe('max');
  });

  it('cancelClaudeConnect POSTs the cancel path', async () => {
    const calls = capture({ ok: true });
    await cancelClaudeConnect();
    expect(calls[0].url).toContain('/api/subscriptions/claude/login/cancel');
    expect(calls[0].method).toBe('POST');
  });
});
