import { useEffect } from 'preact/hooks';

/**
 * Stack of active Escape handlers (most-recently-opened modal is last).
 * Only the topmost handler runs on a given Escape press so that nested
 * overlays (Drawer-inside-Sheet, Onboarding-over-Palette, etc.) close one
 * layer at a time instead of every layer at once.
 */
const stack: Array<() => void> = [];

let listenerAttached = false;
function ensureListener() {
  if (listenerAttached || typeof window === 'undefined') return;
  listenerAttached = true;
  window.addEventListener('keydown', (e: KeyboardEvent) => {
    if (e.key !== 'Escape') return;
    const top = stack[stack.length - 1];
    if (!top) return;
    e.stopPropagation();
    top();
  });
}

export function useEscape(active: boolean, onClose: () => void): void {
  useEffect(() => {
    if (!active) return;
    ensureListener();
    stack.push(onClose);
    return () => {
      const i = stack.lastIndexOf(onClose);
      if (i >= 0) stack.splice(i, 1);
    };
  }, [active, onClose]);
}

/** Test-only: clear all handlers between tests. Not exported from index. */
export function _resetEscapeStack(): void {
  stack.length = 0;
}
