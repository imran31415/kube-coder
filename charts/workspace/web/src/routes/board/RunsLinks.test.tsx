import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, cleanup, act, fireEvent, within } from '@testing-library/preact';
import { RunsPanel } from './RunsPanel';
import { BoardRoute } from './index';
import {
  activeRun,
  boardRuns,
  boards,
  boardTab,
  reviewBoardId,
  reviewFocusItemId,
  reviewGroups,
  selectedBoardId,
  _resetBoardsForTest,
  _resetRunFormsForTest,
} from '../../store/boards';
import { serverMode } from '../../store/server-mode';
import type {
  Board,
  BoardRun,
  BoardRunItem,
  BoardRunSummary,
  ReviewGroup,
  RunItemState,
  StagedRecord,
} from '../../api/boards';

/**
 * From a run's item table to the thing a person does next (#704).
 *
 * The table said what each agent concluded and stopped there: to act on
 * "needs review" you went to the Review tab and hunted for the ticket. An
 * outcome that has a review card now opens it.
 */

const realFetch = globalThis.fetch;

function mkBoard(id = 'b1'): Board {
  return {
    id,
    vendor: 'github',
    display_name: id,
    base_url: 'https://api.github.com',
    credential_ref: '@board-creds/T',
    credential_set: true,
  };
}

function mkRunItem(
  key: string,
  state: RunItemState,
  over: Partial<BoardRunItem> = {},
): BoardRunItem {
  return {
    id: key,
    key,
    title: `ticket ${key}`,
    url: '',
    content_hash: 'h',
    state,
    lease_owner: '',
    task_id: '',
    disposition: null,
    reason: '',
    error: '',
    writes_used: 0,
    updated_at: 1,
    ...over,
  };
}

function mkSummary(): BoardRunSummary {
  return {
    id: 'run-1', board_id: 'b1', mode: 'propose', status: 'running',
    concurrency: 2, requested_concurrency: 2, clamp_reason: '',
    created_at: 1, updated_at: 2, finished_at: null, error: '',
    listing_complete: true, truncation_reason: '', total: 5,
    counts: { pending: 0, claimed: 0, working: 1, done: 3, failed: 1, skipped: 0 },
    done: 3, failed: 1, skipped: 0,
  };
}

function mkRun(): BoardRun {
  return {
    ...mkSummary(),
    select: {},
    stop_on: {},
    stop_requested: false,
    consecutive_failures: 0,
    items: {
      '1': mkRunItem('1', 'done', { disposition: 'needs_review', task_id: 'task-1' }),
      '2': mkRunItem('2', 'done', { disposition: 'needs_rescoping', task_id: 'task-2' }),
      // The build died before the agent reported: no card exists.
      '3': mkRunItem('3', 'failed', {
        disposition: 'failed', error: 'the build ended as error', task_id: 'task-3',
      }),
      // Reported, but the review read that follows has not landed yet.
      '4': mkRunItem('4', 'done', { disposition: 'completed', task_id: 'task-4' }),
      '5': mkRunItem('5', 'working', { task_id: 'task-5' }),
    },
  };
}

function card(key: string, disposition: string): StagedRecord {
  return {
    board_id: 'b1', item_id: key, item_key: key, item_title: `ticket ${key}`,
    item_url: '', content_hash: 'h', run_id: 'run-1', state: 'pending',
    disposition, reason: 'why', evidence: {}, actions: [], pending_actions: [],
    open: true, decided_by: '', result: null, created_at: 1, updated_at: 1,
  } as StagedRecord;
}

const QUEUE: ReviewGroup[] = [
  { disposition: 'needs_review', count: 1, items: [card('1', 'needs_review')] },
  { disposition: 'needs_rescoping', count: 1, items: [card('2', 'needs_rescoping')] },
];

function seed({ reviewFor = 'b1' as string | null } = {}) {
  boards.value = [mkBoard()];
  selectedBoardId.value = 'b1';
  boardRuns.value = { b1: [mkSummary()] };
  activeRun.value = mkRun();
  if (reviewFor) {
    reviewGroups.value = QUEUE;
    reviewBoardId.value = reviewFor;
  }
}

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
    if (/\/review$/.test(path)) return Promise.resolve(json({ groups: QUEUE, total: 2, open: 2 }));
    if (/\/runs\/run-1$/.test(path)) return Promise.resolve(json(mkRun()));
    if (/\/runs$/.test(path)) return Promise.resolve(json({ runs: [mkSummary()] }));
    if (/\/api\/boards$/.test(path)) return Promise.resolve(json({ boards: [mkBoard()] }));
    return Promise.resolve(json({
      items: [], complete: true, truncation_reason: '', pages_fetched: 1,
      strategies: {}, orders: [], credentials: [],
    }));
  }) as unknown as typeof fetch;
}

