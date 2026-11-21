import { afterEach, describe, expect, it, vi } from 'vitest';
import { signupRequest } from './signup';

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });
describe('signup transport', () => {
  it('sends a scoped empty mutation with CSRF and rejects redirects', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response('{}', { headers: { 'Content-Type': 'application/json' } }));
    vi.stubGlobal('fetch', fetcher);
    await signupRequest('workspace', 'csrf');
    expect(fetcher).toHaveBeenCalledWith('/api/signup/workspace', expect.objectContaining({ method: 'POST', body: '{}', redirect: 'error', headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf' }) }));
  });
  it.each([
    new Response('login HTML', { status: 401 }),
    new Response('<html>gateway error</html>', { status: 502 }),
    new Response('{}', { status: 503, headers: { 'Content-Type': 'application/json' } }),
    new Response('{broken', { status: 502, headers: { 'Content-Type': 'application/json' } }),
  ])('always exposes a meaningful safe error', async response => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response));
    await expect(signupRequest('me')).rejects.toMatchObject({ problem: { error: expect.any(String), code: expect.any(String) } });
  });
  it('preserves structured backend failure and retry information', async () => {
    const problem = { code: 'busy', error: 'Try again shortly.', retryable: true, error_id: 'reference' };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(problem), { status: 429, headers: { 'Content-Type': 'application/json' } })));
    await expect(signupRequest('retry', 'csrf')).rejects.toMatchObject({ status: 429, problem });
  });
  it('honors signals cancelled before a request starts', async () => {
    const abort = new AbortController(); abort.abort();
    const fetcher = vi.fn((_path, init) => {
      expect(init.signal.aborted).toBe(true);
      return Promise.reject(new DOMException('Aborted', 'AbortError'));
    });
    vi.stubGlobal('fetch', fetcher);
    await expect(signupRequest('me', undefined, abort.signal)).rejects.toMatchObject({ status: 0 });
  });
});
