import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/preact';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SignupPage } from './SignupPage';
import { signupRequest, SignupFailure } from '../api/signup';

vi.mock('../api/signup', async importOriginal => {
  const actual = await importOriginal<typeof import('../api/signup')>();
  return { ...actual, signupRequest: vi.fn() };
});
const request = vi.mocked(signupRequest);
const me = { login: 'octocat', eligible: true, csrfToken: 'csrf', operation: null };
const op = { operationId: 'op', slug: 'octocat', stage: 'identity', updatedAt: Date.now() / 1000, url: null, error: null };

beforeEach(() => { request.mockReset(); });
afterEach(cleanup);

describe('signup', () => {
  it('offers sign-in without calling admin APIs', async () => {
    request.mockRejectedValue(new SignupFailure({ code: 'unauthorized', error: 'Sign in.', retryable: false }, 401));
    render(<SignupPage />);
    expect(await screen.findByText('Continue with GitHub')).toHaveAttribute('href', '/oauth2/start?rd=%2Fsignup');
    expect(request.mock.calls[0][0]).toBe('me');
  });

  it('explains eligibility and never offers creation to unapproved users', async () => {
    request.mockResolvedValue({ ...me, eligible: false });
    render(<SignupPage />);
    await screen.findByText(/isn’t approved yet/);
    expect(screen.queryByText('Create workspace')).toBeNull();
    expect(screen.getByText('Check again')).toBeInTheDocument();
  });

  it('submits once and shows real stages', async () => {
    request.mockResolvedValueOnce(me).mockResolvedValueOnce({ operation: op });
    render(<SignupPage />);
    fireEvent.click(await screen.findByText('Create workspace'));
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Preparing sign-in'));
    expect(request.mock.calls.filter(call => call[0] === 'workspace')).toHaveLength(1);
  });

  it('resumes existing progress after refresh', async () => {
    request.mockResolvedValue({ ...me, operation: { ...op, stage: 'deployment' } });
    render(<SignupPage />);
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Starting workspace'));
    expect(screen.queryByText('Create workspace')).toBeNull();
  });

  it('offers a short failure reason and retry', async () => {
    request.mockResolvedValue({ ...me, operation: { ...op, stage: 'failed', error: { code: 'deploy_failed', error: 'Workspace deployment failed. Try again.', retryable: true } } });
    render(<SignupPage />);
    expect(await screen.findByText('Retry setup')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Workspace deployment failed.');
  });

  it('does not offer a retry for ownership conflicts', async () => {
    request.mockResolvedValue({ ...me, operation: { ...op, stage: 'failed', error: { code: 'identity_conflict', error: 'Contact your administrator.', retryable: false } } });
    render(<SignupPage />);
    await screen.findByText('Contact your administrator.');
    expect(screen.queryByText('Retry setup')).toBeNull();
  });

  it('shows an open action only when ready', async () => {
    request.mockResolvedValue({ ...me, operation: { ...op, stage: 'ready', url: 'https://octocat.example.test/' } });
    render(<SignupPage />);
    expect(await screen.findByText('Open workspace →')).toHaveAttribute('href', 'https://octocat.example.test/');
  });

  it('checks status instead of replaying an ambiguous submission', async () => {
    request.mockResolvedValueOnce(me).mockRejectedValueOnce(new SignupFailure({ code: 'network_error', error: 'Connection lost.', retryable: true }));
    render(<SignupPage />);
    fireEvent.click(await screen.findByText('Create workspace'));
    await screen.findByText(/Checking whether setup started/);
    expect(screen.getByText('Create workspace')).toBeDisabled();
  });
});
