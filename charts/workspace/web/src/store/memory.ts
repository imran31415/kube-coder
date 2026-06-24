import { signal, computed } from '@preact/signals';
import {
  subscribeEvents,
  eventStreamConnected,
  type DashboardEvent,
} from '../api/events';
import {
  listMemories,
  getMemory,
  upsertMemory as apiUpsert,
  deleteMemory as apiDelete,
  unlinkRelation as apiUnlinkRelation,
  exportMemory as apiExport,
  importMemory as apiImport,
  type MemoryRecord,
  type MemoryUpsertInput,
  type MemoryExport,
  type MemoryImportMode,
} from '../api/memory';
import { pushToast } from './ui';

export const memories = signal<MemoryRecord[]>([]);
export const memoriesLoading = signal(false);
export const memoriesError = signal<string | null>(null);
export const memoryFilter = signal('');
export const memoryNamespaceFacet = signal<string | null>(null);

export const selectedMemoryId = signal<number | null>(null);
export const selectedMemory = signal<MemoryRecord | null>(null);
export const selectedMemoryLoading = signal(false);

export const namespaces = computed(() => {
  const set = new Set<string>();
  for (const m of memories.value) set.add(m.namespace);
  return [...set].sort();
});

export const filteredMemories = computed(() => {
  const needle = memoryFilter.value.trim().toLowerCase();
  const ns = memoryNamespaceFacet.value;
  return memories.value.filter((m) => {
    if (ns && m.namespace !== ns) return false;
    if (!needle) return true;
    const hay = `${m.namespace}.${m.key} ${m.value} ${m.tags}`.toLowerCase();
    return hay.includes(needle);
  });
});

// Dedupe in-flight list fetches so the poll tick + an explicit refresh
// can't race and clobber each other (the slower response would win).
let _refreshInFlight: Promise<void> | null = null;
export async function refreshMemories(): Promise<void> {
  if (_refreshInFlight) return _refreshInFlight;
  memoriesLoading.value = true;
  _refreshInFlight = (async () => {
    try {
      const r = await listMemories({ limit: 200 });
      memories.value = r.memories;
      memoriesError.value = null;
    } catch (err) {
      memoriesError.value = err instanceof Error ? err.message : String(err);
    } finally {
      memoriesLoading.value = false;
      _refreshInFlight = null;
    }
  })();
  return _refreshInFlight;
}

// Same race-guard idea as loadSelectedTask in store/tasks.ts: a slow
// response for an earlier selection must not clobber a fresher one.
let _selectedLoadToken = 0;
export async function loadSelected(ns: string, key: string): Promise<void> {
  const token = ++_selectedLoadToken;
  selectedMemoryLoading.value = true;
  try {
    const m = await getMemory(ns, key);
    if (token !== _selectedLoadToken) return;
    // Only overwrite if the detail response is a real record (has the
    // identifying fields). If the server returns garbage or a partial
    // object, keep whatever selectMemory() set from the list row instead
    // — that data is always full per the list endpoint.
    if (m && m.id != null && m.namespace && m.key) {
      selectedMemory.value = m;
      selectedMemoryId.value = m.id;
    } else {
      // eslint-disable-next-line no-console
      console.warn('[memory] getMemory returned partial response; keeping list-row data', m);
    }
  } catch (err) {
    if (token !== _selectedLoadToken) return;
    pushToast(err instanceof Error ? err.message : 'Memory load failed', { kind: 'danger' });
  } finally {
    if (token === _selectedLoadToken) selectedMemoryLoading.value = false;
  }
}

export function selectMemory(m: MemoryRecord | null) {
  selectedMemory.value = m;
  selectedMemoryId.value = m?.id ?? null;
}

export async function saveMemory(input: MemoryUpsertInput): Promise<MemoryRecord | null> {
  try {
    const m = await apiUpsert(input);
    pushToast('Saved', { kind: 'success' });
    await refreshMemories();
    selectMemory(m);
    return m;
  } catch (err) {
    pushToast(err instanceof Error ? err.message : 'Save failed', { kind: 'danger' });
    return null;
  }
}

export async function removeMemory(ns: string, key: string): Promise<void> {
  try {
    await apiDelete(ns, key);
    pushToast('Deleted', { kind: 'warn' });
    if (selectedMemory.value && selectedMemory.value.namespace === ns && selectedMemory.value.key === key) {
      selectMemory(null);
    }
    await refreshMemories();
  } catch (err) {
    pushToast(err instanceof Error ? err.message : 'Delete failed', { kind: 'danger' });
  }
}

