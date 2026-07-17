#!/usr/bin/env python3
"""Loopback ("internal") ChannelAdapter for the Walkie-Talkie preview (#306).

Same `gateway.ChannelAdapter` contract as the WhatsApp adapter, but the transport
is the in-app dashboard instead of Twilio/Meta. It advertises **WhatsApp's**
Capabilities, so the gateway core behaves identically — projection, choice →
buttons/list, ≤4096 chunking, ack/final policy, out-of-window template selection.

Its distinguishing feature is the **wire view**: every outbound message is also
rendered through a real WhatsApp provider's `build_payloads()` (WITHOUT sending),
so the UI can show the exact provider JSON WhatsApp would receive. That's what
lets a user "see what the WhatsApp integration would see" while driving a real
Hypervisor turn locally.

No network, no signatures — the inbound endpoint is bearer-authed (it's the app
user), so `verify()` is a no-op True.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from gateway import (Capabilities, DeliveryResult, InboundMessage,
                    OutboundMessage, RawRequest)
from adapters.whatsapp import MetaProvider, WHATSAPP_MAX_TEXT


class LoopbackAdapter:
    """gateway.ChannelAdapter whose outbound() records to a PreviewTranscript and
    pushes a dashboard event, and whose inbound() reads the preview endpoint's
    JSON. `wire_provider` renders the provider payload for the wire view only."""

    name = 'internal'

    def __init__(self, transcript, *, wire_provider=None,
                 publish: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
                 identity: str = 'internal:local'):
        self.transcript = transcript
        self.identity = identity
        self.publish = publish
        # Meta by default: its interactive/list/template payloads are the richest
        # to preview, and rendering needs no credentials. Represents a registered
        # sender, so proactive=True makes the out-of-window template path demoable.
        self.wire_provider = wire_provider or MetaProvider()
        self.capabilities = Capabilities(
            buttons=True, max_buttons=3, max_list_rows=10, media=True,
            typing_indicator=True, proactive=True, max_text_len=WHATSAPP_MAX_TEXT)

    # ── contract ─────────────────────────────────────────────────────────────
    def verify(self, raw: RawRequest) -> bool:
        return True  # bearer-authed at the route; no provider signature

    def handshake(self, raw: RawRequest) -> Optional[str]:
        return None

    def inbound(self, raw: RawRequest) -> Optional[InboundMessage]:
        frm = raw.form.get('from') or self.identity
        text = raw.form.get('text', '')
        button = raw.form.get('button', '')
        if not (text or button):
            return None
        return InboundMessage(
            channel='internal', channel_identity=frm, text=text,
            button_reply=button, provider_msg_id='')  # empty → never deduped

    def outbound(self, msg: OutboundMessage) -> DeliveryResult:
        wire = self._render_wire(msg)
        kind = 'template' if msg.template else 'message'
        item = self.transcript.add(
            'out', msg.text, wire=wire, quick_replies=list(msg.quick_replies),
            kind=kind, meta={'provider': self.wire_provider.name,
                             'seq': msg.seq})
        if self.publish is not None:
            try:
                self.publish('gateway.preview', {'seq': item['seq']})
            except Exception:
                pass
        return DeliveryResult(ok=True, provider_msg_id=f'loopback-{item["seq"]}')

    # ── wire view ────────────────────────────────────────────────────────────
    def _render_wire(self, msg: OutboundMessage) -> Dict[str, Any]:
        """The exact provider payload(s) this outbound WOULD become on the wire —
        built, never sent. Wrapped so a rendering hiccup can't break delivery."""
        try:
            if msg.template:
                payloads = [self.wire_provider.build_template_payload(msg)]
            else:
                payloads = self.wire_provider.build_payloads(msg, self.capabilities)
        except Exception as e:  # pragma: no cover - defensive
            return {'provider': self.wire_provider.name,
                    'error': f'{type(e).__name__}: {e}', 'payloads': []}
        return {'provider': self.wire_provider.name, 'payloads': payloads}
