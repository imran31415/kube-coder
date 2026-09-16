import { render, screen, fireEvent, waitFor } from '@testing-library/preact';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { NewTaskForm } from './NewTaskForm';
import { createTask } from '../../store/tasks';
import { listAssistants, renameTask } from '../../api/tasks';
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

function promptBox() {
  return screen.getByLabelText('First prompt') as HTMLTextAreaElement;
}

beforeEach(() => {
  localStorage.clear();
  promptTemplates.value = [];
  createTaskMock.mockClear();
  renameTaskMock.mockClear();
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

describe('NewTaskForm — an assistant that needs an API key (#702)', () => {
  const DSH = {
    id: 'deepseek-harness',
    label: 'DeepSeek Harness',
    ready: false,
    needs: ['DEEPSEEK_API_KEY'],
    notReadyReason:
      'DeepSeek Harness needs a DeepSeek API key. Add it in Settings → Provider API keys.',
  };
  const CLAUDE = { id: 'claude', label: 'Claude Code', default: true, ready: true };

  function assistantSelect() {
    return screen.getAllByRole('combobox').find((el) =>
      [...(el as HTMLSelectElement).options].some((o) => o.value === 'claude'),
    ) as HTMLSelectElement;
  }

  it('lists it with a marker but pre-selects a ready one', async () => {
    vi.mocked(listAssistants).mockResolvedValueOnce([CLAUDE, DSH]);
    render(<NewTaskForm onClose={() => undefined} />);
    await waitFor(() => expect(assistantSelect().value).toBe('claude'));
    const dshOption = [...assistantSelect().options].find((o) => o.value === DSH.id);
    expect(dshOption?.textContent).toContain('needs API key');
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('never pre-selects it, even when the server lists it first as default', async () => {
    vi.mocked(listAssistants).mockResolvedValueOnce([
      { ...DSH, default: true },
      { ...CLAUDE, default: false },
    ]);
    render(<NewTaskForm onClose={() => undefined} />);
    await waitFor(() => expect(assistantSelect().value).toBe('claude'));
  });

  it('shows the reason with a Settings link and blocks Start build when picked', async () => {
    vi.mocked(listAssistants).mockResolvedValueOnce([CLAUDE, DSH]);
    render(<NewTaskForm onClose={() => undefined} />);
    await waitFor(() => expect(assistantSelect().value).toBe('claude'));

    fireEvent.change(assistantSelect(), { target: { value: DSH.id } });

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('needs a DeepSeek API key');
    expect(screen.getByRole('link', { name: 'Open Provider API keys' })).toHaveAttribute(
      'href',
      '/settings/providers#providers',
    );
    const start = screen.getByText(/Start build/).closest('button') as HTMLButtonElement;
    expect(start.disabled).toBe(true);
    // The "starts right away" note would contradict the block, so it's hidden.
    expect(screen.queryByText(/terminal —|starts on this prompt/)).toBeNull();

    // Enter in a field submits the form even with the button disabled.
    fireEvent.submit(start.closest('form') as HTMLFormElement);
    expect(createTaskMock).not.toHaveBeenCalled();

    // Switching back to a ready agent clears the note and re-enables Start.
    fireEvent.change(assistantSelect(), { target: { value: 'claude' } });
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
    expect(start.disabled).toBe(false);
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
