import { render, screen, waitFor, fireEvent } from '@testing-library/preact';
import { describe, expect, it, beforeEach, vi } from 'vitest';
import type { GithubConnectStart, GithubConnectPoll } from '../api/github';

let startImpl: () => Promise<GithubConnectStart>;
let pollImpl: () => Promise<GithubConnectPoll>;
const cancelCalls: number[] = [];

vi.mock('../api/github', async (orig) => ({
  ...(await orig<typeof import('../api/github')>()),
  startGithubConnect: () => startImpl(),
  pollGithubConnect: () => pollImpl(),
  cancelGithubConnect: () => {
    cancelCalls.push(1);
    return Promise.resolve({ ok: true as const });
  },
}));

vi.mock('../store/ui', () => ({ pushToast: vi.fn() }));

import { GithubConnect } from './GithubConnect';

describe('GithubConnect', () => {
  beforeEach(() => {
    cancelCalls.length = 0;
    vi.useRealTimers();
  });

  it('shows the already-connected state without a flow', () => {
    render(<GithubConnect connected user="octocat" />);
    expect(screen.getByText(/octocat/)).toBeTruthy();
    expect(screen.getByText(/Reconnect a different account/)).toBeTruthy();
  });

  it('starts the flow and renders the one-time code + verify link', async () => {
    startImpl = () =>
      Promise.resolve({ code: '1A2B-3C4D', verification_uri: 'https://github.com/login/device' });
    pollImpl = () =>
      Promise.resolve({ connected: false, in_progress: true } as GithubConnectPoll);

    render(<GithubConnect />);
    fireEvent.click(screen.getByText(/Connect GitHub account/));

    const code = await screen.findByText('1A2B-3C4D');
    expect(code).toBeTruthy();
    const link = screen.getByText(/Open GitHub to authorize/).closest('a') as HTMLAnchorElement;
    expect(link.href).toBe('https://github.com/login/device');
  });

  it('reaches the done state when polling reports connected', async () => {
    const onConnected = vi.fn();
    startImpl = () =>
      Promise.resolve({ code: 'WXYZ-6789', verification_uri: 'https://github.com/login/device' });
    pollImpl = () =>
      Promise.resolve({ connected: true, in_progress: false, connected_user: 'octocat' } as GithubConnectPoll);

    render(<GithubConnect onConnected={onConnected} />);
    fireEvent.click(screen.getByText(/Connect GitHub account/));
    await screen.findByText('WXYZ-6789');

    await waitFor(() => expect(onConnected).toHaveBeenCalledWith('octocat'), { timeout: 4000 });
    expect(await screen.findByText(/GitHub connected/)).toBeTruthy();
  });
});
