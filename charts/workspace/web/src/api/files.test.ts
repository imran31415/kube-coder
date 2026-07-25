import { describe, expect, it, afterEach, vi } from 'vitest';
import { previewFile, deleteFile, renameFile, fileRawUrl, downloadFile, uploadFile, uploadZip, uploadBatch } from './files';

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
  vi.restoreAllMocks();
});

function captureFetch(body: unknown, init: { status?: number; contentType?: string } = {}) {
  const status = init.status ?? 200;
  const ct = init.contentType ?? 'application/json';
  const calls: Array<{ url: string; init: RequestInit }> = [];
  globalThis.fetch = vi.fn(async (url: string, reqInit: RequestInit) => {
    calls.push({ url, init: reqInit });
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: '',
      headers: { get: (k: string) => (k.toLowerCase() === 'content-type' ? ct : null) },
      json: async () => body,
      text: async () => (typeof body === 'string' ? body : JSON.stringify(body)),
      blob: async () => body as Blob,
    } as unknown as Response;
  }) as unknown as typeof fetch;
  return calls;
}

describe('files api', () => {
  it('fetches a preview descriptor', async () => {
    const calls = captureFetch({ kind: 'text', path: 'a.txt', mime: 'text/plain', size: 3, content: 'abc', truncated: false });
    const r = await previewFile('a.txt');
    expect(r.kind).toBe('text');
    expect(calls[0].url).toContain('/api/files/preview');
    expect(calls[0].url).toContain('path=a.txt');
  });

  it('DELETEs with an encoded path', async () => {
    const calls = captureFetch({ ok: true });
    await deleteFile('dir/some file.txt');
    expect(calls[0].init.method).toBe('DELETE');
    expect(calls[0].url).toContain('/api/files?path=');
    expect(calls[0].url).toContain('some%20file.txt');
  });

  it('POSTs a rename and returns the new path', async () => {
    const calls = captureFetch({ ok: true, path: 'b.txt' });
    const out = await renameFile('a.txt', 'b.txt');
    expect(out).toBe('b.txt');
    expect(calls[0].init.method).toBe('POST');
    expect(calls[0].url).toContain('/api/files/rename');
    expect(JSON.parse(String(calls[0].init.body))).toEqual({ from: 'a.txt', to: 'b.txt' });
  });

  it('builds a raw media URL with the oauth prefix + encoded path', () => {
    const url = fileRawUrl('pics/my shot.png');
    expect(url).toContain('/api/files/raw?path=');
    expect(url).toContain('my%20shot.png');
  });

  it('downloads via an authenticated fetch + object-url anchor', async () => {
    const calls = captureFetch('BYTES', { contentType: 'application/octet-stream' });
    // jsdom lacks createObjectURL / anchor.click side effects — stub them.
    const createObjectURL = vi.fn(() => 'blob:mock');
    const revokeObjectURL = vi.fn();
    (globalThis.URL as unknown as { createObjectURL: unknown }).createObjectURL = createObjectURL;
    (globalThis.URL as unknown as { revokeObjectURL: unknown }).revokeObjectURL = revokeObjectURL;
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});

    await downloadFile('logs/app.log', 'app.log');

    expect(calls[0].url).toContain('/api/files/download?path=');
    expect(calls[0].url).toContain('logs%2Fapp.log');
    expect(calls[0].init.method).toBe('GET');
    expect(createObjectURL).toHaveBeenCalled();
    expect(clickSpy).toHaveBeenCalled();
  });

  it('uploads with encoded dest/filename headers and no X-Extract', async () => {
    const calls = captureFetch({ ok: true, path: 'up/a b.txt', absolute_path: '/home/dev/up/a b.txt', size: 3 });
    const r = await uploadFile(new File(['abc'], 'a b.txt', { type: 'text/plain' }), 'up');
    expect(r.absolute_path).toBe('/home/dev/up/a b.txt');
    const h = calls[0].init.headers as Record<string, string>;
    expect(calls[0].url).toContain('/api/files/upload');
    expect(h['X-Dest-Path']).toBe('up');
    expect(h['X-Filename']).toBe('a%20b.txt');
    expect(h['X-Extract']).toBeUndefined();
  });

  it('uploadZip sends X-Extract: zip and returns the extract summary', async () => {
    const calls = captureFetch({ ok: true, path: 'up', absolute_path: '/home/dev/up', extracted: 4 });
    const r = await uploadZip(new File(['PK'], 'bundle.zip', { type: 'application/zip' }), 'up');
    expect(r.extracted).toBe(4);
    const h = calls[0].init.headers as Record<string, string>;
    expect(h['X-Extract']).toBe('zip');
    expect(h['X-Filename']).toBe('bundle.zip');
  });

  it('uploadBatch uploads every item, reports combined progress, and collects failures', async () => {
    // Fail exactly one request (the 2nd) to prove the batch keeps going.
    let n = 0;
    globalThis.fetch = vi.fn(async () => {
      const fail = ++n === 2;
      return {
        ok: !fail,
        status: fail ? 500 : 200,
        statusText: fail ? 'boom' : '',
        headers: { get: () => 'application/json' },
        json: async () => (fail ? { error: 'disk full' } : { ok: true, path: 'p', absolute_path: '/home/dev/p', size: 1 }),
        text: async () => (fail ? JSON.stringify({ error: 'disk full' }) : '{}'),
      } as unknown as Response;
    }) as unknown as typeof fetch;

    const items = ['a.txt', 'b.txt', 'c.txt'].map((name) => ({
      file: new File(['x'], name, { type: 'text/plain' }),
      destPath: 'up',
    }));
    const progress: Array<[number, number]> = [];
    const r = await uploadBatch(items, (done, total) => progress.push([done, total]), 2);
    expect(r.done).toBe(2);
    expect(r.failed).toHaveLength(1);
    expect(r.failed[0].name).toBe('b.txt');
    expect(progress).toHaveLength(3);
    expect(progress[2]).toEqual([3, 3]);
  });
});
