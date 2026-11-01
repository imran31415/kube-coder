import { signal } from '@preact/signals';
import { subscribeEvents } from '../api/events';
import { ApiError } from '../api/client';
import { decideBoardRequest, listBoardRequests, prepareBoardRequest, type BoardRequest } from '../api/boardRequests';

export function boardRequestError(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return 'Connection interrupted. Refresh status before trying again.';
}

/** One mounted conversation owns its reads, timer and subscriptions. */
export function createBoardRequestsStore(thread: string) {
  const requests = signal<BoardRequest[]>([]);
  const error = signal<string | null>(null);
  const busy = signal<string | null>(null);
  const lastChecked = signal<number | null>(null);
  let disposed = false;
  let loading = false;
  let rerun = false;
  const decisions = new Map<string, string>();
  function upsert(record: BoardRequest) {
    if (requests.value.some(r => r.id === record.id && r.version > record.version)) return;
    if (!disposed) requests.value = [record, ...requests.value.filter(r => r.id !== record.id)]
      .sort((a, b) => b.created_at - a.created_at);
  }
  async function refresh() {
    if (disposed) return;
    if (loading) { rerun = true; return; }
    loading = true;
    try {
      const result = await listBoardRequests(thread);
      if (!disposed) {
        requests.value = result.requests.map(record => {
          const current = requests.value.find(r => r.id === record.id);
          return current && current.version > record.version ? current : record;
        });
        error.value = null; lastChecked.value = Date.now();
      }
    } catch (err) {
      if (!disposed) error.value = boardRequestError(err);
    } finally {
      loading = false;
      if (rerun && !disposed) { rerun = false; void refresh(); }
    }
  }
  async function decide(record: BoardRequest, decision: 'approve' | 'reject') {
    if (busy.value) return false;
    busy.value = record.id;
    error.value = null;
    const key = `kc.boardDecision.${record.id}.${decision}`;
    let id = decisions.get(key);
    if (!id) {
      try { id = sessionStorage.getItem(key) ?? undefined; } catch { /* memory fallback */ }
      id ??= crypto.randomUUID();
      decisions.set(key, id);
      try { sessionStorage.setItem(key, id); } catch { /* memory fallback */ }
    }
    try {
      upsert(await decideBoardRequest(record, decision, id));
      return true;
    } catch (err) {
      if (!disposed) error.value = boardRequestError(err);
      return false;
    } finally { if (!disposed) busy.value = null; }
  }
  async function adjust(record: BoardRequest, limit: number, concurrency: number) {
    if (!await decide(record, 'reject')) return;
    busy.value = record.id;
    try {
      upsert(await prepareBoardRequest({ thread_id: thread, board_id: record.board_id,
        repository: record.scope.repository, options: { ...record.scope.options,
          select: { ...record.scope.options.select, limit }, concurrency } }));
    } catch (err) { if (!disposed) error.value = boardRequestError(err); }
    finally { if (!disposed) busy.value = null; }
  }
  function start() {
    void refresh();
    const unsubscribe = subscribeEvents(event => {
      if ((event.type === 'boards.request' && event.data.thread_id === thread) ||
          (event.type === 'boards.run' && requests.value.some(r => r.run_id === event.data.id)) ||
          (event.type === 'boards.review' && requests.value.some(r => r.board_id === event.data.board_id))) void refresh();
    });
    // A slow local-state fallback; no vendor reads and no agent polling loop.
    const timer = setInterval(() => {
      const unsettled = requests.value.some(r => r.state === 'pending' || r.state === 'starting' || r.run?.status === 'running');
      if (document.visibilityState !== 'hidden' && (unsettled || error.value)) void refresh();
    }, 15000);
    return () => { disposed = true; clearInterval(timer); unsubscribe(); };
  }
  return { requests, error, busy, lastChecked, refresh, decide, adjust, start };
}
export type BoardRequestsStore = ReturnType<typeof createBoardRequestsStore>;
