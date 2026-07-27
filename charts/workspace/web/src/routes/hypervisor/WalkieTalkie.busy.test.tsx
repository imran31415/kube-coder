import { render, waitFor } from '@testing-library/preact';
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import type { PreviewMessage, PreviewState } from '../../api/gatewayPreview';

// Turn ownership + thread scoping (issue #474): the orb must only show
// THINKING… for a turn THIS surface sent — never for activity elsewhere on
// the bound thread (Hypervisor chat, WhatsApp, a stuck/crashed session) —
// and the live card/narration must only ever reflect the CURRENT thread,
// never bleed in a foreign or since-rotated conversation.

let stateNow: PreviewState;
let pushEvent: ((ev: { type: string }) => void) | null = null;
const sendPreviewMock = vi.fn(() => Promise.resolve({ ok: true, action: 'sent', cursor: 0 }));

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

import { WalkieTalkie, messageThreadId, inLiveThread } from './WalkieTalkie';
import { setSpeakReplies } from './voice';

// Minimal SpeechRecognition stand-in — same shape as WalkieTalkie.stop.test.tsx.
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

function makeState(messages: PreviewMessage[]): PreviewState {
  return {
    available: true,
    messages,
    cursor: messages.length ? messages[messages.length - 1].seq : 0,
    linked: true,
    simulate_out_of_window: false,
    provider: 'internal',
    identity: 'internal:local',
    busy: false,
    thread_id: 't-test',
  };
}

const win = window as unknown as Record<string, unknown>;

