import { render, screen, waitFor, fireEvent } from '@testing-library/preact';
import { describe, expect, it, beforeEach, vi } from 'vitest';
import type { GitHubStatus } from '../api/github';

// Onboarding fast-path (issue #505): the wizard opens on the first step the
// user actually has work to do on. On a provisioned workspace git identity +
// SSH are already configured, so the only real gate is "Connect Claude" — it
// used to sit four clicks deep behind steps with nothing to do on them.

let gitStatus: GitHubStatus | null = {
  ssh_key_exists: true,
  gh_authenticated: true,
  git_user_name: 'Ada',
  git_user_email: 'ada@example.com',
};

vi.mock('../api/github', () => ({
  githubStatus: () =>
    gitStatus ? Promise.resolve(gitStatus) : Promise.reject(new Error('unreachable')),
  setGitConfig: vi.fn(() => Promise.resolve({ ok: true })),
  generateSshKey: vi.fn(() => Promise.resolve({ ok: true })),
}));

vi.mock('../api/hypervisor', () => ({
  getHypervisorConfig: () =>
    Promise.resolve({
      enabled: true,
      defaultAssistant: 'claude',
      workdir: '/home/dev',
      readOnly: false,
      assistants: [{ id: 'claude', label: 'Claude Code' }],
    }),
}));

// The Claude step renders <ClaudeCredentialSetup> → <ClaudeConnect>, which
// reaches for these on mount; stub them so the panel renders in isolation.
vi.mock('../api/subscriptions', () => ({
  getSubscriptions: () => Promise.resolve({ subscriptions: {}, claude_ready: false }),
  startClaudeConnect: () => Promise.resolve({ url: 'https://claude.com/oauth', in_progress: true }),
  submitClaudeConnectCode: () => Promise.resolve({ ok: true as const }),
  pollClaudeConnect: () => Promise.resolve({ connected: false, in_progress: false }),
  cancelClaudeConnect: () => Promise.resolve({ ok: true as const }),
}));
vi.mock('../api/providerKeys', () => ({
  setProviderKey: () => Promise.resolve({ ok: true as const, provider: 'ANTHROPIC_API_KEY' }),
}));

import { claudeReady } from '../store/claude';
vi.mock('../store/claude', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../store/claude')>();
  return {
    ...actual,
    // The probe result is whatever the test seeded on the signal.
    refreshClaudeReady: () => Promise.resolve(actual.claudeReady.value),
  };
});

import { Onboarding } from './Onboarding';

const configured: GitHubStatus = {
  ssh_key_exists: true,
  gh_authenticated: true,
  git_user_name: 'Ada',
  git_user_email: 'ada@example.com',
};

describe('Onboarding open logic (issue #505)', () => {
  beforeEach(() => {
    localStorage.clear();
    gitStatus = { ...configured };
    claudeReady.value = null;
  });

  it('opens straight on the Claude step when connecting Claude is the only gate', async () => {
    claudeReady.value = false;
    render(<Onboarding />);
    await waitFor(() => expect(screen.getByText('Connect Claude')).toBeInTheDocument());
    expect(screen.getByText('Step 5 of 6')).toBeInTheDocument();
  });

  it('suppresses the footer until Claude is connected, then offers a single primary Continue', async () => {
    claudeReady.value = false;
    const { container } = render(<Onboarding />);
    await waitFor(() => expect(screen.getByText('Connect Claude')).toBeInTheDocument());
    // The real CTA is inside the panel — no dead ghost button / floating note.
    expect(container.querySelector('.ob-footer')).toBeNull();
    expect(screen.queryByText('Skip for now')).toBeNull();
    expect(screen.queryByText('Connect above to continue')).toBeNull();
    // "Skip tour" in the header is still the escape hatch.
    expect(screen.getByText('Skip tour')).toBeInTheDocument();

    claudeReady.value = true;
    await waitFor(() => expect(container.querySelector('.ob-footer')).not.toBeNull());
    const actions = container.querySelectorAll('.ob-footer button');
    expect(actions).toHaveLength(1);
    expect(actions[0].textContent).toBe('Continue');
  });

  it('opens on the SSH step when the key is the first thing missing', async () => {
    gitStatus = { ...configured, ssh_key_exists: false };
    claudeReady.value = true;
    render(<Onboarding />);
    await waitFor(() => expect(screen.getByText('Generate an SSH key')).toBeInTheDocument());
    expect(screen.getByText('Step 4 of 6')).toBeInTheDocument();
  });

  it('opens on the identity step when git identity is the first thing missing', async () => {
    gitStatus = { ...configured, git_user_email: undefined };
    claudeReady.value = true;
    render(<Onboarding />);
    await waitFor(() => expect(screen.getByText('Set your git identity')).toBeInTheDocument());
    expect(screen.getByText('Step 2 of 6')).toBeInTheDocument();
  });

  it('still welcomes a workspace with nothing configured at all', async () => {
    gitStatus = { ssh_key_exists: false, gh_authenticated: false };
    claudeReady.value = false;
    render(<Onboarding />);
    await waitFor(() => expect(screen.getByText('Welcome to kube-coder')).toBeInTheDocument());
    expect(screen.getByText('Step 1 of 6')).toBeInTheDocument();
  });

  it('never opens when nothing is unmet', async () => {
    claudeReady.value = true;
    const { container } = render(<Onboarding />);
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    expect(container.querySelector('.ob-scrim')).toBeNull();
  });

  it('does not reopen once dismissed', async () => {
    localStorage.setItem('kc.onboardingDone', 'true');
    claudeReady.value = false;
    const { container } = render(<Onboarding />);
    await new Promise((r) => setTimeout(r, 0));
    expect(container.querySelector('.ob-scrim')).toBeNull();
  });

  it('the SSH step still advances to Claude by hand (no step regressions)', async () => {
    gitStatus = { ...configured, ssh_key_exists: false };
    claudeReady.value = true;
    render(<Onboarding />);
    await waitFor(() => expect(screen.getByText('Generate an SSH key')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Skip'));
    await waitFor(() => expect(screen.getByText('Connect Claude')).toBeInTheDocument());
  });
});
