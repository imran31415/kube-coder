/** Focus-aware polling. */
import { useFocusEffect } from '@react-navigation/native';
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  EVENT_DEBOUNCE_MS,
  isLiveConnected,
  matchesEvent,
  onLiveEvent,
  pollPlan,
  subscribeLive,
} from '../store/liveEvents';

/**
 * Run `fn` immediately and then every `intervalMs` — but only while the screen
 * is focused. Tab screens stay mounted when the user switches tabs, so a plain
 * setInterval keeps hammering the API (and the battery) from every tab at
 * once; this stops the timer on blur and refreshes immediately on refocus so
 * the screen never shows stale data.
 *
 * `liveEvents` (#833): event-type prefixes (`'feed.'`, `'task.'`, …) that
 * change what this screen shows. In the desktop app, while the core holds the
 * workspace's event stream, the interval gives way to a refresh on each
 * matching event plus a slow safety poll; the moment the stream drops, the
 * interval is back. Elsewhere no stream is ever reported, so this is the
 * plain interval it always was.
 */
export function usePolling(
  fn: () => void | Promise<void>,
  intervalMs: number,
  liveEvents?: readonly string[],
): void {
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const live = useLive();
  const eventsKey = (liveEvents ?? []).join('|');

  useFocusEffect(
    useCallback(() => {
      const prefixes = eventsKey ? eventsKey.split('|') : [];
      const plan = pollPlan(live, intervalMs, prefixes);
      void fnRef.current();
      const id = setInterval(() => void fnRef.current(), plan.intervalMs);
      let pending: ReturnType<typeof setTimeout> | null = null;
      const off = plan.eventDriven
        ? onLiveEvent((e) => {
            if (!matchesEvent(e.type, prefixes) || pending) return;
            pending = setTimeout(() => {
              pending = null;
              void fnRef.current();
            }, EVENT_DEBOUNCE_MS);
          })
        : () => {};
      return () => {
        clearInterval(id);
        if (pending) clearTimeout(pending);
        off();
      };
    }, [intervalMs, live, eventsKey]),
  );
}

/** Whether the desktop core's event stream is up right now. */
function useLive(): boolean {
  const [v, set] = useState(isLiveConnected());
  useEffect(() => {
    const off = subscribeLive(set);
    set(isLiveConnected()); // a change between render and subscribe (see useConfig)
    return off;
  }, []);
  return v;
}
