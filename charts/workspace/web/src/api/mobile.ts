import { apiGet, apiPost } from './client';

export interface ApiTokenResponse {
  token: string;
}

/**
 * The workspace's Bearer API token — what the kube-coder mobile app (and
 * scripts/curl) use to reach the task/memory/metrics API without a browser
 * session. Both endpoints require the browser's OAuth session
 * (server.py check_oauth_only), so the token can only be read from the
 * signed-in dashboard — never via the token itself.
 */
export const getApiToken = () => apiGet<ApiTokenResponse>('/api/claude/auth/token');

/** Rotate the token. Invalidates any device or script using the current one. */
export const regenerateApiToken = () =>
  apiPost<ApiTokenResponse>('/api/claude/auth/token/regenerate');
