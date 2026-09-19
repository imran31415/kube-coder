import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/preact';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ProviderKeysSection } from './ProviderKeysSection';

const save = vi.hoisted(() => vi.fn().mockResolvedValue({ ok: true }));
vi.mock('../../api/providerKeys', () => ({
  listProviderKeys: async () => ({ providers: {} }),
  setProviderKey: save,
  deleteProviderKey: vi.fn(),
}));
vi.mock('../../api/subscriptions', () => ({
  getSubscriptions: async () => ({ subscriptions: {} }),
  logoutSubscription: vi.fn(),
}));
vi.mock('../../api/hypervisor', () => ({
  getHypervisorConfig: async () => ({ authHelp: {
    codex: { label: 'Codex', instructions: 'Run codex login in the workspace terminal.' },
    antigravity: { label: 'Antigravity', instructions: 'Run agy and complete sign-in.' },
  } }),
}));

beforeEach(() => save.mockClear());
afterEach(cleanup);

it('offers runtime-specific setup instructions and the workspace terminal', async () => {
  render(<ProviderKeysSection />);
  expect(await screen.findByText('Codex authentication')).toBeTruthy();
  expect(screen.getByText('Run codex login in the workspace terminal.')).toBeTruthy();
  expect(screen.getByText('Antigravity authentication')).toBeTruthy();
  expect(screen.getByRole('link', { name: 'Open workspace terminal' }).getAttribute('href')).toContain('/terminal/');
});

it('saves the Zen key through the existing provider-key API', async () => {
  render(<ProviderKeysSection />);
  const input = screen.getByLabelText('OpenCode Zen API key');
  fireEvent.input(input, { target: { value: 'test-placeholder-key' } });
  const row = input.closest('.settings-row')!;
  fireEvent.click(within(row as HTMLElement).getByRole('button', { name: /Save/ }));
  await waitFor(() => expect(save).toHaveBeenCalledWith('OPENCODE_API_KEY', 'test-placeholder-key'));
  await waitFor(() => expect((input as HTMLInputElement).value).toBe(''));
});
