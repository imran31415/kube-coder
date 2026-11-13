import { useEffect, useRef, useState } from 'preact/hooks';
import { failureProblem, responseProblem, type Problem } from '../api/problem';

interface Snapshot<T> {
  key: string;
  data: T | null;
  problem: Problem | null;
  refreshing: boolean;
  updatedAt: number | null;
}

/** One request at a time; a new resource key never borrows the old key's data. */
export function usePollingResource<T>(key: string, load: (signal: AbortSignal) => Promise<T>, interval: number) {
  const loader = useRef(load);
  loader.current = load;
  const retryRef = useRef<() => void>(() => {});
  const [snapshot, setSnapshot] = useState<Snapshot<T>>({ key, data: null, problem: null, refreshing: true, updatedAt: null });
  useEffect(() => {
    let disposed = false;
    let controller: AbortController | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    let lastGood: T | null = null;
    let updatedAt: number | null = null;
    setSnapshot({ key, data: null, problem: null, refreshing: true, updatedAt });
    async function refresh(manual = false) {
      if (disposed || controller || (!manual && document.hidden)) return;
      clearTimeout(timer);
      controller = new AbortController();
      setSnapshot(s => ({ ...s, refreshing: true }));
      let issue: Problem | null = null;
      try {
        const result = await loader.current(controller.signal);
        if (disposed) return;
        issue = responseProblem(result);
        if (!issue) { lastGood = result; updatedAt = Date.now(); failures = 0; }
        else failures += 1;
        setSnapshot({ key, data: lastGood ?? result, problem: issue, refreshing: false, updatedAt });
      } catch (error) {
        if (disposed) return;
        issue = failureProblem(error);
        failures += 1;
        setSnapshot({ key, data: lastGood, problem: issue, refreshing: false, updatedAt });
      } finally {
        controller = null;
        if (!disposed && (!issue || issue.retryable)) {
          timer = setTimeout(() => void refresh(), Math.min(interval * 2 ** Math.min(failures, 3), 120000));
        }
      }
    }
    retryRef.current = () => void refresh(true);
    const visible = () => { if (!document.hidden) void refresh(); };
    document.addEventListener('visibilitychange', visible);
    void refresh();
    return () => {
      disposed = true;
      controller?.abort();
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', visible);
    };
  }, [key, interval]);
  const current = snapshot.key === key ? snapshot : { key, data: null, problem: null, refreshing: true, updatedAt: null };
  return { ...current, retry: () => retryRef.current() };
}
