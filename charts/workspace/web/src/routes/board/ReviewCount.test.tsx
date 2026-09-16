import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, cleanup, act, fireEvent } from '@testing-library/preact';
import type { DashboardEvent } from '../../api/events';

// The route subscribes to the event stream on mount. Capture the handler so a
// test can deliver `boards.review` exactly as the server would.
const stream = vi.hoisted(() => ({
  handler: null as null | ((ev: DashboardEvent) => void),
}));
vi.mock('../../api/events', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/events')>();
  return {
    ...actual,
    subscribeEvents: (h: (ev: DashboardEvent) => void) => {
      stream.handler = h;
      return () => {
        stream.handler = null;
      };
    },
  };
});

import { BoardRoute } from './index';
import {
  boards,
  boardTab,
  openReviewCount,
  refreshReview,
  refreshRuns,
  reviewBoardId,
  selectBoard,
  selectedBoardId,
  startRunPolling,
  stopRunPolling,
  _resetBoardsForTest,
  _resetRunFormsForTest,
} from '../../store/boards';
import { eventStreamConnected } from '../../api/events';
import { serverMode } from '../../store/server-mode';
import type {
  Board,
  BoardRunSummary,
  ReviewGroup,
  StagedRecord,
} from '../../api/boards';

/**
 * The Review badge (#704).
 *
 * It used to be empty until someone opened the Review tab, because only that
 * panel read the queue — so the one number meant to tell you something is
 * waiting said nothing on the tab you were actually on. These tests pin that it
 * is loaded on arrival, counts the right cards, belongs to the board on screen,
 * and keeps up with reports and decisions however they arrive.
 */

const realFetch = globalThis.fetch;

function mkBoard(id: string): Board {
  return {
    id,
    vendor: 'github',
    display_name: id,
    base_url: 'https://api.github.com',
    credential_ref: '@board-creds/T',
    credential_set: true,
  };
}

function card(key: string, over: Partial<StagedRecord> = {}): StagedRecord {
  const state = over.state ?? 'pending';
  return {
    board_id: 'b1',
    item_id: key,
    item_key: key,
    item_title: `ticket ${key}`,
    item_url: '',
    content_hash: 'h',
    run_id: 'r',
    state,
    disposition: 'needs_review',
    reason: 'why',
    evidence: {},
    actions: [],
    pending_actions: [],
    open: state === 'pending' || state === 'partial',
    decided_by: '',
    result: null,
    created_at: 1,
    updated_at: 1,
    ...over,
  } as StagedRecord;
}

function group(disposition: string, items: StagedRecord[]): ReviewGroup {
  return {
    disposition,
    count: items.length,
    items: items.map((i) => ({ ...i, disposition }) as StagedRecord),
  };
}

function waiting(n: number, disposition = 'needs_review'): ReviewGroup[] {
  return [group(disposition, Array.from({ length: n }, (_, i) => card(String(i + 1))))];
}

function liveRun(boardId: string): BoardRunSummary {
  return {
    id: 'run-1', board_id: boardId, mode: 'propose', status: 'running',
    concurrency: 2, requested_concurrency: 2, clamp_reason: '',
    created_at: 1, updated_at: 1, finished_at: null, error: '',
    listing_complete: true, truncation_reason: '', total: 4,
    counts: { pending: 2, claimed: 0, working: 2, done: 0, failed: 0, skipped: 0 },
    done: 0, failed: 0, skipped: 0,
  };
}

/**
 * A fake workspace. Review reads can be HELD so a test decides the order the
 * answers come back in — each held read answers with the queue as it was when
 * the read was made, which is what a real slow response does.
 */
const server = {
  queues: {} as Record<string, ReviewGroup[]>,
  runs: {} as Record<string, BoardRunSummary[]>,
  hold: false,
  held: [] as { board: string; release: () => void }[],
  reviewReads: [] as string[],
};

