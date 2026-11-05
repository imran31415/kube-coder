#!/usr/bin/env python3
"""Board review cards over WhatsApp — Approve / Reject from the phone (#709 Part 4).

A Board item waiting on a staged approval already reaches the phone as a push
(FeedManager.emit → push_notify.dispatch). This sends the same Feed item to the
paired WhatsApp number too, and lets a tap decide it.

Two card shapes, and the line between them is the whole safety story:

* **Short write** — ONE staged `comment` whose full text fits in a message with
  headroom (SHORT_BODY_MAX). The card shows the full text with
  Approve / Reject / Open in app.
* **Anything else** — long text, several writes, a status change — a one-line
  summary and a link. Nobody approves customer-visible text they can't read.

A button carries `kcr1.<verb>.<nonce>.<hash16>.<mac>`: the card nonce, a prefix
of the ticket `content_hash` the card was drawn from, and an HMAC over both
with a per-workspace key. The nonce names a card record on the PVC that holds
the board, item, full content hash, the proposal digest and WHICH identity it
was sent to. A tap is honoured only when the MAC verifies, the card exists, it
was sent to the identity tapping, and its hash matches — then it goes through
the SAME BoardReviewManager.approve/decide the app's routes call, so the ticket
staleness, card-echo and replay guards all apply. The approval id is derived
from the nonce, so a double tap or a retried delivery replays the first result
instead of writing twice.

Outside the 24-h window a free-form message is refused by WhatsApp, so the card
goes out as a pre-approved template (`board_review` / `board_review_link` in
gateway.TemplateRegistry). The Twilio sandbox can't hold a Content template; in
the window it gets the numbered-text fallback ("1. Approve") the gateway uses
for every choice, and a bare "1"/"2"/"3" counts only while that card is still
the last thing the gateway sent this number.

Stdlib plus the pure `boards.review`; never imports server.py. The server hands
in the record reader and the decider (see server.get_gateway).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Tuple

from boards import review as board_review
from gateway import (GATEWAY_DIR, IdentityRegistry, InboundResult,
                     OutboundMessage)

PAYLOAD_PREFIX = 'kcr1'
VERBS = ('a', 'r', 'o')               # approve | reject | open in app
BUTTON_TITLES = {'a': 'Approve', 'r': 'Reject', 'o': 'Open in app'}
NUMBERED = {'1': 'a', '2': 'r', '3': 'o'}

# Meta caps an interactive / template body at 1,024 chars and Twilio's Content
# quick-reply at the same. The card adds a header of up to ~200 (key, title,
# "Proposed public reply"), so 700 leaves headroom on every path.
SHORT_BODY_MAX = 700
COMMENT_ACTION = 'comment'

CARD_TTL = 7 * 24 * 3600              # a card older than this decides nothing
NUMBERED_TTL = 24 * 3600              # the free-form window it was sent in

_NONCE_RE = re.compile(r'^[0-9a-f]{24}$')
_HASH_RE = re.compile(r'^[0-9a-f]{16}$')
_MAC_RE = re.compile(r'^[0-9a-f]{20}$')

CHANGED_TEXT = 'This ticket changed. Open the app to review.'


# ───────────────────────────────────────────────────────────────────────────
# Pure pieces
# ───────────────────────────────────────────────────────────────────────────
def review_ref(item: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """`(board_id, item_id)` when a Feed item is a Board review waiting on a
    human, else None. Board ids are [A-Za-z0-9_-]; item ids may hold colons."""
    if not isinstance(item, dict) or not item.get('waiting'):
        return None
    if not str(item.get('source') or '').startswith('board:'):
        return None
    for link in item.get('links') or []:
        ref = (link or {}).get('ref') if isinstance(link, dict) else ''
        parts = str(ref or '').split(':', 2)
        if len(parts) == 3 and parts[0] == 'board' and parts[1] and parts[2]:
            return parts[1], parts[2]
    return None


def pending_actions(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [a for a in (record or {}).get('actions') or []
            if a.get('state') == 'pending']


def short_comment(pending: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The one staged comment this card may show in full, or None. Any other
    parameter than `body`/`public` means the text isn't the whole write."""
    if len(pending) != 1:
        return None
    action = pending[0]
    params = action.get('params') or {}
    body = params.get('body')
    if action.get('action') != COMMENT_ACTION or set(params) - {'body', 'public'}:
        return None
    if not isinstance(body, str) or not body.strip() or len(body) > SHORT_BODY_MAX:
        return None
    return action


