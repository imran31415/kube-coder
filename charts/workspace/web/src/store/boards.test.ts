import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  boards,
  boardsError,
  boardItems,
  filteredItems,
  itemFilter,
  itemsError,
  loadItems,
  refreshBoards,
  selectBoard,
  selectItem,
  selectedBoard,
  selectedItem,
  // review
  approveStaged,
  rejectStaged,
  sendBackStaged,
  lastResumeOutcome,
  editStaged,
  refreshReview,
  reviewGroups,
  reviewError,
  openReviewCount,
  newApprovalId,
  // runs
  refreshRuns,
  startRun,
  runsError,
  selectedBoardId,
  startRunPolling,
  stopRunPolling,
  // credentials
  saveCredential,
  credentialsError,
  decisionPending,
  boardStanding,
  boardRuns,
  _resetBoardsForTest,
} from './boards';
import type {
  Board,
  BoardItem,
  BoardRunSummary,
  StagedRecord,
} from '../api/boards';

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

function mkItem(over: Partial<BoardItem> = {}): BoardItem {
  return {
    id: '46',
    key: 'SUP-5',
    ref: { issue_key: 'SUP-5' },
    title: 'Refund not received',
    body: 'Dana says the refund never arrived.',
    status: { normalized: 'IN_PROGRESS', raw: 'In Review' },
    priority: { normalized: 'HIGH', raw: 'P2' },
    assignee: {},
    contact: { name: 'Dana' },
    collection: {},
    tags: ['billing'],
    url: 'https://acme.atlassian.net/browse/SUP-5',
    created_at: '',
    updated_at: '',
    raw: {},
    ...over,
  };
}

function listing(items: BoardItem[], over = {}) {
  return {
    items,
    complete: true,
    truncation_reason: '',
    pages_fetched: 1,
    ...over,
  };
}

describe('boards store', () => {
  beforeEach(() => {
    _resetBoardsForTest();
  });

  afterEach(() => {
    _resetBoardsForTest();
    globalThis.fetch = realFetch;
    vi.restoreAllMocks();
  });

  it('loads the board list', async () => {
    respond(200, { boards: [{ id: 'acme-jira', display_name: 'Acme', vendor: 'jira' }] });
    await refreshBoards();
    expect(boards.value).toHaveLength(1);
    expect(boards.value[0].id).toBe('acme-jira');
    expect(boardsError.value).toBeNull();
  });

  it('captures an error instead of throwing', async () => {
    respond(500, { error: 'boom' });
    await refreshBoards();
    expect(boardsError.value).toBeTruthy();
    expect(boards.value).toEqual([]);
  });

  it('dedupes concurrent refreshes into one request', async () => {
    const calls = respond(200, { boards: [] });
    await Promise.all([refreshBoards(), refreshBoards(), refreshBoards()]);
    expect(calls).toHaveLength(1);
  });

  it('dedupes concurrent item loads per board', async () => {
    const calls = respond(200, listing([mkItem()]));
    await Promise.all([loadItems('b1'), loadItems('b1')]);
    expect(calls).toHaveLength(1);
  });

  it('selecting a board loads its items once and clears item selection', async () => {
    const calls = respond(200, listing([mkItem()]));
    await selectBoard('b1');
    expect(boardItems.value['b1'].items).toHaveLength(1);
    await selectBoard('b1');
    expect(calls).toHaveLength(1); // already cached
  });

  it('changing board resets the filter and the selected item', async () => {
    respond(200, listing([mkItem()]));
    await selectBoard('b1');
    selectItem('46');
    itemFilter.value = 'refund';
    await selectBoard('b2');
    expect(selectedItem.value).toBeNull();
    expect(itemFilter.value).toBe('');
  });

  it('records a per-board item error without clobbering other boards', async () => {
    respond(200, listing([mkItem()]));
    await loadItems('b1');
    respond(500, { error: 'rate limited' });
    await loadItems('b2');
    expect(itemsError.value['b2']).toBeTruthy();
    expect(itemsError.value['b1']).toBeNull();
    expect(boardItems.value['b1'].items).toHaveLength(1);
  });

  it('selectedBoard resolves from the loaded list', async () => {
    respond(200, { boards: [{ id: 'b1', display_name: 'One', vendor: 'jira' }] });
    await refreshBoards();
    await selectBoard('b1');
    expect(selectedBoard.value?.display_name).toBe('One');
  });

  it('filters items by key, title and tag', async () => {
    respond(
      200,
      listing([
        mkItem({ id: '1', key: 'SUP-1', title: 'Refund', tags: ['billing'] }),
        mkItem({ id: '2', key: 'SUP-2', title: 'Login broken', tags: ['auth'] }),
      ]),
    );
    await selectBoard('b1');

    itemFilter.value = 'login';
    expect(filteredItems.value.map((i) => i.id)).toEqual(['2']);

    itemFilter.value = 'billing';
    expect(filteredItems.value.map((i) => i.id)).toEqual(['1']);

    itemFilter.value = 'SUP-2';
    expect(filteredItems.value.map((i) => i.id)).toEqual(['2']);

    itemFilter.value = '';
    expect(filteredItems.value).toHaveLength(2);
  });

  it('preserves the incomplete flag on the listing', async () => {
    respond(
      200,
      listing([mkItem()], {
        complete: false,
        truncation_reason: 'full_page_no_pagination_metadata',
      }),
    );
    await selectBoard('b1');
    expect(boardItems.value['b1'].complete).toBe(false);
    expect(boardItems.value['b1'].truncation_reason).toBe(
      'full_page_no_pagination_metadata',
    );
  });

  it('never polls on a timer (item fetches are outbound vendor calls)', async () => {
    vi.useFakeTimers();
    const calls = respond(200, listing([mkItem()]));
    await selectBoard('b1');
    const after = calls.length;
    vi.advanceTimersByTime(120_000);
    expect(calls.length).toBe(after);
    vi.useRealTimers();
  });
});

