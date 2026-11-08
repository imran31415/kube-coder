import { render, screen, within } from '@testing-library/preact';
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { TaskList } from './TaskList';
import {
  tasks,
  selectedTaskId,
  taskFilter,
  taskStatusFilter,
  tasksError,
  selectTask,
  stopTaskPolling,
  taskCounts,
} from '../../store/tasks';
import type { TaskSummary, WaitingReason } from '../../api/tasks';

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
  // Tests expect both the running 'aa' task AND the killed 'reddit' task to
  // be visible — force the 'all' filter so the default 'running' filter
  // doesn't hide past tasks.
  taskStatusFilter.value = 'all';
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
  it('marks a Build parked on the folder-trust dialog (#800)', () => {
    // Same status as a task that finished its turn — the pill is the only
    // thing that tells the two apart at a glance.
    tasks.value = [{
      ...sample[0], task_id: 'wedged-1', name: 'wedged',
      status: 'waiting-for-input', prompt_delivered: false,
      stalled_at_trust_dialog: true,
    }];
    render(<TaskList />);
    expect(screen.getByText('trust dialog')).toBeInTheDocument();
  });

  it('marks a Build whose prompt was never submitted (#805)', () => {
    // Same status, same quiet screen as a Build that finished its turn — and
    // the prompt is sitting unsent in its input box.
    tasks.value = [{
      ...sample[0], task_id: 'unsent-1', name: 'unsent',
      status: 'waiting-for-input', prompt_delivered: false,
    }];
    render(<TaskList />);
    expect(screen.getByText('prompt unsent')).toBeInTheDocument();
  });

  it('shows only the trust-dialog pill when that is what ate the prompt', () => {
    // Both are true at once there; two danger pills saying the same thing is
    // noise, and the dialog one is the actionable half.
    tasks.value = [{
      ...sample[0], task_id: 'wedged-2', status: 'waiting-for-input',
      prompt_delivered: false, stalled_at_trust_dialog: true,
    }];
    render(<TaskList />);
    expect(screen.getByText('trust dialog')).toBeInTheDocument();
    expect(screen.queryByText('prompt unsent')).toBeNull();
  });

  it('leaves a Build whose delivery is still in flight unmarked', () => {
    // null, not false: the delivery thread has not reported yet.
    tasks.value = [{ ...sample[0], status: 'running', prompt_delivered: null }];
    render(<TaskList />);
    expect(screen.queryByText('prompt unsent')).toBeNull();
  });

  it('leaves an ordinary waiting task unmarked', () => {
    tasks.value = [{ ...sample[0], status: 'waiting-for-input', prompt_delivered: true }];
    render(<TaskList />);
    expect(screen.queryByText('trust dialog')).toBeNull();
  });

  describe('waiting_reason', () => {
    const waiting = (id: string, waiting_reason?: WaitingReason, idleFor = 60) => ({
      ...sample[0], task_id: id, name: id, status: 'waiting-for-input' as const,
      last_activity_at: Math.floor(Date.now() / 1000) - idleFor,
      memory_injected: [],
      ...(waiting_reason ? { waiting_reason } : {}),
    });
    const row = (id: string) =>
      document.querySelector(`[data-task-id="${id}"] .tl-row`) as HTMLElement;

    it('steps an idle row down from the waiting treatment', () => {
      tasks.value = [waiting('parked', 'idle')];
      render(<TaskList />);
      const r = within(row('parked'));
      expect(r.getByText('idle')).toBeInTheDocument();
      expect(r.queryByText('waiting')).toBeNull();
      expect(r.getByText('Turn ended')).toBeInTheDocument();
      expect(row('parked').className).not.toContain('tl-row-waiting');
      expect(row('parked').querySelector('.tl-waiting-indicator')).toBeNull();
    });

    it('never words idle as done or finished', () => {
      tasks.value = [waiting('parked', 'idle')];
      render(<TaskList />);
      expect(row('parked').textContent).not.toMatch(/\b(done|finished)\b/i);
      expect(row('parked').textContent).toContain('no question detected');
    });

    it('keeps a question in the waiting treatment and names it', () => {
      tasks.value = [waiting('asks', 'question')];
      render(<TaskList />);
      const r = within(row('asks'));
      expect(r.getByText('waiting')).toBeInTheDocument();
      expect(r.getByText('question')).toBeInTheDocument();
      expect(r.getByText('Question waiting')).toBeInTheDocument();
      expect(row('asks').className).toContain('tl-row-waiting');
    });

    it.each([
      ['trust_dialog', 'Folder-trust dialog'],
      ['undelivered', 'Prompt not delivered'],
    ] as const)('heads a %s row with what is wrong', (reason, heading) => {
      tasks.value = [waiting('stuck', reason)];
      render(<TaskList />);
      expect(within(row('stuck')).getByText(heading)).toBeInTheDocument();
      expect(row('stuck').className).toContain('tl-row-waiting');
    });

    it('treats a waiting row with no reason as needing you, as it always has', () => {
      tasks.value = [waiting('legacy')];
      render(<TaskList />);
      expect(within(row('legacy')).getByText('Waiting for input')).toBeInTheDocument();
      expect(row('legacy').className).toContain('tl-row-waiting');
      expect(screen.getByText('1 needs you')).toBeInTheDocument();
    });

    it('does not escalate a long-idle row to stale', () => {
      tasks.value = [waiting('old-idle', 'idle', 4 * 3600)];
      render(<TaskList />);
      expect(within(row('old-idle')).queryByText('stale')).toBeNull();
      expect(within(row('old-idle')).queryByText('Needs attention')).toBeNull();
    });

    it('still escalates a long-unanswered question to stale', () => {
      tasks.value = [waiting('old-q', 'question', 4 * 3600)];
      render(<TaskList />);
      expect(within(row('old-q')).getByText('stale')).toBeInTheDocument();
      expect(within(row('old-q')).getByText('Needs attention')).toBeInTheDocument();
    });

    it('counts only the first group as needing you', () => {
      tasks.value = [
        waiting('a', 'question'), waiting('b', 'trust_dialog'),
        waiting('c', 'undelivered'), waiting('d', 'idle'), waiting('e', 'idle'),
      ];
      render(<TaskList />);
      expect(screen.getByText('3 need you')).toBeInTheDocument();
      expect(screen.getByText('2 idle')).toBeInTheDocument();
    });

    it('keeps every waiting task in the Running filter and count', () => {
      tasks.value = [waiting('a', 'question'), waiting('b', 'idle')];
      taskStatusFilter.value = 'running';
      render(<TaskList />);
      expect(taskCounts.value.running).toBe(2);
      expect(row('a')).toBeTruthy();
      expect(row('b')).toBeTruthy();
    });

    it('shows no summary when nothing is waiting', () => {
      render(<TaskList />);
      expect(screen.queryByText(/need(s)? you/)).toBeNull();
    });
  });

  it('renders one row per task with status pills', () => {
    render(<TaskList />);
    expect(screen.getByText('aa')).toBeInTheDocument();
    expect(screen.getByText('login to reddit with playwrite mcp for me')).toBeInTheDocument();
    expect(screen.getAllByText('running').length).toBeGreaterThan(0);
    expect(screen.getByText('killed')).toBeInTheDocument();
  });

  it('filters rows when the user types in the filter input', async () => {
    const { container } = render(<TaskList />);
    const input = container.querySelector('input[aria-label="Filter tasks by text"]') as HTMLInputElement;
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

  it('shows status counts on the segmented filter', () => {
    render(<TaskList />);
    const seg = screen.getByLabelText('Filter by status');
    // The "Running" button shows its count badge; the "All" button shows total.
    expect(within(seg).getByRole('tab', { name: /Running\s*1/ })).toBeInTheDocument();
    expect(within(seg).getByRole('tab', { name: /All\s*2/ })).toBeInTheDocument();
  });

  it('clicking a row calls selectTask with the task id', () => {
    const { container } = render(<TaskList />);
    const row = container.querySelector('.tl-row') as HTMLButtonElement;
    row.click();
    expect(selectedTaskId.value).toBe(sample[0].task_id);
    selectTask(null);
  });

  it('defaults to showing only running tasks when there are any', async () => {
    taskStatusFilter.value = 'running';
    render(<TaskList />);
    // Only the running 'aa' task should be visible by default.
    expect(screen.getByText('aa')).toBeInTheDocument();
    expect(screen.queryByText('login to reddit with playwrite mcp for me')).toBeNull();
  });

  it('auto-relaxes to all when filter is "running" but none exist', async () => {
    // Replace sample with no-running tasks, keep filter at default 'running'.
    tasks.value = [sample[1]]; // just the killed task
    taskStatusFilter.value = 'running';
    render(<TaskList />);
    // The killed task should still appear because we auto-relax to 'all'.
    expect(screen.getByText('login to reddit with playwrite mcp for me')).toBeInTheDocument();
    // And the "no running" banner should explain why.
    expect(screen.getByRole('status')).toHaveTextContent(/No running tasks/);
  });

  it('renders an EmptyState when filter yields nothing', async () => {
    render(<TaskList />);
    const input = document.querySelector('input[aria-label="Filter tasks by text"]') as HTMLInputElement;
    input.value = 'noooo-match';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await screen.findByText('No matches');
  });
});
