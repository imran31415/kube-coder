import { describe, expect, it, afterEach, vi } from 'vitest';
import { api, apiGet, apiPost, ApiError, isErrorResponse, withOauthPrefix } from './client';

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

function mockOnce(body: unknown, init: { status?: number; contentType?: string } = {}) {
  const status = init.status ?? 200;
  const ct = init.contentType ?? 'application/json';
  globalThis.fetch = vi.fn(async () => ({
    ok: status >= 200 && status < 300,
    status,
    statusText: '',
    headers: { get: (k: string) => (k.toLowerCase() === 'content-type' ? ct : null) },
    json: async () => body,
    text: async () => (typeof body === 'string' ? body : JSON.stringify(body)),
  })) as unknown as typeof fetch;
}

describe('api client', () => {
  it('parses JSON responses', async () => {
    mockOnce({ hello: 'world' });
    const r = await apiGet<{ hello: string }>('/api/foo');
    expect(r).toEqual({ hello: 'world' });
  });

  it('appends query params and skips undefined/null/empty', async () => {
    let url = '';
    globalThis.fetch = vi.fn(async (u: string) => {
      url = u;
      return {
        ok: true,
        status: 200,
        headers: { get: () => 'application/json' },
        json: async () => ({}),
      } as unknown as Response;
    }) as unknown as typeof fetch;
    await api('/api/x', { query: { a: 1, b: '', c: null, d: undefined, e: 'yes' } });
    expect(url).toContain('a=1');
    expect(url).toContain('e=yes');
    expect(url).not.toContain('b=');
    expect(url).not.toContain('c=');
    expect(url).not.toContain('d=');
  });

  it('serializes body and sets Content-Type for POST', async () => {
    let calledInit: RequestInit | undefined;
    globalThis.fetch = vi.fn(async (_u: string, init: RequestInit) => {
      calledInit = init;
      return {
        ok: true,
        status: 200,
        headers: { get: () => 'application/json' },
        json: async () => ({ ok: true }),
      } as unknown as Response;
    }) as unknown as typeof fetch;
    await apiPost('/api/x', { hello: 'world' });
    expect(calledInit?.method).toBe('POST');
    expect(calledInit?.body).toBe(JSON.stringify({ hello: 'world' }));
    expect((calledInit?.headers as Record<string, string>)['Content-Type']).toBe('application/json');
  });

  it('prepends /oauth to /api/* paths when the server injects the oauth2 prefix', async () => {
    (window as { __KC_AUTH_PREFIX__?: string }).__KC_AUTH_PREFIX__ = '/oauth';
    let url = '';
    globalThis.fetch = vi.fn(async (u: string) => {
      url = u;
      return {
        ok: true,
        status: 200,
        headers: { get: () => 'application/json' },
        json: async () => ({}),
      } as unknown as Response;
    }) as unknown as typeof fetch;
    await apiGet('/api/claude/tasks');
    expect(url).toBe('/oauth/api/claude/tasks');
    delete (window as { __KC_AUTH_PREFIX__?: string }).__KC_AUTH_PREFIX__;
  });

  it('withOauthPrefix follows the server-injected prefix (/oauth for oauth2, none for basic auth)', () => {
    // oauth2 deployment: server injects '/oauth' — prefix is added.
    (window as { __KC_AUTH_PREFIX__?: string }).__KC_AUTH_PREFIX__ = '/oauth';
    expect(withOauthPrefix('/api/foo')).toBe('/oauth/api/foo');
    expect(withOauthPrefix('/oauth/api/foo')).toBe('/oauth/api/foo');
    // basic-auth deployment: server injects '' — no prefix.
    (window as { __KC_AUTH_PREFIX__?: string }).__KC_AUTH_PREFIX__ = '';
    expect(withOauthPrefix('/api/foo')).toBe('/api/foo');
    // Non-/api paths and absolute URLs are untouched in both modes.
    expect(withOauthPrefix('/health')).toBe('/health');
    expect(withOauthPrefix('https://example.com/api/x')).toBe('https://example.com/api/x');
    delete (window as { __KC_AUTH_PREFIX__?: string }).__KC_AUTH_PREFIX__;
  });

  it('throws ApiError with status + body on non-2xx', async () => {
    mockOnce({ error: 'nope' }, { status: 400 });
    await expect(apiGet('/api/x')).rejects.toBeInstanceOf(ApiError);
    try {
      await apiGet('/api/x');
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError);
      expect((e as ApiError).status).toBe(400);
      expect((e as ApiError).message).toBe('nope');
    }
  });
});

describe('isErrorResponse', () => {
  it('matches { error } objects', () => {
    expect(isErrorResponse({ error: 'boom' })).toBe(true);
  });

  it('rejects success-shaped objects', () => {
    expect(isErrorResponse({ ok: true })).toBe(false);
  });

  // Regression for issue #44: api() returns 2xx non-JSON bodies as a string,
  // and a bare `'error' in r` on a string throws
  // `TypeError: Cannot use 'in' operator…` which surfaced in a toast.
  it('does not throw on string bodies', () => {
    expect(() => isErrorResponse('<html>proxy page</html>')).not.toThrow();
    expect(isErrorResponse('<html>proxy page</html>')).toBe(false);
  });

  it('rejects null and undefined', () => {
    expect(isErrorResponse(null)).toBe(false);
    expect(isErrorResponse(undefined)).toBe(false);
  });
});