describe('boards review store', () => {
  beforeEach(() => {
    _resetBoardsForTest();
  });

  afterEach(() => {
    _resetBoardsForTest();
    globalThis.fetch = realFetch;
    vi.restoreAllMocks();
  });

  function mkRecord(over: Partial<StagedRecord> = {}): StagedRecord {
    return {
      board_id: 'b1',
      item_id: '46',
      item_key: 'SUP-5',
      item_title: 'Refund not received',
      item_url: 'https://x/browse/SUP-5',
      content_hash: 'hash-1',
      run_id: 'run-1-aaaa',
      state: 'pending',
      disposition: 'needs_review',
      reason: 'wants a call back',
      evidence: {},
      actions: [],
      pending_actions: [],
      open: true,
      decided_by: '',
      result: null,
      created_at: 1,
      updated_at: 1,
      ...over,
    };
  }

  it('an approval id is a real id and is REUSED across retries', () => {
    /* The whole replay mechanism depends on the id being minted once per
       DECISION rather than per attempt — a fresh id per retry would look like
       a second reviewer and post a second comment. */
    const a = newApprovalId();
    const b = newApprovalId();
    expect(a.length).toBeGreaterThanOrEqual(8);
    expect(a).not.toBe(b);
  });

  it('approve sends the hash the card carried, not a freshly read one', async () => {
    const calls = respond(200, { replayed: false, result: { ok: true } });
    const record = mkRecord({ content_hash: 'hash-from-the-card' });
    await approveStaged('b1', record, 'approval-1234');
    const approve = calls.find((c) => c.url.includes('/approve'));
    expect(approve).toBeTruthy();
    const body = JSON.parse(approve!.body as string);
    expect(body.content_hash).toBe('hash-from-the-card');
    expect(body.approval_id).toBe('approval-1234');
  });

  it('a retry reuses the same approval id when the caller passes it', async () => {
    const calls = respond(200, { replayed: true, result: { ok: true } });
    const record = mkRecord();
    await approveStaged('b1', record, 'approval-1234');
    await approveStaged('b1', record, 'approval-1234');
    const ids = calls
      .filter((c) => c.url.includes('/approve'))
      .map((c) => JSON.parse(c.body as string).approval_id);
    expect(new Set(ids).size).toBe(1);
  });

  it('a 409 is captured and the queue is reloaded rather than left stale', async () => {
    const calls = respond(409, { error: 'stale', code: 'stale' });
    const err = await approveStaged('b1', mkRecord());
    expect(err).toBeTruthy();
    expect(reviewError.value).toBeTruthy();
    // approve + the reload of the queue behind it
    expect(calls.some((c) => c.url.includes('/review'))).toBe(true);
  });

  it('counts only OPEN items for the badge', async () => {
    respond(200, {
      groups: [
        {
          disposition: 'needs_review',
          count: 2,
          items: [mkRecord(), mkRecord({ item_id: '47', open: false })],
        },
      ],
      total: 2,
      open: 1,
    });
    await refreshReview('b1');
    expect(reviewGroups.value).toHaveLength(1);
    expect(openReviewCount.value).toBe(1);
  });

  it('reject and send-back are distinct calls that write nothing', async () => {
    const calls = respond(200, { replayed: false });
    await rejectStaged('b1', '46', 'tone is wrong');
    await sendBackStaged('b1', '46', 'which refund?');
    const paths = calls.map((c) => c.url);
    expect(paths.some((p) => p.includes('/reject'))).toBe(true);
    expect(paths.some((p) => p.includes('/send-back'))).toBe(true);
  });

  it('send-back sends the text as `note`, not `reason`', async () => {
    // The server REQUIRES it and treats it as an instruction to the agent —
    // reject's `reason` is a different field with a different meaning.
    const calls = respond(200, { replayed: false });
    await sendBackStaged('b1', '46', 'which refund — Jan or Mar?');
    const sent = calls.find((c) => c.url.includes('/send-back'));
    const body = JSON.parse(sent!.body as string);
    expect(body.note).toBe('which refund — Jan or Mar?');
    expect(body.reason).toBeUndefined();
    expect(typeof body.approval_id).toBe('string');
  });

  it('records the resume outcome so the reviewer is told what actually happened',
    async () => {
      // Only one of the three tiers keeps the agent's own reasoning. A
      // reviewer told their context survived when it did not would trust the
      // next answer more than it deserves.
      respond(200, {
        replayed: false,
        resume: { dispatched: true, run_id: 'run-1', detail: 'started fresh' },
      });
      await sendBackStaged('b1', '46', 'which refund?');
      expect(lastResumeOutcome.value).toEqual({
        dispatched: true,
        run_id: 'run-1',
        detail: 'started fresh',
      });
    });

  it('a send-back that could not be re-dispatched still reports honestly',
    async () => {
      respond(200, {
        replayed: false,
        resume: { dispatched: false, detail: 'run run-9 still holds this item' },
      });
      const err = await sendBackStaged('b1', '46', 'which refund?');
      expect(err).toBeNull();                       // the DECISION succeeded
      expect(lastResumeOutcome.value?.dispatched).toBe(false);
    });

  it('edit posts the new params and does not decide the item', async () => {
    const calls = respond(200, mkRecord());
    await editStaged('b1', '46', 'a1', { body: 'warmer' });
    const edit = calls.find((c) => c.url.includes('/edit'));
    expect(JSON.parse(edit!.body as string)).toEqual({
      action_id: 'a1',
      params: { body: 'warmer' },
    });
  });
});

