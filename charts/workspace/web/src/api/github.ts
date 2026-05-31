import { apiGet, apiPost } from './client';

export interface GitHubStatus {
  ssh_key_exists: boolean;
  ssh_public_key?: string;
  gh_authenticated: boolean;
  gh_user?: string;
  git_user_name?: string;
  git_user_email?: string;
}

export const githubStatus = () => apiGet<GitHubStatus>('/api/github/status');

export const generateSshKey = (email: string) =>
  apiPost<{ public_key: string }>('/api/github/ssh/generate', { email });

export const setGitConfig = (name: string, email: string) =>
  apiPost<{ ok: true }>('/api/github/config', { name, email });
