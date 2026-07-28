import { render, fireEvent, waitFor } from '@testing-library/preact';
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import type { HvEvent } from './transcript';

// Transcript scroll pinning (#530). The reported symptom: after sending, the
// transcript lands *short* of the newest message. happy-dom does no layout, so
// these tests install a tiny fake one on the scroll container — every rendered
// message block is 100px tall — which makes the regression reproducible: the
// "Working…" placeholder that appears once a send is in flight is rendered
// outside `turns`, so a pin keyed only on `turns` stops one block too high.

vi.mock('../../store/hypervisor', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../store/hypervisor')>();
  return {
    ...actual,
    // Same optimistic append the real one does, minus the network.
    sendMessage: vi.fn(async (text: string) => {
      actual.sending.value = true;
      actual.events.value = [
        ...actual.events.value,
        { seq: -1, ts: 0, role: 'user', type: 'message', text },
      ];
    }),
  };
});

import { Chat } from './Chat';
import { events, activeThreadId, activeStatus, sending, config } from '../../store/hypervisor';

const BLOCK_H = 100;
const realFetch = globalThis.fetch;

function msg(seq: number, role: HvEvent['role'], text: string): HvEvent {
  return { seq, ts: seq, role, type: 'message', text };
}

/** Fake layout for the transcript viewport: height derived from the rendered
 *  message blocks, scrollTop clamped like a real scroller. */
function fakeLayout(el: Element, clientHeight = 250) {
  let top = 0;
  let clientH = clientHeight;
  const scrollHeight = () => el.querySelectorAll('.hv-msg, .hv-turn').length * BLOCK_H;
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: scrollHeight });
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => clientH });
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => top,
    set: (v: number) => {
      top = Math.max(0, Math.min(v, Math.max(0, scrollHeight() - clientH)));
    },
  });
  return {
    /** Distance from the bottom, the way the component measures it (a viewport
     *  taller than the content still counts as "at the bottom"). */
    fromBottom: () => Math.max(0, scrollHeight() - top - clientH),
    scrollHeight,
    setClientHeight: (h: number) => {
      clientH = h;
    },
    scrollTo: (v: number) => {
      (el as HTMLElement).scrollTop = v;
      fireEvent.scroll(el);
    },
  };
}

/** Let the pin's next-frame write land. */
function nextFrame() {
  return new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
}

/** Let effects *and* their follow-up frame settle — for asserting that nothing
 *  moved, where a single frame would pass trivially. */
async function settle() {
  for (let i = 0; i < 4; i++) await nextFrame();
}

beforeEach(() => {
  globalThis.fetch = vi.fn(async () => ({
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: async () => ({}),
  })) as unknown as typeof fetch;
  config.value = {
    enabled: true,
    defaultAssistant: 'claude',
    workdir: '/home/dev',
    readOnly: false,
    assistants: [],
  };
  activeThreadId.value = 't1';
  activeStatus.value = 'idle';
  sending.value = false;
  // Long enough that the transcript actually scrolls under the fake layout.
  events.value = [
    msg(1, 'user', 'hello'),
    msg(2, 'assistant', 'hi there'),
    msg(3, 'user', 'and then?'),
    msg(4, 'assistant', 'this and that'),
    msg(5, 'user', 'go on'),
    msg(6, 'assistant', 'more still'),
  ];
});

afterEach(() => {
  globalThis.fetch = realFetch;
  events.value = [];
  activeThreadId.value = null;
  sending.value = false;
  config.value = null;
});

describe('Chat transcript pinning (#530)', () => {
  it('lands on the newest message after sending, past the thinking placeholder', async () => {
    const { container } = render(<Chat />);
    const el = container.querySelector('.hv-transcript') as HTMLElement;
    const layout = fakeLayout(el);

    const ta = container.querySelector('textarea') as HTMLTextAreaElement;
    fireEvent.input(ta, { target: { value: 'what next?' } });
    fireEvent.keyDown(ta, { key: 'Enter' });

    // The optimistic user bubble *and* the "Working…" turn are both on screen.
    await waitFor(() => expect(container.querySelectorAll('.hv-turn').length).toBe(4));
    await waitFor(() => expect(layout.fromBottom()).toBe(0));
  });

  it('re-pins when the thinking placeholder appears without any new event', async () => {
    // The turn goes running from the 2s poll: "Working…" is rendered outside
    // `turns`, so a pin keyed only on the event list never fires and the newest
    // message ends up a block above the fold.
    const { container } = render(<Chat />);
    const el = container.querySelector('.hv-transcript') as HTMLElement;
    const layout = fakeLayout(el);
    // A user message lands (and is followed) …
    events.value = [...events.value, msg(7, 'user', 'ping')];
    await waitFor(() => expect(layout.fromBottom()).toBe(0));

    // … then the turn goes running, adding a block the event list knows nothing
    // about.
    activeStatus.value = 'running';
    await waitFor(() => expect(container.textContent).toContain('Working…'));
    await waitFor(() => expect(layout.fromBottom()).toBe(0));
  });

  it('follows a streaming reply that keeps growing after the send', async () => {
    const { container } = render(<Chat />);
    const el = container.querySelector('.hv-transcript') as HTMLElement;
    const layout = fakeLayout(el);

    const ta = container.querySelector('textarea') as HTMLTextAreaElement;
    fireEvent.input(ta, { target: { value: 'go' } });
    fireEvent.keyDown(ta, { key: 'Enter' });
    await waitFor(() => expect(layout.fromBottom()).toBe(0));

    // The agent's turn arrives, then a second one — each must be followed.
    events.value = [...events.value, msg(7, 'assistant', 'thinking about it')];
    await waitFor(() => expect(layout.fromBottom()).toBe(0));
    events.value = [...events.value, msg(8, 'assistant', 'and here is the answer')];
    await waitFor(() => expect(layout.fromBottom()).toBe(0));
  });

  it('leaves a reader who scrolled up alone when the poll brings new events', async () => {
    const { container } = render(<Chat />);
    const el = container.querySelector('.hv-transcript') as HTMLElement;
    const layout = fakeLayout(el);
    await settle();

    layout.scrollTo(0); // scrolled up to read history — unpins
    events.value = [...events.value, msg(7, 'assistant', 'a poll result')];
    await settle();
    expect(el.scrollTop).toBe(0);

    // Scrolling back down re-pins, and the next event follows again.
    layout.scrollTo(layout.scrollHeight());
    events.value = [...events.value, msg(8, 'assistant', 'another')];
    await waitFor(() => expect(layout.fromBottom()).toBe(0));
  });

  it('stays pinned when the composer grows under it (#348 echo guard)', async () => {
    const { container } = render(<Chat />);
    const el = container.querySelector('.hv-transcript') as HTMLElement;
    const layout = fakeLayout(el);
    await settle();
    expect(layout.fromBottom()).toBe(0);

    // The composer auto-grows: the viewport shrinks *after* the pin write, so
    // the scroll echo measures a stale "not at the bottom" — it must not unpin.
    layout.setClientHeight(150);
    fireEvent.scroll(el);
    events.value = [...events.value, msg(7, 'assistant', 'still following')];
    await waitFor(() => expect(layout.fromBottom()).toBe(0));
  });
});
