import { apiGet, apiPost } from './client';

/**
 * Client for the Walkie-Talkie in-app loopback preview (issue #306).
 *
 * The preview drives the SAME Conversation Gateway core through an internal
 * loopback channel adapter, so what you see here — projection, choice → buttons,
 * chunking, ack/final policy, out-of-window template — is exactly how the gateway
 * behaves, while a real Hypervisor turn runs locally. Only the internal loopback
 * transport is connected today; other providers will be added soon.
 * See charts/workspace/server.py handle_gateway_internal_* + adapters/internal.py.
 */

export interface PreviewWire {
  provider?: string;
  /** Outbound provider message objects, as they'd hit the wire. */
  payloads?: unknown[];
  /** Inbound provider webhook shape. */
  inbound?: Record<string, unknown>;
  error?: string;
}

export type PreviewDirection = 'in' | 'out';
export type PreviewKind = 'message' | 'template' | 'notice';

export interface PreviewMessage {
  seq: number;
  ts: number;
  direction: PreviewDirection;
  kind: PreviewKind;
  text: string;
  quick_replies: string[];
  wire: PreviewWire | null;
  meta: Record<string, unknown>;
}

export interface PreviewState {
  available: boolean;
  messages: PreviewMessage[];
  cursor: number;
  linked: boolean;
  simulate_out_of_window: boolean;
  provider: string;
  identity: string;
  busy: boolean;
  thread_id: string | null;
}

export interface PreviewSendResult {
  ok: boolean;
  action: string;
  cursor: number;
}

export function fetchPreview(since = 0) {
  return apiGet<PreviewState>('/api/gateway/internal/transcript', { since });
}

export function sendPreview(text: string, button?: string) {
  return apiPost<PreviewSendResult>(
    '/api/gateway/internal/inbound',
    button ? { button } : { text },
  );
}

export type PreviewControlAction = 'link' | 'simulate' | 'reset';

export function previewControl(action: PreviewControlAction, on?: boolean) {
  return apiPost<{ ok: boolean; linked?: boolean; simulate_out_of_window?: boolean }>(
    '/api/gateway/internal/control',
    { action, on },
  );
}
