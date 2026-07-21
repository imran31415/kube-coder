#!/usr/bin/env python3
"""WhatsApp ChannelAdapter (issue #306, B7).

Implements the `gateway.ChannelAdapter` contract for WhatsApp, with a
provider-agnostic seam so the Twilio→Meta swap (issue D1) is a config change, not
a rewrite. Everything channel-agnostic (identity, routing, projection, policy)
lives in `gateway.py`; this file owns ONLY:

  * signature verification — Twilio `X-Twilio-Signature` (base64 HMAC-SHA1 over
    URL + alphabetically-sorted form params) vs Meta `X-Hub-Signature-256`
    (`sha256=` + HMAC-SHA256 of the RAW body). Twilio's scheme is custom and easy
    to get subtly wrong, so it has its own tests (issue §9).
  * the Meta GET verify handshake (`hub.mode`/`hub.challenge`/`hub.verify_token`).
  * inbound parse — Twilio form (`From`, `Body`, `MessageSid`, `NumMedia`,
    `MediaUrl0…`, `ButtonText`) and Meta JSON (`entry[].changes[].value.messages`).
  * rendering a core `choice` into WhatsApp interactive **reply buttons** (≤3) /
    **list** (≤10 rows) / **numbered-text fallback** (>10), media messages, and
    ≤4096-char chunking.
  * the outbound send API (Twilio Messages / Meta Cloud API), with in-window
    free-form vs out-of-window template EXECUTION (the core SELECTS; only the
    adapter knows the provider's template API).

Twilio note: dynamic interactive buttons require pre-created Content templates,
which the sandbox can't do — so the Twilio path renders a `choice` as a numbered
text list (reliable everywhere), while the Meta path uses native interactive
messages. Both round-trip a tapped reply back to the chosen option text.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from gateway import (Capabilities, DeliveryResult, InboundMessage, MediaItem,
                    OutboundMessage, RawRequest, chunk_text)

WHATSAPP_MAX_TEXT = 4096
BUTTON_TITLE_MAX = 20    # Meta reply-button label limit
ROW_TITLE_MAX = 24       # Meta list-row title limit


# ───────────────────────────────────────────────────────────────────────────
# choice → interactive rendering (pure — unit-tested)
# ───────────────────────────────────────────────────────────────────────────
@dataclass
class InteractiveSpec:
    kind: str = 'text'                       # 'buttons' | 'list' | 'text'
    buttons: List[Dict[str, str]] = field(default_factory=list)  # {id,title}
    rows: List[Dict[str, str]] = field(default_factory=list)     # {id,title,description}
    numbered_text: str = ''                  # appended to the body for 'text'


def _truncate(s: str, limit: int) -> str:
    s = s or ''
    return s if len(s) <= limit else s[: max(0, limit - 1)] + '…'


def render_choice(options: List[str], caps: Capabilities) -> InteractiveSpec:
    """Map a core choice's options onto the best WhatsApp affordance.

    ≤max_buttons (3)   → reply buttons   (label truncated to 20 chars)
    ≤max_list_rows (10) → list message   (row title 24 chars; full text in the
                                          row description so nothing is lost)
    >max_list_rows      → numbered-text fallback (no interactive element)
    Row/button ids are the 1-based index as a string, so an inbound tap maps back
    to the option by position."""
    opts = [o for o in (options or []) if isinstance(o, str) and o.strip()]
    if not opts:
        return InteractiveSpec(kind='text', numbered_text='')
    if len(opts) <= caps.max_buttons:
        return InteractiveSpec(
            kind='buttons',
            buttons=[{'id': str(i + 1), 'title': _truncate(o, BUTTON_TITLE_MAX)}
                     for i, o in enumerate(opts)])
    if len(opts) <= caps.max_list_rows:
        return InteractiveSpec(
            kind='list',
            rows=[{'id': str(i + 1), 'title': _truncate(o, ROW_TITLE_MAX),
                   'description': o if len(o) > ROW_TITLE_MAX else ''}
                  for i, o in enumerate(opts)])
    numbered = '\n'.join(f'{i + 1}. {o}' for i, o in enumerate(opts))
    return InteractiveSpec(kind='text', numbered_text=numbered)


# ───────────────────────────────────────────────────────────────────────────
# Provider catalog — declarative spec (issue #328)
# ───────────────────────────────────────────────────────────────────────────
# Each provider declares WHAT it needs (credential fields + sender field) and
# WHAT it can do (Capabilities) as data. A registry maps provider_id → (spec,
# adapter factory), so `build_provider(id, creds)` constructs a provider from a
# flat creds dict and `list_providers()` exposes the specs. The spec is fully
# JSON-serializable (via to_dict) — the Settings form in stage 3 is rendered
# entirely from it, so adding a provider is a new adapter class + one
# register_provider(...) call, never a UI or core change.
@dataclass
class CredentialField:
    """One credential the user must supply for a provider. Also the schema the
    stage-3 Settings form renders from and the key the stage-2 store persists
    under, so `key` doubles as the creds-dict / storage key."""
    key: str                    # creds-dict key AND future storage key
    label: str                  # UI label
    secret: bool = True         # masked in UI; redacted in stage-2 public_view()
    placeholder: str = ''
    help_url: str = ''
    required: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            'key': self.key,
            'label': self.label,
            'secret': self.secret,
            'placeholder': self.placeholder,
            'help_url': self.help_url,
            'required': self.required,
        }


@dataclass
class ProviderSpec:
    """Declarative description of a messaging provider — its identity, the
    credential fields it needs, its sending-identity field, and its
    Capabilities. Serializable so the UI is data-driven."""
    id: str                             # 'twilio' | 'meta'
    display_name: str
    credential_fields: List[CredentialField]
    sender_field: CredentialField       # provider-specific sending identity
    capabilities: Capabilities

    def field_keys(self) -> List[str]:
        """Every creds-dict key this provider consumes (credentials + sender)."""
        return [f.key for f in self.credential_fields] + [self.sender_field.key]

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'display_name': self.display_name,
            'credential_fields': [f.to_dict() for f in self.credential_fields],
            'sender_field': self.sender_field.to_dict(),
            'capabilities': asdict(self.capabilities),
        }


# WhatsApp platform capabilities are the same across providers EXCEPT proactive:
# out-of-window sends need an approved template, which the real Meta Cloud API
# supports but the Twilio sandbox does not.
_WA_CAPS = dict(buttons=True, max_buttons=3, max_list_rows=10, media=True,
                typing_indicator=True, max_text_len=WHATSAPP_MAX_TEXT)
TWILIO_CAPS = Capabilities(proactive=False, **_WA_CAPS)
META_CAPS = Capabilities(proactive=True, **_WA_CAPS)


# ───────────────────────────────────────────────────────────────────────────
# Provider seam
# ───────────────────────────────────────────────────────────────────────────
class _Provider:
    name = 'base'
    # Providers declare their capabilities as a class attribute; the adapter
    # reads provider.capabilities instead of branching on the provider type.
    capabilities: Capabilities = Capabilities()

    def verify(self, raw: RawRequest) -> bool:
        raise NotImplementedError

    def handshake(self, raw: RawRequest) -> Optional[str]:
        return None

    def parse_inbound(self, raw: RawRequest) -> Optional[InboundMessage]:
        raise NotImplementedError

    def send(self, msg: OutboundMessage, caps: Capabilities) -> DeliveryResult:
        raise NotImplementedError


class TwilioProvider(_Provider):
    """Twilio WhatsApp — SMS-shaped form webhook + REST Messages API.

    Inbound: `application/x-www-form-urlencoded` with `From=whatsapp:+E164`,
    `Body`, `MessageSid`, `NumMedia`, `MediaUrl0…`, and `ButtonText` for a tapped
    quick-reply. Signature: base64(HMAC-SHA1(AuthToken, URL + sorted k+v))."""

    name = 'twilio'
    capabilities = TWILIO_CAPS

    def __init__(self, *, auth_token: str = '', account_sid: str = '',
                 from_number: str = ''):
        self.auth_token = auth_token
        self.account_sid = account_sid
        self.from_number = from_number

    # -- signature -----------------------------------------------------------
    @staticmethod
    def signature(auth_token: str, url: str, params: Dict[str, str]) -> str:
        """Twilio's X-Twilio-Signature: sort the POST params by key, concatenate
        the full request URL then each key immediately followed by its value,
        HMAC-SHA1 with the AuthToken, base64 the digest."""
        data = url + ''.join(f'{k}{params[k]}' for k in sorted(params.keys()))
        digest = hmac.new(auth_token.encode('utf-8'),
                          data.encode('utf-8'), hashlib.sha1).digest()
        return base64.b64encode(digest).decode('ascii')

    def verify(self, raw: RawRequest) -> bool:
        if not self.auth_token:
            # Fail closed unless explicitly opted out (dev/sandbox).
            return _allow_unsigned()
        provided = raw.headers.get('X-Twilio-Signature', '')
        if not provided:
            return False
        expected = self.signature(self.auth_token, raw.url, raw.form)
        try:
            return hmac.compare_digest(expected, provided)
        except (TypeError, ValueError):
            return False

    def parse_inbound(self, raw: RawRequest) -> Optional[InboundMessage]:
        f = raw.form
        frm = f.get('From') or f.get('from')
        if not frm:
            return None
        media: List[MediaItem] = []
        try:
            num_media = int(f.get('NumMedia', '0') or '0')
        except ValueError:
            num_media = 0
        for i in range(num_media):
            url = f.get(f'MediaUrl{i}')
            if url:
                media.append(MediaItem(kind='image', url=url,
                                       mime=f.get(f'MediaContentType{i}', '')))
        return InboundMessage(
            channel='whatsapp',
            channel_identity=frm if frm.startswith('whatsapp:') else f'whatsapp:{frm}',
            text=f.get('Body', ''),
            media=media,
            provider_msg_id=f.get('MessageSid', ''),
            # A tapped quick-reply arrives as ButtonText (Twilio) — that IS the
            # option text, so it maps straight back to the choice.
            button_reply=f.get('ButtonText', ''),
        )

    def send(self, msg: OutboundMessage, caps: Capabilities) -> DeliveryResult:
        payloads = self.build_payloads(msg, caps)
        last = DeliveryResult(ok=True)
        for body in payloads:
            last = self._post(body)
            if not last.ok:
                break
        return last

    def build_payloads(self, msg: OutboundMessage,
                       caps: Capabilities) -> List[Dict[str, str]]:
        """Twilio Messages form params, one per ≤4096 chunk. Interactive choices
        degrade to a numbered text list (sandbox can't create Content buttons)."""
        to = msg.channel_identity
        body = msg.text or ''
        if msg.quick_replies:
            spec = render_choice(msg.quick_replies, caps)
            if spec.kind == 'buttons':
                body += '\n\n' + '\n'.join(
                    f'{b["id"]}. {b["title"]}' for b in spec.buttons)
            elif spec.kind == 'list':
                body += '\n\n' + '\n'.join(
                    f'{r["id"]}. {r["title"]}' for r in spec.rows)
            elif spec.numbered_text:
                body += '\n\n' + spec.numbered_text
        out = []
        for chunk in chunk_text(body, caps.max_text_len):
            params = {'To': to, 'Body': chunk}
            if self.from_number:
                params['From'] = self.from_number
            for m in msg.media:
                if m.url:
                    params['MediaUrl'] = m.url
                    break
            out.append(params)
        return out

    def _post(self, params: Dict[str, str]) -> DeliveryResult:
        if not (self.account_sid and self.auth_token):
            return DeliveryResult(ok=False, error='twilio credentials not configured')
        url = (f'https://api.twilio.com/2010-04-01/Accounts/'
               f'{self.account_sid}/Messages.json')
        data = urllib.parse.urlencode(params).encode('utf-8')
        auth = base64.b64encode(
            f'{self.account_sid}:{self.auth_token}'.encode()).decode()
        req = urllib.request.Request(url, data=data, method='POST', headers={
            'Authorization': f'Basic {auth}',
            'Content-Type': 'application/x-www-form-urlencoded',
        })
        return _do_send(req, id_key='sid')


class MetaProvider(_Provider):
    """Meta WhatsApp Cloud API — JSON webhook + Graph send API.

    Inbound: `entry[].changes[].value.messages[]` with `from`, `id`,
    `text.body`, `type`, and interactive replies under `interactive`.
    Signature: `X-Hub-Signature-256` = `sha256=` + HMAC-SHA256(AppSecret, raw
    body) — already matches kube-coder's generic verifier."""

    name = 'meta'
    capabilities = META_CAPS
    GRAPH = 'https://graph.facebook.com/v19.0'

    def __init__(self, *, app_secret: str = '', verify_token: str = '',
                 phone_number_id: str = '', access_token: str = ''):
        self.app_secret = app_secret
        self.verify_token = verify_token
        self.phone_number_id = phone_number_id
        self.access_token = access_token

    def verify(self, raw: RawRequest) -> bool:
        if not self.app_secret:
            return _allow_unsigned()
        provided = raw.headers.get('X-Hub-Signature-256', '')
        if not provided:
            return False
        expected = 'sha256=' + hmac.new(
            self.app_secret.encode('utf-8'), raw.raw_body, hashlib.sha256).hexdigest()
        try:
            return hmac.compare_digest(expected, provided.strip())
        except (TypeError, ValueError):
            return False

    def handshake(self, raw: RawRequest) -> Optional[str]:
        """GET verify: echo hub.challenge iff mode=subscribe and the verify
        token matches. Returns the challenge string to send back, or None."""
        q = raw.query
        if q.get('hub.mode') == 'subscribe' \
                and q.get('hub.verify_token') == self.verify_token \
                and self.verify_token:
            return q.get('hub.challenge')
        return None

    def parse_inbound(self, raw: RawRequest) -> Optional[InboundMessage]:
        try:
            payload = json.loads(raw.raw_body.decode('utf-8')) if raw.raw_body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        for entry in payload.get('entry', []) or []:
            for change in entry.get('changes', []) or []:
                value = change.get('value', {}) or {}
                messages = value.get('messages') or []
                if not messages:
                    continue  # status callbacks carry no `messages`
                m = messages[0]
                frm = m.get('from')
                if not frm:
                    continue
                text = ''
                button_reply = ''
                mtype = m.get('type')
                if mtype == 'text':
                    text = (m.get('text') or {}).get('body', '')
                elif mtype == 'interactive':
                    inter = m.get('interactive') or {}
                    br = inter.get('button_reply') or inter.get('list_reply') or {}
                    button_reply = br.get('title') or br.get('id') or ''
                elif mtype == 'button':
                    button_reply = (m.get('button') or {}).get('text', '')
                media: List[MediaItem] = []
                for mk in ('image', 'document', 'audio', 'video'):
                    if mtype == mk and isinstance(m.get(mk), dict):
                        media.append(MediaItem(
                            kind=mk, url=m[mk].get('id', ''),
                            mime=m[mk].get('mime_type', ''),
                            caption=m[mk].get('caption', '')))
                return InboundMessage(
                    channel='whatsapp',
                    channel_identity=f'whatsapp:{frm}' if not frm.startswith('whatsapp:') else frm,
                    text=text, media=media,
                    provider_msg_id=m.get('id', ''),
                    button_reply=button_reply)
        return None

    def build_payloads(self, msg: OutboundMessage,
                       caps: Capabilities) -> List[Dict[str, Any]]:
        """Meta Cloud API message objects, one per ≤4096 chunk. Interactive
        choices render as native reply-button / list messages on the LAST chunk;
        >10 options fall back to numbered text."""
        to = msg.channel_identity.replace('whatsapp:', '')
        chunks = chunk_text(msg.text or '', caps.max_text_len)
        payloads: List[Dict[str, Any]] = []
        spec = render_choice(msg.quick_replies, caps) if msg.quick_replies else None
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            base = {'messaging_product': 'whatsapp', 'to': to}
            if last and spec and spec.kind == 'buttons':
                base['type'] = 'interactive'
                base['interactive'] = {
                    'type': 'button',
                    'body': {'text': chunk or '​'},
                    'action': {'buttons': [
                        {'type': 'reply',
                         'reply': {'id': b['id'], 'title': b['title']}}
                        for b in spec.buttons]},
                }
            elif last and spec and spec.kind == 'list':
                base['type'] = 'interactive'
                base['interactive'] = {
                    'type': 'list',
                    'body': {'text': chunk or '​'},
                    'action': {'button': 'Choose', 'sections': [
                        {'title': 'Options', 'rows': spec.rows}]},
                }
            else:
                text = chunk
                if last and spec and spec.kind == 'text' and spec.numbered_text:
                    text = (chunk + '\n\n' + spec.numbered_text).strip()
                base['type'] = 'text'
                base['text'] = {'body': text or '​', 'preview_url': False}
            payloads.append(base)
        # Media as trailing message(s) — capability-gated by the caller.
        if caps.media:
            for m in msg.media:
                if not m.url:
                    continue
                payloads.append({
                    'messaging_product': 'whatsapp', 'to': to, 'type': m.kind,
                    m.kind: {'link': m.url, **({'caption': m.caption} if m.caption else {})},
                })
        return payloads

    def build_template_payload(self, msg: OutboundMessage) -> Dict[str, Any]:
        """Out-of-window template send (the core SELECTED it; we execute)."""
        to = msg.channel_identity.replace('whatsapp:', '')
        return {
            'messaging_product': 'whatsapp', 'to': to, 'type': 'template',
            'template': {
                'name': msg.template,
                'language': {'code': 'en'},
            },
        }

    def send(self, msg: OutboundMessage, caps: Capabilities) -> DeliveryResult:
        if msg.template:
            return self._post(self.build_template_payload(msg))
        last = DeliveryResult(ok=True)
        for body in self.build_payloads(msg, caps):
            last = self._post(body)
            if not last.ok:
                break
        return last

    def _post(self, body: Dict[str, Any]) -> DeliveryResult:
        if not (self.phone_number_id and self.access_token):
            return DeliveryResult(ok=False, error='meta credentials not configured')
        url = f'{self.GRAPH}/{self.phone_number_id}/messages'
        req = urllib.request.Request(
            url, data=json.dumps(body).encode('utf-8'), method='POST', headers={
                'Authorization': f'Bearer {self.access_token}',
                'Content-Type': 'application/json',
            })
        return _do_send(req, id_key='messages')


def _allow_unsigned() -> bool:
    """Opt-in escape hatch for dev/sandbox — mirrors WebhookManager's
    KC_ALLOW_UNSIGNED_WEBHOOKS. Off by default so production fails closed."""
    return os.environ.get('KC_ALLOW_UNSIGNED_WEBHOOKS', '').strip().lower() in (
        '1', 'true', 'yes', 'on')


def _do_send(req: urllib.request.Request, *, id_key: str) -> DeliveryResult:
    """Execute an outbound send with a short timeout; never raises."""
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode('utf-8', 'replace')
            status = getattr(resp, 'status', 200)
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            data = {}
        pid = ''
        if id_key == 'sid':
            pid = data.get('sid', '')
        elif isinstance(data.get('messages'), list) and data['messages']:
            pid = data['messages'][0].get('id', '')
        return DeliveryResult(ok=True, provider_msg_id=pid, status=status)
    except Exception as e:  # network / HTTP error — surface, never crash
        return DeliveryResult(ok=False, error=f'{type(e).__name__}: {e}')


# ───────────────────────────────────────────────────────────────────────────
# The adapter
# ───────────────────────────────────────────────────────────────────────────
class WhatsAppAdapter:
    """gateway.ChannelAdapter for WhatsApp. Provider chosen at construction
    (Twilio Phase-1 default, Meta Cloud API for scale). Capabilities are the same
    either way — the WhatsApp platform's, not the provider's."""

    name = 'whatsapp'

    def __init__(self, provider: Optional[_Provider] = None):
        self.provider = provider or _provider_from_env()
        # Capabilities are declared by the provider (proactive out-of-window
        # sends need an approved template — Meta Cloud API only, not the Twilio
        # sandbox), so the adapter reads them rather than branching on type.
        self.capabilities = self.provider.capabilities

    def verify(self, raw: RawRequest) -> bool:
        return self.provider.verify(raw)

    def handshake(self, raw: RawRequest) -> Optional[str]:
        return self.provider.handshake(raw)

    def inbound(self, raw: RawRequest) -> Optional[InboundMessage]:
        return self.provider.parse_inbound(raw)

    def outbound(self, msg: OutboundMessage) -> DeliveryResult:
        return self.provider.send(msg, self.capabilities)


# ───────────────────────────────────────────────────────────────────────────
# Provider registry (issue #328)
# ───────────────────────────────────────────────────────────────────────────
# provider_id → (spec, factory). The factory maps a flat creds dict (keyed by
# the spec's CredentialField.key values) onto the provider's kwargs constructor.
DEFAULT_PROVIDER_ID = 'twilio'
_ProviderFactory = Callable[[Dict[str, str]], _Provider]
_REGISTRY: 'Dict[str, Tuple[ProviderSpec, _ProviderFactory]]' = {}


def register_provider(spec: ProviderSpec, factory: _ProviderFactory) -> None:
    """Register a provider so build_provider/list_providers can see it. Called
    at import time; adding a provider is a new adapter class + one call here."""
    _REGISTRY[spec.id] = (spec, factory)


def list_providers() -> List[ProviderSpec]:
    """All registered provider specs, in registration order (data for the UI)."""
    return [spec for spec, _factory in _REGISTRY.values()]


def get_provider_spec(provider_id: str) -> Optional[ProviderSpec]:
    entry = _REGISTRY.get((provider_id or '').strip().lower())
    return entry[0] if entry else None


def build_provider(provider_id: str,
                   creds: Optional[Dict[str, str]] = None) -> _Provider:
    """Construct a provider from its id + a flat creds dict. Raises ValueError
    on an unknown id so API callers (stage 2) fail loudly; env callers normalize
    the id first so they never hit that path."""
    entry = _REGISTRY.get((provider_id or '').strip().lower())
    if entry is None:
        raise ValueError(f'unknown provider: {provider_id!r}')
    _spec, factory = entry
    return factory(creds or {})


# -- Twilio -------------------------------------------------------------------
TWILIO_SPEC = ProviderSpec(
    id='twilio',
    display_name='Twilio',
    credential_fields=[
        CredentialField(
            key='account_sid', label='Account SID', secret=False,
            placeholder='ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
            help_url='https://www.twilio.com/console'),
        CredentialField(
            key='auth_token', label='Auth Token', secret=True,
            help_url='https://www.twilio.com/console'),
    ],
    sender_field=CredentialField(
        key='from_number', label='WhatsApp sender number', secret=False,
        placeholder='whatsapp:+14155238886',
        help_url='https://www.twilio.com/docs/whatsapp'),
    capabilities=TWILIO_CAPS,
)


def _twilio_factory(creds: Dict[str, str]) -> _Provider:
    return TwilioProvider(
        auth_token=creds.get('auth_token', ''),
        account_sid=creds.get('account_sid', ''),
        from_number=creds.get('from_number', ''))


# -- Meta ---------------------------------------------------------------------
META_SPEC = ProviderSpec(
    id='meta',
    display_name='Meta (WhatsApp Cloud API)',
    credential_fields=[
        CredentialField(
            key='access_token', label='Access Token', secret=True,
            help_url='https://developers.facebook.com/docs/whatsapp/cloud-api/get-started'),
        CredentialField(
            key='app_secret', label='App Secret', secret=True,
            help_url='https://developers.facebook.com/docs/graph-api/webhooks/getting-started'),
        CredentialField(
            key='verify_token', label='Webhook Verify Token', secret=False,
            placeholder='a token you choose',
            help_url='https://developers.facebook.com/docs/graph-api/webhooks/getting-started'),
    ],
    # Meta's sending identity is the opaque Phone Number ID (the WABA sender),
    # not a dialable number — so it lives here, not among the numbered fields.
    sender_field=CredentialField(
        key='phone_number_id', label='Phone Number ID', secret=False,
        placeholder='1234567890',
        help_url='https://developers.facebook.com/docs/whatsapp/cloud-api/get-started'),
    capabilities=META_CAPS,
)


def _meta_factory(creds: Dict[str, str]) -> _Provider:
    return MetaProvider(
        app_secret=creds.get('app_secret', ''),
        verify_token=creds.get('verify_token', ''),
        phone_number_id=creds.get('phone_number_id', ''),
        access_token=creds.get('access_token', ''))


register_provider(TWILIO_SPEC, _twilio_factory)
register_provider(META_SPEC, _meta_factory)


def _provider_from_env() -> _Provider:
    """Build the configured provider from env, via the registry. Env stays a
    valid source for platform-managed deployments. KC_WHATSAPP_PROVIDER selects
    twilio (default) or meta; the id is normalized to a known provider before
    build_provider, so this never raises on a bad value (any non-'meta' → twilio,
    preserving the pre-#328 behavior)."""
    which = os.environ.get('KC_WHATSAPP_PROVIDER', 'twilio').strip().lower()
    if which == 'meta':
        return build_provider('meta', {
            'app_secret': os.environ.get('KC_META_APP_SECRET', ''),
            'verify_token': os.environ.get('KC_META_VERIFY_TOKEN', ''),
            'phone_number_id': os.environ.get('KC_META_PHONE_NUMBER_ID', ''),
            'access_token': os.environ.get('KC_META_ACCESS_TOKEN', ''),
        })
    return build_provider('twilio', {
        'auth_token': os.environ.get('KC_TWILIO_AUTH_TOKEN', ''),
        'account_sid': os.environ.get('KC_TWILIO_ACCOUNT_SID', ''),
        'from_number': os.environ.get('KC_TWILIO_FROM', ''),
    })
