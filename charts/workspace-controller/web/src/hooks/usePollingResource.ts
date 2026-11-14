import { useEffect, useRef, useState } from 'preact/hooks';
import { failureProblem, responseProblem, type Problem } from '../api/problem';

interface Snapshot<T> {
  key: string;
  data: T | null;
  problem: Problem | null;
  refreshing: boolean;
  updatedAt: number | null;
  /** `data` is an earlier reading kept because the latest request failed. */
  stale: boolean;
}

export interface PollingOptions {
  /** Before the first reading, retry a failed request this many times every
   *  `coldRetryMs`, showing the loading state rather than an error. Right
   *  after a controller restart its kubectl discovery cache is cold and
   *  several endpoints fire at once on page load, so the first read can fail
   *  transiently. */
  coldRetries?: number;
  coldRetryMs?: number;
}

/** How often a non-retryable state (monitoring not configured, no workspace
 *  reporting yet) is re-checked. Those states end when an operator acts, so
 *  polling stops being urgent but must not stop: the panel recovers on its own
 *  once workspaces start reporting. */
export const SLOW_RECHECK_MS = 5 * 60 * 1000;

/** One request at a time; a new resource key never borrows the old key's data. */
export function usePollingResource<T>(key: string, load: (signal: AbortSignal) => Promise<T>, interval: number,
  { coldRetries = 0, coldRetryMs = 2000 }: PollingOptions = {}) {
  const loader = useRef(load);
  loader.current = load;
  const retryRef = useRef<() => void>(() => {});
  const [snapshot, setSnapshot] = useState<Snapshot<T>>({ key, data: null, problem: null, refreshing: true, updatedAt: null, stale: false });
  useEffect(() => {
    let disposed = false;
    let controller: AbortController | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    let coldAttempts = 0;
    let lastGood: T | null = null;
    let updatedAt: number | null = null;
    setSnapshot({ key, data: null, problem: null, refreshing: true, updatedAt, stale: false });
    async function refresh(manual = false) {
      if (disposed || controller || (!manual && document.hidden)) return;
      clearTimeout(timer);
      controller = new AbortController();
      setSnapshot(s => ({ ...s, refreshing: true }));
      let issue: Problem | null = null;
      let delay = interval;
      try {
        const result = await loader.current(controller.signal);
        if (disposed) return;
        issue = responseProblem(result);
        // A response that carries a problem is still the freshest data there
        // is (e.g. kubectl figures with Prometheus down): show it, with the
        // problem, rather than an older complete reading.
        lastGood = result;
        updatedAt = Date.now();
        failures = issue ? failures + 1 : 0;
        setSnapshot({ key, data: result, problem: issue, refreshing: false, updatedAt, stale: false });
      } catch (error) {
        if (disposed) return;
        issue = failureProblem(error);
        if (lastGood === null && issue.retryable && coldAttempts < coldRetries) {
          coldAttempts += 1;
          delay = coldRetryMs;
          issue = null; // stay in the loading state; this is not a failure yet
        } else {
          failures += 1;
          setSnapshot({ key, data: lastGood, problem: issue, refreshing: false, updatedAt, stale: lastGood !== null });
        }
      } finally {
        controller = null;
        if (!disposed) {
          if (issue && !issue.retryable) delay = Math.max(interval, SLOW_RECHECK_MS);
          else if (issue) delay = Math.min(interval * 2 ** Math.min(failures, 3), 120000);
          timer = setTimeout(() => void refresh(), delay);
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
  }, [key, interval, coldRetries, coldRetryMs]);
  const current = snapshot.key === key ? snapshot : { key, data: null, problem: null, refreshing: true, updatedAt: null, stale: false };
  return { ...current, retry: () => retryRef.current() };
}
