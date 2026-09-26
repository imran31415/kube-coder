import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/preact';
import { RunsPanel } from './RunsPanel';
import {
  selectedBoardId,
  boards,
  boardRuns,
  runFormFor,
  _resetRunFormsForTest,
  _resetBoardsForTest,
} from '../../store/boards';
import { serverMode } from '../../store/server-mode';
import type { Board, BoardRunSummary } from '../../api/boards';

/**
 * Repository runs (#701): the Runs form can point agents at a git checkout,
 * isolation is on by default the moment one is picked, and turning it off
 * with more than one agent at once is said out loud before Start.
 */

const realFetch = globalThis.fetch;
let posted: Record<string, unknown>[] = [];
/** What GET …/runs answers — the panel refreshes the list on mount. */
let listedRuns: unknown[] = [];

function mkBoard(id: string): Board {
  return {
    id, vendor: 'github', display_name: id, base_url: 'https://github.com',
    credential_ref: '@board-creds/GITHUB_TOKEN', credential_set: true,
  };
}

beforeEach(() => {
  localStorage.clear();
  _resetRunFormsForTest();
  _resetBoardsForTest();
  serverMode.value = { readOnly: false, authed: true, authMode: 'basic', demoShowAll: false };
  boards.value = [mkBoard('board-a')];
  selectedBoardId.value = 'board-a';
  posted = [];
  listedRuns = [];
  globalThis.fetch = vi.fn(async (url: unknown, init?: RequestInit) => {
    const u = String(url);
    let body: unknown = { runs: listedRuns, strategies: {}, orders: [] };
    if (u.includes('/api/workspace/dirs')) {
      body = {
        dirs: [
          { path: '/home/dev', label: 'home', is_git_repo: false },
          { path: '/home/dev/app', label: 'app', is_git_repo: true },
        ],
      };
    } else if (init?.method === 'POST' && u.endsWith('/runs')) {
      posted.push(JSON.parse(String(init.body)));
      body = { id: 'run-1', board_id: 'board-a', items: {}, status: 'running' };
    }
    return {
      ok: true,
      status: 200,
      headers: { get: () => 'application/json' },
      json: async () => body,
      text: async () => JSON.stringify(body),
    } as unknown as Response;
  }) as unknown as typeof fetch;
});

afterEach(() => {
  cleanup();
  globalThis.fetch = realFetch;
  localStorage.clear();
  _resetRunFormsForTest();
  selectedBoardId.value = null;
  boards.value = [];
});

function repoSelect() {
  return screen.getByLabelText('Repository') as HTMLSelectElement;
}
function atOnce() {
  return screen.getByLabelText('At once') as HTMLInputElement;
}

describe('RunsPanel — repository runs (#701)', () => {
  it('lists only git folders and defaults to a tracker-only run', async () => {
    render(<RunsPanel />);
    await screen.findByText('app');
    const options = Array.from(repoSelect().options).map((o) => o.textContent);
    expect(options).toEqual(['None — tracker only', 'app']);
    expect(repoSelect().value).toBe('');
    expect(screen.queryByLabelText('Isolated worktree per item')).not.toBeInTheDocument();
  });

  it('turns isolation ON when a repository is picked, and remembers it', async () => {
    render(<RunsPanel />);
    await screen.findByText('app');
    fireEvent.input(repoSelect(), { target: { value: '/home/dev/app' } });
    const box = await screen.findByLabelText('Isolated worktree per item') as HTMLInputElement;
    expect(box.checked).toBe(true);
    expect(runFormFor('board-a')).toMatchObject({ workdir: '/home/dev/app', isolate: true });
  });

  it('warns when several agents would share one checkout', async () => {
    render(<RunsPanel />);
    await screen.findByText('app');
    fireEvent.input(repoSelect(), { target: { value: '/home/dev/app' } });
    fireEvent.input(atOnce(), { target: { value: '3' } });
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText('Isolated worktree per item'));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      /3 agents will work in the same checkout/);
    fireEvent.input(atOnce(), { target: { value: '1' } });
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
  });

  it('sends the repository and isolation with the run', async () => {
    render(<RunsPanel />);
    await screen.findByText('app');
    fireEvent.input(repoSelect(), { target: { value: '/home/dev/app' } });
    fireEvent.click(screen.getByText('Start run'));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({ workdir: '/home/dev/app', isolate: true });
  });

  it('a tracker-only run sends neither', async () => {
    render(<RunsPanel />);
    await screen.findByText('app');
    fireEvent.click(screen.getByText('Start run'));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).not.toHaveProperty('workdir');
    expect(posted[0]).not.toHaveProperty('isolate');
  });

  it('marks isolated and shared-tree runs in the list', async () => {
    const base = {
      board_id: 'board-a', mode: 'propose', status: 'done', concurrency: 2,
      requested_concurrency: 2, clamp_reason: '', created_at: 1, updated_at: 1,
      finished_at: 2, error: '', listing_complete: true, truncation_reason: '',
      total: 2, counts: { pending: 0, claimed: 0, working: 0, done: 2, failed: 0, skipped: 0 },
      done: 2, failed: 0, skipped: 0,
    } as const;
    listedRuns = [
      { ...base, id: 'run-iso', workdir: '/home/dev/app', isolate: true, warnings: [] },
      { ...base, id: 'run-shared', workdir: '/home/dev/app', isolate: false, warnings: ['shared_tree'] },
    ] as BoardRunSummary[];
    boardRuns.value = { 'board-a': listedRuns as BoardRunSummary[] };
    render(<RunsPanel />);
    expect(await screen.findByText('isolated')).toBeInTheDocument();
    expect(screen.getByText('shared tree')).toBeInTheDocument();
  });
});

describe('Runs form storage — repository fields (#701)', () => {
  function stored(form: Record<string, unknown>) {
    localStorage.setItem('kc.boardRunForm', JSON.stringify({ 'board-a': form }));
    _resetRunFormsForTest();
    return runFormFor('board-a');
  }

  it('keeps a valid repository and the isolation choice', () => {
    expect(stored({ workdir: '/home/dev/app/web', isolate: false }))
      .toMatchObject({ workdir: '/home/dev/app/web', isolate: false });
  });

  it('drops anything that is not a path inside /home/dev', () => {
    for (const workdir of ['/etc', '/home/devious', 'relative', '/home/dev/../etc', 42, null]) {
      expect(stored({ workdir }).workdir).toBe('');
    }
  });

  it('defaults isolation to on, and refuses a non-boolean', () => {
    expect(stored({}).isolate).toBe(true);
    expect(stored({ isolate: 'no' }).isolate).toBe(true);
  });
});