describe('boards runs store', () => {
  beforeEach(() => {
    _resetBoardsForTest();
  });

  afterEach(() => {
    _resetBoardsForTest();
    stopRunPolling();
    globalThis.fetch = realFetch;
    vi.restoreAllMocks();
  });

  it('run progress DOES poll — unlike items, a run record is a local file', async () => {
    vi.useFakeTimers();
    const calls = respond(200, {
      runs: [{ id: 'run-1-aaaa', status: 'running', board_id: 'b1' }],
    });
    selectedBoardId.value = 'b1';
    await refreshRuns('b1');
    const before = calls.length;
    startRunPolling('b1');
    await vi.advanceTimersByTimeAsync(10_000);
    expect(calls.length).toBeGreaterThan(before);
    stopRunPolling();
    vi.useRealTimers();
  });

  it('polling stops when nothing is running', async () => {
    vi.useFakeTimers();
    const calls = respond(200, {
      runs: [{ id: 'run-1-aaaa', status: 'done', board_id: 'b1' }],
    });
    selectedBoardId.value = 'b1';
    await refreshRuns('b1');
    const before = calls.length;
    startRunPolling('b1');
    await vi.advanceTimersByTimeAsync(10_000);
    expect(calls.length).toBe(before);
    stopRunPolling();
    vi.useRealTimers();
  });

  it('captures a start error instead of throwing', async () => {
    respond(400, { error: 'select.limit must be an integer between 1 and 500' });
    const err = await startRun('b1', { concurrency: 2 });
    expect(err).toBeTruthy();
    expect(runsError.value).toContain('select.limit');
  });
});

