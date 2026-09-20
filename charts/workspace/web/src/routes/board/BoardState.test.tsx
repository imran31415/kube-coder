import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, cleanup, act } from '@testing-library/preact';
import { BoardRoute } from './index';
import { RunsPanel } from './RunsPanel';
import {
  boardRuns,
  boards,
  boardStanding,
  restoreBoardSelection,
  selectBoard,
  selectedBoardId,
  reviewBoardId,
  reviewGroups,
  _resetBoardsForTest,
  _resetRunFormsForTest,
} from '../../store/boards';
import { serverMode } from '../../store/server-mode';
import type {
  Board,
  BoardRunSummary,
  RunItemState,
  StagedRecord,
} from '../../api/boards';

/**
 * Three of the four Board complaints in #712, on the dashboard side:
 *
 * - arriving at /board opened a picker ("Select a board") instead of the board
 *   you were last on;
 * - nothing on the page said what the board as a whole was doing;
 * - Start run stayed enabled while a run was in flight, and a second run can
 *   only skip whatever the first one holds.
 *
 * (The fourth — where a board notification lands — is server-side: see
 * `charts/workspace/tests/boards_state_test.py`.)
 */

const realFetch = globalThis.fetch;

function mkBoard(over: Partial<Board> = {}): Board {
  return {
    id: 'acme-jira',
    vendor: 'jira',
    display_name: 'Acme — Support',
    base_url: 'https://acme.atlassian.net',
    credential_ref: '@board-creds/JIRA',
    credential_set: true,
    ...over,
  };
}

function mkSummary(over: Partial<BoardRunSummary> = {}): BoardRunSummary {
  const counts: Record<RunItemState, number> = {
    pending: 1, claimed: 0, working: 1, done: 1, failed: 0, skipped: 0,
  };
  return {
    id: 'run-1700000000-abcd',
    board_id: 'acme-jira',
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
    total: 3,
    counts,
    done: 1,
    failed: 0,
    skipped: 0,
    ...over,
  };
}

function mkStaged(over: Partial<StagedRecord> = {}): StagedRecord {
  return {
    board_id: 'acme-jira',
    item_id: '46',
    item_key: 'SUP-46',
    item_title: 'Refund not received',
    item_url: '',
    content_hash: 'h',
    state: 'pending',
    disposition: 'needs_review',
    reason: 'wants a human',
    evidence: {},
    actions: [],
    pending_actions: [],
    open: true,
    decided_by: '',
    run_id: '',
    task_id: '',
    created_at: 1,
    updated_at: 1,
    ...over,
  } as StagedRecord;
}

function writable() {
  serverMode.value = {
    readOnly: false, authed: true, authMode: 'basic', demoShowAll: false,
  };
}

/** Let the mount effect's fetches — and the renders they cause — finish. */
async function settle() {
  await act(async () => {
    for (let i = 0; i < 10; i++) await Promise.resolve();
  });
}

beforeEach(() => {
  window.history.replaceState({}, '', '/board');
  localStorage.clear();
  _resetBoardsForTest();
  _resetRunFormsForTest();
  writable();
  globalThis.fetch = vi.fn(async () => ({
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: async () => ({ items: [], runs: [], strategies: {}, orders: [], groups: [] }),
    text: async () => '{}',
  })) as unknown as typeof fetch;
});

afterEach(() => {
  cleanup();
  globalThis.fetch = realFetch;
  localStorage.clear();
  _resetBoardsForTest();
  _resetRunFormsForTest();
  vi.restoreAllMocks();
});

describe('/board remembers which board you were on', () => {
  it('reopens the board the last visit left on', async () => {
    boards.value = [mkBoard(), mkBoard({ id: 'kube-coder-gh' })];
    await selectBoard('kube-coder-gh');

    // A fresh page load: the selection is gone, the preference is not.
    selectedBoardId.value = null;
    await restoreBoardSelection();
    expect(selectedBoardId.value).toBe('kube-coder-gh');
  });

  it('falls back to the first board rather than showing a picker', async () => {
    boards.value = [mkBoard(), mkBoard({ id: 'kube-coder-gh' })];
    await restoreBoardSelection();
    expect(selectedBoardId.value).toBe('acme-jira');
  });

  it('falls back when the remembered board has been disconnected', async () => {
    boards.value = [mkBoard(), mkBoard({ id: 'kube-coder-gh' })];
    await selectBoard('kube-coder-gh');
    selectedBoardId.value = null;
    boards.value = [mkBoard()]; // the other one was removed meanwhile
    await restoreBoardSelection();
    expect(selectedBoardId.value).toBe('acme-jira');
  });

  it('never overrides a deep link that already selected a board', async () => {
    // /board?board=…&review=… is the waiting badge's landing. The remembered
    // board must not steal that arrival.
    boards.value = [mkBoard(), mkBoard({ id: 'kube-coder-gh' })];
    await selectBoard('kube-coder-gh');
    await selectBoard('acme-jira');
    await restoreBoardSelection();
    expect(selectedBoardId.value).toBe('acme-jira');
  });

  it('does nothing at all when no boards are connected', async () => {
    boards.value = [];
    await restoreBoardSelection();
    expect(selectedBoardId.value).toBeNull();
  });
});

