import { describe, expect, it, afterEach, vi } from 'vitest';
import {
  getMemoryRelations,
  unlinkRelation,
  exportMemory,
  importMemory,
  type MemoryExport,
} from './memory';

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

/** Capture the URL + method the api layer hits, returning `body` as JSON. */
function capture(body: unknown) {
  const calls: { url: string; method: string; body?: string }[] = [];
  globalThis.fetch = vi.fn(async (u: string, init?: RequestInit) => {
    calls.push({ url: u, method: init?.method ?? 'GET', body: init?.body as string | undefined });
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

describe('memory lifecycle api (#134)', () => {
  it('getMemoryRelations hits the relations endpoint and unwraps the array', async () => {
    const calls = capture({
      relations: [
        { id: 1, kind: 'related-to', weight: 1, created_at: 0, direction: 'out', other_namespace: 'b', other_key: 'y' },
      ],
      count: 1,
    });
    const rels = await getMemoryRelations('a', 'x');
    expect(calls[0].url).toContain('/api/memory/a/x/relations');
    expect(calls[0].method).toBe('GET');
    expect(rels).toHaveLength(1);
    expect(rels[0].id).toBe(1);
  });

  it('getMemoryRelations tolerates a missing array (coerces to [])', async () => {
    capture({ count: 0 });
    expect(await getMemoryRelations('a', 'x')).toEqual([]);
  });

  it('unlinkRelation issues a DELETE to the relation id', async () => {
    const calls = capture({ deleted: 1 });
    await unlinkRelation('a', 'x', 42);
    expect(calls[0].method).toBe('DELETE');
    expect(calls[0].url).toContain('/api/memory/a/x/relations/42');
  });

  it('exportMemory GETs the export endpoint', async () => {
    const calls = capture({ version: 1, exported_at: 0, memories: [], relations: [] });
    const exp = await exportMemory();
    expect(calls[0].url).toContain('/api/memory/export');
    expect(exp.version).toBe(1);
  });

  it('importMemory POSTs payload + mode and unwraps result', async () => {
    const calls = capture({
      status: 'ok',
      result: { imported: 2, skipped: 0, failed: 0, relations_imported: 1, relations_failed: 0, errors: [] },
    });
    const payload: MemoryExport = { version: 1, exported_at: 0, memories: [{}, {}], relations: [{}] };
    const res = await importMemory(payload, 'skip');
    expect(calls[0].method).toBe('POST');
    expect(calls[0].url).toContain('/api/memory/_import');
    expect(JSON.parse(calls[0].body as string).mode).toBe('skip');
    expect(res.imported).toBe(2);
    expect(res.relations_imported).toBe(1);
  });

  it('importMemory defaults to merge mode', async () => {
    const calls = capture({ status: 'ok', result: { imported: 0, skipped: 0, failed: 0, relations_imported: 0, relations_failed: 0, errors: [] } });
    await importMemory({ version: 1, exported_at: 0, memories: [], relations: [] });
    expect(JSON.parse(calls[0].body as string).mode).toBe('merge');
  });
});