describe('boards credentials store', () => {
  beforeEach(() => {
    _resetBoardsForTest();
  });

  afterEach(() => {
    _resetBoardsForTest();
    globalThis.fetch = realFetch;
    vi.restoreAllMocks();
  });

  it('saving a credential also refreshes the BOARD list', async () => {
    /* A board that read "needs key" may have just become usable, and that
       state lives on the board rather than on the credential. */
    const calls = respond(200, { credentials: [], boards: [] });
    await saveCredential('JIRA_API_TOKEN', { secret: 'x' });
    expect(calls.some((c) => c.url.endsWith('/api/boards'))).toBe(true);
  });

  it('captures a validation error instead of throwing', async () => {
    respond(400, { error: 'username is required for format="basic"' });
    const err = await saveCredential('JIRA', { secret: 'x', format: 'basic' });
    expect(err).toContain('username is required');
    expect(credentialsError.value).toBeTruthy();
  });
});

describe('a decision fans out to everything it changed', () => {
  beforeEach(() => {
    _resetBoardsForTest();
  });

  afterEach(() => {
    _resetBoardsForTest();
    globalThis.fetch = realFetch;
    vi.restoreAllMocks();
  });

  function mkRecord(over: Partial<StagedRecord> = {}): StagedRecord {
    return {
      board_id: 'b1',
      item_id: '46',
      item_key: 'SUP-5',
      item_title: 'Refund not received',
      item_url: 'https://x/browse/SUP-5',
      content_hash: 'hash-1',
      run_id: 'run-1-aaaa',
      state: 'pending',
      disposition: 'needs_review',
      reason: 'wants a call back',
      evidence: {},
      actions: [],
      pending_actions: [],
      open: true,
      decided_by: '',
      result: null,
      created_at: 1,
      updated_at: 1,
      ...over,
    };
  }

  function seedOpenCard(record = mkRecord()) {
    selectedBoardId.value = 'b1';
    reviewGroups.value = [
      { disposition: 'needs_review', count: 1, items: [record] },
    ];
    return record;
  }

  it('refreshes runs and metrics too, not just the review queue', async () => {
    /* The bug this pins: approving closed the real ticket and settled the run
       item, but only the queue was re-read — so the Runs tab kept showing the
       item as unsettled and the approval-rate strip quoted a figure from
       before the decision. */
    const calls = respond(200, { replayed: false, result: { ok: true } });
    seedOpenCard();

    await approveStaged('b1', mkRecord(), 'ap-1');
    await vi.waitFor(() => {
      expect(calls.some((c) => c.url.includes('/runs'))).toBe(true);
      expect(calls.some((c) => c.url.includes('/metrics'))).toBe(true);
    });
    expect(calls.some((c) => c.url.includes('/review'))).toBe(true);
  });

  it('settles the card on the FIRST round trip, not the second', async () => {
    /* A decision used to cost two serialized round trips before anything moved
       on screen: the write, then a full queue re-read. Here the re-read never
       resolves, and the card must still have settled. */
    const record = seedOpenCard();
    globalThis.fetch = vi.fn(async (url: unknown, init?: RequestInit) => {
      const u = String(url);
      if ((init?.method ?? 'GET') === 'GET' && u.includes('/review')) {
        return new Promise<Response>(() => {}); // never resolves
      }
      return {
        ok: true,
        status: 200,
        headers: { get: () => 'application/json' },
        json: async () => ({ replayed: false, result: { ok: true } }),
        text: async () => '{}',
      } as unknown as Response;
    }) as unknown as typeof fetch;

    await approveStaged('b1', record, 'ap-2');

    const card = reviewGroups.value[0].items[0];
    expect(card.open).toBe(false);
    expect(card.state).toBe('approved');
    expect(card.pending_actions).toEqual([]);
    expect(openReviewCount.value).toBe(0);
  });

  it('names the decision in flight, then clears it', async () => {
    /* A boolean could grey the card out but could not say WHICH verb was
       waiting — an approve on a slow vendor looked like a wedged page. */
    seedOpenCard();
    let seen: string | undefined;
    globalThis.fetch = vi.fn(async (url: unknown) => {
      if (String(url).includes('/approve')) {
        seen = decisionPending.value['46'];
      }
      return {
        ok: true,
        status: 200,
        headers: { get: () => 'application/json' },
        json: async () => ({ replayed: false, result: { ok: true } }),
        text: async () => '{}',
      } as unknown as Response;
    }) as unknown as typeof fetch;

    await approveStaged('b1', mkRecord(), 'ap-3');
    expect(seen).toBe('approve');
    expect(decisionPending.value['46']).toBeUndefined();
  });

  it('clears the in-flight marker even when the decision fails', async () => {
    respond(409, { error: 'stale', code: 'stale' });
    seedOpenCard();
    await approveStaged('b1', mkRecord(), 'ap-4');
    expect(decisionPending.value['46']).toBeUndefined();
  });

  it('reject and send back fan out the same way', async () => {
    const calls = respond(200, { ok: true, resume: null });
    seedOpenCard();
    await rejectStaged('b1', '46', 'out of scope', 'ap-5');
    await vi.waitFor(() => {
      expect(calls.some((c) => c.url.includes('/runs'))).toBe(true);
    });
    expect(reviewGroups.value[0].items[0].state).toBe('rejected');
  });

  it('edit does NOT fan out — nothing has been written to the board yet', async () => {
    /* Editing a staged action changes what WOULD be sent. Treating it like a
       decision would re-read the vendor's item list for no reason and burn
       rate-limit budget on a click that changed nothing outside this app. */
    const calls = respond(200, { ok: true });
    seedOpenCard();
    await editStaged('b1', '46', 'a1', { body: 'reworded' });
    await new Promise((r) => setTimeout(r, 0));
    expect(calls.some((c) => c.url.includes('/runs'))).toBe(false);
    expect(calls.some((c) => c.url.includes('/metrics'))).toBe(false);
    expect(calls.some((c) => c.url.includes('/review'))).toBe(true);
  });
});

