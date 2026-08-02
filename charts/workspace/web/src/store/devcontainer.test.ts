import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  devcontainers,
  devcontainerFor,
  devcontainerError,
  loadDevcontainer,
  applyDevcontainerTo,
  resetDevcontainerAt,
  openApplyDialog,
  closeApplyDialog,
  applyDialogWorkdir,
  _resetDevcontainerForTest,
} from './devcontainer';

const realFetch = globalThis.fetch;

beforeEach(() => {
  _resetDevcontainerForTest();
});

afterEach(() => {
  globalThis.fetch = realFetch;
  vi.restoreAllMocks();
  _resetDevcontainerForTest();
});

function routeFetch(
  routes: Array<[(url: string, method: string) => boolean, () => { status: number; body: unknown }]>,
) {
  const calls: { url: string; method: string; body?: unknown }[] = [];
  globalThis.fetch = vi.fn(async (u: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET';
    calls.push({ url: u, method, body: init?.body ? JSON.parse(init.body as string) : undefined });
    for (const [test, respond] of routes) {
      if (test(u, method)) {
        const { status, body } = respond();
        return {
          ok: status >= 200 && status < 300,
          status,
          statusText: '',
          headers: {
            get: (k: string) => (k.toLowerCase() === 'content-type' ? 'application/json' : null),
          },
          json: async () => body,
          text: async () => JSON.stringify(body),
        } as unknown as Response;
      }
    }
    throw new Error(`unrouted ${method} ${u}`);
  }) as unknown as typeof fetch;
  return calls;
}

const record = (workdir: string, over: Record<string, unknown> = {}) => ({
  workdir, found: true, rel_path: '.devcontainer/devcontainer.json',
  error: '', config_hash: 'hash-1', name: 'API',
  lifecycle: { postCreate: [{ display: 'npm ci', kind: 'shell', needs_root: false }] },
  ports: [{ port: 3000, label: 'API' }],
  ...over,
});

describe('loadDevcontainer', () => {
  it('populates the store keyed by workdir', async () => {
    routeFetch([[(u) => u.includes('/api/devcontainer?'), () => ({ status: 200, body: record('/home/dev/api') })]]);
    await loadDevcontainer('/home/dev/api');
    expect(devcontainerFor('/home/dev/api')?.name).toBe('API');
    expect(devcontainerFor('/home/dev/other')).toBeNull();
  });

  it('keeps the previous record when a refresh fails', async () => {
    routeFetch([[() => true, () => ({ status: 200, body: record('/home/dev/api') })]]);
    await loadDevcontainer('/home/dev/api');
    routeFetch([[() => true, () => ({ status: 500, body: { error: 'boom' } })]]);
    await loadDevcontainer('/home/dev/api');
    expect(devcontainerFor('/home/dev/api')?.name).toBe('API');
    expect(devcontainerError.value).toBeTruthy();
  });

  it('dedupes concurrent loads of the same workdir', async () => {
    const calls = routeFetch([[() => true, () => ({ status: 200, body: record('/home/dev/api') })]]);
    await Promise.all([
      loadDevcontainer('/home/dev/api'),
      loadDevcontainer('/home/dev/api'),
      loadDevcontainer('/home/dev/api'),
    ]);
    expect(calls.filter((c) => c.method === 'GET')).toHaveLength(1);
  });

  it('ignores an empty workdir', async () => {
    const calls = routeFetch([[() => true, () => ({ status: 200, body: {} })]]);
    await loadDevcontainer('');
    expect(calls).toHaveLength(0);
  });
});