def comment_kind(action: Dict[str, Any]) -> str:
    public = (action.get('params') or {}).get('public')
    if public is True:
        return 'public reply (the customer will see it)'
    if public is False:
        return 'internal note'
    return 'comment'


def summary_line(record: Dict[str, Any], pending: List[Dict[str, Any]]) -> str:
    """What the Open-in-app card says instead of the text."""
    if len(pending) > 1:
        names = ', '.join(sorted({a.get('action') or '?' for a in pending}))
        return f'{len(pending)} proposed writes ({names})'
    action = pending[0] if pending else {}
    body = (action.get('params') or {}).get('body')
    if action.get('action') == COMMENT_ACTION and isinstance(body, str):
        return f'a proposed {comment_kind(action)} of {len(body)} characters'
    return f'a proposed {action.get("action") or "write"}'


def item_label(record: Dict[str, Any]) -> str:
    return str(record.get('item_key') or record.get('item_id') or 'A board item')[:40]


def flatten(text: str) -> str:
    """A template parameter may not hold a newline, a tab or more than four
    spaces in a row (Meta rejects the send), so whitespace runs become one
    space. Every word survives; only line breaks are lost."""
    return re.sub(r'\s+', ' ', text or '').strip()


def open_link(workspace_host: str, board_id: str, item_id: str) -> str:
    """The dashboard's review target — the same `board:<board>:<item>` ref a
    push carries, in the form the SPA routes (`/board?board=&review=`, see
    WaitingBadge). https, because WhatsApp does not linkify a custom scheme."""
    host = re.sub(r'^[a-z]+://', '', (workspace_host or '').strip()).strip('/')
    if not host:
        return ''
    query = urllib.parse.urlencode({'board': board_id, 'review': item_id})
    return f'https://{host}/board?{query}'


def _mac(key: bytes, body: str) -> str:
    return hmac.new(key, body.encode('utf-8'), hashlib.sha256).hexdigest()[:20]


def encode_payload(key: bytes, verb: str, nonce: str, content_hash: str) -> str:
    body = f'{PAYLOAD_PREFIX}.{verb}.{nonce}.{(content_hash or "")[:16]}'
    return f'{body}.{_mac(key, body)}'


def decode_payload(key: bytes, payload: str) -> Optional[Tuple[str, str, str]]:
    """`(verb, nonce, hash16)` for a payload this workspace signed, else None."""
    parts = (payload or '').strip().split('.')
    if len(parts) != 5 or parts[0] != PAYLOAD_PREFIX:
        return None
    _p, verb, nonce, h, mac = parts
    if verb not in VERBS or not _NONCE_RE.match(nonce) or not _HASH_RE.match(h) \
            or not _MAC_RE.match(mac):
        return None
    if not hmac.compare_digest(mac, _mac(key, '.'.join(parts[:4]))):
        return None
    return verb, nonce, h


def select_review_template(templates, name: str,
                           provider_id: str) -> Optional[Dict[str, Any]]:
    """The approved template for an out-of-window card, or None. Twilio's has
    to name a Content template (HX… SID): the built-in default is a logical
    name Twilio has never seen."""
    tpl = templates.select(name, provider_id=provider_id)
    if not tpl:
        return None
    if provider_id == 'twilio' and not str(tpl.get('provider_name') or '').startswith('HX'):
        return None
    return tpl