function json(body: unknown) {
  return {
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

function installServer() {
  globalThis.fetch = vi.fn((input: unknown) => {
    const path = String(input).split('?')[0];
    const review = path.match(/\/api\/boards\/([^/]+)\/review$/);
    if (review) {
      const board = decodeURIComponent(review[1]);
      server.reviewReads.push(board);
      const answer = json({ groups: server.queues[board] ?? [], total: 0, open: 0 });
      if (!server.hold) return Promise.resolve(answer);
      return new Promise((resolve) => {
        server.held.push({ board, release: () => resolve(answer) });
      });
    }
    const runs = path.match(/\/api\/boards\/([^/]+)\/runs$/);
    if (runs) {
      return Promise.resolve(
        json({ runs: server.runs[decodeURIComponent(runs[1])] ?? [] }),
      );
    }
    if (/\/api\/boards$/.test(path)) {
      return Promise.resolve(json({ boards: boards.value }));
    }
    return Promise.resolve(json({
      items: [], complete: true, truncation_reason: '', pages_fetched: 1,
      strategies: {}, orders: [], credentials: [],
    }));
  }) as unknown as typeof fetch;
}

/** Let fetch promises and the renders they cause run to completion. */
async function settle() {
  await act(async () => {
    for (let i = 0; i < 10; i++) await Promise.resolve();
  });
}

function badge(container: Element) {
  return container.querySelector('.board-tab-badge') as HTMLElement | null;
}

function activeTab(container: Element) {
  return container.querySelector('.board-tab.is-active')?.textContent ?? '';
}

beforeEach(() => {
  localStorage.clear();
  _resetBoardsForTest();
  _resetRunFormsForTest();
  window.history.replaceState({}, '', '/board');
  serverMode.value = {
    readOnly: false, authed: true, authMode: 'basic', demoShowAll: false,
  };
  server.queues = {};
  server.runs = {};
  server.hold = false;
  server.held = [];
  server.reviewReads = [];
  eventStreamConnected.value = true;
  installServer();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  stopRunPolling();
  _resetBoardsForTest();
  globalThis.fetch = realFetch;
  eventStreamConnected.value = false;
  localStorage.clear();
});

describe('the Review badge is there on arrival', () => {
  it('shows the waiting count on the Items tab without opening Review', async () => {
    boards.value = [mkBoard('b1')];
    selectedBoardId.value = 'b1';
    server.queues.b1 = [
      group('needs_review', [card('1'), card('2', { state: 'approved' })]),
      group('needs_rescoping', [card('3')]),
      group('blocked', [card('4'), card('5', { state: 'rejected' })]),
      group('failed', [card('6', { state: 'partial' })]),
      group('completed', [card('7', { state: 'approved' })]),
    ];

    const { container } = render(<BoardRoute />);
    await settle();

    expect(activeTab(container)).toContain('Items');
    expect(server.reviewReads).toContain('b1');
    // needs review 1 + needs rescoping 1 + blocked 1 + partial 1. Decided cards
    // (approved, rejected) are not waiting on anybody.
    expect(badge(container)?.textContent).toBe('4');
  });

  it('says what the count is made of, to a pointer and to a screen reader', async () => {
    boards.value = [mkBoard('b1')];
    selectedBoardId.value = 'b1';
    server.queues.b1 = [
      group('needs_review', [card('1'), card('2')]),
      group('blocked', [card('3')]),
    ];

    const { container } = render(<BoardRoute />);
    await settle();

    const label = '3 awaiting your decision: 2 needs review · 1 blocked';
    expect(badge(container)?.getAttribute('title')).toBe(label);
    expect(screen.getByRole('button', { name: `Review, ${label}` })).toBeTruthy();
  });

  it('shows no badge when nothing is waiting', async () => {
    boards.value = [mkBoard('b1')];
    selectedBoardId.value = 'b1';
    server.queues.b1 = [group('needs_review', [card('1', { state: 'approved' })])];

    const { container } = render(<BoardRoute />);
    await settle();

    expect(server.reviewReads).toContain('b1');
    expect(badge(container)).toBeNull();
  });
});

describe('the badge belongs to the board on screen', () => {
  it('drops the old count at once on a switch and ignores the old board’s late answer', async () => {
    boards.value = [mkBoard('b1'), mkBoard('b2')];
    selectedBoardId.value = 'b1';
    server.queues.b1 = waiting(3);
    server.queues.b2 = waiting(1);

    const { container } = render(<BoardRoute />);
    await settle();
    expect(badge(container)?.textContent).toBe('3');

    // A read for b1 is still out when the operator switches to b2.
    server.hold = true;
    void refreshReview('b1');
    await act(async () => {
      void selectBoard('b2');
    });
    await settle();
    // Nothing from b1 may stand in for b2 while b2's own read is out.
    expect(badge(container)).toBeNull();

    const b2 = server.held.find((h) => h.board === 'b2');
    const b1 = server.held.find((h) => h.board === 'b1');
    expect(b2).toBeTruthy();
    expect(b1).toBeTruthy();
    b2!.release();
    await settle();
    expect(badge(container)?.textContent).toBe('1');

    b1!.release();
    await settle();
    expect(badge(container)?.textContent).toBe('1');
    expect(reviewBoardId.value).toBe('b2');
  });

  it('never lets an older read of the same board overwrite a newer one', async () => {
    boards.value = [mkBoard('b1')];
    selectedBoardId.value = 'b1';
    server.queues.b1 = waiting(1);

    const { container } = render(<BoardRoute />);
    await settle();
    expect(badge(container)?.textContent).toBe('1');

    // Two agents report together; each report publishes an event and starts a
    // read. The first read sees the queue before the second report landed.
    server.hold = true;
    server.queues.b1 = waiting(2);
    void refreshReview('b1');
    server.queues.b1 = waiting(3);
    void refreshReview('b1');
    const [older, newer] = server.held;

    newer.release();
    await settle();
    expect(badge(container)?.textContent).toBe('3');

    older.release();
    await settle();
    // The count must never go backwards.
    expect(badge(container)?.textContent).toBe('3');
  });
});

describe('the badge keeps up', () => {
  it('re-reads on a boards.review event, with no poll involved', async () => {
    boards.value = [mkBoard('b1')];
    selectedBoardId.value = 'b1';
    server.queues.b1 = waiting(1);

    const { container } = render(<BoardRoute />);
    await settle();
    expect(badge(container)?.textContent).toBe('1');
    expect(stream.handler).toBeTruthy();

    server.queues.b1 = waiting(2);
    const before = server.reviewReads.length;
    await act(async () => {
      stream.handler!({ type: 'boards.review', data: { board_id: 'b1', op: 'report' } });
    });
    // The event itself started the read — there is no live run, so nothing
    // else could have.
    expect(server.reviewReads.length).toBe(before + 1);
    await settle();
    expect(badge(container)?.textContent).toBe('2');
  });

  it('ignores a boards.review event for a board that is not on screen', async () => {
    boards.value = [mkBoard('b1'), mkBoard('b2')];
    selectedBoardId.value = 'b1';
    server.queues.b1 = waiting(1);

    render(<BoardRoute />);
    await settle();
    const before = server.reviewReads.length;
    await act(async () => {
      stream.handler!({ type: 'boards.review', data: { board_id: 'b2', op: 'report' } });
    });
    expect(server.reviewReads.length).toBe(before);
  });

  it('rides the run poll while a run is live and the stream is down, and only then', async () => {
    vi.useFakeTimers();
    boards.value = [mkBoard('b1')];
    selectedBoardId.value = 'b1';
    server.runs.b1 = [liveRun('b1')];
    server.queues.b1 = waiting(2);
    // The run is already known to be live when the poll first ticks.
    await refreshRuns('b1');

    eventStreamConnected.value = false;
    startRunPolling('b1');
    await vi.advanceTimersByTimeAsync(3000);
    expect(server.reviewReads).toEqual(['b1']);
    expect(openReviewCount.value).toBe(2);

    // With the stream up, the event already did this work.
    eventStreamConnected.value = true;
    server.reviewReads = [];
    await vi.advanceTimersByTimeAsync(3000);
    expect(server.reviewReads).toEqual([]);
  });

  it('does not poll review when nothing is running, even with the stream down', async () => {
    vi.useFakeTimers();
    boards.value = [mkBoard('b1')];
    selectedBoardId.value = 'b1';
    eventStreamConnected.value = false;
    startRunPolling('b1');
    await vi.advanceTimersByTimeAsync(9000);
    expect(server.reviewReads).toEqual([]);
  });
});

describe('the tab is kept', () => {
  it('comes back to the tab it left from, e.g. after following View session', async () => {
    boards.value = [mkBoard('b1')];
    selectedBoardId.value = 'b1';
    server.queues.b1 = waiting(1);

    const first = render(<BoardRoute />);
    await settle();
    fireEvent.click(screen.getByRole('button', { name: 'Runs' }));
    await settle();
    expect(activeTab(first.container)).toContain('Runs');

    first.unmount();
    const second = render(<BoardRoute />);
    await settle();
    expect(activeTab(second.container)).toContain('Runs');
    expect(boardTab.value).toBe('runs');
  });

  it('still lets a ?review= link open the Review tab on that card', async () => {
    boards.value = [mkBoard('b1')];
    server.queues.b1 = waiting(3);
    boardTab.value = 'runs';
    window.history.replaceState({}, '', '/board?board=b1&review=2');

    const { container } = render(<BoardRoute />);
    await settle();

    expect(activeTab(container)).toContain('Review');
    expect(
      container.querySelector('.board-review-card.is-focused')?.getAttribute('data-item-id'),
    ).toBe('2');
  });
});
