import { render, screen, fireEvent, waitFor } from '@testing-library/preact';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { WorktreesSection } from './WorktreesSection';
import { ApiError } from '../../api/client';
import {
  listWorktrees,
  removeWorktree,
  sweepWorktrees,
  type WorktreeList,
  type WorktreeRow,
} from '../../api/worktrees';
import { serverMode } from '../../store/server-mode';

vi.mock('../../api/worktrees', () => ({
  listWorktrees: vi.fn(),
  removeWorktree: vi.fn(),
  sweepWorktrees: vi.fn(),
}));

const listMock = vi.mocked(listWorktrees);
const removeMock = vi.mocked(removeWorktree);
const sweepMock = vi.mocked(sweepWorktrees);

function row(over: Partial<WorktreeRow> = {}): WorktreeRow {
  return {
    repo: 'app', slug: 't-1', path: '/home/dev/.worktrees/app/t-1', branch: 'kc/t-1',
    port: 3101, source_root: '/home/dev/app', task_id: 't1', owner_name: 'fix login',
    owner_status: 'completed', live: false, created_by: 'server', created_at: 1,
    stat: null, keep_reason: '', ...over,
  };
}

function list(rows: WorktreeRow[], over: Partial<WorktreeList> = {}): WorktreeList {
  return {
    worktrees: rows, count: rows.length, max: 20, root: '/home/dev/.worktrees',
    available: true,
    sweep: { running: true, last_run_at: null, removed: 0, kept: 0, error: '' },
    ...over,
  };
}

beforeEach(() => {
  serverMode.value = { readOnly: false, authed: true, authMode: 'basic', demoShowAll: false };
  listMock.mockReset();
  removeMock.mockReset();
  sweepMock.mockReset();
});

describe('Settings → Worktrees (#701)', () => {
  it('lists every worktree with its owner, state and why it was kept', async () => {
    listMock.mockResolvedValue(list([
      row({ keep_reason: 'unpushed', stat: { files_changed: 2, insertions: 3, deletions: 1, ahead: 1, dirty: 0, untracked: 0, branch: 'kc/t-1', at: 1 } }),
      row({ slug: 't-2', path: '/p/2', branch: 'kc/t-2', task_id: 't2', owner_name: 'deploy', live: true, owner_status: 'running' }),
      row({ slug: 'manual', path: '/p/3', branch: 'kc/manual', task_id: '', owner_name: '' }),
    ]));
    render(<WorktreesSection />);
    expect(await screen.findByText('kc/t-1')).toBeInTheDocument();
    expect(screen.getByText('3 / 20')).toBeInTheDocument();
    expect(screen.getByText('finished')).toBeInTheDocument();
    expect(screen.getByText('running')).toBeInTheDocument();
    expect(screen.getByText('made by hand')).toBeInTheDocument();
    expect(screen.getByText('2 files changed (+3 −1)')).toBeInTheDocument();
    expect(screen.getByText(/kept: has commits not on any remote/)).toBeInTheDocument();
    expect(screen.getByText('fix login').getAttribute('href')).toMatch(/\/tasks\/t1\/changes$/);
    // A running Build's worktree cannot be removed from here.
    const removes = screen.getAllByText('Remove').map((el) => el.closest('button')!);
    expect(removes[1]).toBeDisabled();
    expect(removes[0]).not.toBeDisabled();
  });

  it('removes one, escalating to force only after the server says it is dirty', async () => {
    listMock.mockResolvedValue(list([row()]));
    removeMock
      .mockRejectedValueOnce(new ApiError('dirty', 409, { code: 'dirty' }))
      .mockResolvedValueOnce({ removed: true, branch: 'kc/t-1', branch_kept: true });
    render(<WorktreesSection />);
    fireEvent.click(await screen.findByText('Remove'));
    fireEvent.click(await screen.findByText('Remove worktree'));
    await waitFor(() => expect(removeMock).toHaveBeenCalledWith('app', 't-1', false));
    fireEvent.click(await screen.findByText('Discard and remove'));
    await waitFor(() => expect(removeMock).toHaveBeenLastCalledWith('app', 't-1', true));
    await waitFor(() => expect(listMock).toHaveBeenCalledTimes(2));
  });

  it('runs the cleanup on demand', async () => {
    listMock.mockResolvedValue(list([row()]));
    sweepMock.mockResolvedValue({ removed: [], kept: [], at: 1, dry_run: false });
    render(<WorktreesSection />);
    fireEvent.click(await screen.findByText('Clean up now', { selector: 'button' }));
    await waitFor(() => expect(sweepMock).toHaveBeenCalledWith());
  });

  it('explains itself when the server has no worktree support', async () => {
    listMock.mockResolvedValue(list([], { available: false }));
    render(<WorktreesSection />);
    expect(await screen.findByText(/not available in this workspace/)).toBeInTheDocument();
    expect(screen.queryByText('Clean up now', { selector: 'button' })).not.toBeInTheDocument();
  });
});
