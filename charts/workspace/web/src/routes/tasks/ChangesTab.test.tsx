import { render, screen, fireEvent, waitFor } from '@testing-library/preact';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ChangesTab } from './ChangesTab';
import { ApiError } from '../../api/client';
import {
  getTaskWorktree,
  getTaskWorktreeDiff,
  removeTaskWorktree,
  type TaskWorktreeView,
} from '../../api/tasks';
import { serverMode } from '../../store/server-mode';

vi.mock('../../api/tasks', () => ({
  getTaskWorktree: vi.fn(),
  getTaskWorktreeDiff: vi.fn(),
  removeTaskWorktree: vi.fn(),
}));

const getMock = vi.mocked(getTaskWorktree);
const diffMock = vi.mocked(getTaskWorktreeDiff);
const removeMock = vi.mocked(removeTaskWorktree);

function view(over: Partial<TaskWorktreeView> = {}): TaskWorktreeView {
  return {
    task_id: 't1',
    worktree: {
      path: '/home/dev/.worktrees/app/t-1', slug: 't-1', branch: 'kc/t-1', port: 3101,
      repo_root: '/home/dev/app', repo_key: 'app', source_workdir: '/home/dev/app', subdir: '',
      base_ref: 'main', base_sha: 'abcdef0123456789', created_at: 1, removed_at: null,
    },
    exists: true, repo_exists: true, branch_exists: true,
    owner_task_id: 't1', live: false,
    status: {
      branch: 'kc/t-1', head_sha: 'f00', detached: false, ahead: 1, behind: 2,
      dirty: 1, untracked: 1, files_changed: 2, insertions: 5, deletions: 1,
      files: [
        { path: 'src/app.ts', status: 'M', added: 5, deleted: 1, binary: false, uncommitted: true },
        { path: 'notes.md', status: '?', added: null, deleted: null, binary: false, uncommitted: true },
      ],
      truncated: false, base_known: true, computed_at: 2,
    },
    status_error: '',
    push_remote: 'fork',
    push_command: 'git -C /home/dev/.worktrees/app/t-1 push -u fork kc/t-1',
    remove_blocked: 'dirty',
    ...over,
  };
}

beforeEach(() => {
  serverMode.value = { readOnly: false, authed: true, authMode: 'basic', demoShowAll: false };
  getMock.mockReset();
  diffMock.mockReset();
  removeMock.mockReset();
});

