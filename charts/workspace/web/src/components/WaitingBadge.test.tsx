import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render } from '@testing-library/preact';
import { WaitingBadge, waitingTasks } from './WaitingBadge';
import { tasks } from '../store/tasks';
import { currentPath } from '../store/router';
import type { TaskSummary } from '../api/tasks';

const sample: TaskSummary[] = [
  {
    task_id: 'live-1', name: 'live one', prompt: 'hi',
    status: 'running', created_at: 1, finished_at: null,
    source: null, kind: 'claude',
    memory_injected: [], memory_injection_disabled: false,
  },
  {
    task_id: 'waiting-1', name: 'awaiting', prompt: 'paused on prompt',
    status: 'waiting-for-input', created_at: 2, finished_at: null,
    source: null, kind: 'claude',
    memory_injected: [], memory_injection_disabled: false,
  },
];

beforeEach(() => {
  tasks.value = [];
});
afterEach(() => {
  tasks.value = [];
});

describe('WaitingBadge', () => {
  it('renders nothing when no tasks are waiting', () => {
    tasks.value = [sample[0]];
    const r = render(<WaitingBadge />);
    expect(r.container.querySelector('.waiting-badge')).toBeNull();
  });

  it('renders with the waiting count when at least one task is paused', () => {
    tasks.value = sample;
    const r = render(<WaitingBadge />);
    const btn = r.container.querySelector('.waiting-badge') as HTMLButtonElement | null;
    expect(btn).not.toBeNull();
    expect(btn!.textContent).toMatch(/1/);
    expect(btn!.getAttribute('aria-label')).toMatch(/1 task is waiting/);
  });

  it('pluralises the aria-label when more than one task is waiting', () => {
    tasks.value = [
      sample[1],
      { ...sample[1], task_id: 'waiting-2' },
    ];
    const r = render(<WaitingBadge />);
    const btn = r.container.querySelector('.waiting-badge') as HTMLButtonElement;
    expect(btn.getAttribute('aria-label')).toMatch(/2 tasks are waiting/);
  });

  it('clicking the badge navigates to the first waiting task', () => {
    tasks.value = sample;
    currentPath.value = '/desktop';
    const r = render(<WaitingBadge />);
    const btn = r.container.querySelector('.waiting-badge') as HTMLButtonElement;
    btn.click();
    expect(currentPath.value).toBe('/tasks/waiting-1');
  });

  it('waitingTasks computed signal reflects the tasks signal', () => {
    tasks.value = sample;
    expect(waitingTasks.value).toHaveLength(1);
    expect(waitingTasks.value[0].task_id).toBe('waiting-1');
    tasks.value = [];
    expect(waitingTasks.value).toHaveLength(0);
  });
});
