import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import {
  tasks,
  selectedTask,
  selectedTaskId,
  taskFilter,
  filteredTasks,
  taskCounts,
  refreshTasks,
  selectTask,
} from './tasks';
import type { TaskSummary } from '../api/tasks';

const realFetch = globalThis.fetch;
beforeEach(() => {
  tasks.value = [];
  selectedTaskId.value = null;
  selectedTask.value = null;
  taskFilter.value = '';
});
afterEach(() => {
  globalThis.fetch = realFetch;
});

const sample: TaskSummary[] = [
  {
    task_id: '1', name: 'alpha', prompt: 'Refactor auth module',
    status: 'running', created_at: 1700000000, finished_at: null, source: null,
    kind: 'claude', memory_injected: [], memory_injection_disabled: false,
  },
  {
    task_id: '2', name: null, prompt: 'Build the docs site',
    status: 'completed', created_at: 1699999999, finished_at: 1700000050, source: 'webhook',
    kind: 'claude', memory_injected: [], memory_injection_disabled: false,
  },
  {
    task_id: '3', name: null, prompt: 'Kill switch test',
    status: 'killed', created_at: 1699999000, finished_at: 1699999500, source: null,
    kind: 'claude', memory_injected: [], memory_injection_disabled: false,
  },
];

function mockTasks(list: TaskSummary[]) {
  globalThis.fetch = vi.fn(async () => ({
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: async () => ({ tasks: list }),
  })) as unknown as typeof fetch;
}

describe('tasks store', () => {
  it('refreshTasks() populates the list', async () => {
    mockTasks(sample);
    await refreshTasks();
    expect(tasks.value).toHaveLength(3);
    expect(tasks.value[0].name).toBe('alpha');
  });

  it('filteredTasks honors the filter signal', () => {
    tasks.value = sample;
    taskFilter.value = 'auth';
    expect(filteredTasks.value).toHaveLength(1);
    expect(filteredTasks.value[0].task_id).toBe('1');

    taskFilter.value = 'killed';
    expect(filteredTasks.value).toHaveLength(1);
    expect(filteredTasks.value[0].task_id).toBe('3');

    taskFilter.value = '';
    expect(filteredTasks.value).toHaveLength(3);
  });

  it('taskCounts buckets statuses', () => {
    tasks.value = sample;
    const c = taskCounts.value;
    expect(c.all).toBe(3);
    expect(c.running).toBe(1);
    expect(c.completed).toBe(1);
    expect(c.error).toBe(1); // killed counts as error
  });

  it('selectTask(null) clears the selected task', () => {
    selectedTaskId.value = '1';
    selectedTask.value = { ...sample[0] } as never;
    selectTask(null);
    expect(selectedTaskId.value).toBeNull();
    expect(selectedTask.value).toBeNull();
  });
});
