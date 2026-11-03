/** Focus-aware polling. */
import { useFocusEffect } from '@react-navigation/native';
import { useCallback, useEffect, useRef, useState } from 'react';
import { isLiveConnected, onLiveEventDebounced, pollPlan, subscribeLive } from '../store/liveEvents';
import { useWindowVisible } from './useWindowVisible';

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
 *
 * A hidden window (#833: the desktop app minimised or hidden, the phone app in
 * the background) does not poll at all; showing it again refreshes at once.
 */
export function usePolling(
  fn: () => void | Promise<void>,
  intervalMs: number,
  liveEvents?: readonly string[],
): void {
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const live = useLive();
  const visible = useWindowVisible();
  const eventsKey = (liveEvents ?? []).join('|');

  useFocusEffect(
    useCallback(() => {
      const prefixes = eventsKey ? eventsKey.split('|') : [];
      const plan = pollPlan(live, intervalMs, prefixes, visible);
      if (plan.paused) return;
      void fnRef.current();
      const id = setInterval(() => void fnRef.current(), plan.intervalMs);
      const off = plan.eventDriven ? onLiveEventDebounced(prefixes, () => void fnRef.current()) : () => {};
      return () => {
        clearInterval(id);
        off();
      };
    }, [intervalMs, live, visible, eventsKey]),
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
