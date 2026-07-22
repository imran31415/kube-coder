import { apiGet, apiPost, apiPut, apiDelete } from './client';

/**
 * Client for the Conversation Gateway config + link endpoints (issues #328/#329).
 *
 * The provider catalog is data-driven: GET /api/gateway/providers returns each
 * provider's declared credential fields (from the registry in
 * adapters/whatsapp.py), so the Settings form renders whatever the selected
 * provider needs with zero UI changes. Credentials are stored per-workspace and
 * always returned REDACTED — secret fields expose only `set` + a last-4 `hint`,
 * never the value. Mirrors api/providerKeys.ts + api/gatewayPreview.ts.
 */

// ── provider catalog (mirrors ProviderSpec.to_dict / CredentialField.to_dict) ──
export interface CredentialField {
  key: string;
  label: string;
  secret: boolean;
  placeholder: string;
  help_url: string;
  required: boolean;
}

export interface ProviderCapabilities {
  proactive: boolean;
  max_text_len: number;
  [k: string]: unknown;
}

export interface ProviderSpec {
  id: string;
  display_name: string;
  credential_fields: CredentialField[];
  sender_field: CredentialField;
  capabilities: ProviderCapabilities;
}

// ── redacted credential state (mirrors GatewayCredentialsManager.public_view) ──
export interface CredentialFieldState {
  set: boolean;
  hint?: string; // secret fields: "…9999"; never the value
  value?: string; // non-secret fields only (SID, verify token, sender number)
}

export interface CredentialsView {
  configured: boolean;
  provider_id: string | null;
  sender_field?: string;
  fields: Record<string, CredentialFieldState>;
}

// ── identity bindings (mirrors IdentityRegistry.public_view) ──
export interface LinkBinding {
  workspace: string;
  workspace_host: string;
  is_default: boolean;
  has_thread: boolean;
  token_set: boolean;
  bound_at: number;
}

export interface GatewayLink {
  id: string;
  channel: string;
  created_at: number;
  updated_at: number;
  bindings: LinkBinding[];
}

export interface PairingCode {
  code: string;
  expires_in: number;
  whatsapp_number: string;
  workspace: string;
}

// ── config API ──
export const getProviders = () =>
  apiGet<{ providers: ProviderSpec[]; available: boolean }>('/api/gateway/providers');

export const getCredentials = () =>
  apiGet<{ credentials: CredentialsView }>('/api/gateway/credentials');

export const putCredentials = (body: {
  provider_id: string;
  creds: Record<string, string>;
  sender_number?: string;
}) => apiPut<{ ok: true; credentials: CredentialsView }>('/api/gateway/credentials', body);

export const deleteCredentials = () =>
  apiDelete<{ ok: true }>('/api/gateway/credentials');

export const testConnection = () =>
  apiPost<{ ok: boolean; detail: string }>('/api/gateway/test');

// ── link (pairing) API — endpoints from issue #306 ──
export const createLink = (workspace = 'workspace') =>
  apiPost<PairingCode>('/api/gateway/link', { workspace });

export const listLinks = () =>
  apiGet<{
    links: GatewayLink[];
    available: boolean;
    whatsapp_number?: string;
    proactive?: boolean;
  }>('/api/gateway/links');

export const deleteLink = (id: string) =>
  apiDelete<{ ok: boolean }>(`/api/gateway/link/${id}`);
