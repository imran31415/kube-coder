import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import {
  subscribeEvents,
  eventStreamConnected,
  _resetEventStreamForTest,
} from './events';

/** Minimal EventSource stand-in — jsdom doesn't implement one. */
class MockEventSource {
  static instances: MockEventSource[] = [];
  url: string;
  withCredentials: boolean;
  closed = false;
  onerror: ((e: unknown) => void) | null = null;
  private listeners: Record<string, ((e: { data: string }) => void)[]> = {};

  constructor(url: string, opts?: { withCredentials?: boolean }) {
    this.url = url;
    this.withCredentials = !!opts?.withCredentials;
    MockEventSource.instances.push(this);
  }
  addEventListener(type: string, cb: (e: { data: string }) => void) {
    (this.listeners[type] ||= []).push(cb);
  }
  emit(type: string, data = '{}') {
    (this.listeners[type] || []).forEach((cb) => cb({ data }));
  }
  close() {
    this.closed = true;
  }
}

beforeEach(() => {
  MockEventSource.instances = [];
  (globalThis as unknown as { EventSource: unknown }).EventSource = MockEventSource;
  _resetEventStreamForTest();
  eventStreamConnected.value = false;
});

afterEach(() => {
  _resetEventStreamForTest();
  delete (globalThis as unknown as { EventSource?: unknown }).EventSource;
});

describe('event stream client', () => {
  it('opens one EventSource on first subscribe, closes on last unsubscribe', () => {
    const unsub = subscribeEvents(() => {});
    expect(MockEventSource.instances).toHaveLength(1);
    expect(MockEventSource.instances[0].withCredentials).toBe(true);
    unsub();
    expect(MockEventSource.instances[0].closed).toBe(true);
  });

  it('shares a single EventSource across multiple subscribers', () => {
    const u1 = subscribeEvents(() => {});
    const u2 = subscribeEvents(() => {});
    expect(MockEventSource.instances).toHaveLength(1);
    u1();
    expect(MockEventSource.instances[0].closed).toBe(false); // u2 still active
    u2();
    expect(MockEventSource.instances[0].closed).toBe(true);
  });

  it('marks connected on ready and disconnected on error/end', () => {
    subscribeEvents(() => {});
    const es = MockEventSource.instances[0];
    es.emit('ready');
    expect(eventStreamConnected.value).toBe(true);
    es.onerror?.(new Event('error'));
    expect(eventStreamConnected.value).toBe(false);
    es.emit('ready');
    es.emit('end');
    expect(eventStreamConnected.value).toBe(false);
  });

  it('dispatches task events with parsed JSON data', () => {
    const seen: unknown[] = [];
    subscribeEvents((ev) => seen.push(ev));
    MockEventSource.instances[0].emit(
      'task.status',
      JSON.stringify({ task_id: 't1', status: 'completed' }),
    );
    expect(seen).toEqual([
      { type: 'task.status', data: { task_id: 't1', status: 'completed' } },
    ]);
  });

  it('delivers an empty payload for malformed data without throwing', () => {
    const seen: unknown[] = [];
    subscribeEvents((ev) => seen.push(ev));
    MockEventSource.instances[0].emit('task.created', '{not json');
    expect(seen).toEqual([{ type: 'task.created', data: {} }]);
  });

  it('a throwing subscriber does not break delivery to others', () => {
    const seen: string[] = [];
    subscribeEvents(() => {
      throw new Error('boom');
    });
    subscribeEvents((ev) => seen.push(ev.type));
    MockEventSource.instances[0].emit('task.status', '{}');
    expect(seen).toEqual(['task.status']);
  });
});
