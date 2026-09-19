import { render, screen, fireEvent, waitFor } from '@testing-library/preact';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { NewTaskForm } from './NewTaskForm';
import { createTask } from '../../store/tasks';
import { listWorkdirs, renameTask } from '../../api/tasks';
import { promptTemplates, saveTemplate } from '../../store/promptTemplates';

vi.mock('../../store/tasks', () => ({
  createTask: vi.fn(async () => ({ task_id: 'task-1', name: 'random-name-7' })),
  selectTask: vi.fn(),
}));

vi.mock('../../api/tasks', () => ({
  listWorkdirs: vi.fn(async () => []),
  listAssistants: vi.fn(async () => [{ id: 'claude', label: 'Claude', default: true }]),
  renameTask: vi.fn(async () => ({})),
}));

const createTaskMock = vi.mocked(createTask);
const renameTaskMock = vi.mocked(renameTask);
const listWorkdirsMock = vi.mocked(listWorkdirs);

function promptBox() {
  return screen.getByLabelText('First prompt') as HTMLTextAreaElement;
}

beforeEach(() => {
  localStorage.clear();
  promptTemplates.value = [];
  createTaskMock.mockClear();
  renameTaskMock.mockClear();
  listWorkdirsMock.mockReset();
  listWorkdirsMock.mockResolvedValue([]);
});

describe('NewTaskForm — seed prompt', () => {
  it('sends the typed prompt with the create call', async () => {
    render(<NewTaskForm onClose={() => undefined} />);
    fireEvent.input(promptBox(), { target: { value: '  Fix the flaky auth test  ' } });
    fireEvent.click(screen.getByText(/Start build/));

    await waitFor(() => expect(createTaskMock).toHaveBeenCalled());
    expect(createTaskMock.mock.calls[0][0]).toMatchObject({
      prompt: 'Fix the flaky auth test',
      workdir: '/home/dev',
    });
  });

  it('still allows an empty prompt (interactive REPL)', async () => {
    render(<NewTaskForm onClose={() => undefined} />);
    fireEvent.click(screen.getByText(/Start build/));

    await waitFor(() => expect(createTaskMock).toHaveBeenCalled());
    expect(createTaskMock.mock.calls[0][0].prompt).toBe('');
  });

  it('renames the new build and reports a failed rename', async () => {
    renameTaskMock.mockRejectedValueOnce(new Error('nope'));
    const onClose = vi.fn();
    render(<NewTaskForm onClose={onClose} />);
    fireEvent.click(screen.getByText(/Start build/));

    // The rename is awaited now, so onClose only runs after it settles —
    // a failure no longer leaves the user silently on the random name.
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(renameTaskMock).toHaveBeenCalledWith('task-1', expect.any(String));
  });
});

describe('NewTaskForm — saved prompt templates', () => {
  it('saves the current prompt as a template and fills it back in', async () => {
    render(<NewTaskForm onClose={() => undefined} />);
    expect(screen.getByText(/No templates yet/)).toBeInTheDocument();

    fireEvent.input(promptBox(), { target: { value: 'Run the test suite' } });
    fireEvent.click(screen.getByText('Save as template'));
    fireEvent.input(screen.getByLabelText('Template name'), { target: { value: 'Tests' } });
    fireEvent.click(screen.getByText('Save'));

    const chip = await screen.findByTitle('Run the test suite');
    expect(chip).toHaveTextContent('Tests');
    expect(promptTemplates.value).toHaveLength(1);

    // Clearing then clicking the chip refills the prompt.
    fireEvent.input(promptBox(), { target: { value: '' } });
    fireEvent.click(chip);
    await waitFor(() => expect(promptBox().value).toBe('Run the test suite'));
  });

  it('does not start a build when saving a template', async () => {
    render(<NewTaskForm onClose={() => undefined} />);
    fireEvent.input(promptBox(), { target: { value: 'Deploy staging' } });
    fireEvent.click(screen.getByText('Save as template'));
    fireEvent.click(screen.getByText('Save'));

    await waitFor(() => expect(promptTemplates.value).toHaveLength(1));
    expect(createTaskMock).not.toHaveBeenCalled();
  });

  it('lists existing templates and deletes one', async () => {
    saveTemplate('Nightly', 'run the nightly job');
    render(<NewTaskForm onClose={() => undefined} />);

    expect(screen.getByText('Nightly')).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText('Delete template Nightly'));
    await waitFor(() => expect(screen.queryByText('Nightly')).not.toBeInTheDocument());
    expect(promptTemplates.value).toHaveLength(0);
  });
});

