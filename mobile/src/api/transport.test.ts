/**
 * The desktop transport (#833): a workspace request becomes a core command
 * with a path on the connected host, and the webview's Authorization header
 * (which carries only a mask on desktop) never reaches the core.
 */
import { describe, expect, it, vi } from 'vitest';
import { desktopFetch, pathOnHost } from './transport';

const HOST = 'https://me.example.com';

describe('pathOnHost', () => {
  it('strips the connected host', () => {
    expect(pathOnHost(HOST, `${HOST}/api/feed?limit=5`)).toBe('/api/feed?limit=5');
    expect(pathOnHost(`${HOST}/`, `${HOST}/api/x`)).toBe('/api/x');
  });

  it('refuses any other host, including look-alikes', () => {
    expect(pathOnHost(HOST, 'https://evil.com/api/feed')).toBeNull();
    expect(pathOnHost(HOST, 'https://me.example.com.evil.com/api/feed')).toBeNull();
    expect(pathOnHost(HOST, 'https://me.example.com@evil.com/')).toBeNull();
    expect(pathOnHost('', `${HOST}/api/feed`)).toBeNull();
  });
});

describe('desktopFetch', () => {
  function setup(resp = { status: 200, statusText: 'OK', contentType: 'application/json', body: '{"a":1}' }) {
    const invoke = vi.fn(async (_cmd: string, _args?: Record<string, unknown>) => resp as never);
    return { invoke, fetch: desktopFetch(invoke, () => HOST) };
  }

  it('sends the path and the benign headers, never Authorization', async () => {
    const { invoke, fetch } = setup();
    const res = await fetch(`${HOST}/api/feed/f1/read`, {
      method: 'POST',
      headers: { Accept: 'application/json', Authorization: 'Bearer ••••abcd', 'Content-Type': 'application/json' },
      body: '{}',
    });
    expect(invoke).toHaveBeenCalledWith('kc_request', {
      req: {
        method: 'POST',
        path: '/api/feed/f1/read',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: '{}',
      },
    });
    expect(res.ok).toBe(true);
    expect(res.headers.get('Content-Type')).toBe('application/json');
    expect(await res.json()).toEqual({ a: 1 });
  });

  it('base64-encodes a binary body', async () => {
    const { invoke, fetch } = setup();
    await fetch(`${HOST}/api/files/upload`, { method: 'POST', body: new Blob([new Uint8Array([0, 1, 2, 255])]) });
    const req = (invoke.mock.calls[0][1] as { req: Record<string, unknown> }).req;
    expect(req.bodyBase64).toBe('AAEC/w==');
    expect(req.body).toBeUndefined();
  });

  it('refuses a URL off the connected host before calling the core', async () => {
    const { invoke, fetch } = setup();
    await expect(fetch('https://evil.com/api/feed')).rejects.toThrow(/other than the connected workspace/);
    expect(invoke).not.toHaveBeenCalled();
  });

  it('hands a redirect back as a status so the client refuses it', async () => {
    const { fetch } = setup({ status: 302, statusText: 'Found', contentType: '', body: '' });
    const res = await fetch(`${HOST}/api/x`);
    expect(res.ok).toBe(false);
    expect(res.status).toBe(302);
  });

  it('honours an abort like fetch does', async () => {
    const invoke = vi.fn(() => new Promise<never>(() => {}));
    const fetch = desktopFetch(invoke, () => HOST);
    const ac = new AbortController();
    const p = fetch(`${HOST}/api/x`, { signal: ac.signal });
    ac.abort();
    await expect(p).rejects.toMatchObject({ name: 'AbortError' });
  });
});
