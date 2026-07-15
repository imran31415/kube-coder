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

/** The real /api/github/status payload is nested (ssh / gh_cli / git_config).
 *  This typed accessor is used by the Desktop identity header; it's kept
 *  separate from the (flat, legacy) GitHubStatus type above so existing
 *  callers are undisturbed. */
export type GitAuthMode = 'app' | 'personal';

export interface GithubFullStatus {
  ssh?: { configured?: boolean; key_type?: string; key_fingerprint?: string; public_key?: string };
  gh_cli?: { installed?: boolean; authenticated?: boolean; username?: string | null };
  git_config?: { user_name?: string; user_email?: string };
  /** 'personal' = the user's own gh/git login wins; 'app' = managed App token (#256). */
  auth_mode?: GitAuthMode;
  /** Whether switching back to 'app' mode is meaningful (App flow configured). */
  app_available?: boolean;
}

export const getGithubFullStatus = () => apiGet<GithubFullStatus>('/api/github/status');

/** Switch the workspace GitHub auth mode. Returns the refreshed full status. */
export const setAuthMode = (mode: GitAuthMode) =>
  apiPost<GithubFullStatus>('/api/github/auth-mode', { mode });

/** Best display handle for the workspace operator: the gh CLI login when
 *  signed in, else the configured git user name. Returns null when neither
 *  is known (e.g. an unauthenticated read-only visitor gets a 401). */
export function githubDisplayName(s: GithubFullStatus | null | undefined): string | null {
  const name = s?.gh_cli?.username?.trim() || s?.git_config?.user_name?.trim();
  return name || null;
}

export const generateSshKey = (email: string) =>
  apiPost<{ public_key: string }>('/api/github/ssh/generate', { email });

export const setGitConfig = (name: string, email: string) =>
  apiPost<{ ok: true }>('/api/github/config', { name, email });