describe('boardStanding — what is happening and what to do next', () => {
  beforeEach(() => {
    _resetBoardsForTest();
  });
  afterEach(() => {
    _resetBoardsForTest();
    globalThis.fetch = realFetch;
  });

  function seedBoard(over: Partial<Board> = {}) {
    boards.value = [
      {
        id: 'b1',
        vendor: 'github',
        display_name: 'acme/demo',
        base_url: 'https://api.github.com',
        credential_ref: '@board-creds/T',
        credential_set: true,
        ...over,
      } as Board,
    ];
    selectedBoardId.value = 'b1';
  }

  function seedItems(n: number) {
    boardItems.value = {
      b1: {
        items: Array.from({ length: n }, (_, i) => ({
          id: String(i),
          key: `#${i}`,
          title: `item ${i}`,
          tags: [],
        })) as unknown as BoardItem[],
        complete: true,
        truncation_reason: '',
      } as never,
    };
  }

  function seedRun(over: Partial<BoardRunSummary> = {}) {
    boardRuns.value = {
      b1: [
        {
          id: 'run-1',
          board_id: 'b1',
          mode: 'propose',
          status: 'running',
          concurrency: 2,
          requested_concurrency: 2,
          clamp_reason: '',
          created_at: 1,
          updated_at: 2,
          finished_at: null,
          error: '',
          listing_complete: true,
          truncation_reason: '',
          total: 5,
          counts: { working: 2, pending: 1, done: 2, failed: 0, skipped: 0, claimed: 0 },
          done: 2,
          failed: 0,
          skipped: 0,
          ...over,
        } as BoardRunSummary,
      ],
    };
  }

  it('points at the credential before anything else', () => {
    seedBoard({ credential_set: false });
    expect(boardStanding.value.next).toMatch(/credential/i);
    expect(boardStanding.value.nextTab).toBe('credentials');
  });

  it('counts live work while a run is in flight', () => {
    seedBoard();
    seedItems(9);
    seedRun();
    const s = boardStanding.value;
    expect(s.live).toBe(true);
    expect(s.working).toBe(2);
    expect(s.queued).toBe(1);
    expect(s.settled).toBe(2);
    expect(s.items).toBe(9);
    expect(s.next).toMatch(/in flight/i);
  });

  it('sends you to Review the moment something is waiting, even mid-run', () => {
    seedBoard();
    seedItems(9);
    seedRun();
    reviewGroups.value = [
      {
        disposition: 'needs_review',
        count: 1,
        items: [
          {
            item_id: '1', open: true, state: 'pending', pending_actions: [],
          } as unknown as StagedRecord,
        ],
      },
    ];
    expect(boardStanding.value.awaiting).toBe(1);
    expect(boardStanding.value.nextTab).toBe('review');
  });

  it('tells a board that has never been run to start one', () => {
    seedBoard();
    seedItems(9);
    expect(boardStanding.value.nextTab).toBe('runs');
  });

  it('says nothing at all when no board is selected', () => {
    expect(boardStanding.value.next).toBe('');
  });
});

