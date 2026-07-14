import { apiGet, apiDelete } from './client';

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
  apiGet<{ subscriptions: SubscriptionsView }>('/api/subscriptions');

export const logoutSubscription = (provider: SubscriptionProvider) =>
  apiDelete<{ ok: true }>(`/api/subscriptions/${provider}`);
