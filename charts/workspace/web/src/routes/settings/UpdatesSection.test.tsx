import { render, screen, waitFor, fireEvent } from '@testing-library/preact';
import { describe, expect, it, beforeEach, vi } from 'vitest';
import type { WorkspaceVersion, UpdateResult } from '../../api/update';

let version: () => Promise<WorkspaceVersion>;
let rolled = true;
const updates: number[] = [];
vi.mock('../../api/update', async (orig) => ({
  ...(await orig<typeof import('../../api/update')>()),
  getWorkspaceVersion: () => version(),
  updateWorkspace: () => {
    updates.push(1);
    return Promise.resolve({
      ok: true,
      fromVersion: 'v1.3.0',
      toVersion: 'v1.4.0',
      rolled,
      persisted: true,
    } as UpdateResult);
  },
}));

import { UpdatesSection } from './UpdatesSection';

describe('UpdatesSection', () => {
  beforeEach(() => {
    updates.length = 0;
    rolled = true;
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    // The restart overlay polls /health via fetch — stub it so the effect's
    // timers have something to call (they don't fire within these fast tests).
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: false } as Response)));
  });

  it('renders nothing when self-serve is unavailable', async () => {
    version = () => Promise.resolve({ available: false, reason: 'not configured' });
    const { container } = render(<UpdatesSection />);
    // Give the effect a tick to resolve, then assert empty.
    await waitFor(() => expect(container.querySelector('.settings-section')).toBeNull());
  });

  it('offers an update and brokers it on click', async () => {
    version = () =>
      Promise.resolve({
        available: true,
        version: 'v1.3.0',
        latestVersion: 'v1.4.0',
        updateAvailable: true,
      });
    render(<UpdatesSection />);
    const btn = await screen.findByText('Restart & update');
    expect((btn as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(btn);
    await waitFor(() => expect(updates).toHaveLength(1));
    // A rolled update swaps the section for the full-screen restarting overlay.
    await screen.findByText('Your workspace is restarting…');
  });

  it('does not show the restart overlay for a no-op update', async () => {
    rolled = false;
    version = () =>
      Promise.resolve({
        available: true,
        version: 'v1.3.0',
        latestVersion: 'v1.4.0',
        updateAvailable: true,
      });
    render(<UpdatesSection />);
    fireEvent.click(await screen.findByText('Restart & update'));
    await waitFor(() => expect(updates).toHaveLength(1));
    expect(screen.queryByText('Your workspace is restarting…')).toBeNull();
  });

  it('shows up-to-date and disables the button on the latest version', async () => {
    version = () =>
      Promise.resolve({
        available: true,
        version: 'v1.4.0',
        latestVersion: 'v1.4.0',
        updateAvailable: false,
      });
    render(<UpdatesSection />);
    const btn = await screen.findByRole('button', { name: 'Up to date' });
    expect((btn as HTMLButtonElement).disabled).toBe(true);
  });
});
