import { apiGet, apiPost, apiDelete } from './client';

export type TaskStatus = 'running' | 'completed' | 'killed' | 'error' | 'unknown';
export type TaskKind = 'claude' | 'terminal' | string;

export interface MemoryInjection {
  namespace: string;
  key: string;
}

export interface TaskSummary {
  task_id: string;
  name: string | null;
  prompt: string;
  status: TaskStatus;
  created_at: number | null;
  finished_at: number | null;
  source: string | null;
  kind: TaskKind;
  memory_injected: MemoryInjection[];
  memory_injection_disabled: boolean;
}

export interface TaskDetail extends TaskSummary {
  workdir?: string;
  session_id?: string;
  tmux_session?: string;
  assistant?: string;
  recent_output?: string;
  // Server occasionally adds fields we don't model; allow them.
  [key: string]: unknown;
}

export interface AssistantOption {
  id: string;
  label: string;
  description?: string;
  default?: boolean;
}

export interface WorkdirOption {
  path: string;
  label?: string;
  is_git?: boolean;
}

interface ListResponse {
  tasks: TaskSummary[];
}

export const listTasks = () => apiGet<ListResponse>('/api/claude/tasks').then((r) => r.tasks);

export const getTask = (id: string) => apiGet<TaskDetail>(`/api/claude/tasks/${id}`);

export const getTaskOutput = (id: string, tail?: number) =>
  apiGet<{ output: string }>(`/api/claude/tasks/${id}/output`, tail ? { tail } : undefined);

export interface CreateTaskInput {
  prompt: string;
  workdir?: string;
  assistant?: string;
  disable_memory_injection?: boolean;
}
export const createTask = (input: CreateTaskInput) => apiPost<TaskDetail>('/api/claude/tasks', input);

export const sendMessage = (id: string, prompt: string) =>
  apiPost<TaskDetail>(`/api/claude/tasks/${id}/message`, { prompt });

export const renameTask = (id: string, name: string) =>
  apiPost<TaskDetail>(`/api/claude/tasks/${id}/rename`, { name });

export const killTask = (id: string) => apiDelete<{ ok: true }>(`/api/claude/tasks/${id}`);

export const listAssistants = () => apiGet<{ assistants: AssistantOption[] }>('/api/claude/assistants').then((r) => r.assistants);

export const listWorkdirs = () => apiGet<{ dirs: WorkdirOption[] }>('/api/workspace/dirs').then((r) => r.dirs);

/**
 * Open the SSE stream for a task's live output. Returns the EventSource so
 * the caller can close it on unmount. The handler receives the accumulated
 * screen text, not raw diffs — server.py emits the full pane content each
 * tick after stripping ANSI escapes.
 */
export function openTaskStream(
  id: string,
  onChunk: (text: string) => void,
  onEnd?: (info: { status?: string }) => void,
  onError?: (err: Event) => void,
): EventSource {
  const url = `/api/claude/tasks/${id}/stream?from=start`;
  const es = new EventSource(url);
  es.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data) as { text?: string; output?: string };
      const text = data.text ?? data.output ?? '';
      if (text) onChunk(text);
    } catch {
      // Some implementations send raw text after `data: `.
      onChunk(e.data);
    }
  };
  es.addEventListener('end', (e: MessageEvent) => {
    try {
      onEnd?.(JSON.parse(e.data));
    } catch {
      onEnd?.({});
    }
    es.close();
  });
  es.onerror = (e) => {
    onError?.(e);
  };
  return es;
}
