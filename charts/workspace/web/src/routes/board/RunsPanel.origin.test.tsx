import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, cleanup } from '@testing-library/preact';
import { RunsPanel } from './RunsPanel';
import { selectedBoardId, boards, boardRuns, _resetRunFormsForTest } from '../../store/boards';
import { serverMode } from '../../store/server-mode';
import { startedByLabel, type BoardRunSummary } from '../../api/boards';

/**
 * A run a cron or webhook started says which one (#709 part 2): a stream of
 * trigger-started runs is otherwise indistinguishable from someone pressing
 * Start run, and "why did this run happen at 2am?" has no answer on screen.
 */

const realFetch = globalThis.fetch;

function mkRun(id: string, over: Partial<BoardRunSummary> = {}): BoardRunSummary {
  return {
    id, board_id: 'board-a', mode: 'propose', origin: 'manual', trigger: null,
    status: 'done', concurrency: 1, requested_concurrency: 1, clamp_reason: '',
    created_at: 1, updated_at: 1, finished_at: 2, error: '', listing_complete: true,
    truncation_reason: '', total: 1, done: 1, failed: 0, skipped: 0,
    counts: { pending: 0, claimed: 0, working: 0, done: 1, failed: 0, skipped: 0 },
    ...over,
  };
}

const RUNS = [
  mkRun('run-2-bbbb', { origin: 'cron', trigger: { type: 'cron', id: 'nightly-ai' } }),
  mkRun('run-1-aaaa'),
];

beforeEach(() => {
  localStorage.clear();
  _resetRunFormsForTest();
  serverMode.value = { readOnly: false, authed: true, authMode: 'basic', demoShowAll: false };
  boards.value = [{ id: 'board-a', vendor: 'github', display_name: 'board-a',
                    base_url: 'https://github.com', credential_ref: '@workspace-github',
                    credential_set: true }];
  selectedBoardId.value = 'board-a';
  boardRuns.value = { 'board-a': RUNS };
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => ({
    ok: true, status: 200, headers: { get: () => 'application/json' },
    json: async () => (String(input).includes('/runs')
      ? { runs: RUNS } : { strategies: {}, orders: [] }),
  })) as unknown as typeof fetch;
});

afterEach(() => {
  cleanup();
  globalThis.fetch = realFetch;
  selectedBoardId.value = null;
  boards.value = [];
  boardRuns.value = {};
});

describe('Runs tab — trigger-started runs', () => {
  it('names the trigger that started a run, and only that run', async () => {
    render(<RunsPanel />);
    expect(await screen.findByText('started by nightly-ai')).toBeInTheDocument();
    expect(screen.getAllByText(/^started by/)).toHaveLength(1);
  });

  it('startedByLabel is empty for a manual run', () => {
    expect(startedByLabel(mkRun('r'))).toBe('');
    expect(startedByLabel({ trigger: { type: 'webhook', id: 'gh-ai' } })).toBe('started by gh-ai');
  });
});
