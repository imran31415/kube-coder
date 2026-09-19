import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, cleanup, within } from '@testing-library/preact';
import { RunsPanel } from './RunsPanel';
import { ReviewPanel } from './ReviewPanel';
import {
  activeRun,
  boardRuns,
  boards,
  decisionPending,
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
  RunItemState,
  StagedRecord,
} from '../../api/boards';

/**
 * What a run and a decision look like while they are happening.
 *
 * The maintainer's report was that the board's state was hard to read: a run
 * item's state rendered as its raw enum in the same ink as everything around
 * it, items came out in map-insertion order so finished work sat between
 * running work, and a decision gave no feedback at all until two round trips
 * later. These tests pin the parts of that a screenshot would catch.
 */

const realFetch = globalThis.fetch;

function writable() {
  serverMode.value = {
    readOnly: false, authed: true, authMode: 'basic', demoShowAll: false,
  };
}

function mkBoard(): Board {
  return {
    id: 'b1',
    vendor: 'github',
    display_name: 'acme/demo',
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
    title: `work ${key}`,
    url: '',
    content_hash: 'h',
    state,
    lease_owner: '',
    task_id: '',
    disposition: null,
    reason: '',
    error: '',
    writes_used: 0,
    updated_at: Math.floor(Date.now() / 1000) - 42,
    ...over,
  };
}

function mkSummary(over: Partial<BoardRunSummary> = {}): BoardRunSummary {
  return {
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
    total: 4,
    counts: {
      pending: 1, claimed: 0, working: 1, done: 1, failed: 1, skipped: 0,
    },
    done: 1,
    failed: 1,
    skipped: 0,
    ...over,
  };
}

/** A run whose items deliberately arrive in the WRONG order: done first. */
function seedRun() {
  const summary = mkSummary();
  boards.value = [mkBoard()];
  selectedBoardId.value = 'b1';
  boardRuns.value = { b1: [summary] };
  activeRun.value = {
    ...summary,
    select: {},
    stop_on: {},
    stop_requested: false,
    consecutive_failures: 0,
    items: {
      '#4': mkRunItem('#4', 'done', { disposition: 'completed' }),
      '#1': mkRunItem('#1', 'pending'),
      '#3': mkRunItem('#3', 'failed', { error: 'the agent ran out of turns' }),
      '#2': mkRunItem('#2', 'working'),
    },
  } as BoardRun;
}

beforeEach(() => {
  localStorage.clear();
  _resetBoardsForTest();
  _resetRunFormsForTest();
  writable();
  globalThis.fetch = vi.fn(async () => ({
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: async () => ({ runs: [], strategies: {}, orders: [], groups: [] }),
  })) as unknown as typeof fetch;
});

afterEach(() => {
  cleanup();
  globalThis.fetch = realFetch;
  localStorage.clear();
  _resetBoardsForTest();
  _resetRunFormsForTest();
});

