import { describe, it, expect, afterEach, vi } from 'vitest';
import {
  createBoard,
  deleteBoard,
  getBoardItems,
  listBoards,
  testFetchBoard,
  truncationLabel,
  runItemStateLabel,
  runItemOrder,
  isRunItemLive,
  dispositionLabel,
  evidenceValueLabel,
  elapsedLabel,
  updateBoard,
} from './boards';
import type { RunItemState } from './boards';

const realFetch = globalThis.fetch;

function respond(status: number, body: unknown) {
  const calls: { url: string; method: string; body: string | null }[] = [];
  globalThis.fetch = vi.fn(async (url: unknown, init?: RequestInit) => {
    calls.push({
      url: String(url),
      method: init?.method ?? 'GET',
      body: (init?.body as string) ?? null,
    });
    return {
      ok: status >= 200 && status < 300,
      status,
      headers: { get: () => 'application/json' },
      json: async () => body,
      text: async () => JSON.stringify(body),
    } as unknown as Response;
  }) as unknown as typeof fetch;
  return calls;
}

describe('boards api client', () => {
  afterEach(() => {
    globalThis.fetch = realFetch;
    vi.restoreAllMocks();
  });

  it('lists boards', async () => {
    const calls = respond(200, { boards: [] });
    await listBoards();
    expect(calls[0].method).toBe('GET');
    expect(calls[0].url).toContain('/api/boards');
  });

  it('encodes the board id in every path', async () => {
    const calls = respond(200, { items: [] });
    await getBoardItems('weird id/../x');
    expect(calls[0].url).toContain(encodeURIComponent('weird id/../x'));
    expect(calls[0].url).not.toContain('/../');
  });

  it('creates and updates with the connector as the body', async () => {
    let calls = respond(201, { id: 'b1' });
    await createBoard({ id: 'b1', vendor: 'jira' });
    expect(calls[0].method).toBe('POST');
    expect(JSON.parse(calls[0].body!)).toEqual({ id: 'b1', vendor: 'jira' });

    calls = respond(200, { id: 'b1' });
    await updateBoard('b1', { id: 'b1', vendor: 'jira' });
    expect(calls[0].method).toBe('PUT');
  });

  it('deletes', async () => {
    const calls = respond(200, { ok: true });
    await deleteBoard('b1');
    expect(calls[0].method).toBe('DELETE');
  });

  it('sends max_pages only when asked', async () => {
    let calls = respond(200, {});
    await testFetchBoard('b1');
    expect(JSON.parse(calls[0].body!)).toEqual({});

    calls = respond(200, {});
    await testFetchBoard('b1', 2);
    expect(JSON.parse(calls[0].body!)).toEqual({ max_pages: 2 });
  });
});

describe('truncationLabel', () => {
  it('says nothing when the listing is complete', () => {
    expect(truncationLabel({ complete: true, truncation_reason: '' })).toBe('');
  });

  it('explains the full-page-no-metadata case in plain language', () => {
    const msg = truncationLabel({
      complete: false,
      truncation_reason: 'full_page_no_pagination_metadata',
    });
    expect(msg).toMatch(/may be more items/i);
  });

  it('explains each known reason', () => {
    for (const reason of ['max_pages', 'cursor_expired', 'items_path_not_found']) {
      expect(
        truncationLabel({ complete: false, truncation_reason: reason }),
      ).not.toBe('');
    }
  });

  it('renders an http status readably', () => {
    expect(
      truncationLabel({ complete: false, truncation_reason: 'http_401' }),
    ).toContain('HTTP 401');
  });

  it('falls back for an unknown reason rather than going silent', () => {
    expect(
      truncationLabel({ complete: false, truncation_reason: 'something-new' }),
    ).toBe('This list may be incomplete.');
  });
});

describe('labels for the states a human watches', () => {
  it('renders every run-item state as a word, never the wire enum', () => {
    expect(runItemStateLabel('pending')).toBe('queued');
    expect(runItemStateLabel('claimed')).toBe('starting');
    expect(runItemStateLabel('working')).toBe('working');
    expect(runItemStateLabel('done')).toBe('done');
    expect(runItemStateLabel('failed')).toBe('failed');
    expect(runItemStateLabel('skipped')).toBe('skipped');
  });

  it('sorts live work ahead of settled work', () => {
    const states: RunItemState[] = [
      'skipped', 'done', 'failed', 'pending', 'claimed', 'working',
    ];
    const sorted = [...states].sort((a, b) => runItemOrder(a) - runItemOrder(b));
    expect(sorted).toEqual([
      'working', 'claimed', 'pending', 'failed', 'done', 'skipped',
    ]);
  });

  it('counts queued work as live — it is part of what the run still owes', () => {
    expect(isRunItemLive('working')).toBe(true);
    expect(isRunItemLive('claimed')).toBe(true);
    expect(isRunItemLive('pending')).toBe(true);
    expect(isRunItemLive('done')).toBe(false);
    expect(isRunItemLive('failed')).toBe(false);
    expect(isRunItemLive('skipped')).toBe(false);
  });

  it('spells dispositions out, and passes an unknown one through readably', () => {
    expect(dispositionLabel('needs_rescoping')).toBe('needs rescoping');
    expect(dispositionLabel('needs_review')).toBe('needs review');
    expect(dispositionLabel('completed')).toBe('completed');
    expect(dispositionLabel('some_new_thing')).toBe('some new thing');
    expect(dispositionLabel(null)).toBe('');
  });

  it('never renders evidence as [object Object]', () => {
    /* Evidence is arbitrary JSON from the agent. String(value) on an object
       produced a chip that said nothing at all. */
    expect(evidenceValueLabel({ changed: 2 })).toBe('{"changed":2}');
    expect(evidenceValueLabel(['a', 'b'])).toBe('a, b');
    expect(evidenceValueLabel('18 passed')).toBe('18 passed');
    expect(evidenceValueLabel(3)).toBe('3');
    expect(evidenceValueLabel(false)).toBe('false');
    expect(evidenceValueLabel(null)).toBe('—');
    expect(evidenceValueLabel(undefined)).toBe('—');
  });

  it('survives evidence that cannot be serialised', () => {
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    expect(() => evidenceValueLabel(cyclic)).not.toThrow();
  });

  it('reports elapsed time at the precision a watcher cares about', () => {
    const now = 1_000_000_000_000; // ms
    const at = (secsAgo: number) => Math.floor(now / 1000) - secsAgo;
    expect(elapsedLabel(at(4), now)).toBe('4s');
    expect(elapsedLabel(at(130), now)).toBe('2m 10s');
    expect(elapsedLabel(at(3780), now)).toBe('1h 3m');
    // A missing timestamp says nothing rather than "56 years".
    expect(elapsedLabel(0, now)).toBe('');
  });

  it('never reports a negative elapsed time when clocks disagree', () => {
    const now = 1_000_000_000_000;
    expect(elapsedLabel(Math.floor(now / 1000) + 30, now)).toBe('0s');
  });
});