describe('applyDevcontainerTo', () => {
  it('sends the loaded config hash with the selected hooks', async () => {
    const calls = routeFetch([
      [(u, m) => u.includes('/apply') && m === 'POST',
        () => ({ status: 202, body: { ports_pinned: [3000], port_conflicts: [], settings_written: [], settings_skipped: [], extensions_installed: [], extension_failures: [], env: [], hooks_started: ['postCreate'], unsupported: [] } })],
      [(u) => u.includes('/api/devcontainer?'), () => ({ status: 200, body: record('/home/dev/api') })],
    ]);
    const out = await applyDevcontainerTo('/home/dev/api', ['postCreate'], 'hash-1');
    expect(out.ok).toBe(true);
    const post = calls.find((c) => c.method === 'POST');
    expect((post?.body as { config_hash: string }).config_hash).toBe('hash-1');
  });

  it('omits the hash when nothing will be executed', async () => {
    const calls = routeFetch([
      [(u, m) => u.includes('/apply') && m === 'POST',
        () => ({ status: 202, body: { ports_pinned: [], port_conflicts: [], settings_written: [], settings_skipped: [], extensions_installed: [], extension_failures: [], env: [], hooks_started: [], unsupported: [] } })],
      [(u) => u.includes('/api/devcontainer?'), () => ({ status: 200, body: record('/home/dev/api') })],
    ]);
    await applyDevcontainerTo('/home/dev/api', [], 'hash-1');
    const post = calls.find((c) => c.method === 'POST');
    expect((post?.body as { config_hash?: string }).config_hash).toBeUndefined();
  });

  it('reloads the record on a stale-hash conflict so the dialog re-prompts', async () => {
    let applied = 0;
    routeFetch([
      [(u, m) => u.includes('/apply') && m === 'POST', () => {
        applied += 1;
        return { status: 409, body: { code: 'hash_mismatch', error: 'changed' } };
      }],
      [(u) => u.includes('/api/devcontainer?'),
        () => ({ status: 200, body: record('/home/dev/api', { config_hash: 'hash-2' }) })],
    ]);
    const out = await applyDevcontainerTo('/home/dev/api', ['postCreate'], 'hash-1');
    expect(out).toEqual({ ok: false, conflict: 'hash_mismatch' });
    expect(applied).toBe(1);
    expect(devcontainerFor('/home/dev/api')?.config_hash).toBe('hash-2');
  });

  it('reports a busy conflict without losing state', async () => {
    routeFetch([
      [(u, m) => u.includes('/apply') && m === 'POST',
        () => ({ status: 409, body: { code: 'busy', error: 'running' } })],
      [(u) => u.includes('/api/devcontainer?'), () => ({ status: 200, body: record('/home/dev/api') })],
    ]);
    const out = await applyDevcontainerTo('/home/dev/api', ['postCreate'], 'hash-1');
    expect(out).toEqual({ ok: false, conflict: 'busy' });
  });

  it('returns an error outcome for anything else', async () => {
    routeFetch([
      [(u, m) => u.includes('/apply') && m === 'POST',
        () => ({ status: 422, body: { error: 'invalid JSON' } })],
    ]);
    const out = await applyDevcontainerTo('/home/dev/api', [], 'hash-1');
    expect(out.ok).toBe(false);
    expect((out as { error: string }).error).toBeTruthy();
  });
});

describe('resetDevcontainerAt', () => {
  it('posts and reloads', async () => {
    const calls = routeFetch([
      [(u, m) => u.includes('/reset') && m === 'POST',
        () => ({ status: 200, body: { cleared: true, unpinned: [3000] } })],
      [(u) => u.includes('/api/devcontainer?'), () => ({ status: 200, body: record('/home/dev/api') })],
    ]);
    expect(await resetDevcontainerAt('/home/dev/api', true)).toBe(true);
    expect(calls.some((c) => c.method === 'GET')).toBe(true);
  });
});

describe('apply dialog state', () => {
  it('opens and closes for one workdir at a time', () => {
    expect(applyDialogWorkdir.value).toBeNull();
    openApplyDialog('/home/dev/api');
    expect(applyDialogWorkdir.value).toBe('/home/dev/api');
    openApplyDialog('/home/dev/web');
    expect(applyDialogWorkdir.value).toBe('/home/dev/web');
    closeApplyDialog();
    expect(applyDialogWorkdir.value).toBeNull();
  });
});

describe('_resetDevcontainerForTest', () => {
  it('clears everything', async () => {
    routeFetch([[() => true, () => ({ status: 200, body: record('/home/dev/api') })]]);
    await loadDevcontainer('/home/dev/api');
    openApplyDialog('/home/dev/api');
    _resetDevcontainerForTest();
    expect(devcontainers.value).toEqual({});
    expect(applyDialogWorkdir.value).toBeNull();
  });
});