# ───────────────────────────────────────────────────────────────────────────
# Card store — one JSON per sent card, plus the signing key
# ───────────────────────────────────────────────────────────────────────────
class ReviewCardStore:
    """`<gateway>/review_cards/<nonce>.json`, 0600, atomic writes — the same
    discipline as IdentityRegistry. The signing key sits beside it in `.key`
    and is minted on first use."""

    def __init__(self, base_dir: str = GATEWAY_DIR):
        self.dir = os.path.join(base_dir, 'review_cards')
        self._key: Optional[bytes] = None
        self._lock = threading.Lock()

    def _ensure(self) -> None:
        os.makedirs(self.dir, mode=0o700, exist_ok=True)

    def key(self) -> bytes:
        with self._lock:
            if self._key is not None:
                return self._key
            self._ensure()
            path = os.path.join(self.dir, '.key')
            try:
                with open(path) as f:
                    value = f.read().strip()
            except OSError:
                value = ''
            if not re.match(r'^[0-9a-f]{64}$', value):
                value = secrets.token_hex(32)
                tmp = path + '.tmp'
                with open(tmp, 'w') as f:
                    f.write(value)
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            self._key = bytes.fromhex(value)
            return self._key

    def _path(self, nonce: str) -> str:
        return os.path.join(self.dir, f'{nonce}.json')

    def new_nonce(self) -> str:
        return secrets.token_hex(12)

    def save(self, card: Dict[str, Any]) -> None:
        self._ensure()
        IdentityRegistry._atomic_write(self._path(card['nonce']), card)

    def get(self, nonce: str) -> Optional[Dict[str, Any]]:
        if not _NONCE_RE.match(nonce or ''):
            return None
        try:
            with open(self._path(nonce)) as f:
                card = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        if time.time() - float(card.get('sent_at') or 0) > CARD_TTL:
            return None
        return card

    def _all(self) -> List[Dict[str, Any]]:
        out = []
        try:
            names = os.listdir(self.dir)
        except OSError:
            return out
        for name in names:
            if name.endswith('.json'):
                card = self.get(name[:-5])
                if card:
                    out.append(card)
        return out

    def find_sent(self, identity_hash: str, board_id: str, item_id: str,
                  content_hash: str, digest: str) -> Optional[Dict[str, Any]]:
        """A card already sent for exactly this proposal — the Feed coalesces
        repeats into one row, and the phone should not get them twice."""
        for card in self._all():
            if (card.get('identity_hash'), card.get('board_id'),
                    card.get('item_id'), card.get('content_hash'),
                    card.get('digest')) == (identity_hash, board_id, item_id,
                                            content_hash, digest):
                return card
        return None

    def latest_numbered(self, identity_hash: str) -> Optional[Dict[str, Any]]:
        cards = [c for c in self._all()
                 if c.get('identity_hash') == identity_hash and c.get('numbered')]
        return max(cards, key=lambda c: c.get('sent_at') or 0) if cards else None

    def prune(self) -> None:
        now = time.time()
        try:
            names = os.listdir(self.dir)
        except OSError:
            return
        for name in names:
            if not name.endswith('.json'):
                continue
            path = os.path.join(self.dir, name)
            try:
                with open(path) as f:
                    stale = now - float(json.load(f).get('sent_at') or 0) > CARD_TTL
            except (OSError, ValueError, json.JSONDecodeError):
                stale = True
            if stale:
                try:
                    os.remove(path)
                except OSError:
                    pass


# ───────────────────────────────────────────────────────────────────────────
# The notifier
# ───────────────────────────────────────────────────────────────────────────
Decider = Callable[..., Tuple[Optional[Dict[str, Any]], Any]]


