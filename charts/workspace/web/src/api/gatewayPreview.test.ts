import { describe, expect, it, afterEach, vi } from 'vitest';
import { fetchPreview, sendPreview, previewControl } from './gatewayPreview';

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

function capture(body: unknown) {
  const calls: { url: string; method: string; body?: string }[] = [];
  globalThis.fetch = vi.fn(async (u: string, init?: RequestInit) => {
    calls.push({
      url: u,
      method: init?.method ?? 'GET',
      body: init?.body as string | undefined,
    });
    return {
      ok: true,
      status: 200,
      statusText: '',
      headers: {
        get: (k: string) =>
          k.toLowerCase() === 'content-type' ? 'application/json' : null,
      },
      json: async () => body,
      text: async () => JSON.stringify(body),
    } as unknown as Response;
  }) as unknown as typeof fetch;
  return calls;
}

describe('gatewayPreview api', () => {
  it('fetchPreview GETs the transcript with a since cursor', async () => {
    const calls = capture({ available: true, messages: [], cursor: 0 });
    await fetchPreview(5);
    expect(calls[0].method).toBe('GET');
    expect(calls[0].url).toContain('/api/gateway/internal/transcript');
    expect(calls[0].url).toContain('since=5');
  });

  it('sendPreview posts text', async () => {
    const calls = capture({ ok: true, action: 'dispatched', cursor: 2 });
    await sendPreview('hello');
    expect(calls[0].method).toBe('POST');
    expect(calls[0].url).toContain('/api/gateway/internal/inbound');
    expect(JSON.parse(calls[0].body!)).toEqual({ text: 'hello' });
  });

  it('sendPreview posts a button reply as button (not text)', async () => {
    const calls = capture({ ok: true, action: 'dispatched', cursor: 3 });
    await sendPreview('', 'Yes');
    expect(JSON.parse(calls[0].body!)).toEqual({ button: 'Yes' });
  });

  it('previewControl posts the action + flag', async () => {
    const calls = capture({ ok: true });
    await previewControl('simulate', true);
    expect(calls[0].url).toContain('/api/gateway/internal/control');
    expect(JSON.parse(calls[0].body!)).toEqual({ action: 'simulate', on: true });
  });
});
