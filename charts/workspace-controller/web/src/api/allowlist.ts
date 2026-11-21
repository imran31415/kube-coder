import { ApiError, api, apiGet } from './client';
import type { Problem } from './problem';

// The runtime signup allowlist: who may create a workspace at public
// signup. Admin-only; the public signup listener has no route to any of this.
// IDs are numeric GitHub IDs as strings — a login is display only.

export interface AccessWorkspace {
  slug: string;
  stage: string;
}

export interface AccessEntry {
  githubId: string;
  /** As it was when added (Helm entries borrow it from their workspace, if any). */
  login: string | null;
  /** `helm` entries come from the chart values and are read-only here. */
  source: 'helm' | 'console';
  addedBy?: string;
  /** Epoch seconds. */
  addedAt?: number;
  note?: string;
  workspace: AccessWorkspace | null;
}

export interface AccessOverview {
  entries: AccessEntry[];
  /** Most console entries the list holds (Helm entries don't count). */
  limit: number;
  /** Longest note the server accepts. */
  noteMax: number;
  /** The public signup page, to share with the people on the list. */
  signupUrl: string;
}

export interface GithubAccount {
  githubId: string;
  login: string;
  name: string;
  avatarUrl: string;
  allowed: boolean;
  source: 'helm' | 'console' | null;
  workspace: AccessWorkspace | null;
}

/** Writes are idempotent, so a slow one is checked (by reloading the list)
 *  rather than left spinning or blindly replayed. */
export const WRITE_TIMEOUT_MS = 20000;

export const getAccessList = (signal?: AbortSignal) =>
  apiGet<AccessOverview>('/api/allowlist', undefined, signal);

export const lookupAccount = (login: string) =>
  apiGet<GithubAccount>('/api/allowlist/lookup', { login });

export const allowAccount = (login: string, githubId: string, note: string) =>
  api<{ entry: AccessEntry; created: boolean }>('/api/allowlist', {
    method: 'POST',
    body: { login, githubId, note },
    timeoutMs: WRITE_TIMEOUT_MS,
  });

export const removeAccount = (githubId: string) =>
  api<{ removed: boolean }>(`/api/allowlist/${encodeURIComponent(githubId)}`, {
    method: 'DELETE',
    timeoutMs: WRITE_TIMEOUT_MS,
  });

// The server's messages are already short and safe; these few are clearer in
// the context of this page.
const MESSAGES: Record<string, string> = {
  state_unavailable: "Couldn't reach the signup list storage. Try again.",
  busy: 'Someone else is changing the list right now. Try again.',
  network_error: "Can't reach the controller. Check your connection.",
  request_timeout: "The controller didn't answer in time.",
  unauthorized: 'Your session expired. Sign in again.',
  signup_off: 'Public signup is not turned on for this controller.',
};

/** Any failure from the calls above, as one short message plus what the user
 *  can do about it (Retry when `retryable`, the reference for a bug report). */
export function accessProblem(error: unknown): Problem {
  const e = error instanceof ApiError ? error : null;
  const body = (e?.body && typeof e.body === 'object' ? e.body : {}) as Record<string, unknown>;
  const status = e?.status ?? 0;
  const code = typeof body.code === 'string' ? body.code
    : e?.code ?? (status === 401 ? 'unauthorized' : status === 404 ? 'signup_off' : 'internal_error');
  return {
    code,
    error: MESSAGES[code] ?? (e?.message || 'Something went wrong. Try again.'),
    retryable: typeof body.retryable === 'boolean' ? body.retryable : code !== 'signup_off' && (status === 0 || status >= 500),
    error_id: typeof body.error_id === 'string' ? body.error_id : undefined,
    request_id: typeof body.request_id === 'string' ? body.request_id : undefined,
  };
}
