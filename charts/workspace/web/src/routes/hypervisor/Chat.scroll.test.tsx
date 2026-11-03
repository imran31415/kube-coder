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
import {
  events,
  activeThreadId,
  activeStatus,
  sending,
  config,
  transcriptSource,
} from '../../store/hypervisor';

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
  transcriptSource.value = 'capture';
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
  transcriptSource.value = null;
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


describe('Chat transcript pinning across a source flip (#644)', () => {
  /** What the server hands back once the turn completes: the same conversation
   *  re-stamped from Claude Code's JSONL, carrying the structured tool detail
   *  the live capture never showed — so it is *longer* than what it replaces. */
  const sessionLog = () => [
    msg(1, 'user', 'hello'),
    msg(2, 'assistant', 'hi there'),
    msg(3, 'user', 'and then?'),
    msg(4, 'assistant', 'this and that'),
    msg(5, 'user', 'go on'),
    msg(6, 'assistant', 'more still'),
    msg(7, 'assistant', 'ran a tool'),
    msg(8, 'assistant', 'read a file'),
    msg(9, 'assistant', 'THE FINAL ANSWER'),
  ];

  it('lands on the new answer when a completing turn swaps in the session log', async () => {
    activeStatus.value = 'running';
    const { container } = render(<Chat />);
    const el = container.querySelector('.hv-transcript') as HTMLElement;
    const layout = fakeLayout(el);
    await waitFor(() => expect(layout.fromBottom()).toBe(0));

    // The turn completes: source flips and every event is replaced.
    transcriptSource.value = 'session_log';
    events.value = sessionLog();
    activeStatus.value = 'idle';
    await settle();

    expect(layout.fromBottom()).toBe(0);
  });

  it('survives the reflow scroll the swap fires — the reader never moved', async () => {
    // The regression. Re-laying out a wholesale-replaced transcript makes the
    // browser clamp scrollTop and fire a `scroll`; read as a gesture, it unpins
    // a reader who never touched the page, stranding them up in older messages
    // because the session log is longer than the capture it replaced.
    activeStatus.value = 'running';
    const { container } = render(<Chat />);
    const el = container.querySelector('.hv-transcript') as HTMLElement;
    const layout = fakeLayout(el);
    await waitFor(() => expect(layout.fromBottom()).toBe(0));

    transcriptSource.value = 'session_log';
    events.value = sessionLog();
    activeStatus.value = 'idle';
    // Commit the swap first — a browser cannot clamp before the new content
    // exists — then let the reflow fire its scroll.
    await nextFrame();
    layout.scrollTo(200);
    await settle();

    expect(layout.fromBottom()).toBe(0);
  });

  it('still leaves a reader who scrolled up alone across the flip', async () => {
    // The hold must not become "always follow": someone reading history when
    // the turn completes keeps their place.
    activeStatus.value = 'running';
    const { container } = render(<Chat />);
    const el = container.querySelector('.hv-transcript') as HTMLElement;
    const layout = fakeLayout(el);
    await settle();

    layout.scrollTo(0); // deliberately reading history — unpins
    await settle();

    transcriptSource.value = 'session_log';
    events.value = sessionLog();
    activeStatus.value = 'idle';
    await settle();

    expect(el.scrollTop).toBe(0);
  });

  it('unpins normally again once the swap has settled', async () => {
    // The hold is scoped to the flip: a genuine scroll away afterwards still
    // stops the poll from yanking the reader down.
    activeStatus.value = 'running';
    const { container } = render(<Chat />);
    const el = container.querySelector('.hv-transcript') as HTMLElement;
    const layout = fakeLayout(el);
    await waitFor(() => expect(layout.fromBottom()).toBe(0));

    transcriptSource.value = 'session_log';
    events.value = sessionLog();
    activeStatus.value = 'idle';

    // Scroll away only once the swap hold has actually lifted, and prove it
    // lifted by the scroll STICKING. `settle()` cannot express this: it counts
    // frames from the signal write, while the component starts its two-frame
    // hold when it re-renders — so on a loaded machine the frames run out
    // before the hold even begins. A scroll landing inside the hold is read as
    // reflow echo and pinned back to the bottom (Chat.tsx `swapEchoRef`),
    // which is a real, permanent 350 rather than a value that settles to 0 a
    // frame later. Reproduced 1-in-12 under 4-way CPU load; see #661.
    await waitFor(() => {
      layout.scrollTo(0);
      expect(el.scrollTop).toBe(0);
    });

    events.value = [...events.value, msg(10, 'assistant', 'a later poll result')];
    await settle();

    expect(el.scrollTop).toBe(0);
  });
});

