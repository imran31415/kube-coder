import { render, waitFor } from '@testing-library/preact';
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import type { PreviewMessage, PreviewState } from '../../api/gatewayPreview';

// Thread rotation vs. our own first turn (issue #524).
//
// The gateway mints `default_thread_id` only AFTER the first dispatch, so the
// first message on a freshly-linked identity — and every "new chat" — rotates
// the live thread id inside the refresh() that immediately follows our own
// send. The rotation-reset effect used to treat that as an external re-bind and
// cancel the THINKING state for the very turn this surface had just started:
// the orb flashed THINKING… and went idle while the agent was still working.

let stateNow: PreviewState;
let pushEvent: ((ev: { type: string }) => void) | null = null;
// The cursor the gateway reports back for our send — i.e. the newest seq at
// the moment the message was accepted. Anything newer than it that comes back
// outbound is the answer to OUR turn (the #474 watermark), so each test sets it
// to match the state it seeded.
let sendCursor = 0;
const sendPreviewMock = vi.fn(() =>
  Promise.resolve({ ok: true, action: 'sent', cursor: sendCursor }),
);

vi.mock('../../api/gatewayPreview', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/gatewayPreview')>()),
  fetchPreview: vi.fn(() => Promise.resolve(stateNow)),
  sendPreview: (...args: unknown[]) => sendPreviewMock(...(args as [])),
  previewControl: vi.fn(() => Promise.resolve({ ok: true, linked: true })),
}));
vi.mock('../../api/events', () => ({
  subscribeEvents: vi.fn((handler: (ev: { type: string }) => void) => {
    pushEvent = handler;
    return () => {
      pushEvent = null;
    };
  }),
}));

import { WalkieTalkie } from './WalkieTalkie';
import { setSpeakReplies } from './voice';

