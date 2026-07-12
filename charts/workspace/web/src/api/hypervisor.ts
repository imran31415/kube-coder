import { apiGet, apiPost, apiDelete } from './client';

/**
 * Hypervisor — the workspace-aware chat tab. Thin client for the /api/hypervisor
 * facade, which is itself a thin layer over the task manager: a "thread" is a
 * hypervisor-flavoured agent session (the user's chosen CLI agent), and the
 * chat just renders it cleanly. See charts/workspace/server.py handle_hypervisor_*.
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

export type ThreadStatus =
  | 'running'
  | 'completed'
  | 'killed'
  | 'error'
  | 'waiting-for-input'
  | 'unknown';

export interface HypervisorThread {
  id: string;
  title: string;
  assistant: string | null;
  status: ThreadStatus;
  created_at: number | null;
  updated_at: number | null;
}

export interface ChatMessage {
  role: 'user' | 'assistant';
  text: string;
  sent_at?: number | null;
}

export interface ThreadDetail {
  thread: HypervisorThread;
  messages: ChatMessage[];
  recent_output: string;
}

export const getHypervisorConfig = () =>
  apiGet<HypervisorConfig>('/api/hypervisor/config');

export const listThreads = () =>
  apiGet<{ threads: HypervisorThread[] }>('/api/hypervisor/threads').then(
    (r) => r.threads ?? [],
  );

export const createThread = (opts: { message?: string; assistant?: string; workdir?: string }) =>
  apiPost<{ thread: HypervisorThread }>('/api/hypervisor/threads', opts).then(
    (r) => r.thread,
  );

export const getThread = (id: string) =>
  apiGet<ThreadDetail>(`/api/hypervisor/threads/${encodeURIComponent(id)}`);

export const sendThreadMessage = (id: string, message: string) =>
  apiPost<{ ok: boolean }>(
    `/api/hypervisor/threads/${encodeURIComponent(id)}/messages`,
    { message },
  );

export const deleteThread = (id: string) =>
  apiDelete<{ ok: boolean }>(`/api/hypervisor/threads/${encodeURIComponent(id)}`);
