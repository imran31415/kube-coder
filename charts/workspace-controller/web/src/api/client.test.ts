import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './client';
import { failureProblem } from './problem';

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

describe('bounded metrics reads', () => {
  it('times out even when a transport ignores abort', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', () => new Promise(() => {}));
    const request = api('/api/capacity', { timeoutMs: 100 });
    const assertion = expect(request).rejects.toMatchObject({ code: 'request_timeout' });
    await vi.advanceTimersByTimeAsync(100);
    await assertion;
  });

  it('cancels when the view unmounts', async () => {
    vi.stubGlobal('fetch', () => new Promise(() => {}));
    const controller = new AbortController();
    const request = api('/api/spend', { signal: controller.signal });
    const assertion = expect(request).rejects.toMatchObject({ name: 'AbortError' });
    controller.abort();
    await assertion;
  });

  it('sends an expired oauth2 session to sign in', async () => {
    // oauth2-proxy answers an expired session with a cross-origin 302, which
    // fetch reports as this TypeError rather than a 401.
    const original = window.location;
    const location = { pathname: '/w/alice', search: '?range=1h', href: '' };
    Object.defineProperty(window, 'location', { value: location, configurable: true, writable: true });
    try {
      vi.resetModules();
      const fresh = await import('./client');
      vi.stubGlobal('fetch', () => Promise.reject(new TypeError('Failed to fetch')));
      await expect(fresh.api('/api/capacity')).rejects.toMatchObject({ status: 401 });
      expect(location.href).toBe(`/oauth2/start?rd=${encodeURIComponent('/w/alice?range=1h')}`);
    } finally {
      Object.defineProperty(window, 'location', { value: original, configurable: true, writable: true });
    }
  });

  it('keeps other failures distinct from an expired session', async () => {
    vi.stubGlobal('fetch', () => Promise.reject(new Error('socket closed')));
    await expect(api('/api/capacity')).rejects.toMatchObject({ code: 'network_error' });
    expect(failureProblem({ status: 401, body: { error: 'unauthorized' } }).code).toBe('unauthorized');
  });

  it('keeps the real status when an error body is empty or not JSON', async () => {
    for (const body of ['', '<html>Bad gateway</html>', '{"truncated']) {
      vi.stubGlobal('fetch', async () => new Response(body, { status: 502, headers: { 'Content-Type': 'application/json' } }));
      await expect(api('/api/capacity')).rejects.toMatchObject({ status: 502, message: 'Request failed (502)' });
    }
  });

  it('preserves correlation without exposing an upstream error body', () => {
    const problem = failureProblem({ body: { code: 'internal_error', error: 'private', error_id: 'abc' } });
    expect(problem.error_id).toBe('abc');
    expect(problem.error).not.toContain('private');
  });
});
