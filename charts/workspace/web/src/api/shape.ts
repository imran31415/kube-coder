/**
 * Tiny runtime schema coercion for API responses — the cure for "Cannot read
 * properties of undefined (reading 'length')" bugs.
 *
 * The pattern we kept hitting: the TS type declares `tags_list: string[]` but
 * the server occasionally omits it for soft-deleted rows / history snapshots
 * / bare-terminal tasks / etc. Render code does `m.tags_list.length` and
 * crashes the entire page.
 *
 * This module wraps the typed `apiGet`/`apiPost` boundary with `coerce*`
 * helpers that fill in safe defaults for fields the renderer indexes into
 * (arrays, strings) and leaves nullable scalars as `undefined` so the UI
 * can branch with `typeof === 'number'` guards. Cheap (~50 lines, no deps)
 * and prevents the next regression structurally.
 *
 * Add new shapes here as we add new API responses. Run them inside the
 * api/*.ts wrappers (see api/memory.ts for the canonical usage).
 */

import type { MemoryRecord } from './memory';
import type { TaskSummary, TaskDetail } from './tasks';

/** Generic helpers — call when the server may omit array/string fields. */
export function safeArray<T>(v: T[] | null | undefined): T[] {
  return Array.isArray(v) ? v : [];
}
export function safeString(v: string | null | undefined): string {
  return typeof v === 'string' ? v : '';
}

/**
 * Fill in defaults for fields that the renderer assumes exist. Anything
 * the server might legitimately omit (history rows, soft-deleted records)
 * gets a sensible default rather than `undefined`.
 *
 * Important: we DO NOT default `importance`/`version`/`access_count`/
 * `updated_at` to 0 — that would silently render "0%", "v0", "0 reads",
 * "1/1/1970" as if real data. The renderer guards those with
 * `typeof === 'number'` and shows `—` when missing.
 */
export function coerceMemoryRecord(raw: unknown): MemoryRecord {
  const r = (raw && typeof raw === 'object') ? raw as Record<string, unknown> : {};
  return {
    id: typeof r.id === 'number' ? r.id : -1,
    namespace: safeString(r.namespace as string | undefined),
    key: safeString(r.key as string | undefined),
    value: safeString(r.value as string | undefined),
    kind: safeString(r.kind as string | undefined) || 'semantic',
    tags: safeString(r.tags as string | undefined),
    tags_list: safeArray(r.tags_list as string[] | undefined),
    importance: typeof r.importance === 'number' ? r.importance : undefined,
    confidence: typeof r.confidence === 'number' ? r.confidence : 1,
    source: typeof r.source === 'string' ? r.source : null,
    created_at: typeof r.created_at === 'number' ? r.created_at : 0,
    updated_at: typeof r.updated_at === 'number' ? r.updated_at : undefined,
    last_accessed_at: typeof r.last_accessed_at === 'number' ? r.last_accessed_at : null,
    access_count: typeof r.access_count === 'number' ? r.access_count : undefined,
    version: typeof r.version === 'number' ? r.version : undefined,
    expires_at: typeof r.expires_at === 'number' ? r.expires_at : null,
    deleted_at: typeof r.deleted_at === 'number' ? r.deleted_at : null,
  } as MemoryRecord;
}

export function coerceMemoryRecordList(raw: unknown): MemoryRecord[] {
  const arr = Array.isArray(raw) ? raw : [];
  return arr.map(coerceMemoryRecord);
}

export function coerceTaskSummary(raw: unknown): TaskSummary {
  const r = (raw && typeof raw === 'object') ? raw as Record<string, unknown> : {};
  return {
    task_id: safeString(r.task_id as string | undefined),
    name: typeof r.name === 'string' ? r.name : null,
    prompt: safeString(r.prompt as string | undefined),
    status: (r.status as TaskSummary['status']) ?? 'unknown',
    created_at: typeof r.created_at === 'number' ? r.created_at : null,
    finished_at: typeof r.finished_at === 'number' ? r.finished_at : null,
    source: typeof r.source === 'string' ? r.source : null,
    kind: safeString(r.kind as string | undefined) || 'claude',
    parent_task_id: typeof r.parent_task_id === 'string' ? r.parent_task_id : null,
    memory_injected: safeArray(r.memory_injected as TaskSummary['memory_injected']),
    memory_injection_disabled: r.memory_injection_disabled === true,
    waiting_for_input: r.waiting_for_input === true,
    last_input_prompt: typeof r.last_input_prompt === 'string' ? r.last_input_prompt : undefined,
    last_activity_at: typeof r.last_activity_at === 'number' ? r.last_activity_at : null,
  };
}

export function coerceTaskDetail(raw: unknown): TaskDetail {
  const base = coerceTaskSummary(raw);
  const r = (raw && typeof raw === 'object') ? raw as Record<string, unknown> : {};
  // Spread `r` FIRST so unknown server-extra fields pass through, then let
  // `base` + the typed defaults below override every known field. The
  // previous order (`...base, ...r`) silently undid every safeString /
  // safeArray coercion above for task_id, prompt, status, name, etc.
  return {
    ...r,
    ...base,
    workdir: typeof r.workdir === 'string' ? r.workdir : undefined,
    session_id: typeof r.session_id === 'string' ? r.session_id : undefined,
    tmux_session: typeof r.tmux_session === 'string' ? r.tmux_session : undefined,
    assistant: typeof r.assistant === 'string' ? r.assistant : undefined,
    recent_output: typeof r.recent_output === 'string' ? r.recent_output : undefined,
  };
}