class ReviewNotifier:
    """Sends review cards and answers their taps.

    `record_getter(board_id, item_id)` reads the staged record;
    `decider(decision, board_id, item_id, *, content_hash, proposal_digest,
    approval_id, actor)` is BoardReviewManager.approve / decide behind one
    signature. `spawn(fn)` runs delivery off the caller's thread (tests pass a
    synchronous one)."""

    def __init__(self, gateway, *, adapter_getter: Callable[[], Any],
                 record_getter: Callable[[str, str], Optional[Dict[str, Any]]],
                 decider: Decider,
                 store: Optional[ReviewCardStore] = None,
                 spawn: Optional[Callable[[Callable[[], None]], None]] = None):
        self.gateway = gateway
        self.adapter_getter = adapter_getter
        self.record_getter = record_getter
        self.decider = decider
        self.store = store or ReviewCardStore(gateway.registry.base)
        self.spawn = spawn or (lambda fn: threading.Thread(
            target=fn, daemon=True).start())

    # ── outbound ─────────────────────────────────────────────────────────────
    def on_feed_item(self, item: Dict[str, Any]) -> bool:
        """Called beside push_notify.dispatch for EVERY Feed emit. Cheap for
        everything that isn't a Board review; for one, delivery runs on
        another thread and can never fail the emit. True when queued."""
        ref = review_ref(item)
        if ref is None:
            return False
        self.spawn(lambda: self._deliver_logged(*ref))
        return True

    def _deliver_logged(self, board_id: str, item_id: str) -> None:
        try:
            self.deliver(board_id, item_id)
        except Exception as e:
            print(f'[gateway-review] delivery failed for {board_id}: {e}', flush=True)

    def deliver(self, board_id: str, item_id: str) -> int:
        """Send this item's card to every paired WhatsApp number whose link is
        still valid. Returns how many went out."""
        record = self.record_getter(board_id, item_id)
        if not record or record.get('state') not in board_review.OPEN_STATES:
            return 0
        pending = pending_actions(record)
        if not pending:
            return 0
        targets = self.gateway.registry.notify_targets('whatsapp')
        if not targets:
            return 0
        adapter = self.adapter_getter()
        if adapter is None:
            return 0
        self.store.prune()
        sent = 0
        for rec in targets:
            binding = IdentityRegistry.select_binding(rec)
            if not binding or not self.gateway.token_verifier(binding.get('token', '')):
                continue
            if self._send_card(adapter, rec, binding, board_id, record, pending):
                sent += 1
        return sent

    def _send_card(self, adapter, rec, binding, board_id, record, pending) -> bool:
        identity = rec['notify_to']
        item_id = str(record.get('item_id') or '')
        content_hash = str(record.get('content_hash') or '')
        digest = board_review.proposal_digest(record)
        if self.store.find_sent(rec['identity_hash'], board_id, item_id,
                                content_hash, digest):
            return False
        card = {
            'nonce': self.store.new_nonce(),
            'identity_hash': rec['identity_hash'],
            'board_id': board_id,
            'item_id': item_id,
            'item_key': item_label(record),
            'content_hash': content_hash,
            'digest': digest,
            'short': short_comment(pending) is not None,
            'numbered': False,
            'link': open_link(binding.get('workspace_host', ''), board_id, item_id),
            'sent_at': time.time(),
        }
        msg = self._build(adapter, identity, card, record, pending)
        if msg is None:
            print(f'[gateway-review] {board_id}/{card["item_key"]}: outside the '
                  f'24-h window and no approved review template — not sent '
                  f'(the push still went)', flush=True)
            return False
        result = self.gateway._send(adapter, identity, msg.text, msg=msg)
        if not result.ok:
            return False
        card['seq'] = msg.seq
        self.store.save(card)
        return True

    def _build(self, adapter, identity, card, record,
               pending) -> Optional[OutboundMessage]:
        provider_id = getattr(getattr(adapter, 'provider', None), 'name', '') \
            or getattr(adapter, 'name', '')
        key = self.store.key()
        action = short_comment(pending)
        label = item_label(record)
        title = (record.get('item_title') or '')[:80]
        link = card['link']
        buttons = [{'id': encode_payload(key, v, card['nonce'], card['content_hash']),
                    'title': BUTTON_TITLES[v]} for v in VERBS]

        name = 'board_review' if action else 'board_review_link'
        tpl = select_review_template(self.gateway.templates, name, provider_id)
        twilio_content = provider_id == 'twilio' and tpl is not None
        window = self.gateway._window_open(identity)

        if window and not twilio_content:
            if action:
                card['numbered'] = provider_id == 'twilio'
                text = (f'{label} needs your review' + (f' — {title}' if title else '')
                        + f'\n\nProposed {comment_kind(action)}:\n\n'
                        + action['params']['body'])
                return OutboundMessage(channel_identity=identity, text=text,
                                       buttons=buttons,
                                       quick_replies=[b['title'] for b in buttons])
            where = f'Open in app: {link}' if link else 'Open the kube-coder app to review it.'
            text = (f'{label} needs your review' + (f' — {title}' if title else '')
                    + f': {summary_line(record, pending)}. {where}')
            return OutboundMessage(channel_identity=identity, text=text)

        if tpl is None or not (twilio_content or adapter.capabilities.proactive):
            return None
        args: Dict[str, Any] = {'provider_name': tpl.get('provider_name') or name,
                                'language': tpl.get('language') or 'en'}
        if twilio_content:
            args['content_sid'] = tpl['provider_name']
        if action:
            args['body_params'] = [label, comment_kind(action),
                                   flatten(action['params']['body'])]
            text = (f'{label} needs your review.\n\nProposed '
                    f'{comment_kind(action)}:\n{args["body_params"][2]}')
            return OutboundMessage(channel_identity=identity, text=text,
                                   buttons=buttons, template=name,
                                   template_args=args)
        args['body_params'] = [label, flatten(summary_line(record, pending)),
                               link or 'the kube-coder app']
        text = (f'{label} needs your review: {args["body_params"][1]}. '
                f'Open it here: {args["body_params"][2]}')
        return OutboundMessage(channel_identity=identity, text=text,
                               template=name, template_args=args)

    # ── inbound ──────────────────────────────────────────────────────────────
    def handle_inbound(self, adapter, identity: str, rec: Dict[str, Any],
                       inbound) -> Optional[InboundResult]:
        """ConversationGateway.review_handler. Runs only for a LINKED sender
        whose token still verifies. None = not a review tap (route as chat)."""
        payload = (inbound.button_payload or '').strip()
        text = (inbound.button_reply or inbound.text or '').strip()
        if not payload.startswith(PAYLOAD_PREFIX + '.') \
                and text.startswith(PAYLOAD_PREFIX + '.'):
            payload = text
        if payload.startswith(PAYLOAD_PREFIX + '.'):
            parsed = decode_payload(self.store.key(), payload)
            card = self.store.get(parsed[1]) if parsed else None
            if parsed is None or card is None \
                    or card.get('identity_hash') != rec.get('identity_hash') \
                    or not str(card.get('content_hash') or '').startswith(parsed[2]):
                # Forged, tampered, expired or someone else's card: decide
                # nothing and say nothing.
                print('[gateway-review] ignored a review payload that did not '
                      'verify', flush=True)
                return InboundResult(status=200, action='rejected',
                                     detail='review payload did not verify')
            verb = parsed[0]
        elif text in NUMBERED:
            card = self.store.latest_numbered(rec.get('identity_hash') or '')
            if card is None or not self._numbered_live(identity, card):
                return None
            verb = NUMBERED[text]
        else:
            return None
        return self._act(adapter, identity, card, verb)

    def _numbered_live(self, identity: str, card: Dict[str, Any]) -> bool:
        """A bare digit means this card only while it is still the last thing
        sent here: any later message (the agent asking "1 or 2?") takes the
        digit back. After a restart the counter is 0 and the card stands."""
        if time.time() - float(card.get('sent_at') or 0) > NUMBERED_TTL:
            return False
        return self.gateway.sequencer.current(identity) in (0, card.get('seq'))

    def _act(self, adapter, identity, card, verb) -> InboundResult:
        link = card.get('link') or ''
        open_text = f'Open in app: {link}' if link else 'Open the kube-coder app to review it.'
        if verb == 'o':
            self.gateway._send(adapter, identity, open_text)
            return InboundResult(status=200, action='review_open')
        if not card.get('short'):
            # Only short cards are ever sent with these buttons.
            return InboundResult(status=200, action='rejected',
                                 detail='this card has no decision buttons')
        decision = 'approve' if verb == 'a' else 'reject'
        result, err = self.decider(
            decision, card['board_id'], card['item_id'],
            content_hash=card['content_hash'], proposal_digest=card['digest'],
            approval_id=f'wa-{card["nonce"]}-{verb}',
            actor=f'whatsapp:{card["identity_hash"][:12]}')
        reply = self._reply(decision, card, result, err, open_text)
        self.gateway._send(adapter, identity, reply)
        return InboundResult(status=200, action=f'review_{decision}',
                             detail=getattr(err, 'code', '') or
                             ('replayed' if (result or {}).get('replayed') else 'ok'))

    @staticmethod
    def _reply(decision, card, result, err, open_text) -> str:
        label = card.get('item_key') or 'This item'
        if err is not None:
            code = getattr(err, 'code', '')
            if code in ('stale', 'hash_mismatch'):
                return f'{CHANGED_TEXT} {open_text}'.strip()
            if code == 'already_decided':
                return f'{label}: {getattr(err, "detail", "already decided")}.'
            return (f"Couldn't {decision} {label}: {getattr(err, 'detail', err)}. "
                    f'{open_text}')
        result = result or {}
        if result.get('replayed'):
            return f'{label} was already {"approved" if decision == "approve" else "rejected"}.'
        if decision == 'reject':
            return f'❌ Rejected {label}. Nothing was posted.'
        if (result.get('result') or {}).get('ok'):
            return f'✅ Approved — posted to {label}.'
        return f'⚠️ Approved {label}, but a write failed. {open_text}'
