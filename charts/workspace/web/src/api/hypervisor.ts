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

export const sendThreadMessage = (id: string, message: string) =>
  apiPost<{ ok: boolean }>(
    `/api/hypervisor/threads/${encodeURIComponent(id)}/messages`,
    { message },
  );

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
