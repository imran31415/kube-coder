import { describe, expect, it, afterEach, vi } from 'vitest';
import {
  getDevcontainer,
  scanDevcontainers,
  applyDevcontainer,
  resetDevcontainer,
  DevcontainerConflictError,
} from './devcontainer';

/**
 * API-layer tests for #594. The two that carry weight:
 *   * the config_hash goes into the apply body — it is the compare-and-swap
 *     that makes "the user approved this exact text" true;
 *   * a 409 surfaces as a TYPED conflict so the dialog can reload and
 *     re-prompt instead of showing a raw error string.
 */

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

function respond(status: number, body: unknown) {
  const calls: { url: string; method: string; body?: string }[] = [];
  globalThis.fetch = vi.fn(async (u: string, init?: RequestInit) => {
    calls.push({ url: u, method: init?.method ?? 'GET', body: init?.body as string | undefined });
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
  }) as unknown as typeof fetch;
  return calls;
}

describe('getDevcontainer', () => {
  it('URL-encodes the workdir', async () => {
    const calls = respond(200, { found: false });
    await getDevcontainer('/home/dev/my project');
    expect(calls[0].url).toContain('workdir=%2Fhome%2Fdev%2Fmy+project');
  });

  it('coerces missing arrays so the renderer cannot crash', async () => {
    respond(200, { found: true, workdir: '/home/dev/api' });
    const rec = await getDevcontainer('/home/dev/api');
    expect(rec.ports).toEqual([]);
    expect(rec.unsupported).toEqual([]);
    expect(rec.lifecycle.postCreate).toEqual([]);
    expect(rec.applied.ports_pinned).toEqual([]);
    expect(rec.applied.auto_apply).toBe(false);
  });

  it('passes a found:false 200 through as data, not an error', async () => {
    respond(200, { found: false, workdir: '/home/dev/plain' });
    const rec = await getDevcontainer('/home/dev/plain');
    expect(rec.found).toBe(false);
  });

  it('keeps every lifecycle hook present', async () => {
    respond(200, {
      found: true,
      lifecycle: { postCreate: [{ display: 'npm ci', kind: 'shell' }] },
    });
    const rec = await getDevcontainer('/home/dev/api');
    expect(rec.lifecycle.postCreate).toHaveLength(1);
    expect(rec.lifecycle.onCreate).toEqual([]);
    expect(rec.lifecycle.postStart).toEqual([]);
  });
});

describe('scanDevcontainers', () => {
  it('unwraps the envelope', async () => {
    respond(200, { devcontainers: [{ workdir: '/home/dev/api' }], count: 1 });
    expect(await scanDevcontainers()).toHaveLength(1);
  });

  it('defaults to an empty list when the field is missing', async () => {
    respond(200, {});
    expect(await scanDevcontainers()).toEqual([]);
  });
});

describe('applyDevcontainer', () => {
  it('sends the config hash and the selected hooks', async () => {
    const calls = respond(202, { workdir: '/home/dev/api', hooks_started: ['postCreate'] });
    await applyDevcontainer({
      workdir: '/home/dev/api', hooks: ['postCreate'], config_hash: 'abc123',
    });
    const body = JSON.parse(calls[0].body as string);
    expect(calls[0].method).toBe('POST');
    expect(body.config_hash).toBe('abc123');
    expect(body.hooks).toEqual(['postCreate']);
  });

  it('surfaces a stale-hash 409 as a typed conflict', async () => {
    respond(409, { code: 'hash_mismatch', error: 'devcontainer.json changed' });
    await expect(applyDevcontainer({
      workdir: '/home/dev/api', hooks: ['postCreate'], config_hash: 'old',
    })).rejects.toBeInstanceOf(DevcontainerConflictError);
  });

  it('surfaces a busy 409 with its code', async () => {
    respond(409, { code: 'busy', error: 'already running' });
    await applyDevcontainer({ workdir: '/home/dev/api', hooks: ['postCreate'], config_hash: 'x' })
      .then(() => { throw new Error('should have rejected'); })
      .catch((err) => {
        expect(err).toBeInstanceOf(DevcontainerConflictError);
        expect(err.code).toBe('busy');
      });
  });

  it('leaves other errors as ApiError', async () => {
    respond(422, { error: 'invalid JSON' });
    await expect(applyDevcontainer({ workdir: '/home/dev/api' }))
      .rejects.not.toBeInstanceOf(DevcontainerConflictError);
  });
});

describe('resetDevcontainer', () => {
  it('posts the workdir and the unpin flag', async () => {
    const calls = respond(200, { cleared: true, unpinned: [3000] });
    await resetDevcontainer('/home/dev/api', true);
    const body = JSON.parse(calls[0].body as string);
    expect(body).toEqual({ workdir: '/home/dev/api', unpin_ports: true });
  });
});
