import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/preact';
import type { ProvisionOptions, ProvisionStatus, ValidateUserResponse } from '../api/provision';

// Plain-function module mock (not vi.fn spies) for the same reason CapacityPanel
// uses one: deferred promise settles tracked by a spy can surface as spurious
// cross-test failures. `respondValidate` / `created` steer behaviour.
let respondValidate: () => Promise<ValidateUserResponse>;
let respondStatus: () => Promise<ProvisionStatus>;
const created: ProvisionOptions[] = [];
const deployed: string[] = [];
vi.mock('../api/provision', async (orig) => ({
  ...(await orig<typeof import('../api/provision')>()),
  validateUser: () => respondValidate(),
  getProvisionStatus: () => respondStatus(),
  createProvision: (opts: ProvisionOptions) => {
    created.push(opts);
    return Promise.resolve({ slug: opts.user, job: 'pending', message: '', workspace: null, url: '' });
  },
  deployExisting: (slug: string) => {
    deployed.push(slug);
    return Promise.resolve({ slug, job: 'pending', message: '', workspace: null, url: '' });
  },
}));

import { ProvisionForm } from './ProvisionForm';
import { provisionConfig } from '../store';

const sampleUser = (over: Partial<ValidateUserResponse> = {}): ValidateUserResponse => ({
  login: 'octocat',
  slug: 'octocat',
  name: 'Octo Cat',
  email: 'octo@example.com',
  avatarUrl: null,
  host: 'octocat.dev.scalebase.io',
  exists: false,
  configExists: false,
  ...over,
});

describe('ProvisionForm (create view)', () => {
  beforeEach(() => {
    location.hash = '#/provision';
    provisionConfig.value = { enabled: true, workspaceDomain: 'dev.scalebase.io', oauthAppNewUrl: 'https://github.com/settings/applications/new' };
    respondValidate = () => Promise.resolve(sampleUser());
    respondStatus = () => Promise.resolve({ slug: 'octocat', job: 'pending', message: '', workspace: null, url: '' });
    created.length = 0;
    deployed.length = 0;
  });

  it('looks up a username and shows the preview with the derived host', async () => {
    render(<ProvisionForm />);
    fireEvent.input(screen.getByPlaceholderText('octocat'), { target: { value: 'octocat' } });
    fireEvent.click(screen.getByText('Look up'));
    await waitFor(() => expect(screen.getByText('Octo Cat')).toBeInTheDocument());
    expect(screen.getByText('octocat.dev.scalebase.io')).toBeInTheDocument();
  });

  it('warns when the workspace already exists', async () => {
    respondValidate = () => Promise.resolve(sampleUser({ exists: true, slug: 'octocat' }));
    render(<ProvisionForm />);
    fireEvent.input(screen.getByPlaceholderText('octocat'), { target: { value: 'octocat' } });
    fireEvent.click(screen.getByText('Look up'));
    await waitFor(() => expect(screen.getByText(/already exists/)).toBeInTheDocument());
  });

  it('shows the OAuth App callback URL and creates with the pasted creds', async () => {
    render(<ProvisionForm />);
    fireEvent.input(screen.getByPlaceholderText('octocat'), { target: { value: 'octocat' } });
    fireEvent.click(screen.getByText('Look up'));
    await waitFor(() => expect(screen.getByText('Octo Cat')).toBeInTheDocument());
    // The exact callback URL to register must be shown.
    expect(screen.getByText('https://octocat.dev.scalebase.io/oauth2/callback')).toBeInTheDocument();
    // Create is gated on both creds being present.
    const createBtn = screen.getByText('Create workspace') as HTMLButtonElement;
    expect(createBtn.disabled).toBe(true);
    fireEvent.input(screen.getByPlaceholderText('Ov23li…'), { target: { value: 'Ov23liexampleid' } });
    fireEvent.input(screen.getByPlaceholderText('paste the generated secret'), { target: { value: 'shhh-secret' } });
    expect(createBtn.disabled).toBe(false);
    fireEvent.click(createBtn);
    await waitFor(() => expect(created).toHaveLength(1));
    expect(created[0]).toMatchObject({ user: 'octocat', clientId: 'Ov23liexampleid', clientSecret: 'shhh-secret' });
  });

  it('deploys from saved config (skips the creds form) when configExists', async () => {
    respondValidate = () => Promise.resolve(sampleUser({ configExists: true, slug: 'octocat' }));
    render(<ProvisionForm />);
    fireEvent.input(screen.getByPlaceholderText('octocat'), { target: { value: 'octocat' } });
    fireEvent.click(screen.getByText('Look up'));
    await waitFor(() => expect(screen.getByText('Deploy workspace')).toBeInTheDocument());
    expect(screen.queryByText('Create workspace')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('Deploy workspace'));
    await waitFor(() => expect(deployed).toEqual(['octocat']));
    expect(created).toHaveLength(0);
  });

  it('treats a failed Job as incomplete (not ready) even when a pod is running, and finishes the deploy', async () => {
    respondStatus = () =>
      Promise.resolve({
        slug: 'octocat',
        job: 'failed',
        message: 'provisioner Job failed — see Job logs',
        workspace: { state: 'running' } as ProvisionStatus['workspace'],
        url: 'https://octocat.dev.scalebase.io/',
      });
    location.hash = '#/provision/octocat';
    render(<ProvisionForm />);
    await waitFor(() => expect(screen.getByText('Provisioning incomplete')).toBeInTheDocument());
    expect(screen.queryByText('Workspace ready')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('Finish deploy'));
    await waitFor(() => expect(deployed).toEqual(['octocat']));
  });

  it('shows a disabled-state message when provisioning is off', () => {
    provisionConfig.value = { enabled: false, workspaceDomain: '', oauthAppNewUrl: '' };
    render(<ProvisionForm />);
    expect(screen.getByText(/Provisioning is not configured/)).toBeInTheDocument();
  });
});
