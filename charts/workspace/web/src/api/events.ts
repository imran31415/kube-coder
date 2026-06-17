import { signal } from '@preact/signals';
import { withOauthPrefix } from './client';

/**
 * Client for the backend `/api/events` Server-Sent Events stream (issue #93).
 *
 * A single shared EventSource is opened the first time something subscribes
 * and closed when the last subscriber leaves (ref-counted). EventSource's
 * built-in reconnection handles drops, so we only track connectivity and fan
 * named events out to subscribers; `eventStreamConnected` lets the polling
 * stores fall back to a slow safety poll when the stream is up and resume
 * normal polling when it's down.
 *
 * Auth: EventSource can't send a Bearer header, so it relies on the same
 * cookie/ingress auth the SPA was served with — hence withOauthPrefix(), the
 * same prefix the rest of the API client uses.
 */

export const eventStreamConnected = signal(false);

export interface DashboardEvent {
  type: string;
  data: Record<string, unknown>;
}

type Handler = (ev: DashboardEvent) => void;

// Event names the backend emits. Add new ones here once the server publishes
// them (see EventBroker.publish in server.py).
const EVENT_TYPES = [
  'task.created',
  'task.status',
  'trigger.fired',
  'memory.changed',
];

let es: EventSource | null = null;
const handlers = new Set<Handler>();

function dispatch(type: string, raw: string): void {
  let data: Record<string, unknown> = {};
  try {
    data = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
  } catch {
    /* malformed frame — deliver an empty payload rather than throw */
  }
  for (const h of [...handlers]) {
    try {
      h({ type, data });
    } catch {
      /* a bad subscriber must not break delivery to the others */
    }
  }
}

function open(): void {
  if (es) return;
  if (typeof EventSource === 'undefined') return; // SSR / jsdom / unsupported
  es = new EventSource(withOauthPrefix('/api/events'), { withCredentials: true });
  es.addEventListener('ready', () => {
    eventStreamConnected.value = true;
  });
  for (const type of EVENT_TYPES) {
    es.addEventListener(type, (e) => dispatch(type, (e as MessageEvent).data));
  }
  // Server closed the stream gracefully (lifetime cap) — mark disconnected;
  // EventSource will reconnect on its own and fire `ready` again.
  es.addEventListener('end', () => {
    eventStreamConnected.value = false;
  });
  es.onerror = () => {
    // Fires on disconnect; EventSource auto-reconnects unless we close().
    eventStreamConnected.value = false;
  };
}

function close(): void {
  if (es) {
    es.close();
    es = null;
  }
  eventStreamConnected.value = false;
}

/**
 * Subscribe to dashboard events. Opens the shared stream on the first
 * subscriber. Returns an unsubscribe fn that closes the stream when the last
 * subscriber leaves.
 */
export function subscribeEvents(handler: Handler): () => void {
  handlers.add(handler);
  if (handlers.size === 1) open();
  return () => {
    handlers.delete(handler);
    if (handlers.size === 0) close();
  };
}

// Exposed for tests.
export function _resetEventStreamForTest(): void {
  handlers.clear();
  close();
}
