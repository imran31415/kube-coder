import { describe, expect, it, beforeEach } from 'vitest';
import {
  QUEUE_KEY,
  MAX_ATTEMPTS,
  drain,
  enqueue,
  pendingItemIds,
  readQueue,
  remove,
  newApprovalId,
  type QueueStorage,
  type QueuedApproval,
} from './approvalQueue';

/**
 * The offline approval queue (#588 Phase 6).
 *
 * The test that carries the design is
 * `test: a retry sends the SAME approval id` — the server consumes an
 * approval_id once and replays its stored result, so a queue that minted a
 * fresh id per attempt would post a second comment on a customer's ticket
 * every time the train went through a tunnel.
 */

function mkStorage(initial?: string): QueueStorage & { data: Map<string, string> } {
  const data = new Map<string, string>();
  if (initial !== undefined) data.set(QUEUE_KEY, initial);
  return {
    data,
    async getItem(key) {
      return data.get(key) ?? null;
    },
    async setItem(key, value) {
      data.set(key, value);
    },
  };
}

function decision(over: Partial<QueuedApproval> = {}) {
  return {
    board_id: 'acme-jira',
    item_id: '46',
    decision: 'approve' as const,
    content_hash: 'hash-1',
    reason: '',
    ...over,
  };
}

describe('approvalQueue', () => {
  let storage: ReturnType<typeof mkStorage>;

  beforeEach(() => {
    storage = mkStorage();
  });

  it('mints an approval id at enqueue and persists it', async () => {
    const entry = await enqueue(storage, decision());
    expect(entry.approval_id.length).toBeGreaterThanOrEqual(8);
    const stored = await readQueue(storage);
    expect(stored).toHaveLength(1);
    expect(stored[0].approval_id).toBe(entry.approval_id);
    expect(stored[0].attempts).toBe(0);
  });

  it('a retry sends the SAME approval id', async () => {
    const entry = await enqueue(storage, decision());
    const seen: string[] = [];
    const failing = async (e: QueuedApproval) => {
      seen.push(e.approval_id);
      return { status: 503 };
    };
    await drain(storage, failing);
    await drain(storage, failing);
    await drain(storage, failing);
    expect(seen).toEqual([entry.approval_id, entry.approval_id, entry.approval_id]);
  });

  it('a 2xx removes the entry', async () => {
    await enqueue(storage, decision());
    const res = await drain(storage, async () => ({ status: 200 }));
    expect(res.sent).toBe(1);
    expect(await readQueue(storage)).toEqual([]);
  });

  it('a network error keeps the entry for the next drain', async () => {
    await enqueue(storage, decision());
    const res = await drain(storage, async () => {
      throw new Error('Network error: offline');
    });
    expect(res.failed).toBe(1);
    expect(res.remaining).toBe(1);
    const stored = await readQueue(storage);
    expect(stored[0].attempts).toBe(1);
    expect(stored[0].last_error).toContain('offline');
  });

  it('a 5xx keeps the entry; a 4xx does not', async () => {
    await enqueue(storage, decision({ item_id: 'a' }));
    await enqueue(storage, decision({ item_id: 'b' }));
    await drain(storage, async (e) => ({ status: e.item_id === 'a' ? 500 : 400 }));
    const stored = await readQueue(storage);
    expect(stored.map((e) => e.item_id)).toEqual(['a']);
  });

  it('a 409 is TERMINAL — a stale approval is never retried', async () => {
    /* The staleness guard exists so a human looks again when the ticket
       changed. Retrying would either fail forever or, worse, land once the
       queue happened to catch a matching hash. */
    await enqueue(storage, decision());
    const res = await drain(storage, async () => ({ status: 409 }));
    expect(res.dropped).toBe(1);
    expect(await readQueue(storage)).toEqual([]);
  });

  it('gives up after MAX_ATTEMPTS rather than retrying forever', async () => {
    await enqueue(storage, decision());
    for (let i = 0; i < MAX_ATTEMPTS; i += 1) {
      await drain(storage, async () => ({ status: 503 }));
    }
    expect(await readQueue(storage)).toEqual([]);
  });

  it('a second decision on the same item REPLACES the first', async () => {
    /* Tapping Approve then Reject before either has been sent means the
       second one. Sending both would be a write followed by a contradiction. */
    await enqueue(storage, decision({ decision: 'approve' }));
    await enqueue(storage, decision({ decision: 'reject', reason: 'tone' }));
    const stored = await readQueue(storage);
    expect(stored).toHaveLength(1);
    expect(stored[0].decision).toBe('reject');
  });

  it('decisions on DIFFERENT items coexist', async () => {
    await enqueue(storage, decision({ item_id: '46' }));
    await enqueue(storage, decision({ item_id: '47' }));
    expect(await readQueue(storage)).toHaveLength(2);
  });

  it('drains oldest first', async () => {
    await enqueue(storage, decision({ item_id: '1' }));
    await enqueue(storage, decision({ item_id: '2' }));
    const order: string[] = [];
    await drain(storage, async (e) => {
      order.push(e.item_id);
      return { status: 200 };
    });
    expect(order).toEqual(['1', '2']);
  });

  it('carries the content hash only the approve path needs', async () => {
    await enqueue(storage, decision({ content_hash: 'hash-from-the-card' }));
    const sent: QueuedApproval[] = [];
    await drain(storage, async (e) => {
      sent.push(e);
      return { status: 200 };
    });
    expect(sent[0].content_hash).toBe('hash-from-the-card');
  });

  it('an empty queue drains without touching the sender', async () => {
    let called = 0;
    const res = await drain(storage, async () => {
      called += 1;
      return { status: 200 };
    });
    expect(called).toBe(0);
    expect(res).toEqual({
      sent: 0, failed: 0, dropped: 0, remaining: 0, errors: [],
    });
  });

  it('corrupt storage reads as an empty queue rather than throwing', async () => {
    const bad = mkStorage('{ not json');
    expect(await readQueue(bad)).toEqual([]);
    const worse = mkStorage('{"not":"an array"}');
    expect(await readQueue(worse)).toEqual([]);
  });

  it('a malformed entry is dropped without stranding the rest', async () => {
    const mixed = mkStorage(
      JSON.stringify([
        { nope: true },
        { approval_id: 'ap-1', item_id: '46', board_id: 'b', decision: 'approve' },
      ]),
    );
    const stored = await readQueue(mixed);
    expect(stored).toHaveLength(1);
    expect(stored[0].item_id).toBe('46');
  });

  it('remove drops one entry', async () => {
    await enqueue(storage, decision({ item_id: '46' }));
    await enqueue(storage, decision({ item_id: '47' }));
    await remove(storage, 'acme-jira', '46');
    expect((await readQueue(storage)).map((e) => e.item_id)).toEqual(['47']);
  });

  it('pendingItemIds is what greys out a card', async () => {
    await enqueue(storage, decision({ item_id: '46' }));
    const ids = pendingItemIds(await readQueue(storage));
    expect(ids.has('46')).toBe(true);
    expect(ids.has('47')).toBe(false);
  });

  it('generated ids are distinct', () => {
    const ids = new Set(Array.from({ length: 50 }, () => newApprovalId()));
    expect(ids.size).toBe(50);
  });
});