// ── Lifecycle ops (#134): unlink relation, export, import ───────────────────

export async function unlinkRelationAndRefresh(
  ns: string,
  key: string,
  relationId: number,
): Promise<boolean> {
  try {
    await apiUnlinkRelation(ns, key, relationId);
    pushToast('Relation removed', { kind: 'warn' });
    return true;
  } catch (err) {
    pushToast(err instanceof Error ? err.message : 'Unlink failed', { kind: 'danger' });
    return false;
  }
}

export const memoryExporting = signal(false);
export const memoryImporting = signal(false);

export async function exportMemoriesToFile(): Promise<void> {
  memoryExporting.value = true;
  try {
    const data = await apiExport();
    if (typeof document === 'undefined') return;
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    const stamp = new Date().toISOString().slice(0, 10);
    a.download = `kube-coder-memory-${stamp}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    pushToast(`Exported ${data.memories?.length ?? 0} memories`, { kind: 'success' });
  } catch (err) {
    pushToast(err instanceof Error ? err.message : 'Export failed', { kind: 'danger' });
  } finally {
    memoryExporting.value = false;
  }
}

export async function importMemoriesFromObject(
  payload: MemoryExport,
  mode: MemoryImportMode,
): Promise<boolean> {
  memoryImporting.value = true;
  try {
    const res = await apiImport(payload, mode);
    const parts = [`${res.imported} imported`];
    if (res.skipped) parts.push(`${res.skipped} skipped`);
    if (res.failed) parts.push(`${res.failed} failed`);
    if (res.relations_imported) parts.push(`${res.relations_imported} relations`);
    pushToast(parts.join(', '), { kind: res.failed ? 'warn' : 'success' });
    await refreshMemories();
    return res.failed === 0;
  } catch (err) {
    pushToast(err instanceof Error ? err.message : 'Import failed', { kind: 'danger' });
    return false;
  } finally {
    memoryImporting.value = false;
  }
}

// Real-time via the /api/events SSE stream (issue #93): a `memory.changed`
// event refreshes the list immediately. The interval is a safety net — it also
// catches out-of-band writes (e.g. an MCP-authored memory from a Claude task,
// which lands in a different process and so doesn't emit here): it polls
// normally when the stream is down and slows to a heartbeat when it's up.
let pollHandle: ReturnType<typeof setInterval> | null = null;
let visibilityHandler: (() => void) | null = null;
let eventUnsub: (() => void) | null = null;
let eventRefreshTimer: ReturnType<typeof setTimeout> | null = null;
let lastRefreshAt = 0;
const FALLBACK_REFRESH_MS = 45000;

function doRefresh() {
  lastRefreshAt = Date.now();
  void refreshMemories();
}

function onMemoryEvent(ev: DashboardEvent) {
  if (ev.type !== 'memory.changed') return;
  if (eventRefreshTimer != null) return; // coalesce bursts
  eventRefreshTimer = setTimeout(() => {
    eventRefreshTimer = null;
    doRefresh();
  }, 250);
}

export function startMemoryPolling(intervalMs = 30000) {
  doRefresh();
  if (!eventUnsub) eventUnsub = subscribeEvents(onMemoryEvent);
  if (pollHandle) clearInterval(pollHandle);
  // Same visibility guard as task polling — memory changes rarely while
  // the tab is in the background; refresh on focus instead of every
  // interval tick.
  pollHandle = setInterval(() => {
    if (typeof document !== 'undefined' && document.hidden) return;
    // SSE live → events drive updates; the timer is just a slow safety net.
    if (eventStreamConnected.value && Date.now() - lastRefreshAt < FALLBACK_REFRESH_MS) return;
    doRefresh();
  }, intervalMs);
  if (typeof document !== 'undefined') {
    if (visibilityHandler) document.removeEventListener('visibilitychange', visibilityHandler);
    visibilityHandler = () => {
      if (!document.hidden) doRefresh();
    };
    document.addEventListener('visibilitychange', visibilityHandler);
  }
}
export function stopMemoryPolling() {
  if (pollHandle) clearInterval(pollHandle);
  pollHandle = null;
  if (eventUnsub) {
    eventUnsub();
    eventUnsub = null;
  }
  if (eventRefreshTimer != null) {
    clearTimeout(eventRefreshTimer);
    eventRefreshTimer = null;
  }
  if (visibilityHandler && typeof document !== 'undefined') {
    document.removeEventListener('visibilitychange', visibilityHandler);
    visibilityHandler = null;
  }
}