describe('a run in flight is readable at a glance', () => {
  it('puts live work at the top, whatever order the map arrived in', () => {
    /* Object.values() is insertion order, which interleaves finished items
       between running ones — so "what is happening right now" had to be found
       by reading every row. */
    seedRun();
    render(<RunsPanel />);
    const rows = screen.getAllByRole('row').slice(1); // drop the header row
    const keys = rows.map((r) => r.querySelector('td')?.textContent);
    expect(keys).toEqual(['#2', '#1', '#3', '#4']);
  });

  it('spells each state out instead of printing the wire enum', () => {
    seedRun();
    const { container } = render(<RunsPanel />);
    // Scoped to the pills: the tally above the table uses the same words.
    const pills = Array.from(
      container.querySelectorAll('.board-state-pill'),
    ).map((p) => p.textContent);
    expect(pills).toContain('working');
    expect(pills).toContain('queued'); // not "pending"
    expect(pills).toContain('done');
    expect(pills).not.toContain('pending');
  });

  it('marks the working row so it is not one more line of the same ink', () => {
    seedRun();
    const { container } = render(<RunsPanel />);
    const working = container.querySelector('.board-run-item-working');
    expect(working).toBeTruthy();
    expect(within(working as HTMLElement).getByText('working')).toBeTruthy();
    expect((working as HTMLElement).querySelector('.board-spinner')).toBeTruthy();
  });

  it('shows a per-state tally, so "4/9" is not the only thing said', () => {
    seedRun();
    const { container } = render(<RunsPanel />);
    const tally = container.querySelector('.board-run-tally');
    expect(tally).toBeTruthy();
    const text = (tally as HTMLElement).textContent ?? '';
    expect(text).toContain('working');
    expect(text).toContain('queued');
    expect(text).toContain('failed');
  });

  it('renders a disposition as prose, never as needs_rescoping', () => {
    seedRun();
    activeRun.value = {
      ...(activeRun.value as BoardRun),
      items: {
        '#5': mkRunItem('#5', 'done', { disposition: 'needs_rescoping' }),
      },
    } as BoardRun;
    render(<RunsPanel />);
    expect(screen.getByText('needs rescoping')).toBeTruthy();
    expect(screen.queryByText('needs_rescoping')).toBeNull();
  });

  it('shows no timer on any row, and runs none behind the table (#704)', () => {
    // The only clock a row had was `updated_at`, which resets on every change
    // to the row — it measured nothing a reader could use.
    vi.useFakeTimers();
    try {
      seedRun();
      const { container } = render(<RunsPanel />);
      expect(container.querySelector('.board-run-item-working')).toBeTruthy();
      expect(container.querySelector('.board-run-item-elapsed')).toBeNull();
      // No per-second re-render ticking while work is live.
      expect(vi.getTimerCount()).toBe(0);
      // Nothing that reads as a duration next to any state pill.
      const stateCells = Array.from(container.querySelectorAll('tbody tr'))
        .map((row) => row.querySelectorAll('td')[2]?.textContent ?? '');
      for (const text of stateCells) {
        expect(text).not.toMatch(/\d+\s*[smh]\b/);
      }
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('a decision says what it is doing', () => {
  function mkRecord(over: Partial<StagedRecord> = {}): StagedRecord {
    const actions = [
      {
        id: 'a1',
        action: 'comment',
        params: { body: 'Fixed in #12.' },
        preview: 'Fixed in #12.',
        writes: 1,
        state: 'pending' as const,
      },
    ];
    return {
      board_id: 'b1',
      item_id: '46',
      item_key: 'SUP-5',
      item_title: 'Refund not received',
      item_url: '',
      content_hash: 'h',
      run_id: 'run-1',
      state: 'pending',
      disposition: 'needs_review',
      reason: 'looks right',
      evidence: {},
      actions,
      pending_actions: actions,
      open: true,
      decided_by: '',
      result: null,
      created_at: 1,
      updated_at: 1,
      ...over,
    };
  }

  function seedCard(record = mkRecord()) {
    boards.value = [mkBoard()];
    selectedBoardId.value = 'b1';
    reviewGroups.value = [
      { disposition: 'needs_review', count: 1, items: [record] },
    ];
  }

  it('names the verb it is waiting on, not just a disabled button', () => {
    /* Approving is a real call to the vendor and can take seconds. A card
       that only greyed out was indistinguishable from a wedged page. */
    seedCard();
    decisionPending.value = { '46': 'approve' };
    render(<ReviewPanel />);
    expect(screen.getByText('Approving…')).toBeTruthy();
    expect(screen.queryByText('Approve')).toBeNull();
  });

  it('marks the card busy for assistive tech too', () => {
    seedCard();
    decisionPending.value = { '46': 'send_back' };
    const { container } = render(<ReviewPanel />);
    const card = container.querySelector('.board-review-card');
    expect(card?.getAttribute('aria-busy')).toBe('true');
    expect(screen.getByText('Sending back…')).toBeTruthy();
  });

  it('renders object evidence as JSON, not [object Object]', () => {
    seedCard(mkRecord({ evidence: { files: { changed: 2 }, tests: '18 passed' } }));
    render(<ReviewPanel />);
    expect(screen.queryByText('[object Object]')).toBeNull();
    expect(screen.getByText('{"changed":2}')).toBeTruthy();
    expect(screen.getByText('18 passed')).toBeTruthy();
  });

  it('shows a settled card what it was decided as', () => {
    seedCard(
      mkRecord({
        open: false,
        state: 'approved',
        pending_actions: [],
        decided_by: 'raman',
      }),
    );
    const { container } = render(<ReviewPanel />);
    const pill = container.querySelector('.board-decided-approved');
    expect(pill?.textContent).toBe('approved');
  });

  it('titles the group in prose', () => {
    seedCard();
    reviewGroups.value = [
      { disposition: 'needs_rescoping', count: 1, items: [mkRecord()] },
    ];
    render(<ReviewPanel />);
    expect(screen.getByText(/needs rescoping/)).toBeTruthy();
  });
});

describe('the standing strip tells you where you are', () => {
  it('offers a button to the tab that is owed work, and none once you are there', async () => {
    const { BoardRoute } = await import('./index');
    boards.value = [mkBoard()];
    selectedBoardId.value = 'b1';
    const groups = [
      {
        disposition: 'needs_review',
        count: 1,
        items: [
          {
            item_id: '46', item_key: 'SUP-5', item_title: 't', item_url: '',
            board_id: 'b1', content_hash: 'h', run_id: 'r', state: 'pending',
            disposition: 'needs_review', reason: '', evidence: {},
            actions: [], pending_actions: [], open: true, decided_by: '',
            result: null, created_at: 1, updated_at: 1,
          } as StagedRecord,
        ],
      },
    ];
    // The route reads the queue itself on arrival (#704), so it has to come
    // from the server rather than be planted in the store.
    globalThis.fetch = vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: { get: () => 'application/json' },
      json: async () => ({
        boards: [mkBoard()], runs: [], strategies: {}, orders: [], groups,
      }),
    })) as unknown as typeof fetch;

    const { container } = render(<BoardRoute />);
    // Items tab is the landing tab, so the strip should point at Review.
    expect(await screen.findByText(/waiting on your decision/i)).toBeTruthy();
    const go = container.querySelector('.board-standing-go') as HTMLButtonElement;
    expect(go?.textContent).toBe('Open Review');

    go.click();
    await new Promise((r) => setTimeout(r, 0));
    // Now that we are on Review, "open Review" would be noise dressed as advice.
    expect(container.querySelector('.board-standing-go')).toBeNull();
  });
});
