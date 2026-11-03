/**
 * Polling an open Hypervisor thread (#833). The screen used to re-read the
 * whole transcript every 2 s and hand it to setState through an updater that,
 * on an idle chat, always bailed out — and React keeps each bailed-out update
 * (with the transcript its closure captured) queued until the component next
 * renders, which an idle chat never does. These pin the replacement: an idle
 * poll reads one event and reports no change, so the screen sets nothing.
 */
import { describe, expect, it, vi } from 'vitest';
import type { HvEvent, TranscriptSource } from '../api/types';
import { mergeTranscript, syncTranscript, transcriptSince, type HeldTranscript } from './hvTranscript';

const ev = (seq: number, text = `m${seq}`): HvEvent => ({
  seq,
  ts: seq,
  role: seq % 2 ? 'user' : 'assistant',
  type: 'message',
  text,
});
const range = (a: number, b: number) => Array.from({ length: b - a + 1 }, (_, i) => ev(a + i));

/** A server that answers like handle_hypervisor_get_thread: events with
 *  seq > since, from whichever source it reads. */
function server(events: HvEvent[], source: TranscriptSource = 'session_log') {
  const state = { events, source };
  const read = vi.fn(async (since: number) => ({
    thread: { id: 't1', status: 'idle' },
    events: state.events.filter((e) => e.seq > since),
    source: state.source,
  }));
  return { state, read };
}

describe('transcriptSince', () => {
  it('reads everything when there is nothing to stitch onto', () => {
    expect(transcriptSince({ events: [], source: 'session_log' })).toBe(0);
    expect(transcriptSince({ events: range(1, 3), source: null })).toBe(0);
  });

  it('asks from the event before the last, so the answer overlaps by one', () => {
    expect(transcriptSince({ events: range(1, 2000), source: 'capture' })).toBe(1999);
  });

  it('reads everything while the tail is an optimistic (negative seq) turn', () => {
    expect(transcriptSince({ events: [...range(1, 5), { ...ev(9), seq: -1 }], source: 'capture' })).toBe(0);
  });
});

describe('syncTranscript on an idle thread', () => {
  it('reads one event per poll and reports no change, however long it stays open', async () => {
    const srv = server(range(1, 2000));
    let held: HeldTranscript = { events: [], source: null };
    const first = await syncTranscript(held, srv.read);
    expect(first.next?.events).toHaveLength(2000);
    held = first.next!;
    for (let i = 0; i < 100; i++) {
      const r = await syncTranscript(held, srv.read);
      expect(r.next).toBeNull();
      expect(r.detail.events).toHaveLength(1);
    }
    expect(srv.read.mock.calls.slice(1).every(([since]) => since === 1999)).toBe(true);
  });
});

describe('syncTranscript as the thread moves', () => {
  it('appends only what is new, keeping the events it already held', async () => {
    const srv = server(range(1, 10));
    const held = (await syncTranscript({ events: [], source: null }, srv.read)).next!;
    srv.state.events = range(1, 13);
    const r = await syncTranscript(held, srv.read);
    expect(r.next!.events.map((e) => e.seq)).toEqual(range(1, 13).map((e) => e.seq));
    expect(r.next!.events[0]).toBe(held.events[0]);
    expect(r.detail.events).toHaveLength(4);
    expect(srv.read).toHaveBeenCalledTimes(2);
  });

  it('re-reads from the start when the source flips (seqs are re-stamped)', async () => {
    const srv = server(range(1, 10), 'capture');
    const held = (await syncTranscript({ events: [], source: null }, srv.read)).next!;
    srv.state.source = 'session_log';
    srv.state.events = range(1, 8);
    const r = await syncTranscript(held, srv.read);
    expect(srv.read.mock.calls.map(([s]) => s)).toEqual([0, 9, 0]);
    expect(r.next).toEqual({ events: range(1, 8), source: 'session_log' });
  });

  it('re-reads from the start when the overlap event is not the one held', async () => {
    const srv = server(range(1, 10));
    const held = (await syncTranscript({ events: [], source: null }, srv.read)).next!;
    // The last turn's text grew in place, or a trailing notice gave its seq
    // to the next message.
    srv.state.events = [...range(1, 9), ev(10, 'something else'), ev(11)];
    const r = await syncTranscript(held, srv.read);
    expect(srv.read.mock.calls.map(([s]) => s)).toEqual([0, 9, 0]);
    expect(r.next!.events.at(-2)!.text).toBe('something else');
  });

  it('replaces an optimistic user turn with the server copy', async () => {
    const srv = server(range(1, 4));
    const held = (await syncTranscript({ events: [], source: null }, srv.read)).next!;
    const optimistic: HeldTranscript = { events: [...held.events, { ...ev(5, 'hi'), seq: -1 }], source: held.source };
    srv.state.events = [...range(1, 4), ev(5, 'hi')];
    const r = await syncTranscript(optimistic, srv.read);
    expect(srv.read.mock.calls.at(-1)![0]).toBe(0);
    expect(r.next!.events.at(-1)).toEqual(ev(5, 'hi'));
  });
});

describe('mergeTranscript', () => {
  it('reports a full read of the same transcript as unchanged', () => {
    const held: HeldTranscript = { events: range(1, 3), source: 'capture' };
    expect(mergeTranscript(held, 0, { events: range(1, 3), source: 'capture' })).toBe('unchanged');
    expect(mergeTranscript(held, 0, { events: range(1, 4), source: 'capture' })).toEqual({
      events: range(1, 4),
      source: 'capture',
    });
  });

  it('cannot stitch an empty answer onto a held tail', () => {
    expect(mergeTranscript({ events: range(1, 3), source: 'capture' }, 2, { events: [], source: 'capture' })).toBe(
      'refetch',
    );
  });
});