describe('the next step only points somewhere when something is owed', () => {
  beforeEach(() => { _resetBoardsForTest(); });
  afterEach(() => { _resetBoardsForTest(); globalThis.fetch = realFetch; });

  it('offers no destination while a run is simply progressing', () => {
    /* "A run is in flight. Nothing needs you yet." is a complete answer. An
       action button beside it would invent work for a reader who has none. */
    boards.value = [
      {
        id: 'b1', vendor: 'github', display_name: 'acme/demo',
        base_url: '', credential_ref: '@board-creds/T', credential_set: true,
      } as Board,
    ];
    selectedBoardId.value = 'b1';
    boardRuns.value = {
      b1: [
        {
          id: 'r', board_id: 'b1', mode: 'propose', status: 'running',
          concurrency: 1, requested_concurrency: 1, clamp_reason: '',
          created_at: 1, updated_at: 2, finished_at: null, error: '',
          listing_complete: true, truncation_reason: '', total: 2,
          counts: { pending: 1, claimed: 0, working: 1, done: 0, failed: 0, skipped: 0 },
          done: 0, failed: 0, skipped: 0,
        } as BoardRunSummary,
      ],
    };
    expect(boardStanding.value.next).toMatch(/nothing needs you/i);
    expect(boardStanding.value.nextTab).toBeNull();
  });
});
