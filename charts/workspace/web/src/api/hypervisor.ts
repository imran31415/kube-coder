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
}

export interface HypervisorConfig {
  enabled: boolean;
  defaultAssistant: string;
  workdir: string;
  readOnly: boolean;
  assistants: HypervisorAssistant[];
}

export type ThreadStatus = 'idle' | 'running' | 'error' | 'unknown';

export interface HypervisorThread {
  id: string;
  title: string;
  assistant: string | null;
  status: ThreadStatus;
  created_at: number | null;
  updated_at: number | null;
  // Present (unix seconds) only on soft-deleted threads in the trash view.
  deleted_at?: number | null;
}

export interface ThreadDetail {
  thread: HypervisorThread;
  events: HvEvent[];
}

/** One normalized entry in the observability timeline (server-side
 *  build_activity). `kind` discriminates the shape. */
export interface ActivityEntry {
  kind: 'tool' | 'tool_result_orphan' | 'error' | 'status';
  seq: number;
  ts: number | null;
  // kind === 'tool'
  tool?: string | null;
  input?: unknown;
  tool_id?: string | null;
  status?: 'ok' | 'error' | 'pending' | string;
  result_text?: string | null;
  result_seq?: number | null;
  duration_ms?: number | null;
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

export const createThread = (opts: { message?: string; assistant?: string; workdir?: string }) =>
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
