import { apiGet, apiPost, apiDelete } from './client';
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
  /** Set when this task was spawned by another agent (agent-orchestrator). */
  parent_task_id?: string | null;
  /** Optional — bare terminal tasks (kind=='terminal') omit memory fields. */
  memory_injected?: MemoryInjection[];
  memory_injection_disabled?: boolean;
  /** Set when task is waiting for human input */
  waiting_for_input?: boolean;
  /** Last prompt or question that triggered waiting state */
  last_input_prompt?: string;
  /** Unix seconds the rendered screen last changed (drives idle/stale UI). */
  last_activity_at?: number | null;
}

/**
 * A waiting task is "stale" once it's been idle this long — escalated in the
 * UI from a gentle "your turn" amber to a stronger "needs attention" cue.
 */
export const STALE_AFTER_SECONDS = 20 * 60;

/** Seconds the task's screen has been idle, or null if unknown. */
export function idleSeconds(t: TaskSummary): number | null {
  if (typeof t.last_activity_at !== 'number') return null;
  return Math.max(0, Math.floor(Date.now() / 1000 - t.last_activity_at));
}

/** True when the task is waiting AND has been idle past the stale threshold. */
export function isStaleWaiting(t: TaskSummary): boolean {
  if (t.status !== 'waiting-for-input') return false;
  const s = idleSeconds(t);
  return s !== null && s >= STALE_AFTER_SECONDS;
}

/** Short idle-duration label like "3m" / "1h" / "2d", or '' if unknown. */
export function idleLabel(t: TaskSummary): string {
  const s = idleSeconds(t);
  if (s === null) return '';
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
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

// Toggle tmux copy-mode for a task. Replaces the user holding Ctrl+B [
// to scroll and `q` to exit — the SPA's Scroll-mode button POSTs here so
// arrow keys / Page Up / mouse wheel land on copy-mode navigation
// instead of being eaten by Claude TUI's prompt.
export const setScrollMode = (id: string, action: 'enter' | 'exit') =>
  apiPost<{ ok: true; mode: 'enter' | 'exit' }>(
    `/api/claude/tasks/${id}/scroll-mode`,
    { action },
  );

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
export const openLocalhostPort = (port: number, path = '/') =>
  apiPost<{ ok: true } | { error: string }>('/api/open-localhost', { port, path });

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
