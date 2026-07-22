import { render, waitFor } from '@testing-library/preact';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import type { PreviewMessage, PreviewState } from '../../api/gatewayPreview';

// Transport is mocked at the module boundary: fetchPreview serves the state
// below, and the captured subscribeEvents handler lets a test push a fresh
// snapshot (a new turn) without waiting out the 2s safety poll.
let stateNow: PreviewState;
let pushEvent: ((ev: { type: string }) => void) | null = null;

vi.mock('../../api/gatewayPreview', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/gatewayPreview')>()),
  fetchPreview: vi.fn(() => Promise.resolve(stateNow)),
  sendPreview: vi.fn(() => Promise.resolve({ ok: true, action: 'sent', cursor: 0 })),
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

const LONG_YOU =
  'Okay so before you do anything else I need a complete status update on the nightly build, ' +
  'then check whether the database migration actually finished, and also look at the websocket ' +
  'soak test and the dependency upgrade, and if anything failed just show me the logs.';

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

const BASE_MESSAGES: PreviewMessage[] = [
  msg({ seq: 1, direction: 'out', text: 'Linked!' }),
  msg({ seq: 2, direction: 'in', text: LONG_YOU }),
  msg({ seq: 3, direction: 'out', text: 'All three tasks are green.' }),
];

async function findYou(container: Element): Promise<HTMLButtonElement> {
  return waitFor(() => {
    const el = container.querySelector('button.wt-you');
    expect(el).not.toBeNull();
    return el as HTMLButtonElement;
  });
}

describe('WalkieTalkie overflow affordances (issue #409)', () => {
  beforeEach(() => {
    stateNow = makeState(BASE_MESSAGES);
  });

  it('renders the full user utterance in an expandable button, collapsed by default', async () => {
    const { container } = render(<WalkieTalkie />);
    const you = await findYou(container);
    // The complete text is in the DOM (CSS clamps the collapsed view — the
    // old markup hard-truncated with nowrap+ellipsis instead).
    expect(you.textContent).toContain('show me the logs');
    expect(you.getAttribute('aria-expanded')).toBe('false');

    you.click();
    await waitFor(() => expect(you.getAttribute('aria-expanded')).toBe('true'));
    you.click();
    await waitFor(() => expect(you.getAttribute('aria-expanded')).toBe('false'));
  });

  it('re-collapses the utterance when a new turn arrives', async () => {
    const { container } = render(<WalkieTalkie />);
    const you = await findYou(container);
    you.click();
    await waitFor(() => expect(you.getAttribute('aria-expanded')).toBe('true'));

    // A new exchange lands via the SSE path.
    stateNow = makeState([
      ...BASE_MESSAGES,
      msg({ seq: 4, direction: 'in', text: 'And now something short' }),
      msg({ seq: 5, direction: 'out', text: 'Done.' }),
    ]);
    pushEvent?.({ type: 'gateway.preview' });

    await waitFor(() => {
      const el = container.querySelector('button.wt-you') as HTMLButtonElement;
      expect(el.textContent).toContain('And now something short');
      expect(el.getAttribute('aria-expanded')).toBe('false');
    });
  });

  it('keeps every history message reachable inside the scrollable transcript panel', async () => {
    const many: PreviewMessage[] = [];
    for (let i = 1; i <= 40; i++) {
      many.push(
        msg({
          seq: i,
          direction: i % 2 ? 'out' : 'in',
          text: `history message number ${i} with some length to it`,
        }),
      );
    }
    many.push(msg({ seq: 41, direction: 'in', text: 'latest question' }));
    many.push(msg({ seq: 42, direction: 'out', text: 'latest answer' }));
    stateNow = makeState(many);

    const { container, getByText } = render(<WalkieTalkie />);
    const toggle = await waitFor(() => {
      const el = container.querySelector('.wt-history-toggle');
      expect(el).not.toBeNull();
      return el as HTMLButtonElement;
    });
    toggle.click();

    // Every pre-card message is present in the panel — nothing dropped.
    await waitFor(() => {
      const panel = container.querySelector('.wt-history') as HTMLElement;
      expect(panel).not.toBeNull();
      expect(panel.querySelectorAll('.wt-msg').length).toBe(41);
    });
    expect(getByText(/history message number 1 with/)).toBeTruthy();
    expect(getByText(/history message number 40 with/)).toBeTruthy();
  });

  it('renders all quick replies, including very long ones', async () => {
    const replies = [
      'Convert both certs now',
      'Hold the cert-manager bump',
      'Retry the dependency upgrade with --legacy-peer-deps and rebuild the lockfile from scratch',
      'Leave everything as is',
    ];
    stateNow = makeState([
      msg({ seq: 1, direction: 'in', text: 'What now?' }),
      msg({ seq: 2, direction: 'out', text: 'Your call.', quick_replies: replies }),
    ]);

    const { container } = render(<WalkieTalkie />);
    await waitFor(() => {
      const chips = container.querySelectorAll('.wt-reply');
      expect(chips.length).toBe(replies.length);
      expect(chips[2].textContent).toContain('--legacy-peer-deps');
    });
  });
});
