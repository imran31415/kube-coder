import { render, screen, waitFor, fireEvent } from '@testing-library/preact';
import { describe, expect, it, beforeEach, vi } from 'vitest';
import type { GithubFullStatus, GitAuthMode } from '../../api/github';

let status: () => Promise<GithubFullStatus>;
const modeCalls: GitAuthMode[] = [];
vi.mock('../../api/github', async (orig) => ({
  ...(await orig<typeof import('../../api/github')>()),
  getGithubFullStatus: () => status(),
  setAuthMode: (m: GitAuthMode) => {
    modeCalls.push(m);
    return Promise.resolve({ auth_mode: m, app_available: true } as GithubFullStatus);
  },
}));

import { GitSection } from './GitSection';

describe('GitSection auth mode', () => {
  beforeEach(() => {
    modeCalls.length = 0;
  });

  it('switches to the App-managed mode on click', async () => {
    status = () =>
      Promise.resolve({
        auth_mode: 'personal',
        app_available: true,
        gh_cli: { authenticated: true, username: 'imran31415' },
        git_config: { user_name: 'Imran', user_email: 'i@example.com' },
      });
    render(<GitSection />);
    const appBtn = (await screen.findByText('App (managed)')) as HTMLButtonElement;
    // Personal is active initially, so App is clickable.
    expect(appBtn.disabled).toBe(false);
    fireEvent.click(appBtn);
    await waitFor(() => expect(modeCalls).toEqual(['app']));
  });

  it('disables the App option when the App flow is not configured', async () => {
    status = () =>
      Promise.resolve({ auth_mode: 'personal', app_available: false });
    render(<GitSection />);
    const appBtn = (await screen.findByText('App (managed)')) as HTMLButtonElement;
    expect(appBtn.disabled).toBe(true);
    fireEvent.click(appBtn);
    // Disabled → no switch attempted.
    expect(modeCalls).toEqual([]);
  });
});