function row(key: string): HTMLElement {
  const rows = screen.getAllByRole('row').slice(1);
  const match = rows.find((r) => r.querySelector('td')?.textContent === key);
  if (!match) throw new Error(`no row for ${key}`);
  return match as HTMLElement;
}

async function settle() {
  await act(async () => {
    for (let i = 0; i < 10; i++) await Promise.resolve();
  });
}

beforeEach(() => {
  localStorage.clear();
  _resetBoardsForTest();
  _resetRunFormsForTest();
  window.history.replaceState({}, '', '/board');
  serverMode.value = {
    readOnly: false, authed: true, authMode: 'basic', demoShowAll: false,
  };
  installServer();
});

afterEach(() => {
  cleanup();
  _resetBoardsForTest();
  globalThis.fetch = realFetch;
  localStorage.clear();
  vi.restoreAllMocks();
});

describe('a run item outcome opens its review card', () => {
  it('makes an outcome with a card a button that opens that card on Review', () => {
    seed();
    render(<RunsPanel />);

    const button = within(row('1')).getByRole('button', {
      name: 'Open the review for 1: needs review',
    });
    fireEvent.click(button);

    expect(boardTab.value).toBe('review');
    expect(reviewFocusItemId.value).toBe('1');
  });

  it('works for every waiting disposition, not only needs review', () => {
    seed();
    render(<RunsPanel />);
    fireEvent.click(within(row('2')).getByRole('button', { name: /needs rescoping/ }));
    expect(reviewFocusItemId.value).toBe('2');
  });

  it('leaves an outcome with no card as plain text', () => {
    seed();
    render(<RunsPanel />);
    expect(within(row('4')).queryByRole('button')).toBeNull();
    expect(within(row('4')).getByText('completed')).toBeTruthy();
  });

  it('shows a failed build its error, with nothing to open', () => {
    seed();
    render(<RunsPanel />);
    expect(within(row('3')).queryByRole('button')).toBeNull();
    expect(within(row('3')).getByText('the build ended as error')).toBeTruthy();
  });

  it('offers nothing to open on a row that is still working', () => {
    seed();
    render(<RunsPanel />);
    expect(within(row('5')).queryByRole('button')).toBeNull();
  });

  it('does not treat a queue read for another board as this board’s cards', () => {
    seed({ reviewFor: 'some-other-board' });
    render(<RunsPanel />);
    expect(within(row('1')).queryByRole('button')).toBeNull();
    expect(within(row('1')).getByText('needs review')).toBeTruthy();
  });

  it('turns into a button the moment the review read lands', async () => {
    seed({ reviewFor: null });
    render(<RunsPanel />);
    expect(within(row('1')).queryByRole('button')).toBeNull();

    await act(async () => {
      reviewGroups.value = QUEUE;
      reviewBoardId.value = 'b1';
    });
    expect(within(row('1')).getByRole('button', { name: /needs review/ })).toBeTruthy();
  });
});

describe('the whole trip, through the route', () => {
  it('lands on the Review tab with that card highlighted, in view and focused', async () => {
    const scrolled: (string | null)[] = [];
    const original = HTMLElement.prototype.scrollIntoView;
    HTMLElement.prototype.scrollIntoView = function (this: HTMLElement) {
      scrolled.push(this.getAttribute('data-item-id'));
    };
    try {
      seed();
      boardTab.value = 'runs';
      const { container } = render(<BoardRoute />);
      await settle();

      fireEvent.click(within(row('2')).getByRole('button', { name: /needs rescoping/ }));
      await settle();

      expect(container.querySelector('.board-tab.is-active')?.textContent).toContain('Review');
      const focused = container.querySelector('.board-review-card.is-focused');
      expect(focused?.getAttribute('data-item-id')).toBe('2');
      expect(scrolled).toEqual(['2']);
      expect(document.activeElement).toBe(focused);
    } finally {
      HTMLElement.prototype.scrollIntoView = original;
    }
  });
});
