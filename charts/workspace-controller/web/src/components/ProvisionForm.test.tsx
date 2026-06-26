import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/preact';
import type { ManifestResponse, ValidateUserResponse } from '../api/provision';

// Plain-function module mock (not vi.fn spies) for the same reason CapacityPanel
// uses one: deferred promise settles tracked by a spy can surface as spurious
// cross-test failures. `respondValidate` / `submitted` steer behaviour.
let respondValidate: () => Promise<ValidateUserResponse>;
const submitted: ManifestResponse[] = [];
const deployed: string[] = [];
vi.mock('../api/provision', async (orig) => ({
  ...(await orig<typeof import('../api/provision')>()),
  validateUser: () => respondValidate(),
  startManifest: () =>
    Promise.resolve({ action: 'https://github.com/settings/apps/new?state=s', manifest: '{}', state: 's', host: 'octo.dev.scalebase.io' }),
  submitManifestToGithub: (m: ManifestResponse) => {
    submitted.push(m);
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
    provisionConfig.value = { enabled: true, workspaceDomain: 'dev.scalebase.io', githubAppOrg: '' };
    respondValidate = () => Promise.resolve(sampleUser());
    submitted.length = 0;
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

  it('hands the manifest off to GitHub on provision', async () => {
    render(<ProvisionForm />);
    fireEvent.input(screen.getByPlaceholderText('octocat'), { target: { value: 'octocat' } });
    fireEvent.click(screen.getByText('Look up'));
    await waitFor(() => expect(screen.getByText('Octo Cat')).toBeInTheDocument());
    fireEvent.click(screen.getByText(/Register GitHub App/));
    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(submitted[0].action).toContain('github.com/settings/apps/new');
  });

  it('deploys from saved config (skips the manifest) when configExists', async () => {
    respondValidate = () => Promise.resolve(sampleUser({ configExists: true, slug: 'octocat' }));
    render(<ProvisionForm />);
    fireEvent.input(screen.getByPlaceholderText('octocat'), { target: { value: 'octocat' } });
    fireEvent.click(screen.getByText('Look up'));
    await waitFor(() => expect(screen.getByText('Deploy workspace')).toBeInTheDocument());
    expect(screen.queryByText(/Register GitHub App/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('Deploy workspace'));
    await waitFor(() => expect(deployed).toEqual(['octocat']));
    expect(submitted).toHaveLength(0);
  });

  it('shows a disabled-state message when provisioning is off', () => {
    provisionConfig.value = { enabled: false, workspaceDomain: '', githubAppOrg: '' };
    render(<ProvisionForm />);
    expect(screen.getByText(/Provisioning is not configured/)).toBeInTheDocument();
  });
});
