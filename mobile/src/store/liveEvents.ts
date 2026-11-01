/**
 * Live workspace events, for the desktop app (#833).
 *
 * The phone polls because `EventSource` cannot send an Authorization header
 * (see src/api/client.ts). The desktop core can: it holds `/api/events` with
 * the Bearer token and forwards each event here. While that stream is up, a
 * screen that opts in (`usePolling(fn, ms, ['feed.'])`) stops its interval and
 * refreshes when a matching event arrives instead, plus a slow safety refresh.
 * The moment the stream drops it goes back to its interval. On the phone and
 * in a browser nothing ever reports a stream, so nothing changes there.
 *
 * Pure (no react-native imports) so the node-side vitest covers it.
 */

export interface LiveEvent {
  type: string;
  data: Record<string, unknown>;
}

/** With the stream up, a screen still re-reads this often, in case an event
 *  was dropped (the server drops events for a slow consumer). */
export const SAFETY_POLL_MS = 5 * 60 * 1000;
/** A burst of events (a board run finishing ten items) is one refresh. */
export const EVENT_DEBOUNCE_MS = 400;

let connected = false;
const statusListeners = new Set<(v: boolean) => void>();
const eventListeners = new Set<(e: LiveEvent) => void>();

export function setLiveConnected(v: boolean): void {
  if (v === connected) return;
  connected = v;
  statusListeners.forEach((l) => l(v));
}

export function isLiveConnected(): boolean {
  return connected;
}

export function subscribeLive(l: (v: boolean) => void): () => void {
  statusListeners.add(l);
  return () => {
    statusListeners.delete(l);
  };
}

export function emitLiveEvent(e: LiveEvent): void {
  for (const l of [...eventListeners]) {
    try {
      l(e);
    } catch {
      // one bad subscriber must not starve the rest
    }
  }
}

export function onLiveEvent(l: (e: LiveEvent) => void): () => void {
  eventListeners.add(l);
  return () => {
    eventListeners.delete(l);
  };
}

/** True when an event type is one of the screen's prefixes ('feed.' etc). */
export function matchesEvent(type: string, prefixes: readonly string[]): boolean {
  return prefixes.some((p) => type === p || type.startsWith(p));
}

/** How a polling screen should refresh right now. */
export function pollPlan(
  live: boolean,
  intervalMs: number,
  events?: readonly string[],
): { intervalMs: number; eventDriven: boolean } {
  if (live && events && events.length > 0) {
    return { intervalMs: Math.max(intervalMs, SAFETY_POLL_MS), eventDriven: true };
  }
  return { intervalMs, eventDriven: false };
}

/** Test seam: module state outlives a vitest file otherwise. */
export function _resetLiveForTest(): void {
  connected = false;
  statusListeners.clear();
  eventListeners.clear();
}
