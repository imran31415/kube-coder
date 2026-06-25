import { apiGet, apiPost } from './client';
import { type Workspace } from './workspaces';

// Self-service provisioning API. The flow has a browser detour: the controller
// returns a GitHub App *manifest* the SPA POSTs to github.com, the admin clicks
// "Create", and GitHub redirects back to the controller callback which finishes
// the provision and bounces here to #/provision/<slug> for the status poll.

export interface ProvisionConfig {
  /** Whether the controller has all provisioning wiring (else the tab hides). */
  enabled: boolean;
  /** Base domain new workspaces are created under: <slug>.<workspaceDomain>. */
  workspaceDomain: string;
  /** Org the GitHub Apps are created under, or '' for the admin's account. */
  githubAppOrg: string;
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
}

export const validateUser = (user: string) =>
  apiGet<ValidateUserResponse>('/api/provision/validate', { user });

export interface ProvisionOptions {
  user: string;
  pvcSize?: string;
  gitName?: string;
  gitEmail?: string;
  imageTag?: string;
  resources?: {
    requests?: { cpu?: string; memory?: string };
    limits?: { cpu?: string; memory?: string };
  };
}

export interface ManifestResponse {
  /** GitHub URL to POST the manifest to (carries the signed state in its query). */
  action: string;
  /** JSON-stringified GitHub App manifest, submitted as the `manifest` form field. */
  manifest: string;
  state: string;
  host: string;
}

export const startManifest = (opts: ProvisionOptions) =>
  apiPost<ManifestResponse>('/api/provision/github/manifest', opts);

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
 * Hand off to GitHub: build a hidden form and submit it so the browser does a
 * top-level POST to github.com/settings/apps/new with the manifest. (A fetch
 * can't navigate the user to GitHub's confirmation page; a form POST can.)
 */
export function submitManifestToGithub(m: ManifestResponse): void {
  const form = document.createElement('form');
  form.method = 'POST';
  form.action = m.action;
  const field = document.createElement('input');
  field.type = 'hidden';
  field.name = 'manifest';
  field.value = m.manifest;
  form.appendChild(field);
  document.body.appendChild(form);
  form.submit();
}
