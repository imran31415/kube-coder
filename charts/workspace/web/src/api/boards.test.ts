import { describe, it, expect, afterEach, vi } from 'vitest';
import {
  createBoard,
  deleteBoard,
  getBoardItems,
  listBoards,
  testFetchBoard,
  truncationLabel,
  updateBoard,
} from './boards';

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
