import { render, screen, waitFor, fireEvent } from '@testing-library/preact';
import { describe, expect, it, beforeEach, vi } from 'vitest';
import type { ApiTokenResponse } from '../../api/mobile';

let getToken: () => Promise<ApiTokenResponse>;
const regenerated: number[] = [];
const copied: string[] = [];

vi.mock('../../api/mobile', async (orig) => ({
  ...(await orig<typeof import('../../api/mobile')>()),
  getApiToken: () => getToken(),
  regenerateApiToken: () => {
    regenerated.push(1);
    return Promise.resolve({ token: 'tok-NEW-9999' });
  },
}));

import { MobileSection } from './MobileSection';

describe('MobileSection', () => {
  beforeEach(() => {
    getToken = () => Promise.resolve({ token: 'tok-SECRET-1234' });
    regenerated.length = 0;
    copied.length = 0;
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {
        writeText: (t: string) => {
          copied.push(t);
          return Promise.resolve();
        },
      },
    });
  });

  it('shows the workspace host and keeps the token masked until revealed', async () => {
    render(<MobileSection />);
    expect(screen.getByDisplayValue(window.location.origin)).toBeInTheDocument();
    // Wait for the token to load, then confirm it is NOT shown in the clear.
    await waitFor(() =>
      expect((screen.getByText('Reveal') as HTMLButtonElement).disabled).toBe(false),
    );
    expect(screen.queryByDisplayValue('tok-SECRET-1234')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('Reveal'));
    expect(screen.getByDisplayValue('tok-SECRET-1234')).toBeInTheDocument();
  });

  it('copies the token to the clipboard', async () => {
    render(<MobileSection />);
    await waitFor(() =>
      expect((screen.getByText('Reveal') as HTMLButtonElement).disabled).toBe(false),
    );
    const copyButtons = screen.getAllByText('Copy'); // [host, token]
    fireEvent.click(copyButtons[copyButtons.length - 1]);
    await waitFor(() => expect(copied).toContain('tok-SECRET-1234'));
  });

  it('regenerates the token on confirm and reveals the new one', async () => {
    render(<MobileSection />);
    await waitFor(() =>
      expect((screen.getByText('Regenerate token') as HTMLButtonElement).disabled).toBe(false),
    );
    fireEvent.click(screen.getByText('Regenerate token'));
    await waitFor(() => expect(regenerated).toEqual([1]));
    await waitFor(() => expect(screen.getByDisplayValue('tok-NEW-9999')).toBeInTheDocument());
  });

  it('shows Unavailable when the token cannot be fetched', async () => {
    getToken = () => Promise.reject(new Error('boom'));
    render(<MobileSection />);
    await waitFor(() => expect(screen.getByDisplayValue('Unavailable')).toBeInTheDocument());
    expect((screen.getByText('Reveal') as HTMLButtonElement).disabled).toBe(true);
  });
});
