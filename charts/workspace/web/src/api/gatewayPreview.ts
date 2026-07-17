import { apiGet, apiPost } from './client';

/**
 * Client for the Walkie-Talkie in-app loopback preview (issue #306).
 *
 * The preview drives the SAME Conversation Gateway core through a loopback
 * channel adapter that advertises WhatsApp's capabilities, so what you see here
 * is exactly what the WhatsApp integration would send/receive — projection,
 * choice → buttons, chunking, ack/final policy, out-of-window template — while a
 * real Hypervisor turn runs locally. Each message also carries the raw provider
 * "wire" payload so the UI can reveal what WhatsApp actually gets on the wire.
 * See charts/workspace/server.py handle_gateway_internal_* + adapters/internal.py.
 */

export interface PreviewWire {
  provider?: string;
  /** Outbound provider message objects (Meta/Twilio), as they'd hit the wire. */
  payloads?: unknown[];
  /** Inbound provider webhook shape (what WhatsApp would POST to us). */
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
