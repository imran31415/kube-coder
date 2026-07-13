import { apiGet, apiPost, apiDelete } from './client';

// The provider env-var names the workspace lets a user set for themselves.
// Must match server.py ProviderKeysManager.ALLOWED.
export type ProviderVar = 'OPENROUTER_API_KEY' | 'DEEPSEEK_API_KEY' | 'ANTHROPIC_API_KEY';

export interface ProviderKeyStatus {
  set: boolean;
  hint: string; // last-4 like "…cc18"; never the full key
}

export type ProviderKeysView = Record<ProviderVar, ProviderKeyStatus>;

export const listProviderKeys = () =>
  apiGet<{ providers: ProviderKeysView }>('/api/provider-keys');

export const setProviderKey = (provider: ProviderVar, key: string) =>
  apiPost<{ ok: true; provider: ProviderVar }>('/api/provider-keys', { provider, key });

export const deleteProviderKey = (provider: ProviderVar) =>
  apiDelete<{ ok: true }>(`/api/provider-keys/${provider}`);