// Minimal SpeechRecognition stand-in — same shape as WalkieTalkie.busy.test.tsx.
interface FakeRec {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  onresult: ((e: unknown) => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
}
let lastRec: FakeRec | null = null;

class FakeRecognition implements FakeRec {
  lang = '';
  interimResults = false;
  continuous = false;
  onresult: ((e: unknown) => void) | null = null;
  onerror: ((e: { error: string }) => void) | null = null;
  onend: (() => void) | null = null;
  start() {
    lastRec = this;
  }
  stop() {
    this.onend?.();
  }
  abort() {
    this.onend?.();
  }
}

function msg(partial: Partial<PreviewMessage> & { seq: number }): PreviewMessage {
  return {
    ts: 0,
    direction: 'in',
    kind: 'message',
    text: '',
    quick_replies: [],
    wire: null,
    meta: {},
    ...partial,
  };
}

function makeState(
  messages: PreviewMessage[],
  thread: string | null,
  busy = false,
): PreviewState {
  return {
    available: true,
    messages,
    cursor: messages.length ? messages[messages.length - 1].seq : 0,
    linked: true,
    simulate_out_of_window: false,
    provider: 'internal',
    identity: 'internal:local',
    busy,
    thread_id: thread,
  };
}

const win = window as unknown as Record<string, unknown>;

describe('WalkieTalkie thread rotation (issue #524)', () => {
  beforeEach(() => {
    lastRec = null;
    sendCursor = 0;
    sendPreviewMock.mockClear();
    stateNow = makeState([], null);
    win.SpeechRecognition = FakeRecognition;
    win.SpeechSynthesisUtterance = class {
      text: string;
      constructor(text: string) {
        this.text = text;
      }
    };
    win.speechSynthesis = { speaking: false, pending: false, speak: vi.fn(), cancel: vi.fn() };
  });
  afterEach(() => {
    setSpeakReplies(false);
    delete win.SpeechRecognition;
    delete win.speechSynthesis;
    delete win.SpeechSynthesisUtterance;
  });

  async function findOrb(container: Element): Promise<HTMLButtonElement> {
    return waitFor(() => {
      const el = container.querySelector('button.wt-orb') as HTMLButtonElement;
      expect(el).not.toBeNull();
      expect(el.disabled).toBe(false);
      return el;
    });
  }

  function phaseOf(container: Element): string | null {
    return container.querySelector('.wt')!.getAttribute('data-phase');
  }

  /** Wait until the pushed snapshot has landed AND every phase change it
   *  triggered has been painted. Asserting on a bare tick can outrun the
   *  fetch → setState → effect → re-render chain and read a stale data-phase,
   *  which would pass with or without the fix. The History toggle only appears
   *  once a snapshot with off-card messages has rendered. */
  async function settled(container: Element) {
    await waitFor(() =>
      expect(container.querySelector('.wt-history-toggle')).not.toBeNull(),
    );
    await new Promise((r) => setTimeout(r, 25));
  }

  /** Push a spoken message through the orb → sendVoice(). */
  async function speak(container: Element, text: string) {
    const orb = await findOrb(container);
    orb.click(); // idle -> listening
    lastRec!.onresult?.({
      resultIndex: 0,
      results: [{ isFinal: true, 0: { transcript: text }, length: 1 }],
    });
    lastRec!.stop(); // -> onend -> captured -> sendVoice()
    await waitFor(() => expect(sendPreviewMock).toHaveBeenCalledWith(text));
    await waitFor(() => expect(phaseOf(container)).toBe('thinking'));
  }

  it('keeps THINKING… when our own first send mints the thread (null → new)', async () => {
    stateNow = makeState([], null); // freshly linked: no thread yet
    sendCursor = 1; // our message lands as seq 1
    const { container } = render(<WalkieTalkie />);
    await speak(container, 'build me a todo app');

    // The turn we just started creates the thread — the id rotates under us
    // while the agent is still working.
    stateNow = makeState(
      [msg({ seq: 1, direction: 'in', text: 'build me a todo app', meta: { thread_id: 't-new' } })],
      't-new',
      true,
    );
    pushEvent?.({ type: 'gateway.preview' });

    await settled(container);
    expect(phaseOf(container)).toBe('thinking');

    // …and the reply for that same turn still settles it.
    stateNow = makeState(
      [
        msg({ seq: 1, direction: 'in', text: 'build me a todo app', meta: { thread_id: 't-new' } }),
        msg({ seq: 2, direction: 'out', text: 'On it.', meta: { thread_id: 't-new' } }),
      ],
      't-new',
    );
    pushEvent?.({ type: 'gateway.preview' });
    await waitFor(() => expect(phaseOf(container)).toBe('idle'));
    expect(container.querySelector('.wt-card-text')?.textContent).toBe('On it.');
  });

  it('keeps THINKING… when our own send starts a new chat (old thread → new)', async () => {
    stateNow = makeState(
      [msg({ seq: 1, direction: 'out', text: 'earlier reply', meta: { thread_id: 't-old' } })],
      't-old',
    );
    sendCursor = 2; // our message lands as seq 2, after the old thread's reply
    const { container } = render(<WalkieTalkie />);
    await speak(container, 'new chat');

    stateNow = makeState(
      [
        msg({ seq: 1, direction: 'out', text: 'earlier reply', meta: { thread_id: 't-old' } }),
        msg({ seq: 2, direction: 'in', text: 'new chat', meta: { thread_id: 't-fresh' } }),
      ],
      't-fresh',
      true,
    );
    pushEvent?.({ type: 'gateway.preview' });

    await settled(container);
    expect(phaseOf(container)).toBe('thinking');
  });

  it('a rotation with no send of ours in flight still resets — an idle surface never spins up (#474)', async () => {
    stateNow = makeState([], null);
    sendCursor = 1;
    const { container } = render(<WalkieTalkie />);
    await speak(container, 'hello');

    // Our own rotation + reply: the turn resolves and ownership is released.
    stateNow = makeState(
      [
        msg({ seq: 1, direction: 'in', text: 'hello', meta: { thread_id: 't-1' } }),
        msg({ seq: 2, direction: 'out', text: 'hi', meta: { thread_id: 't-1' } }),
      ],
      't-1',
    );
    pushEvent?.({ type: 'gateway.preview' });
    await waitFor(() => expect(phaseOf(container)).toBe('idle'));

    // Someone else re-binds the identity to another thread and makes it busy.
    // Nothing of ours is in flight, so the orb must stay idle.
    stateNow = makeState(
      [msg({ seq: 3, direction: 'in', text: 'from whatsapp', meta: { thread_id: 't-2' } })],
      't-2',
      true,
    );
    pushEvent?.({ type: 'gateway.preview' });

    // The rotated snapshot has landed once the old thread's card is gone.
    await waitFor(() => expect(container.querySelector('.wt-card-text')).toBeNull());
    await new Promise((r) => setTimeout(r, 25));
    expect(phaseOf(container)).toBe('idle');
  });
});