describe('the board names its overall state', () => {
  function seed(over: Partial<Board> = {}) {
    boards.value = [mkBoard(over)];
    selectedBoardId.value = 'acme-jira';
  }

  it('is "Not run yet" on a board nobody has worked', () => {
    seed();
    expect(boardStanding.value.state).toBe('never_run');
    expect(boardStanding.value.stateLabel).toBe('Not run yet');
  });

  it('is "Runs in progress" while a run is in flight', () => {
    seed();
    boardRuns.value = { 'acme-jira': [mkSummary()] };
    expect(boardStanding.value.state).toBe('running');
    expect(boardStanding.value.stateLabel).toBe('Runs in progress');
  });

  it('is "Waiting on you" once a decision is owed and nothing is running', () => {
    seed();
    boardRuns.value = { 'acme-jira': [mkSummary({ status: 'done' })] };
    reviewBoardId.value = 'acme-jira';
    reviewGroups.value = [
      { disposition: 'needs_review', count: 1, open: 1, items: [mkStaged()] },
    ];
    expect(boardStanding.value.state).toBe('awaiting_human');
    expect(boardStanding.value.stateLabel).toBe('Waiting on you');
  });

  it('puts a missing credential ahead of everything else', () => {
    // Nothing else on the page is actionable until it is fixed.
    seed({ credential_set: false });
    boardRuns.value = { 'acme-jira': [mkSummary()] };
    expect(boardStanding.value.state).toBe('needs_credential');
  });
});

describe('a run in flight locks the run form', () => {
  function seedLive() {
    boards.value = [mkBoard()];
    selectedBoardId.value = 'acme-jira';
    boardRuns.value = { 'acme-jira': [mkSummary()] };
  }

  it('disables Start run and says why', () => {
    seedLive();
    render(<RunsPanel />);
    const btn = screen.getByRole('button', { name: /run in progress/i });
    expect(btn).toBeDisabled();
    // The reason is on the page, not only in the tooltip: a greyed button
    // with no explanation reads as a broken page.
    expect(btn).toHaveAttribute('title', expect.stringContaining('already in flight'));
    expect(screen.getByText(/only skip the items this one holds/i)).toBeTruthy();
  });

  it('does not lock on a run whose items have all settled', () => {
    // A run record left at `running` — its last item done, or its process
    // dead until the boot sweep calls it `interrupted` — would otherwise grey
    // the button out forever on a board the server would happily run.
    boards.value = [mkBoard()];
    selectedBoardId.value = 'acme-jira';
    boardRuns.value = {
      'acme-jira': [
        mkSummary({
          status: 'running',
          counts: {
            pending: 0, claimed: 0, working: 0, done: 3, failed: 0, skipped: 0,
          },
          done: 3,
        }),
      ],
    };
    render(<RunsPanel />);
    expect(screen.getByRole('button', { name: 'Start run' })).toBeEnabled();
  });

  it('leaves Start run available once the run has finished', () => {
    boards.value = [mkBoard()];
    selectedBoardId.value = 'acme-jira';
    boardRuns.value = { 'acme-jira': [mkSummary({ status: 'done' })] };
    render(<RunsPanel />);
    expect(screen.getByRole('button', { name: 'Start run' })).toBeEnabled();
  });
});


describe('arriving at /board', () => {
  function serve(list: Board[]) {
    globalThis.fetch = vi.fn((input: unknown) => {
      const path = String(input).split('?')[0];
      const body = /\/api\/boards$/.test(path)
        ? { boards: list }
        : {
            items: [], complete: true, truncation_reason: '',
            pages_fetched: 1, runs: [], groups: [], strategies: {},
            orders: [], credentials: [],
          };
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: { get: () => 'application/json' },
        json: async () => body,
        text: async () => JSON.stringify(body),
      });
    }) as unknown as typeof fetch;
  }

  it('opens the remembered board rather than "Select a board"', async () => {
    const list = [mkBoard(), mkBoard({ id: 'kube-coder-gh' })];
    serve(list);
    localStorage.setItem('kc.boardSelected', 'kube-coder-gh');

    render(<BoardRoute />);
    await settle();

    expect(selectedBoardId.value).toBe('kube-coder-gh');
    expect(screen.queryByText(/Select a board/i)).toBeNull();
  });

  it('leaves a ?board= deep link in charge of where it lands', async () => {
    const list = [mkBoard(), mkBoard({ id: 'kube-coder-gh' })];
    serve(list);
    localStorage.setItem('kc.boardSelected', 'kube-coder-gh');
    window.history.replaceState({}, '', '/board?board=acme-jira');

    render(<BoardRoute />);
    await settle();

    expect(selectedBoardId.value).toBe('acme-jira');
  });

  it('names the board state beside the title, with the sentence as its tooltip', async () => {
    serve([mkBoard()]);
    const { container } = render(<BoardRoute />);
    await settle();

    const badgeEl = container.querySelector('.board-state-badge');
    expect(badgeEl?.textContent).toContain('Not run yet');
    expect(badgeEl?.getAttribute('title')).toBeTruthy();
  });
});