describe('WalkieTalkie turn ownership + thread scoping (issue #474)', () => {
  beforeEach(() => {
    lastRec = null;
    sendPreviewMock.mockClear();
    stateNow = makeState([]);
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

  function chipLabel(container: Element): string | null {
    return container.querySelector('.wt-chip-label')?.textContent ?? null;
  }

  it('never shows THINKING… on mount when the server reports busy but this surface sent nothing', async () => {
    stateNow = { ...makeState([]), busy: true };
    const { container } = render(<WalkieTalkie />);
    await waitFor(() => {
      expect(phaseOf(container)).toBe('idle');
    });
    expect(chipLabel(container)).not.toBe('THINKING…');
    expect(chipLabel(container)).toBe('LINKED');
  });

  it('activity on the bound thread from elsewhere does not spin the orb', async () => {
    stateNow = makeState([]);
    const { container } = render(<WalkieTalkie />);
    await findOrb(container);
    expect(phaseOf(container)).toBe('idle');

    // Some other surface (Hypervisor chat / WhatsApp / a stuck session) makes
    // the bound thread busy — this client never sent anything.
    stateNow = { ...makeState([]), busy: true };
    pushEvent?.({ type: 'gateway.preview' });

    // Give the effect a tick, then assert it never moved off idle.
    await new Promise((r) => setTimeout(r, 0));
    expect(phaseOf(container)).toBe('idle');
  });

  it('shows THINKING… for a turn this surface sent, and settles once its reply lands', async () => {
    const { container } = render(<WalkieTalkie />);
    const orb = await findOrb(container);

    orb.click(); // idle -> listening
    lastRec!.onresult?.({
      resultIndex: 0,
      results: [{ isFinal: true, 0: { transcript: 'what is running' }, length: 1 }],
    });
    lastRec!.stop(); // -> onend -> captured -> sendVoice()

    await waitFor(() => {
      expect(sendPreviewMock).toHaveBeenCalledWith('what is running');
    });
    // dispatch('sent') moves sending -> thinking as soon as the send resolves.
    await waitFor(() => expect(phaseOf(container)).toBe('thinking'));

    // Server confirms busy while we own the turn — stays thinking.
    stateNow = {
      ...makeState([msg({ seq: 1, direction: 'in', text: 'what is running' })]),
      busy: true,
    };
    pushEvent?.({ type: 'gateway.preview' });
    await new Promise((r) => setTimeout(r, 0));
    expect(phaseOf(container)).toBe('thinking');

    // The reply lands — settles to idle.
    stateNow = makeState([
      msg({ seq: 1, direction: 'in', text: 'what is running' }),
      msg({ seq: 2, direction: 'out', text: 'Nothing is running.' }),
    ]);
    pushEvent?.({ type: 'gateway.preview' });
    await waitFor(() => expect(phaseOf(container)).toBe('idle'));
  });

  it('does not re-enter THINKING… when busy resurfaces after our own turn already resolved (watermark is consumed, not sticky)', async () => {
    const { container } = render(<WalkieTalkie />);
    const orb = await findOrb(container);

    orb.click();
    lastRec!.onresult?.({
      resultIndex: 0,
      results: [{ isFinal: true, 0: { transcript: 'ping' }, length: 1 }],
    });
    lastRec!.stop();
    await waitFor(() => expect(sendPreviewMock).toHaveBeenCalled());

    stateNow = makeState([
      msg({ seq: 1, direction: 'in', text: 'ping' }),
      msg({ seq: 2, direction: 'out', text: 'pong' }),
    ]);
    pushEvent?.({ type: 'gateway.preview' });
    await waitFor(() => expect(phaseOf(container)).toBe('idle'));

    // Someone/something else makes the thread busy again, with a NEW message
    // this client never sent — proves the update below is a change, not the
    // stale idle from before.
    stateNow = {
      ...makeState([
        msg({ seq: 1, direction: 'in', text: 'ping' }),
        msg({ seq: 2, direction: 'out', text: 'pong' }),
        msg({ seq: 3, direction: 'in', text: 'someone else typed this' }),
      ]),
      busy: true,
    };
    pushEvent?.({ type: 'gateway.preview' });
    await waitFor(() => {
      expect(container.querySelector('.wt-you')?.textContent).toContain('someone else typed this');
    });
    expect(phaseOf(container)).toBe('idle');
  });

  it('defensively settles a turn that never resolves (max-thinking watchdog)', async () => {
    vi.useFakeTimers();
    try {
      const { container } = render(<WalkieTalkie />);
      const orb = await findOrb(container);
      orb.click();
      lastRec!.onresult?.({
        resultIndex: 0,
        results: [{ isFinal: true, 0: { transcript: 'never answered' }, length: 1 }],
      });
      lastRec!.stop();
      await vi.waitFor(() => expect(sendPreviewMock).toHaveBeenCalled());
      await vi.waitFor(() => expect(phaseOf(container)).toBe('thinking'));

      await vi.advanceTimersByTimeAsync(120_001);
      expect(phaseOf(container)).toBe('idle');
    } finally {
      vi.useRealTimers();
    }
  });

  it('scopes the live card + "you" bubble to the current thread and segments History with a divider', async () => {
    stateNow = {
      ...makeState([
        msg({ seq: 1, direction: 'in', text: 'first turn', meta: { thread_id: 'thread-a' } }),
        msg({ seq: 2, direction: 'out', text: 'first reply', meta: { thread_id: 'thread-a' } }),
        msg({ seq: 3, direction: 'in', text: 'second turn', meta: { thread_id: 'thread-b' } }),
        msg({ seq: 4, direction: 'out', text: 'second reply', meta: { thread_id: 'thread-b' } }),
      ]),
      thread_id: 'thread-b',
    };
    const { container } = render(<WalkieTalkie />);

    await waitFor(() => {
      expect(container.querySelector('.wt-card-text')?.textContent).toBe('second reply');
    });
    // thread-a's reply never bleeds into the live card.
    expect(container.querySelector('.wt-card-text')?.textContent).not.toBe('first reply');
    expect(container.querySelector('.wt-you')?.textContent).toContain('second turn');

    const toggle = container.querySelector('.wt-history-toggle') as HTMLButtonElement;
    expect(toggle).not.toBeNull();
    toggle.click();

    await waitFor(() => {
      expect(container.querySelector('.wt-thread-divider')).not.toBeNull();
    });
    expect(container.textContent).toContain('first turn');
  });

  it('a foreign thread reply does not get narrated aloud', async () => {
    setSpeakReplies(true);
    stateNow = {
      ...makeState([msg({ seq: 1, direction: 'in', text: 'hi', meta: { thread_id: 'thread-a' } })]),
      thread_id: 'thread-a',
    };
    const { container } = render(<WalkieTalkie />);
    await findOrb(container);

    // A reply lands for a DIFFERENT thread than the one live on screen.
    stateNow = {
      ...makeState([
        msg({ seq: 1, direction: 'in', text: 'hi', meta: { thread_id: 'thread-a' } }),
        msg({ seq: 2, direction: 'out', text: 'foreign reply', meta: { thread_id: 'thread-b' } }),
      ]),
      thread_id: 'thread-a',
    };
    pushEvent?.({ type: 'gateway.preview' });

    // Give the narration effect a tick — it must never have spoken/entered
    // 'speaking' for a thread that isn't live.
    await new Promise((r) => setTimeout(r, 0));
    expect(phaseOf(container)).not.toBe('speaking');
    expect(container.querySelector('.wt-card-text')?.textContent ?? '').not.toBe('foreign reply');
  });
});

describe('messageThreadId / inLiveThread (issue #474)', () => {
  it('reads a real thread id out of meta, or null when absent/blank', () => {
    expect(messageThreadId(msg({ seq: 1, meta: { thread_id: 't1' } }))).toBe('t1');
    expect(messageThreadId(msg({ seq: 2, meta: {} }))).toBeNull();
    expect(messageThreadId(msg({ seq: 3, meta: { thread_id: '' } }))).toBeNull();
    expect(messageThreadId(msg({ seq: 4, meta: { thread_id: 42 } }))).toBeNull();
  });

  it('an untagged message is always in the live thread; a tagged one only matches its own', () => {
    const untagged = msg({ seq: 1, meta: {} });
    const tagged = msg({ seq: 2, meta: { thread_id: 't1' } });
    expect(inLiveThread(untagged, 't1')).toBe(true);
    expect(inLiveThread(untagged, null)).toBe(true);
    expect(inLiveThread(tagged, 't1')).toBe(true);
    expect(inLiveThread(tagged, 't2')).toBe(false);
    expect(inLiveThread(tagged, null)).toBe(false);
  });
});
