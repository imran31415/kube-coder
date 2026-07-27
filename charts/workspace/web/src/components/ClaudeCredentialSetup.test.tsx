import { render, screen, fireEvent, waitFor } from '@testing-library/preact';
import { describe, expect, it, beforeEach, vi } from 'vitest';

const saved: Array<[string, string]> = [];

vi.mock('../api/providerKeys', () => ({
  setProviderKey: (provider: string, key: string) => {
    saved.push([provider, key]);
    return Promise.resolve({ ok: true as const, provider });
  },
}));

// ClaudeConnect (rendered as the OAuth option) reaches for the subscriptions
// API on mount/click — stub it so the component renders in isolation.
vi.mock('../api/subscriptions', () => ({
  startClaudeConnect: () => Promise.resolve({ url: 'https://claude.com/oauth/authorize', in_progress: true }),
  submitClaudeConnectCode: () => Promise.resolve({ ok: true as const }),
  pollClaudeConnect: () => Promise.resolve({ connected: false, in_progress: true }),
  cancelClaudeConnect: () => Promise.resolve({ ok: true as const }),
}));

import { ClaudeCredentialSetup } from './ClaudeCredentialSetup';

describe('ClaudeCredentialSetup', () => {
  beforeEach(() => {
    saved.length = 0;
  });

  it('shows the connected confirmation when ready', () => {
    render(<ClaudeCredentialSetup ready={true} />);
    expect(screen.getByText(/Claude is connected/)).toBeTruthy();
    // No connect options rendered when already ready.
    expect(screen.queryByText('Connect Claude account')).toBeNull();
  });

  it('offers OAuth first (recommended) and the API-key path when not ready', () => {
    render(<ClaudeCredentialSetup ready={false} />);
    expect(screen.getByText('Recommended')).toBeTruthy();
    expect(screen.getByText('Connect Claude account')).toBeTruthy();
    expect(screen.getByLabelText('Anthropic API key')).toBeTruthy();
  });

  it('saves a pasted API key and fires onConnected', async () => {
    const onConnected = vi.fn();
    render(<ClaudeCredentialSetup ready={false} onConnected={onConnected} />);
    fireEvent.input(screen.getByLabelText('Anthropic API key'), {
      target: { value: '  sk-ant-abc  ' },
    });
    fireEvent.click(screen.getByText('Save key'));
    await waitFor(() => expect(saved).toEqual([['ANTHROPIC_API_KEY', 'sk-ant-abc']]));
    expect(onConnected).toHaveBeenCalled();
  });
});
