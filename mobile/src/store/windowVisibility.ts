/**
 * Whether the app's window can be seen right now (#833).
 *
 * The desktop app keeps its window, and the web process with it, alive while
 * it is hidden or minimised (macOS hides it on close for up to
 * KC_DESKTOP_UNLOAD_AFTER_MIN minutes). Polling screens, the sidebar's Feed
 * count and CSS spinners have nothing to show anyone then, so they pause and
 * refresh the moment the window is back. On the phone the same signal is the
 * app going to the background.
 *
 * Fed from React Native's AppState (src/util/useWindowVisible.ts), which on
 * web — the desktop app's webview included — follows
 * `document.visibilityState`. Pure (no react-native imports) so the node-side
 * vitest covers it.
 */

let visible = true;
const listeners = new Set<(v: boolean) => void>();

/** AppState's value → visible. Only `background` hides: iOS reports
 *  `inactive` while the notification centre is pulled down over the app,
 *  which is still on screen. */
export function visibleFromAppState(state: string | null | undefined): boolean {
  return state !== 'background';
}

export function setWindowVisible(v: boolean): void {
  if (v === visible) return;
  visible = v;
  for (const l of [...listeners]) l(v);
}

export function isWindowVisible(): boolean {
  return visible;
}

export function subscribeWindowVisible(l: (v: boolean) => void): () => void {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
}

/** Test seam: module state outlives a vitest file otherwise. */
export function _resetWindowVisibleForTest(): void {
  visible = true;
  listeners.clear();
}

/** Test seam: how many subscribers are live. */
export function _windowVisibleListenerCount(): number {
  return listeners.size;
}
