import { apiGet, apiPost, apiDelete } from './client';
import { coerceMemoryRecord, coerceMemoryRecordList, safeArray } from './shape';

export type MemoryKind = 'semantic' | 'episodic' | 'procedural' | string;

export interface MemoryRecord {
  id: number;
  namespace: string;
  key: string;
  // Optional — server occasionally returns rows with missing `value` or
  // `tags_list` (e.g. soft-deleted records, history snapshots without tags).
  // Treating them as optional prevents `.length`/`.slice` crashes in render.
  value?: string;
  kind: MemoryKind;
  tags?: string;
  tags_list?: string[];
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
  apiGet<ListResponse>('/api/memory', q as Record<string, string | number | undefined>)
    .then((r) => ({ ...r, memories: coerceMemoryRecordList(r.memories) }));

export const getMemory = (ns: string, key: string) =>
  // Server wraps single-record responses as {memory: row}, matching the
  // {memories: [...]} / {versions: [...]} convention of list endpoints.
  // Unwrap here so callers get a flat MemoryRecord.
  apiGet<{ memory: MemoryRecord }>(`/api/memory/${encodeURIComponent(ns)}/${encodeURIComponent(key)}`)
    .then((r) => coerceMemoryRecord(r.memory));

export const getMemoryHistory = (ns: string, key: string) =>
  apiGet<{ versions: MemoryRecord[] }>(
    `/api/memory/${encodeURIComponent(ns)}/${encodeURIComponent(key)}/history`,
  ).then((r) => ({ versions: coerceMemoryRecordList(r.versions) }));

export const getMemoryNeighbors = (ns: string, key: string, depth = 1) =>
  apiGet<{ nodes: MemoryRecord[]; edges: { from_id: number; to_id: number; kind: string; weight: number }[] }>(
    `/api/memory/${encodeURIComponent(ns)}/${encodeURIComponent(key)}/neighbors`,
    { depth },
  ).then((r) => ({
    nodes: coerceMemoryRecordList(r.nodes),
    edges: safeArray(r.edges),
  }));

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
  apiPost<MemoryRecord>('/api/memory', input).then(coerceMemoryRecord);

export const deleteMemory = (ns: string, key: string) =>
  apiDelete<{ ok: true }>(`/api/memory/${encodeURIComponent(ns)}/${encodeURIComponent(key)}`);

export interface MemoryStats {
  total: number;
  by_namespace: Record<string, number>;
  by_kind: Record<string, number>;
  health?: Record<string, unknown>;
}

export const memoryStats = () => apiGet<MemoryStats>('/api/memory/stats');