describe('ChangesTab (#701)', () => {
  it('shows the branch, the base, the counts and every changed file', async () => {
    getMock.mockResolvedValue(view());
    render(<ChangesTab taskId="t1" live={false} />);
    expect(await screen.findByText('⎇ kc/t-1')).toBeInTheDocument();
    expect(screen.getByText(/from main/)).toHaveTextContent('from main @ abcdef0');
    expect(screen.getByText('2 files changed (+5 −1)')).toBeInTheDocument();
    expect(screen.getByText('1 commit')).toBeInTheDocument();
    expect(screen.getByText('2 behind main')).toBeInTheDocument();
    expect(screen.getByText('2 uncommitted')).toBeInTheDocument();
    expect(screen.getByText(':3101')).toBeInTheDocument();
    expect(screen.getByText('src/app.ts')).toBeInTheDocument();
    expect(screen.getByText('notes.md')).toBeInTheDocument();
  });

  it('opens one file\'s diff on click, tinting added and removed lines', async () => {
    getMock.mockResolvedValue(view());
    diffMock.mockResolvedValue({
      file: 'src/app.ts', status: 'M', binary: false, truncated: false,
      diff: '--- a/src/app.ts\n+++ b/src/app.ts\n@@ -1 +1 @@\n-old\n+new',
    });
    render(<ChangesTab taskId="t1" live={false} />);
    fireEvent.click(await screen.findByText('src/app.ts'));
    await waitFor(() => expect(diffMock).toHaveBeenCalledWith('t1', 'src/app.ts'));
    expect((await screen.findByText('+new')).className).toBe('wt-l-add');
    expect(screen.getByText('-old').className).toBe('wt-l-del');
    // Clicking again folds it away without another request.
    fireEvent.click(screen.getByText('src/app.ts'));
    await waitFor(() => expect(screen.queryByText('+new')).not.toBeInTheDocument());
    expect(diffMock).toHaveBeenCalledTimes(1);
  });

  it('copies the push command', async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    getMock.mockResolvedValue(view());
    render(<ChangesTab taskId="t1" live={false} />);
    fireEvent.click(await screen.findByLabelText('Copy push command'));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(
      'git -C /home/dev/.worktrees/app/t-1 push -u fork kc/t-1'));
    expect(await screen.findByText(/Copied/)).toBeInTheDocument();
  });

  it('asks before discarding uncommitted work, then removes with force', async () => {
    getMock.mockResolvedValue(view());
    removeMock.mockResolvedValue({ removed: true, branch: 'kc/t-1', branch_kept: true });
    render(<ChangesTab taskId="t1" live={false} />);
    fireEvent.click(await screen.findByText(/Remove worktree/));
    // A dirty worktree goes straight to the cost-spelled-out dialog.
    expect(await screen.findByText('Discard uncommitted changes?')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Discard and remove'));
    await waitFor(() => expect(removeMock).toHaveBeenCalledWith('t1', true));
  });

  it('escalates to the force dialog when the server says the tree became dirty', async () => {
    getMock.mockResolvedValue(view({ remove_blocked: '' }));
    removeMock
      .mockRejectedValueOnce(new ApiError('dirty', 409, { code: 'dirty', dirty: 1, untracked: 0 }))
      .mockResolvedValueOnce({ removed: true, branch: 'kc/t-1', branch_kept: true });
    render(<ChangesTab taskId="t1" live={false} />);
    fireEvent.click(await screen.findByText(/Remove worktree/));
    expect(await screen.findByText('Remove this worktree?')).toBeInTheDocument();
    // The dialog's confirm button carries the same label as the trigger.
    const buttons = screen.getAllByText('Remove worktree');
    fireEvent.click(buttons[buttons.length - 1]);
    await waitFor(() => expect(removeMock).toHaveBeenCalledWith('t1', false));
    fireEvent.click(await screen.findByText('Discard and remove'));
    await waitFor(() => expect(removeMock).toHaveBeenLastCalledWith('t1', true));
  });

  it('will not remove the worktree of a running Build', async () => {
    getMock.mockResolvedValue(view({ live: true, remove_blocked: 'live' }));
    render(<ChangesTab taskId="t1" live />);
    const btn = (await screen.findByText(/Remove worktree/)).closest('button')!;
    expect(btn).toBeDisabled();
    expect(screen.getByText('Stop the Build before removing its worktree.')).toBeInTheDocument();
  });

  it('says so when the worktree is already gone', async () => {
    getMock.mockResolvedValue(view({ exists: false, status: null, remove_blocked: 'removed' }));
    render(<ChangesTab taskId="t1" live={false} />);
    expect(await screen.findByRole('status')).toHaveTextContent(
      'This worktree has been removed. Its branch kc/t-1 and its commits are kept.');
    expect(screen.queryByText(/Remove worktree/)).not.toBeInTheDocument();
  });

  it('reports a load failure with a retry', async () => {
    getMock.mockRejectedValueOnce(new ApiError('this Build does not run in an isolated worktree', 404, null));
    getMock.mockResolvedValueOnce(view());
    render(<ChangesTab taskId="t1" live={false} />);
    expect(await screen.findByRole('alert')).toHaveTextContent(/does not run in an isolated worktree/);
    fireEvent.click(screen.getByText('Try again'));
    expect(await screen.findByText('⎇ kc/t-1')).toBeInTheDocument();
    expect(getMock).toHaveBeenLastCalledWith('t1', true);
  });
});
