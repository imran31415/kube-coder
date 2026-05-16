import { signal, computed } from '@preact/signals';
import {
  listMemories,
  getMemory,
  upsertMemory as apiUpsert,
  deleteMemory as apiDelete,
  memoryStats as apiStats,
  type MemoryRecord,
  type MemoryStats,
  type MemoryUpsertInput,
} from '../api/memory';
import { pushToast } from './ui';

export const memories = signal<MemoryRecord[]>([]);
export const memoriesLoading = signal(false);
export const memoriesError = signal<string | null>(null);
export const memoryFilter = signal('');
export const memoryNamespaceFacet = signal<string | null>(null);
export const stats = signal<MemoryStats | null>(null);

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

export async function refreshMemories(): Promise<void> {
  memoriesLoading.value = true;
  try {
    const r = await listMemories({ limit: 200 });
    memories.value = r.memories;
    memoriesError.value = null;
  } catch (err) {
    memoriesError.value = err instanceof Error ? err.message : String(err);
  } finally {
    memoriesLoading.value = false;
  }
}

export async function refreshStats(): Promise<void> {
  try {
    stats.value = await apiStats();
  } catch {
    // non-fatal
  }
}

export async function loadSelected(ns: string, key: string): Promise<void> {
  selectedMemoryLoading.value = true;
  try {
    const m = await getMemory(ns, key);
    selectedMemory.value = m;
    selectedMemoryId.value = m.id;
  } catch (err) {
    pushToast(err instanceof Error ? err.message : 'Memory load failed', { kind: 'danger' });
  } finally {
    selectedMemoryLoading.value = false;
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

let pollHandle: ReturnType<typeof setInterval> | null = null;
export function startMemoryPolling(intervalMs = 30000) {
  void refreshMemories();
  void refreshStats();
  if (pollHandle) clearInterval(pollHandle);
  pollHandle = setInterval(() => {
    void refreshMemories();
  }, intervalMs);
}
export function stopMemoryPolling() {
  if (pollHandle) clearInterval(pollHandle);
  pollHandle = null;
}
