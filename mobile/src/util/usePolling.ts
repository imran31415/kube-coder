/** Focus-aware polling. */
import { useFocusEffect } from '@react-navigation/native';
import { useCallback, useRef } from 'react';

/**
 * Run `fn` immediately and then every `intervalMs` — but only while the screen
 * is focused. Tab screens stay mounted when the user switches tabs, so a plain
 * setInterval keeps hammering the API (and the battery) from every tab at
 * once; this stops the timer on blur and refreshes immediately on refocus so
 * the screen never shows stale data.
 */
export function usePolling(fn: () => void | Promise<void>, intervalMs: number): void {
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useFocusEffect(
    useCallback(() => {
      void fnRef.current();
      const id = setInterval(() => void fnRef.current(), intervalMs);
      return () => clearInterval(id);
    }, [intervalMs]),
  );
}
