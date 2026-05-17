import { useEffect, useRef } from 'preact/hooks';

export interface PollOptions {
  /** Stop polling while the tab is hidden. Default: true. */
  pauseOnHidden?: boolean;
  /**
   * Multiply the interval by `errorBackoff^errorCount` (capped at
   * `maxBackoffMs`) when `fn` rejects. Default: 1.5x backoff up to 60s.
   */
  errorBackoff?: number;
  maxBackoffMs?: number;
  /**
   * If false, the hook does nothing — useful for conditionally enabling
   * polling without unmounting the component (e.g. only while a task is
   * running). Default: true.
   */
  enabled?: boolean;
}

/**
 * Single source of truth for `setInterval`-style polling. Reasons to
 * use this over a hand-rolled `setInterval`:
 *
 * - **Visibility-aware.** Pauses when the tab is hidden and refires
 *   immediately on visibilitychange → visible, so users coming back
 *   from another tab see fresh data without a poll-tick delay.
 * - **Error backoff.** A run that throws backs off so a broken endpoint
 *   doesn't burn the user's quota.
 * - **In-flight dedup.** A second tick won't fire if the previous fn
 *   is still pending.
 * - **Unmount-safe.** Pending timers are cleared on unmount.
 */
export function usePoll(
  fn: () => void | Promise<void>,
  intervalMs: number,
  opts: PollOptions = {},
): void {
  const {
    pauseOnHidden = true,
    errorBackoff = 1.5,
    maxBackoffMs = 60_000,
    enabled = true,
  } = opts;
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    let timer: number | null = null;
    let errors = 0;
    let running = false;

    const isHidden = () =>
      pauseOnHidden &&
      typeof document !== 'undefined' &&
      document.visibilityState === 'hidden';

    const computeDelay = () => {
      if (errors === 0) return intervalMs;
      const mult = Math.pow(errorBackoff, errors);
      return Math.min(maxBackoffMs, Math.round(intervalMs * mult));
    };

    const schedule = (delay: number) => {
      if (cancelled) return;
      timer = window.setTimeout(tick, delay);
    };

    const tick = async () => {
      if (cancelled) return;
      if (isHidden()) {
        // Don't run while hidden; visibilitychange will kick us when we
        // come back. Keep timer null so we don't double-schedule.
        timer = null;
        return;
      }
      if (running) {
        schedule(intervalMs);
        return;
      }
      running = true;
      try {
        await fnRef.current();
        errors = 0;
      } catch {
        errors += 1;
      } finally {
        running = false;
      }
      schedule(computeDelay());
    };

    const onVisibility = () => {
      if (cancelled) return;
      if (!isHidden() && timer == null) tick();
    };

    // Kick off immediately (the caller is expected to do an initial
    // load themselves; if they don't, this gives them one).
    void tick();
    if (pauseOnHidden && typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisibility);
    }
    return () => {
      cancelled = true;
      if (timer != null) clearTimeout(timer);
      if (pauseOnHidden && typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVisibility);
      }
    };
  }, [intervalMs, enabled, pauseOnHidden, errorBackoff, maxBackoffMs]);
}
