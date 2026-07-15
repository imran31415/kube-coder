import { describe, expect, it, afterEach, vi } from 'vitest';
import { listThreads, listDeletedThreads, deleteThread, restoreThread } from './hypervisor';

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

/** Capture the URL + method the api layer hits, returning `body` as JSON. */
function capture(body: unknown) {
  const calls: { url: string; method: string }[] = [];
  globalThis.fetch = vi.fn(async (u: string, init?: RequestInit) => {
    calls.push({ url: u, method: init?.method ?? 'GET' });
    return {
      ok: true,
      status: 200,
      statusText: '',
      headers: { get: (k: string) => (k.toLowerCase() === 'content-type' ? 'application/json' : null) },
      json: async () => body,
      text: async () => JSON.stringify(body),
    } as unknown as Response;
  }) as unknown as typeof fetch;
  return calls;
}

describe('hypervisor soft-delete api (#260)', () => {
  it('listThreads hits the default (live) list endpoint', async () => {
    const calls = capture({ threads: [{ id: 'a', title: 'A', assistant: 'claude', status: 'idle', created_at: 1, updated_at: 1 }] });
    const r = await listThreads();
    expect(calls[0].url).toContain('/api/hypervisor/threads');
    expect(calls[0].url).not.toContain('deleted=1');
    expect(calls[0].method).toBe('GET');
    expect(r).toHaveLength(1);
  });

  it('listDeletedThreads requests the trash view with deleted=1', async () => {
    const calls = capture({ threads: [{ id: 'x', title: 'gone', assistant: 'claude', status: 'idle', created_at: 1, updated_at: 1, deleted_at: 9 }] });
    const r = await listDeletedThreads();
    expect(calls[0].url).toContain('/api/hypervisor/threads?deleted=1');
    expect(calls[0].method).toBe('GET');
    expect(r[0].deleted_at).toBe(9);
  });

  it('listDeletedThreads coerces a missing array to []', async () => {
    capture({});
    expect(await listDeletedThreads()).toEqual([]);
  });

  it('deleteThread DELETEs the thread (soft-delete server-side)', async () => {
    const calls = capture({ ok: true });
    await deleteThread('t1');
    expect(calls[0].method).toBe('DELETE');
    expect(calls[0].url).toContain('/api/hypervisor/threads/t1');
  });

  it('restoreThread POSTs the restore endpoint', async () => {
    const calls = capture({ ok: true, restored: true });
    const r = await restoreThread('t1');
    expect(calls[0].method).toBe('POST');
    expect(calls[0].url).toContain('/api/hypervisor/threads/t1/restore');
    expect(r.restored).toBe(true);
  });

  it('restoreThread URL-encodes the id', async () => {
    const calls = capture({ ok: true, restored: true });
    await restoreThread('a b');
    expect(calls[0].url).toContain('/api/hypervisor/threads/a%20b/restore');
  });
});
