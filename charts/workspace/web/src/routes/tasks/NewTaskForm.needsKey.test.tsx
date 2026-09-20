import { render, screen, fireEvent, waitFor } from '@testing-library/preact';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { NewTaskForm } from './NewTaskForm';
import { createTask } from '../../store/tasks';

/**
 * "New build" with an installed-but-unauthenticated agent (#702).
 *
 * Two halves that must hold together: the entry is VISIBLE (hiding it made a
 * correct install look like a failed one) and it is NOT STARTABLE (the whole
 * point of the old gate — a keyless run dies on turn one with "Authentication
 * Fails"). Losing either half re-opens a bug.
 */

vi.mock('../../store/tasks', () => ({
  createTask: vi.fn(async () => ({ task_id: 'task-1', name: 'random-name-7' })),
  selectTask: vi.fn(),
}));

vi.mock('../../api/tasks', () => ({
  listWorkdirs: vi.fn(async () => []),
  listAssistants: vi.fn(async () => [
    { id: 'claude', label: 'Claude', default: true },
    {
      id: 'deepseek-harness',
      label: 'DeepSeek Harness',
      ready: false,
      needs: 'DEEPSEEK_API_KEY',
    },
  ]),
  renameTask: vi.fn(async () => ({})),
}));

const createTaskMock = vi.mocked(createTask);

function assistantSelect() {
  return screen.getByText('Assistant').parentElement!.querySelector(
    'select',
  ) as HTMLSelectElement;
}

async function pickDeepseek() {
  render(<NewTaskForm onClose={() => undefined} />);
  await waitFor(() => expect(assistantSelect().options.length).toBe(2));
  fireEvent.change(assistantSelect(), { target: { value: 'deepseek-harness' } });
  await waitFor(() => expect(assistantSelect().value).toBe('deepseek-harness'));
}

beforeEach(() => {
  localStorage.clear();
  createTaskMock.mockClear();
});

describe('NewTaskForm — assistant needs an API key (#702)', () => {
  it('lists the keyless agent and marks it', async () => {
    render(<NewTaskForm onClose={() => undefined} />);
    await waitFor(() => expect(assistantSelect().options.length).toBe(2));
    const dsh = [...assistantSelect().options].find(
      (o) => o.value === 'deepseek-harness',
    );
    expect(dsh!.textContent).toContain('needs API key');
  });

  it('names the missing key and links to provider settings once selected', async () => {
    await pickDeepseek();
    const note = screen.getByRole('alert');
    expect(note.textContent).toContain('DeepSeek Harness needs an API key');
    expect(note.textContent).toContain('DEEPSEEK_API_KEY');
    expect(note.querySelector('a')!.getAttribute('href')).toContain(
      '/settings/providers',
    );
  });

  it('disables Start build while the key is missing', async () => {
    await pickDeepseek();
    const button = screen.getByText(/Start build/).closest('button')!;
    expect(button.disabled).toBe(true);
  });

  it('refuses a submit that bypasses the disabled button', async () => {
    await pickDeepseek();
    const form = assistantSelect().closest('form')!;
    fireEvent.submit(form);
    await waitFor(() =>
      expect(screen.getAllByRole('alert')[0].textContent).toContain(
        'needs an API key',
      ),
    );
    expect(createTaskMock).not.toHaveBeenCalled();
  });

  it('starts normally on a ready assistant', async () => {
    render(<NewTaskForm onClose={() => undefined} />);
    await waitFor(() => expect(assistantSelect().options.length).toBe(2));
    expect(screen.queryByRole('alert')).toBeNull();
    fireEvent.click(screen.getByText(/Start build/));
    await waitFor(() => expect(createTaskMock).toHaveBeenCalled());
    expect(createTaskMock.mock.calls[0][0]).toMatchObject({ assistant: 'claude' });
  });
});
