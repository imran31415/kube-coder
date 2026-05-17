import { signal, computed } from '@preact/signals';
import {
  listTasks,
  getTask,
  createTask as apiCreateTask,
  killTask as apiKillTask,
  renameTask as apiRenameTask,
  sendMessage as apiSendMessage,
  type TaskSummary,
  type TaskDetail,
  type CreateTaskInput,
} from '../api/tasks';
import { pushToast } from './ui';
import { ApiError } from '../api/client';

export const tasks = signal<TaskSummary[]>([]);
export const tasksLoading = signal(false);
export const tasksError = signal<string | null>(null);
export const tasksLastFetch = signal<number | null>(null);

export const selectedTaskId = signal<string | null>(null);
export const selectedTask = signal<TaskDetail | null>(null);
export const selectedTaskLoading = signal(false);

export const taskFilter = signal('');

/** Status filter: 'running' shows only live tasks; 'all' shows everything. */
export type TaskStatusFilter = 'running' | 'all';
export const taskStatusFilter = signal<TaskStatusFilter>('running');

export const filteredTasks = computed(() => {
  const needle = taskFilter.value.trim().toLowerCase();
  // Auto-relax: if user picked 'running' but there are none, fall back to 'all'
  // so the list isn't empty just because every task happens to be finished.
  const runningCount = tasks.value.filter((t) => t.status === 'running' || t.status === 'waiting-for-input').length;
  const effectiveStatus =
    taskStatusFilter.value === 'running' && runningCount === 0 ? 'all' : taskStatusFilter.value;

  let list = tasks.value;
  if (effectiveStatus === 'running') {
    list = list.filter((t) => t.status === 'running' || t.status === 'waiting-for-input');
  }
  if (needle) {
    list = list.filter((t) => {
      const hay = `${t.task_id} ${t.name ?? ''} ${t.prompt} ${t.source ?? ''} ${t.status}`.toLowerCase();
      return hay.includes(needle);
    });
  }
  return list;
});

/** True when the 'running' filter is currently being applied (after auto-relax). */
export const taskStatusFilterEffective = computed<TaskStatusFilter>(() => {
  const runningCount = tasks.value.filter((t) => t.status === 'running' || t.status === 'waiting-for-input').length;
  return taskStatusFilter.value === 'running' && runningCount === 0 ? 'all' : taskStatusFilter.value;
});

export const taskCounts = computed(() => {
  const counts = { all: tasks.value.length, running: 0, completed: 0, error: 0 };
  for (const t of tasks.value) {
    if (t.status === 'running' || t.status === 'waiting-for-input') counts.running++;
    else if (t.status === 'completed') counts.completed++;
    else if (t.status === 'error' || t.status === 'killed') counts.error++;
  }
  return counts;
});

let inFlight: Promise<void> | null = null;

export async function refreshTasks(): Promise<void> {
  if (inFlight) return inFlight;
  tasksLoading.value = true;
  inFlight = (async () => {
    try {
      const list = await listTasks();
      tasks.value = list;
      tasksError.value = null;
      tasksLastFetch.value = Date.now();
    } catch (err) {
      tasksError.value = err instanceof Error ? err.message : String(err);
    } finally {
      tasksLoading.value = false;
      inFlight = null;
    }
  })();
  return inFlight;
}

export async function loadSelectedTask(id: string): Promise<void> {
  selectedTaskLoading.value = true;
  try {
    const t = await getTask(id);
    selectedTask.value = t;
  } catch (err) {
    pushToast(
      err instanceof Error ? err.message : 'Failed to load task',
      { kind: 'danger' },
    );
  } finally {
    selectedTaskLoading.value = false;
  }
}

export function selectTask(id: string | null) {
  selectedTaskId.value = id;
  if (id) void loadSelectedTask(id);
  else selectedTask.value = null;
}

export async function createTask(input: CreateTaskInput): Promise<TaskDetail | null> {
  try {
    const t = await apiCreateTask(input);
    pushToast('Task created', { kind: 'success' });
    await refreshTasks();
    selectTask(t.task_id);
    return t;
  } catch (err) {
    pushToast(
      err instanceof ApiError ? err.message : `Failed to create task: ${err}`,
      { kind: 'danger' },
    );
    return null;
  }
}

export async function killTask(id: string): Promise<void> {
  try {
    await apiKillTask(id);
    pushToast('Task killed', { kind: 'warn' });
    await refreshTasks();
    if (selectedTaskId.value === id) await loadSelectedTask(id);
  } catch (err) {
    pushToast(err instanceof Error ? err.message : 'Kill failed', { kind: 'danger' });
  }
}

export async function renameTask(id: string, name: string): Promise<void> {
  try {
    await apiRenameTask(id, name);
    pushToast('Renamed', { kind: 'success' });
    await refreshTasks();
    if (selectedTaskId.value === id) await loadSelectedTask(id);
  } catch (err) {
    pushToast(err instanceof Error ? err.message : 'Rename failed', { kind: 'danger' });
  }
}

export async function sendFollowup(id: string, prompt: string): Promise<void> {
  try {
    await apiSendMessage(id, prompt);
    pushToast('Message sent', { kind: 'success' });
    if (selectedTaskId.value === id) await loadSelectedTask(id);
  } catch (err) {
    pushToast(err instanceof Error ? err.message : 'Send failed', { kind: 'danger' });
  }
}

// Polling. Phase 2 keeps this; Phase 6 swaps for /api/events SSE.
let pollHandle: ReturnType<typeof setInterval> | null = null;
export function startTaskPolling(intervalMs = 10000) {
  refreshTasks();
  if (pollHandle) clearInterval(pollHandle);
  pollHandle = setInterval(() => {
    void refreshTasks();
    if (selectedTaskId.value) void loadSelectedTask(selectedTaskId.value);
  }, intervalMs);
}
export function stopTaskPolling() {
  if (pollHandle) clearInterval(pollHandle);
  pollHandle = null;
}
