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

  it('keeps a network failure distinct from an expired session', async () => {
    vi.stubGlobal('fetch', () => Promise.reject(new TypeError('Failed to fetch')));
    await expect(api('/api/capacity')).rejects.toMatchObject({ code: 'network_error' });
    expect(failureProblem({ status: 401, body: { error: 'unauthorized' } }).code).toBe('unauthorized');
  });

  it('preserves correlation without exposing an upstream error body', () => {
    const problem = failureProblem({ body: { code: 'internal_error', error: 'private', error_id: 'abc' } });
    expect(problem.error_id).toBe('abc');
    expect(problem.error).not.toContain('private');
  });
});
