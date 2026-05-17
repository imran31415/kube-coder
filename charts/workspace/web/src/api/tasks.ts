import { apiGet, apiPost, apiDelete, withOauthPrefix } from './client';
import { coerceTaskSummary, coerceTaskDetail, safeArray } from './shape';

export type TaskStatus = 'running' | 'completed' | 'killed' | 'error' | 'unknown' | 'waiting-for-input';
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
  /** Optional — bare terminal tasks (kind=='terminal') omit memory fields. */
  memory_injected?: MemoryInjection[];
  memory_injection_disabled?: boolean;
  /** Set when task is waiting for human input */
  waiting_for_input?: boolean;
  /** Last prompt or question that triggered waiting state */
  last_input_prompt?: string;
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

export const listTasks = () =>
  apiGet<ListResponse>('/api/claude/tasks').then((r) => safeArray(r.tasks).map(coerceTaskSummary));

export const getTask = (id: string) =>
  apiGet<TaskDetail>(`/api/claude/tasks/${id}`).then(coerceTaskDetail);

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

/**
 * Wire the workspace's ttyd entrypoint to this task's tmux session before
 * opening /oauth/terminal/. Without this, /oauth/terminal/ drops the user
 * into a fresh bash shell instead of attaching to the task.
 */
export const prepareTerminal = (id: string) =>
  apiPost<{ ok: true; tmux_session?: string }>(`/api/claude/tasks/${id}/prepare-terminal`, {});

/** Absolute URL for the ttyd iframe; cache-busts on each open. */
export const terminalUrl = () => `/oauth/terminal/?t=${Date.now()}`;

/** Absolute URL for the embedded noVNC viewer. */
export const vncUrl = () =>
  `/oauth/vnc-direct/vnc.html?autoconnect=true&resize=scale&view_clip=true&t=${Date.now()}`;

/**
 * Tell the in-pod kiosk Chrome to navigate to localhost:<port>. The dashboard
 * never touches port-forwarding — the in-pod browser is already on the X
 * display, so noVNC immediately reflects the new page. See server.py:3769
 * (open_localhost in server.py).
 */
export const openLocalhostPort = (port: number) =>
  apiPost<{ ok: true } | { error: string }>('/api/open-localhost', { port });

/** Spawn the in-pod kiosk Chrome (or fallback browser). See server.py:3718. */
export const launchInPodBrowser = () =>
  apiPost<{ ok: true } | { error: string }>('/api/launch-chrome', {});

/**
 * Register a plain-bash task before opening ttyd, so the bare terminal
 * session shows up in the task list and can be re-attached if its tab is
 * closed. See server.py:handle_claude_create_terminal_task (called by the
 * legacy dashboard's openTerminal()).
 */
export const createTerminalTask = () =>
  apiPost<TaskDetail>('/api/claude/tasks/terminal', {});

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
  const url = withOauthPrefix(`/api/claude/tasks/${id}/stream?from=start`);
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
