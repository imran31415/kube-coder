import { render, waitFor } from '@testing-library/preact';
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import type { PreviewMessage, PreviewState } from '../../api/gatewayPreview';

// Explicit stop controls (issue #409 follow-up): while the mic captures, a
// visible "Stop & send" pill ends the capture through the same phase-machine
// path as tapping the orb; while TTS narrates, "Stop voice" cuts playback.

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

import { WalkieTalkie } from './WalkieTalkie';
import { setSpeakReplies } from './voice';

// Minimal SpeechRecognition stand-in: start() registers the instance so the
// test can feed results; stop()/abort() fire onend like the real engine.
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

describe('WalkieTalkie explicit stop controls (issue #409)', () => {
  let synth: { speaking: boolean; pending: boolean; speak: ReturnType<typeof vi.fn>; cancel: ReturnType<typeof vi.fn> };
  beforeEach(() => {
    lastRec = null;
    sendPreviewMock.mockClear();
    stateNow = makeState([
      msg({ seq: 1, direction: 'in', text: 'hi' }),
      msg({ seq: 2, direction: 'out', text: 'hello there' }),
    ]);
    win.SpeechRecognition = FakeRecognition;
    win.SpeechSynthesisUtterance = class {
      text: string;
      constructor(text: string) {
        this.text = text;
      }
    };
    synth = { speaking: true, pending: false, speak: vi.fn(), cancel: vi.fn() };
    win.speechSynthesis = synth;
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

  it('shows Stop & send while listening; clicking it ends the capture and sends', async () => {
    const { container } = render(<WalkieTalkie />);
    const orb = await findOrb(container);
    expect(container.querySelector('.wt-stop-btn')).toBeNull();

    orb.click(); // start listening
    const stop = await waitFor(() => {
      const el = container.querySelector('.wt-stop-btn') as HTMLButtonElement;
      expect(el).not.toBeNull();
      expect(el.textContent).toContain('Stop');
      expect(el.textContent).toContain('send');
      return el;
    });

    // The engine produced a final result; the explicit stop must deliver it.
    lastRec!.onresult?.({
      resultIndex: 0,
      results: [{ isFinal: true, 0: { transcript: 'restart the build' }, length: 1 }],
    });
    stop.click();

    await waitFor(() => {
      expect(sendPreviewMock).toHaveBeenCalledWith('restart the build');
      // Capture over — the listening stop control is gone.
      const btn = container.querySelector('.wt-stop-btn');
      expect(btn?.textContent ?? '').not.toContain('send');
    });
  });

  it('shows Stop voice while narrating; clicking it cuts playback', async () => {
    setSpeakReplies(true);
    const { container } = render(<WalkieTalkie />);
    await findOrb(container);

    // A fresh reply arrives → narration starts → phase 'speaking'.
    stateNow = makeState([
      msg({ seq: 1, direction: 'in', text: 'hi' }),
      msg({ seq: 2, direction: 'out', text: 'hello there' }),
      msg({ seq: 3, direction: 'in', text: 'status?' }),
      msg({ seq: 4, direction: 'out', text: 'All tasks are green today.' }),
    ]);
    pushEvent?.({ type: 'gateway.preview' });

    const stop = await waitFor(() => {
      const el = container.querySelector('.wt-stop-btn') as HTMLButtonElement;
      expect(el).not.toBeNull();
      expect(el.textContent).toContain('Stop voice');
      return el;
    });

    stop.click();
    await waitFor(() => {
      expect(synth.cancel).toHaveBeenCalled();
      expect(container.querySelector('.wt-stop-btn')).toBeNull();
    });
  });
});
