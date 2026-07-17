import { apiGet, apiPost, apiDelete } from './client';
import type { HvEvent } from '../routes/hypervisor/transcript';

/**
 * Hypervisor — the workspace-aware chat tab. Thin client for the /api/hypervisor
 * facade. A "thread" is a structured agent session (hypervisor_session.py): the
 * selected CLI runs in machine-readable streaming mode and the server returns a
 * canonical event stream, which the chat renders directly. No terminal, no pane
 * scraping. See charts/workspace/server.py handle_hypervisor_*.
 */

export interface HypervisorAssistant {
  id: string;
  label: string;
  default?: boolean;
  model?: string;
  /** Selectable models for the in-chat model switcher (#308), default first.
   *  Empty/absent → the assistant offers no model choice (switcher hidden). */
  models?: string[];
}

/** One entry in the composer's `/` picker: an invocable skill or a custom
 *  Claude slash command. `name` is the bare token (no leading slash); the
 *  composer inserts `/<name> ` and Claude expands it on the turn. */
export interface HypervisorCommand {
  name: string;
  kind: 'command' | 'skill';
  description: string;
  argument_hint: string;
  scope: string;
}

export interface HypervisorConfig {
  enabled: boolean;
  defaultAssistant: string;
  workdir: string;
  readOnly: boolean;
  assistants: HypervisorAssistant[];
  /** Slash-command / skill picker source (issue #302). Claude-scoped today —
   *  the composer shows the picker only when the selected agent is `claude`. */
  commands?: HypervisorCommand[];
}

export type ThreadStatus = 'idle' | 'running' | 'error' | 'unknown';

export interface HypervisorThread {
  id: string;
  title: string;
  assistant: string | null;
  // The thread's selected model when its assistant offers a choice (#308);
  // '' when none. Reflected by the in-chat switcher when the thread is open.
  model?: string;
  status: ThreadStatus;
  created_at: number | null;
  updated_at: number | null;
  // Present (unix seconds) only on soft-deleted threads in the trash view.
  deleted_at?: number | null;
}

/** Where a thread's rendered transcript came from: `session_log` = parsed from
 *  Claude Code's own JSONL session log (structured, complete); `capture` = the
 *  live events.jsonl stream capture (the fallback, successor to pane scraping). */
export type TranscriptSource = 'session_log' | 'capture';

export interface ThreadDetail {
  thread: HypervisorThread;
  events: HvEvent[];
  source?: TranscriptSource;
}

/** Semantic category the backend assigns each tool call so the panel can
 *  surface high-signal side-effects (sub-builds, sub-agents, …) distinctly. */
export type ActivityCategory =
  | 'build'
  | 'subagent'
  | 'app'
  | 'memory'
  | 'task'
  | 'tool';

/** One normalized entry in the observability timeline (server-side
 *  build_activity). `kind` discriminates the shape. */
export interface ActivityEntry {
  kind: 'tool' | 'tool_result_orphan' | 'error' | 'status';
  seq: number;
  ts: number | null;
  // kind === 'tool'
  tool?: string | null;
  /** Un-namespaced tool name (mcp__dashboard__create_task -> create_task). */
  label?: string | null;
  category?: ActivityCategory;
  input?: unknown;
  tool_id?: string | null;
  status?: 'ok' | 'error' | 'pending' | string;
  result_text?: string | null;
  result_seq?: number | null;
  duration_ms?: number | null;
  // category === 'build': the created task id, for a deep-link.
  task_id?: string | null;
  // category === 'subagent'
  subagent_type?: string | null;
  description?: string | null;
  // kind === 'tool_result_orphan'
  tool_use_id?: string | null;
  // kind === 'error'
  text?: string | null;
}

export interface ActivityCounts {
  tool_calls: number;
  tool_results: number;
  tool_errors: number;
  errors: number;
  messages: number;
  builds: number;
  subagents: number;
}

export interface ThreadActivity {
  thread: HypervisorThread;
  timeline: ActivityEntry[];
  counts: ActivityCounts;
  /** Bounded tail of the runner.log (subprocess stderr + runner diagnostics). */
  runner_log: string;
}

export const getHypervisorConfig = () =>
  apiGet<HypervisorConfig>('/api/hypervisor/config');

export const listThreads = () =>
  apiGet<{ threads: HypervisorThread[] }>('/api/hypervisor/threads').then(
    (r) => r.threads ?? [],
  );

/** The "Recently deleted" trash view — soft-deleted threads only. */
export const listDeletedThreads = () =>
  apiGet<{ threads: HypervisorThread[] }>(
    '/api/hypervisor/threads?deleted=1',
  ).then((r) => r.threads ?? []);

export const createThread = (opts: {
  message?: string;
  assistant?: string;
  workdir?: string;
  model?: string;
}) =>
  apiPost<{ thread: HypervisorThread }>('/api/hypervisor/threads', opts).then(
    (r) => r.thread,
  );

export const getThread = (id: string, since = 0) =>
  apiGet<ThreadDetail>(
    `/api/hypervisor/threads/${encodeURIComponent(id)}?since=${since}`,
  );

/** Per-thread observability: normalized activity timeline + runner.log tail. */
export const getThreadActivity = (id: string) =>
  apiGet<ThreadActivity>(
    `/api/hypervisor/threads/${encodeURIComponent(id)}/activity`,
  );

export const sendThreadMessage = (id: string, message: string) =>
  apiPost<{ ok: boolean }>(
    `/api/hypervisor/threads/${encodeURIComponent(id)}/messages`,
    { message },
  );

export const renameThread = (id: string, title: string) =>
  apiPost<{ thread: HypervisorThread }>(
    `/api/hypervisor/threads/${encodeURIComponent(id)}/rename`,
    { title },
  ).then((r) => r.thread);

/** Switch a live thread's model (#308). Takes effect on the next turn. */
export const setThreadModel = (id: string, model: string) =>
  apiPost<{ thread: HypervisorThread }>(
    `/api/hypervisor/threads/${encodeURIComponent(id)}/model`,
    { model },
  ).then((r) => r.thread);

export const stopThread = (id: string) =>
  apiPost<{ ok: boolean; stopped: boolean }>(
    `/api/hypervisor/threads/${encodeURIComponent(id)}/stop`,
  );

export const deleteThread = (id: string) =>
  apiDelete<{ ok: boolean }>(`/api/hypervisor/threads/${encodeURIComponent(id)}`);

/** Undo a soft-delete: clears deleted_at so the chat reappears in the list. */
export const restoreThread = (id: string) =>
  apiPost<{ ok: boolean; restored: boolean }>(
    `/api/hypervisor/threads/${encodeURIComponent(id)}/restore`,
  );
