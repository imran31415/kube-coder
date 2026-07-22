import { describe, expect, it, afterEach, vi } from 'vitest';
import {
  getProviders,
  getCredentials,
  putCredentials,
  deleteCredentials,
  testConnection,
  createLink,
  listLinks,
  deleteLink,
} from './gateway';

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

function capture(body: unknown) {
  const calls: { url: string; method: string; body?: string }[] = [];
  globalThis.fetch = vi.fn(async (u: string, init?: RequestInit) => {
    calls.push({ url: u, method: init?.method ?? 'GET', body: init?.body as string | undefined });
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

describe('gateway api', () => {
  it('getProviders GETs the catalog', async () => {
    const calls = capture({ providers: [], available: true });
    await getProviders();
    expect(calls[0].method).toBe('GET');
    expect(calls[0].url).toContain('/api/gateway/providers');
  });

  it('getCredentials GETs the redacted view', async () => {
    const calls = capture({ credentials: { configured: false, provider_id: null, fields: {} } });
    await getCredentials();
    expect(calls[0].method).toBe('GET');
    expect(calls[0].url).toContain('/api/gateway/credentials');
  });

  it('putCredentials PUTs the body', async () => {
    const calls = capture({ ok: true, credentials: {} });
    await putCredentials({ provider_id: 'twilio', creds: { account_sid: 'AC1' }, sender_number: 'whatsapp:+1' });
    expect(calls[0].method).toBe('PUT');
    expect(calls[0].url).toContain('/api/gateway/credentials');
    expect(JSON.parse(calls[0].body!)).toEqual({
      provider_id: 'twilio',
      creds: { account_sid: 'AC1' },
      sender_number: 'whatsapp:+1',
    });
  });

  it('deleteCredentials DELETEs', async () => {
    const calls = capture({ ok: true });
    await deleteCredentials();
    expect(calls[0].method).toBe('DELETE');
    expect(calls[0].url).toContain('/api/gateway/credentials');
  });

  it('testConnection POSTs to /test', async () => {
    const calls = capture({ ok: true, detail: 'HTTP 200' });
    await testConnection();
    expect(calls[0].method).toBe('POST');
    expect(calls[0].url).toContain('/api/gateway/test');
  });

  it('createLink POSTs a workspace', async () => {
    const calls = capture({ code: '123456', expires_in: 600, whatsapp_number: '', workspace: 'ws' });
    await createLink('ws');
    expect(calls[0].method).toBe('POST');
    expect(calls[0].url).toContain('/api/gateway/link');
    expect(JSON.parse(calls[0].body!)).toEqual({ workspace: 'ws' });
  });

  it('listLinks GETs the bindings', async () => {
    const calls = capture({ links: [], available: true });
    await listLinks();
    expect(calls[0].method).toBe('GET');
    expect(calls[0].url).toContain('/api/gateway/links');
  });

  it('deleteLink DELETEs by id', async () => {
    const calls = capture({ ok: true });
    await deleteLink('abc123');
    expect(calls[0].method).toBe('DELETE');
    expect(calls[0].url).toContain('/api/gateway/link/abc123');
  });
});
