import { render, screen, within } from '@testing-library/preact';
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { TaskList } from './TaskList';
import {
  tasks,
  selectedTaskId,
  taskFilter,
  tasksError,
  selectTask,
  stopTaskPolling,
} from '../../store/tasks';
import type { TaskSummary } from '../../api/tasks';

const sample: TaskSummary[] = [
  {
    task_id: '1778896823-f60960f6', name: 'aa', prompt: 'Refactor auth',
    status: 'running', created_at: Math.floor(Date.now() / 1000) - 60,
    finished_at: null, source: null, kind: 'claude',
    memory_injected: [{ namespace: 'a', key: 'b' }, { namespace: 'c', key: 'd' }, { namespace: 'e', key: 'f' }],
    memory_injection_disabled: false,
  },
  {
    task_id: '1778896038-dc9222e2', name: null, prompt: 'login to reddit with playwrite mcp for me',
    status: 'killed', created_at: Math.floor(Date.now() / 1000) - 3600,
    finished_at: null, source: 'manual', kind: 'claude',
    memory_injected: [], memory_injection_disabled: false,
  },
];

const realFetch = globalThis.fetch;

beforeEach(() => {
  tasks.value = sample;
  selectedTaskId.value = null;
  taskFilter.value = '';
  tasksError.value = null;
  globalThis.fetch = vi.fn(async () => ({
    ok: true, status: 200,
    headers: { get: () => 'application/json' },
    json: async () => ({ tasks: sample }),
  })) as unknown as typeof fetch;
});
afterEach(() => {
  stopTaskPolling();
  globalThis.fetch = realFetch;
});

describe('TaskList', () => {
  it('renders one row per task with status pills', () => {
    render(<TaskList />);
    expect(screen.getByText('aa')).toBeInTheDocument();
    expect(screen.getByText('login to reddit with playwrite mcp for me')).toBeInTheDocument();
    expect(screen.getAllByText('running').length).toBeGreaterThan(0);
    expect(screen.getByText('killed')).toBeInTheDocument();
  });

  it('filters rows when the user types in the filter input', async () => {
    const { container } = render(<TaskList />);
    const input = container.querySelector('input[aria-label="Filter tasks"]') as HTMLInputElement;
    input.value = 'reddit';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    expect(taskFilter.value).toBe('reddit');
    // Signal re-render is async; wait for the "aa" row to disappear.
    const { waitFor } = await import('@testing-library/preact');
    await waitFor(() => {
      expect(screen.queryByText('aa')).toBeNull();
    });
    expect(screen.getByText(/reddit/i)).toBeInTheDocument();
  });

  it('shows status counts', () => {
    render(<TaskList />);
    const counts = screen.getByLabelText('Task counts');
    expect(within(counts).getByText(/1 running/)).toBeInTheDocument();
    expect(within(counts).getByText(/1 failed/)).toBeInTheDocument();
  });

  it('clicking a row calls selectTask with the task id', () => {
    const { container } = render(<TaskList />);
    const row = container.querySelector('.tl-row') as HTMLButtonElement;
    row.click();
    expect(selectedTaskId.value).toBe(sample[0].task_id);
    selectTask(null);
  });

  it('renders an EmptyState when filter yields nothing', async () => {
    render(<TaskList />);
    const input = document.querySelector('input[aria-label="Filter tasks"]') as HTMLInputElement;
    input.value = 'noooo-match';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await screen.findByText('No matches');
  });
});