describe('Chat transcript — reading back through history', () => {
  /** 80 alternating user/assistant turns: the tail window of 30 hides 50. */
  function longThread(): HvEvent[] {
    return Array.from({ length: 80 }, (_, i) =>
      msg(i + 1, i % 2 === 0 ? 'user' : 'assistant', `turn ${i}`),
    );
  }

  it('pulls older turns in on the way to the top, keeping the reader on the same message', async () => {
    events.value = longThread();
    const { container } = render(<Chat />);
    const el = container.querySelector('.hv-transcript') as HTMLElement;
    const layout = fakeLayout(el);
    await settle(); // let the mount-time pin finish before the reader moves
    const rendered = () => container.querySelectorAll('.hv-msg, .hv-turn').length;
    expect(rendered()).toBe(30);

    // Scroll up to the edge of the window. Without this the reader stopped dead
    // at the top and had to find the "Show earlier" button.
    layout.scrollTo(300);
    await waitFor(() => expect(rendered()).toBe(60));

    // 30 blocks (3000px) landed above the viewport: the same message must stay
    // under the reader's eye, i.e. scrollTop moves by exactly what was added.
    expect(el.scrollTop).toBe(300 + 30 * BLOCK_H);
    // And it is still a reader looking at history, not pinned to the bottom.
    expect(layout.fromBottom()).toBeGreaterThan(0);
  });

  it('does not reveal anything while the reader is well clear of the top', async () => {
    events.value = longThread();
    const { container } = render(<Chat />);
    const el = container.querySelector('.hv-transcript') as HTMLElement;
    const layout = fakeLayout(el);
    await settle(); // let the mount-time pin finish before the reader moves

    layout.scrollTo(1500);
    await settle();
    expect(container.querySelectorAll('.hv-msg, .hv-turn').length).toBe(30);
  });

  it('keeps the place when the Show earlier button is used instead', async () => {
    events.value = longThread();
    const { container } = render(<Chat />);
    const el = container.querySelector('.hv-transcript') as HTMLElement;
    const layout = fakeLayout(el);
    await settle(); // let the mount-time pin finish before the reader moves
    layout.scrollTo(1500); // clear of the auto-reveal edge
    await settle();

    fireEvent.click(container.querySelector('.hv-earlier-btn') as HTMLElement);
    await waitFor(() =>
      expect(container.querySelectorAll('.hv-msg, .hv-turn').length).toBe(60),
    );
    expect(el.scrollTop).toBe(1500 + 30 * BLOCK_H);
  });

  it('stops revealing once the whole history is on screen', async () => {
    events.value = longThread();
    const { container } = render(<Chat />);
    const el = container.querySelector('.hv-transcript') as HTMLElement;
    const layout = fakeLayout(el);
    await settle(); // let the mount-time pin finish before the reader moves
    const rendered = () => container.querySelectorAll('.hv-msg, .hv-turn').length;

    layout.scrollTo(0);
    await waitFor(() => expect(rendered()).toBe(60));
    layout.scrollTo(0);
    await waitFor(() => expect(rendered()).toBe(80));
    layout.scrollTo(0);
    await settle();
    expect(rendered()).toBe(80);
    expect(container.querySelector('.hv-earlier-btn')).toBeNull();
  });
});

describe('Chat transcript — a heavy embed scrolled out of view keeps its height (#814)', () => {
  // happy-dom's IntersectionObserver never calls back, so drive it by hand.
  const realIO = globalThis.IntersectionObserver;
  let notify: ((isIntersecting: boolean) => void) | null = null;

  beforeEach(() => {
    globalThis.IntersectionObserver = class {
      constructor(cb: IntersectionObserverCallback) {
        notify = (isIntersecting) =>
          cb([{ isIntersecting } as IntersectionObserverEntry], this as unknown as IntersectionObserver);
      }
      observe() {}
      unobserve() {}
      disconnect() {}
      takeRecords() {
        return [];
      }
    } as unknown as typeof IntersectionObserver;
  });

  afterEach(() => {
    globalThis.IntersectionObserver = realIO;
    notify = null;
  });

  it('holds its slot at the measured height, not the minHeight guess', async () => {
    events.value = [
      msg(1, 'user', 'show me the chart'),
      {
        seq: 2,
        ts: 2,
        role: 'assistant',
        type: 'tool_call',
        tool_id: 'r1',
        tool: { name: 'mcp__dashboard__show_media', input: { url: 'https://example.test/chart.png', height: 300 } },
      },
    ];
    const { container } = render(<Chat />);
    const slot = () => container.querySelector('.hv-lazyblock') as HTMLElement;
    // Never seen yet: only the guess (the tool's own height) is available.
    expect(slot().style.minHeight).toBe('300px');

    notify!(true); // scrolled into view — the real embed mounts
    await waitFor(() => expect(slot().childElementCount).toBeGreaterThan(0));
    // Laid out taller than the guess (title bar, caption, …).
    Object.defineProperty(slot(), 'offsetHeight', { configurable: true, get: () => 420 });

    notify!(false); // scrolled well away — unmounted to a placeholder
    await waitFor(() => expect(slot().childElementCount).toBe(0));
    // The placeholder keeps the height the reader actually saw, so nothing
    // below it moves while they scroll past.
    expect(slot().style.minHeight).toBe('420px');
  });
});
