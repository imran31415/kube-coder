import { apiGet, apiPost, apiDelete } from './client';

// Subscription-based CLI logins (Claude Max/Pro OAuth, Codex ChatGPT OAuth),
// surfaced alongside the pasted API keys in Settings. Read-only status +
// logout; the server never returns any token material (see server.py
// SubscriptionStatusManager.public_view).
export type SubscriptionProvider = 'claude' | 'codex';

export interface SubscriptionStatus {
  logged_in: boolean;
  /** 'subscription' (OAuth) or 'api_key' (Codex logged in with an OpenAI key). */
  kind?: 'subscription' | 'api_key';
  /** Human-ish plan label — Claude: "max"/"pro"; Codex: "ChatGPT". */
  plan?: string;
  /** Epoch millis the OAuth token expires; null when unknown. Claude only. */
  expires_at?: number | null;
  expired?: boolean;
  /** True when a pasted ANTHROPIC_API_KEY overrides the Claude subscription. */
  overridden_by_key?: boolean;
  /** Codex: false when the `codex` CLI isn't present in this image. */
  available?: boolean;
}

export type SubscriptionsView = Record<SubscriptionProvider, SubscriptionStatus>;

export const getSubscriptions = () =>
  apiGet<{ subscriptions: SubscriptionsView; claude_ready: boolean }>('/api/subscriptions');

export const logoutSubscription = (provider: SubscriptionProvider) =>
  apiDelete<{ ok: true }>(`/api/subscriptions/${provider}`);

// Browser-less "Connect Claude account" flow. The server drives
// `claude auth login` in a background session (ClaudeWebLoginManager);
// the browser only ever sees the public OAuth URL and posts back the
// one-time authorization code — no token material crosses the API.

/** Sign-in URL to open in the user's own browser (paste-code OAuth flow). */
export interface ClaudeConnectStart {
  url: string;
  in_progress: boolean;
}

/** Poll result: connected carries the refreshed subscription view. */
export interface ClaudeConnectPoll {
  connected: boolean;
  in_progress: boolean;
  state?: string;
  error?: string;
  subscriptions?: SubscriptionsView;
  /** Whether a spawned Claude session now has a working credential (#494). */
  claude_ready?: boolean;
}

export const startClaudeConnect = () =>
  apiPost<ClaudeConnectStart>('/api/subscriptions/claude/login/start');

export const submitClaudeConnectCode = (code: string) =>
  apiPost<{ ok: true }>('/api/subscriptions/claude/login/code', { code });

export const pollClaudeConnect = () =>
  apiPost<ClaudeConnectPoll>('/api/subscriptions/claude/login/poll');

export const cancelClaudeConnect = () =>
  apiPost<{ ok: true }>('/api/subscriptions/claude/login/cancel');