describe('NewTaskForm — isolated worktree (#701)', () => {
  const DIRS = [
    { path: '/home/dev', label: 'home', is_git_repo: false },
    { path: '/home/dev/app', label: 'app', is_git_repo: true },
  ];

  function pickDir(path: string) {
    const select = document.querySelector('select.ntf-select') as HTMLSelectElement;
    fireEvent.change(select, { target: { value: path } });
  }

  it('labels git folders and offers isolation only for them', async () => {
    listWorkdirsMock.mockResolvedValue(DIRS);
    render(<NewTaskForm onClose={() => undefined} />);
    await screen.findByText(/app\s+\(git\)/);
    expect(screen.queryByLabelText('Isolated worktree')).not.toBeInTheDocument();
    pickDir('/home/dev/app');
    expect(await screen.findByLabelText('Isolated worktree')).toBeInTheDocument();
    pickDir('/home/dev');
    await waitFor(() => expect(screen.queryByLabelText('Isolated worktree')).not.toBeInTheDocument());
  });

  it('sends isolate and the base ref when ticked', async () => {
    listWorkdirsMock.mockResolvedValue(DIRS);
    render(<NewTaskForm onClose={() => undefined} />);
    await screen.findByText(/app\s+\(git\)/);
    pickDir('/home/dev/app');
    fireEvent.click(await screen.findByLabelText('Isolated worktree'));
    fireEvent.input(screen.getByLabelText('Branch from'), { target: { value: ' origin/main ' } });
    fireEvent.click(screen.getByText(/Start build/));

    await waitFor(() => expect(createTaskMock).toHaveBeenCalled());
    expect(createTaskMock.mock.calls[0][0]).toMatchObject({
      workdir: '/home/dev/app', isolate: true, base_ref: 'origin/main',
    });
  });

  it('does not send isolate for a folder that cannot be isolated', async () => {
    listWorkdirsMock.mockResolvedValue(DIRS);
    render(<NewTaskForm onClose={() => undefined} />);
    await screen.findByText(/app\s+\(git\)/);
    pickDir('/home/dev/app');
    fireEvent.click(await screen.findByLabelText('Isolated worktree'));
    pickDir('/home/dev');
    fireEvent.click(screen.getByText(/Start build/));
    await waitFor(() => expect(createTaskMock).toHaveBeenCalled());
    expect(createTaskMock.mock.calls[0][0]).not.toHaveProperty('isolate');
  });

  it("shows the server's reason inline when the build is refused", async () => {
    createTaskMock.mockImplementationOnce(async (_input, opts) => {
      opts?.onError?.('worktree limit reached (20/20). Remove finished worktrees in Settings → Worktrees');
      return null;
    });
    render(<NewTaskForm onClose={() => undefined} />);
    fireEvent.click(screen.getByLabelText('Isolated worktree'));
    fireEvent.click(screen.getByText(/Start build/));
    expect(await screen.findByRole('alert')).toHaveTextContent(/worktree limit reached/);
  });

  it('links to the in-app guide', async () => {
    render(<NewTaskForm onClose={() => undefined} />);
    const link = screen.getByText("What's this?") as HTMLAnchorElement;
    expect(link.getAttribute('href')).toMatch(/\/docs\/builds-worktrees$/);
  });
});
