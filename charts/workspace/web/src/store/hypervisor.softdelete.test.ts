import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  threads,
  deletedThreads,
  activeThreadId,
  removeThread,
  reviveThread,
  refreshDeletedThreads,
} from './hypervisor';
import type { HypervisorThread } from '../api/hypervisor';

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
  vi.restoreAllMocks();
});

function mk(id: string, deleted = false): HypervisorThread {
  return {
    id, title: id, assistant: 'claude', status: 'idle',
    created_at: 1, updated_at: 1, deleted_at: deleted ? 9 : null,
  };
}

/**
 * Route fetch by URL + method. `live` and `trash` are the two list payloads;
 * DELETE/POST-restore resolve `{ ok: true }`. Records every call.
 */
function routeFetch(live: HypervisorThread[], trash: HypervisorThread[]) {
  const calls: { url: string; method: string }[] = [];
  globalThis.fetch = vi.fn(async (u: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET';
    calls.push({ url: u, method });
    let body: unknown = { ok: true };
    if (method === 'GET' && u.includes('/api/hypervisor/threads')) {
      body = { threads: u.includes('deleted=1') ? trash : live };
    }
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

describe('store/hypervisor — soft-delete (#260)', () => {
  beforeEach(() => {
    threads.value = [];
    deletedThreads.value = [];
    activeThreadId.value = null;
  });

  it('refreshDeletedThreads loads the trash view', async () => {
    routeFetch([], [mk('gone', true)]);
    await refreshDeletedThreads();
    expect(deletedThreads.value.map((t) => t.id)).toEqual(['gone']);
  });

  it('removeThread deletes then refreshes the live list', async () => {
    const calls = routeFetch([mk('b')], []);
    await removeThread('a');
    // A DELETE was issued for the thread, then the live list refetched.
    const del = calls.find((c) => c.method === 'DELETE');
    expect(del?.url).toContain('/api/hypervisor/threads/a');
    expect(threads.value.map((t) => t.id)).toEqual(['b']);
  });

  it('removeThread skips the trash refetch when the trash was never opened', async () => {
    const calls = routeFetch([], []);
    await removeThread('a');
    // deletedThreads started empty → no deleted=1 GET.
    expect(calls.some((c) => c.url.includes('deleted=1'))).toBe(false);
  });

  it('removeThread refreshes the trash too when it is already populated', async () => {
    deletedThreads.value = [mk('old', true)];
    const calls = routeFetch([], [mk('old', true), mk('a', true)]);
    await removeThread('a');
    expect(calls.some((c) => c.url.includes('deleted=1'))).toBe(true);
    expect(deletedThreads.value.map((t) => t.id)).toEqual(['old', 'a']);
  });

  it('removeThread closes the active thread when it is the one deleted', async () => {
    activeThreadId.value = 'a';
    routeFetch([], []);
    await removeThread('a');
    expect(activeThreadId.value).toBeNull();
  });

  it('reviveThread restores then refreshes both lists', async () => {
    deletedThreads.value = [mk('a', true)];
    const calls = routeFetch([mk('a')], []);
    await reviveThread('a');
    const post = calls.find((c) => c.method === 'POST');
    expect(post?.url).toContain('/api/hypervisor/threads/a/restore');
    // live list now has the revived thread; trash is empty.
    expect(threads.value.map((t) => t.id)).toEqual(['a']);
    expect(deletedThreads.value).toEqual([]);
  });
});
