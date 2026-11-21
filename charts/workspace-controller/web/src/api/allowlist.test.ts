import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from './client';
import { WRITE_TIMEOUT_MS, accessProblem, allowAccount, getAccessList, lookupAccount, removeAccount } from './allowlist';

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

function recordFetch(response: unknown = {}, status = 200) {
  const calls: { url: string; init: RequestInit }[] = [];
  vi.stubGlobal('fetch', (url: string, init: RequestInit) => {
    calls.push({ url, init });
    return Promise.resolve(new Response(JSON.stringify(response), {
      status, headers: { 'Content-Type': 'application/json' } }));
  });
  return calls;
}

describe('signup allowlist API', () => {
  it('calls the admin routes through the oauth prefix', async () => {
    const calls = recordFetch({ entries: [], limit: 1000, signupUrl: '' });
    await getAccessList();
    await lookupAccount('mona lisa');
    await allowAccount('Mona', '456', 'intern');
    await removeAccount('456');
    expect(calls.map((c) => [c.init.method, c.url])).toEqual([
      ['GET', '/oauth/api/allowlist'],
      ['GET', '/oauth/api/allowlist/lookup?login=mona+lisa'],
      ['POST', '/oauth/api/allowlist'],
      ['DELETE', '/oauth/api/allowlist/456'],
    ]);
    expect(JSON.parse(calls[2].init.body as string)).toEqual({ login: 'Mona', githubId: '456', note: 'intern' });
    expect((calls[2].init.headers as Record<string, string>)['Content-Type']).toBe('application/json');
  });

  it('gives up on a stuck write instead of spinning forever', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', () => new Promise(() => {}));
    const request = removeAccount('456');
    const assertion = expect(request).rejects.toMatchObject({ code: 'request_timeout' });
    await vi.advanceTimersByTimeAsync(WRITE_TIMEOUT_MS);
    await assertion;
  });

  it('turns failures into one short message and what to do next', () => {
    const server = (status: number, body: Record<string, unknown>) =>
      accessProblem(new ApiError(String(body.error ?? ''), status, body));
    expect(server(404, { error: 'No GitHub account has that username.', code: 'github_user_not_found', retryable: false, error_id: 'abc' }))
      .toEqual({ code: 'github_user_not_found', error: 'No GitHub account has that username.', retryable: false, error_id: 'abc', request_id: undefined });
    expect(server(503, { error: 'Workspace progress is temporarily unavailable.', code: 'state_unavailable', retryable: true }))
      .toMatchObject({ error: "Couldn't reach the signup list storage. Try again.", retryable: true });
    expect(server(404, { error: 'not found' })).toMatchObject({ code: 'signup_off', retryable: false });
    expect(server(429, { error: 'Workspace setup is busy. Try again shortly.', code: 'busy', retryable: true }))
      .toMatchObject({ error: 'Someone else is changing the list right now. Try again.', retryable: true });
    expect(accessProblem(new ApiError('Session expired.', 401, null))).toMatchObject({ code: 'unauthorized', retryable: false });
    expect(accessProblem(new ApiError("Can't reach the server.", 0, null, 'network_error')))
      .toMatchObject({ error: "Can't reach the controller. Check your connection.", retryable: true });
    expect(accessProblem(new Error('boom'))).toMatchObject({ code: 'internal_error', retryable: true });
  });
});
