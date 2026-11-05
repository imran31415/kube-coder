import { api } from './client';
import type { BoardRunSummary } from './boards';

export interface BoardRequest {
  id: string;
  thread_id: string;
  board_id: string;
  revision: string;
  version: number;
  state: 'pending' | 'rejected' | 'expired' | 'starting' | 'started' | 'failed' | 'interrupted';
  created_at: number;
  expires_at: number;
  run_id: string | null;
  decision_id: string | null;
  scope: {
    board_id: string;
    board_name: string;
    repository: string;
    operation: 'start' | 'stop';
    options: { select: Record<string, unknown> & { limit: number }; concurrency: number;
      workdir: string; base_ref: string; isolate: boolean; mode: 'propose' };
  };
  error: { error?: string; code?: string; retryable?: boolean };
  run?: BoardRunSummary | null;
  review_needed?: number;
}

async function request<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12000);
  try { return await api<T>(path, { method, body, signal: controller.signal, redirectOnNetworkAuthError: false }); }
  finally { clearTimeout(timeout); }
}

export const listBoardRequests = (thread: string) =>
  request<{ requests: BoardRequest[] }>(`/api/hypervisor/threads/${encodeURIComponent(thread)}/board-requests`);
export const getBoardRequest = (id: string) => request<BoardRequest>(`/api/boards/run-requests/${encodeURIComponent(id)}`);
export const prepareBoardRequest = (body: unknown) => request<BoardRequest>('/api/boards/run-requests', 'POST', body);
export const decideBoardRequest = (record: BoardRequest, decision: 'approve' | 'reject', decisionId: string) =>
  request<BoardRequest>(`/api/boards/run-requests/${encodeURIComponent(record.id)}/decision`, 'POST',
    { revision: record.revision, decision, decision_id: decisionId });
