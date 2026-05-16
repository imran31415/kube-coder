import { apiGet, apiPost, apiDelete } from './client';

export type MemoryKind = 'semantic' | 'episodic' | 'procedural' | string;

export interface MemoryRecord {
  id: number;
  namespace: string;
  key: string;
  value: string;
  kind: MemoryKind;
  tags: string;
  tags_list: string[];
  importance: number;
  confidence: number;
  source: string | null;
  created_at: number;
  updated_at: number;
  last_accessed_at: number | null;
  access_count: number;
  version: number;
  expires_at: number | null;
  deleted_at: number | null;
}

export interface MemoryListQuery {
  namespaces?: string;       // comma-separated glob list, e.g. "user.*,project.*"
  kinds?: string;            // comma-separated kind list
  tags?: string;             // comma-separated AND-filter
  search?: string;           // free-text against value
  limit?: number;
  offset?: number;
}

interface ListResponse {
  memories: MemoryRecord[];
  count: number;
}

export const listMemories = (q: MemoryListQuery = {}) =>
  apiGet<ListResponse>('/api/memory', q as Record<string, string | number | undefined>);

export const getMemory = (ns: string, key: string) =>
  apiGet<MemoryRecord>(`/api/memory/${encodeURIComponent(ns)}/${encodeURIComponent(key)}`);

export const getMemoryHistory = (ns: string, key: string) =>
  apiGet<{ versions: MemoryRecord[] }>(
    `/api/memory/${encodeURIComponent(ns)}/${encodeURIComponent(key)}/history`,
  );

export const getMemoryNeighbors = (ns: string, key: string, depth = 1) =>
  apiGet<{ nodes: MemoryRecord[]; edges: { from_id: number; to_id: number; kind: string; weight: number }[] }>(
    `/api/memory/${encodeURIComponent(ns)}/${encodeURIComponent(key)}/neighbors`,
    { depth },
  );

export interface MemoryUpsertInput {
  namespace: string;
  key: string;
  value: string;
  kind?: MemoryKind;
  importance?: number;
  tags?: string[];
  expires_in_days?: number;
}

export const upsertMemory = (input: MemoryUpsertInput) =>
  apiPost<MemoryRecord>('/api/memory', input);

export const deleteMemory = (ns: string, key: string) =>
  apiDelete<{ ok: true }>(`/api/memory/${encodeURIComponent(ns)}/${encodeURIComponent(key)}`);

export interface MemoryStats {
  total: number;
  by_namespace: Record<string, number>;
  by_kind: Record<string, number>;
  health?: Record<string, unknown>;
}

export const memoryStats = () => apiGet<MemoryStats>('/api/memory/stats');
