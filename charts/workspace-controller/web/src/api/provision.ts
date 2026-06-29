import { apiGet, apiPost } from './client';
import { type Workspace } from './workspaces';

// Self-service provisioning API. The admin creates a GitHub OAuth App by hand
// (callback https://<slug>.<domain>/oauth2/callback) and pastes its Client ID +
// Secret here; the controller writes the config to a GitOps repo, launches the
// deploy Job, and returns the initial status so the SPA can poll #/provision/<slug>.

export interface ProvisionConfig {
  /** Whether the controller has all provisioning wiring (else the tab hides). */
  enabled: boolean;
  /** Base domain new workspaces are created under: <slug>.<workspaceDomain>. */
  workspaceDomain: string;
  /** GitHub URL where the admin creates the OAuth App to paste creds from. */
  oauthAppNewUrl: string;
}

export const getProvisionConfig = () => apiGet<ProvisionConfig>('/api/provision/config');

export interface ValidateUserResponse {
  login: string;
  slug: string;
  name: string;
  email: string;
  avatarUrl: string | null;
  host: string;
  /** True if a workspace for this slug already exists (provision = re-deploy). */
  exists: boolean;
  /** True if the GitHub App + config were already pushed to the GitOps repo on a
   * prior attempt — deploy from saved config instead of re-registering the App. */
  configExists: boolean;
}

export const validateUser = (user: string) =>
  apiGet<ValidateUserResponse>('/api/provision/validate', { user });

export interface ProvisionOptions {
  user: string;
  /** OAuth App Client ID (starts with "Ov…"); pasted by the admin. */
  clientId: string;
  /** OAuth App Client Secret; pasted by the admin. */
  clientSecret: string;
  pvcSize?: string;
  gitName?: string;
  gitEmail?: string;
  imageTag?: string;
  resources?: {
    requests?: { cpu?: string; memory?: string };
    limits?: { cpu?: string; memory?: string };
  };
}

export type JobState = 'none' | 'pending' | 'running' | 'succeeded' | 'failed';

export interface ProvisionStatus {
  slug: string;
  job: JobState;
  message: string;
  workspace: Workspace | null;
  url: string;
}

export const getProvisionStatus = (slug: string) =>
  apiGet<ProvisionStatus>(`/api/provision/${slug}/status`);

/**
 * Create the workspace from the operator-supplied OAuth App creds: the
 * controller validates, pushes config to the GitOps repo, launches the deploy
 * Job, and returns the initial status to poll.
 */
export const createProvision = (opts: ProvisionOptions) =>
  apiPost<ProvisionStatus>('/api/provision/create', opts);

/**
 * Deploy from already-saved GitOps config (the OAuth creds were saved on a prior
 * attempt). Skips re-entering the creds and just relaunches the Job.
 */
export const deployExisting = (slug: string) =>
  apiPost<ProvisionStatus>(`/api/provision/${slug}/deploy`, {});
