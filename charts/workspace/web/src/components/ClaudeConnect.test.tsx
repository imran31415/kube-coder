import { render, screen, waitFor, fireEvent } from '@testing-library/preact';
import { describe, expect, it, beforeEach, vi } from 'vitest';
import type { ClaudeConnectPoll, SubscriptionsView } from '../api/subscriptions';

const started: number[] = [];
const codes: string[] = [];
const cancels: number[] = [];
let pollResult: () => ClaudeConnectPoll;

vi.mock('../api/subscriptions', async (orig) => ({
  ...(await orig<typeof import('../api/subscriptions')>()),
  startClaudeConnect: () => {
    started.push(1);
    return Promise.resolve({
      url: 'https://claude.com/cai/oauth/authorize?code=true&state=xyz',
      in_progress: true,
    });
  },
  submitClaudeConnectCode: (code: string) => {
    codes.push(code);
    return Promise.resolve({ ok: true as const });
  },
  pollClaudeConnect: () => Promise.resolve(pollResult()),
  cancelClaudeConnect: () => {
    cancels.push(1);
    return Promise.resolve({ ok: true as const });
  },
}));

import { ClaudeConnect } from './ClaudeConnect';

const SUBS: SubscriptionsView = {
  claude: { logged_in: true, kind: 'subscription', plan: 'max' },
  codex: { logged_in: false },
};

describe('ClaudeConnect', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    started.length = 0;
    codes.length = 0;
    cancels.length = 0;
    pollResult = () => ({ connected: false, in_progress: true, state: 'awaiting_code' });
  });

  async function beginFlow() {
    render(<ClaudeConnect onConnected={onConnected} />);
    fireEvent.click(screen.getByText('Connect Claude account'));
    await vi.waitFor(() => expect(started.length).toBe(1));
    // The awaiting step shows the sign-in link with the server's URL.
    const link = (await screen.findByText(/Open Claude to sign in/)).closest('a')!;
    expect(link.getAttribute('href')).toContain('oauth/authorize');
  }

  let connectedWith: (SubscriptionsView | undefined)[] = [];
  const onConnected = (subs?: SubscriptionsView) => connectedWith.push(subs);

  it('walks start → paste code → poll → connected', async () => {
    connectedWith = [];
    await beginFlow();

    const input = screen.getByPlaceholderText('Paste authorization code');
    fireEvent.input(input, { target: { value: '  abc123_def#state  ' } });
    pollResult = () => ({ connected: true, in_progress: false, subscriptions: SUBS });
    fireEvent.click(screen.getByText('Connect'));
    await vi.waitFor(() => expect(codes).toEqual(['abc123_def#state']));

    await vi.advanceTimersByTimeAsync(2000);
    await waitFor(() => expect(connectedWith).toEqual([SUBS]));
    expect(screen.getByText(/Claude account connected/)).toBeTruthy();
  });

  it('shows the server error and offers retry when the exchange fails', async () => {
    connectedWith = [];
    await beginFlow();

    fireEvent.input(screen.getByPlaceholderText('Paste authorization code'),
      { target: { value: 'bad-code-123' } });
    pollResult = () => ({ connected: false, in_progress: false, error: 'Login failed: 400' });
    fireEvent.click(screen.getByText('Connect'));
    await vi.waitFor(() => expect(codes.length).toBe(1));

    await vi.advanceTimersByTimeAsync(2000);
    await waitFor(() => expect(screen.getByText('Login failed: 400')).toBeTruthy());
    expect(screen.getByText('Try again')).toBeTruthy();
    expect(connectedWith).toEqual([]);
  });

  it('cancel aborts the server-side session', async () => {
    await beginFlow();
    fireEvent.click(screen.getByText('Cancel'));
    await vi.waitFor(() => expect(cancels.length).toBe(1));
    expect(screen.getByText('Connect Claude account')).toBeTruthy();
  });
});
