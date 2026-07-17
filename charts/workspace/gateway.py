#!/usr/bin/env python3
"""Conversation Gateway — talk to the workspace Hypervisor from outside the app.

This is the **channel-agnostic core** for issue #306. It lets a user drive their
workspace's Hypervisor agent over an ordinary messaging channel (WhatsApp first;
voice/SMS later) without the agent plumbing knowing anything about the channel.

Architecture (see the issue's §2):

    Channel Adapter  ──inbound()──▶  ConversationGateway (this module)  ──▶  Hypervisor
    (WhatsApp/echo)  ◀─outbound()──                                          (facade ops)

The CORE (this file) owns everything channel-independent:
  * identity → workspace/token mapping + allowlist enforcement (IdentityRegistry)
  * pairing-code enrollment (no secret ever typed over the channel)
  * thread routing + keyword commands (new chat / start over / stop / unlink /
    workspaces / @workspace)
  * dispatch through the Hypervisor's existing operations (HypervisorClient port —
    a CONSUMER of the facade, it never changes the event schema)
  * the messageable projection (imported from hypervisor_session — shared with the
    CarPlay work #301) turning the raw event stream into sendable prose
  * a channel-pluggable long-turn policy engine (ack → stream → final →
    background-and-notify → out-of-window template), driven purely by
    (elapsed, capabilities.proactive, window_open)

The ADAPTER (adapters/whatsapp.py, or the EchoAdapter here) owns only the
provider specifics: signature verification, inbound parse, rendering choices to
buttons/lists, media, chunking, and the outbound send API.

The EchoAdapter at the bottom is the core's test harness: a fake inbound
round-trips through a REAL HypervisorSession and comes back rendered, with zero
WhatsApp code involved (issue Phase-0 exit criterion).

Storage mirrors WebhookManager's file-store pattern, on the PVC:
    /home/dev/.claude-triggers/gateway/identities/<sha256(channel_identity)>.json
    /home/dev/.claude-triggers/gateway/pending/<code>.json
    /home/dev/.claude-triggers/gateway/templates/<name>.json
The channel identity (phone number) is HASHED at rest; the stored token is
redacted from every list view.
"""

from __future__ import annotations

import collections
import hashlib
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import (Any, Callable, Dict, List, Optional, Protocol,
                    runtime_checkable)

# The projection + turn hook live in hypervisor_session (build once, beside
# build_activity). Import defensively so a partial install degrades rather than
# crashing server.py's import of this module.
try:
    from hypervisor_session import (build_messageable, summarize_tool_activity,
                                    register_turn_observer)
    _HV_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only on a broken install
    build_messageable = None  # type: ignore
    summarize_tool_activity = None  # type: ignore
    register_turn_observer = None  # type: ignore
    _HV_AVAILABLE = False


GATEWAY_DIR = '/home/dev/.claude-triggers/gateway'


def _now() -> float:
    return time.time()


# ───────────────────────────────────────────────────────────────────────────
# §2.1 The adapter contract (deliberately tiny)
# ───────────────────────────────────────────────────────────────────────────
@dataclass
class Capabilities:
    """What a channel can do. The policy engine and renderers branch on these,
    never on the channel name — that's what keeps the core channel-agnostic."""
    buttons: bool = False           # interactive reply buttons / list
    max_buttons: int = 3            # WhatsApp: 3 reply buttons
    max_list_rows: int = 10         # WhatsApp: 10 list rows
    media: bool = False             # send/receive images, docs, audio
    typing_indicator: bool = False  # WhatsApp: read receipt / typing
    proactive: bool = False         # can send outside a live inbound? (WhatsApp:
                                    # only via an approved out-of-window template)
    max_text_len: int = 4096        # WhatsApp text body cap


@dataclass
class MediaItem:
    kind: str = 'image'             # image | document | audio | video
    url: str = ''                   # hosted link (adapter may upload instead)
    mime: str = ''
    caption: str = ''


@dataclass
class RawRequest:
    """Provider payload as it arrived at the webhook, pre-parse. The adapter's
    signature verifier needs the raw bytes + headers + the exact URL."""
    method: str = 'POST'
    url: str = ''                   # full external URL (Twilio signs over it)
    headers: Dict[str, str] = field(default_factory=dict)
    raw_body: bytes = b''
    form: Dict[str, str] = field(default_factory=dict)   # parsed form params (Twilio)
    query: Dict[str, str] = field(default_factory=dict)


@dataclass
class InboundMessage:
    """A normalized inbound. `channel_identity` is OPAQUE to the core — it only
    ever looks it up in the registry, never parses it."""
    channel: str
    channel_identity: str           # "whatsapp:+E164", a voice socket id, …
    text: str = ''
    media: List[MediaItem] = field(default_factory=list)
    provider_msg_id: str = ''       # Twilio MessageSid / Meta wamid — for dedupe
    button_reply: str = ''          # the option text when the user tapped a choice


@dataclass
class OutboundMessage:
    channel_identity: str
    text: str = ''
    media: List[MediaItem] = field(default_factory=list)
    quick_replies: List[str] = field(default_factory=list)  # → buttons/list
    template: Optional[str] = None  # logical template name for out-of-window
    template_args: Dict[str, Any] = field(default_factory=dict)
    seq: int = 0                    # per-conversation monotonic outbound sequence


@dataclass
class DeliveryResult:
    ok: bool = True
    provider_msg_id: str = ''
    error: str = ''
    status: int = 0


@runtime_checkable
class ChannelAdapter(Protocol):
    name: str
    capabilities: Capabilities

    def verify(self, raw: RawRequest) -> bool: ...
    def handshake(self, raw: RawRequest) -> Optional[str]: ...   # provider GET verify
    def inbound(self, raw: RawRequest) -> Optional[InboundMessage]: ...
    def outbound(self, msg: OutboundMessage) -> DeliveryResult: ...


# ───────────────────────────────────────────────────────────────────────────
# §4 Identity & auth mapping — the allowlist IS the registry
# ───────────────────────────────────────────────────────────────────────────
def hash_identity(channel_identity: str) -> str:
    """sha256 hex of a channel identity. The raw phone number never lands in a
    filename or a list view — a leaked PVC directory reveals no directory of
    real numbers."""
    return hashlib.sha256((channel_identity or '').encode('utf-8')).hexdigest()


class IdentityRegistry:
    """File-store of channel-identity → workspace/token/thread bindings, plus
    short-lived pairing codes. Mirrors WebhookManager: 0o700 dir, atomic writes,
    secrets redacted from public views. In-memory pieces (rate window, last
    inbound) are fine for a single-pod workspace — they'd move to Redis only if
    the IDE pod ever horizontal-scales, same caveat as _ReplayCache."""

    _CODE_RE = re.compile(r'^\d{6}$')

    def __init__(self, base_dir: str = GATEWAY_DIR):
        self.base = base_dir
        self.identities_dir = os.path.join(base_dir, 'identities')
        self.pending_dir = os.path.join(base_dir, 'pending')

    # ── dirs ────────────────────────────────────────────────────────────────
    def ensure_dirs(self) -> None:
        os.makedirs(self.identities_dir, mode=0o700, exist_ok=True)
        os.makedirs(self.pending_dir, mode=0o700, exist_ok=True)

    def _identity_path(self, identity_hash: str) -> str:
        return os.path.join(self.identities_dir, f'{identity_hash}.json')

    @staticmethod
    def _atomic_write(path: str, obj: Dict[str, Any]) -> None:
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(obj, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    # ── bindings ──────────────────────────────────────────────────────────────
    def lookup(self, channel_identity: str) -> Optional[Dict[str, Any]]:
        """The full record for a channel identity, or None (i.e. not linked)."""
        path = self._identity_path(hash_identity(channel_identity))
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def is_linked(self, channel_identity: str) -> bool:
        rec = self.lookup(channel_identity)
        return bool(rec and rec.get('bindings'))

    def bind(self, channel_identity: str, channel: str, *, workspace: str,
             workspace_host: str, token: str,
             default_thread_id: Optional[str] = None,
             make_default: bool = True) -> Dict[str, Any]:
        """Bind (or update) `channel_identity` to a workspace. Idempotent per
        workspace name — re-binding the same workspace updates it rather than
        duplicating. Multiple workspaces may coexist with exactly one default."""
        self.ensure_dirs()
        h = hash_identity(channel_identity)
        rec = self.lookup(channel_identity) or {
            'id': h, 'channel': channel, 'identity_hash': h,
            'bindings': [], 'created_at': _now(),
        }
        binding = {
            'workspace': workspace,
            'workspace_host': workspace_host,
            'token': token,
            'default_thread_id': default_thread_id,
            'is_default': make_default,
            'bound_at': _now(),
        }
        bindings = [b for b in rec.get('bindings', [])
                    if b.get('workspace') != workspace]
        if make_default:
            for b in bindings:
                b['is_default'] = False
        if not bindings and not make_default:
            binding['is_default'] = True  # first binding is always default
        bindings.append(binding)
        rec['bindings'] = bindings
        rec['updated_at'] = _now()
        self._atomic_write(self._identity_path(h), rec)
        return rec

    def set_default_thread(self, channel_identity: str, workspace: str,
                           thread_id: Optional[str]) -> None:
        """Persist the active thread for a binding so the next inbound continues
        the same conversation (`new chat` clears/replaces it)."""
        rec = self.lookup(channel_identity)
        if not rec:
            return
        for b in rec.get('bindings', []):
            if b.get('workspace') == workspace:
                b['default_thread_id'] = thread_id
        rec['updated_at'] = _now()
        self._atomic_write(self._identity_path(rec['identity_hash']), rec)

    @staticmethod
    def select_binding(rec: Dict[str, Any],
                       workspace: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Pick a binding: the named workspace, else the default, else the first.
        Returns None only for a record with no bindings."""
        bindings = rec.get('bindings') or []
        if not bindings:
            return None
        if workspace:
            for b in bindings:
                if b.get('workspace', '').lower() == workspace.lower():
                    return b
            return None  # explicitly-named workspace that isn't bound
        for b in bindings:
            if b.get('is_default'):
                return b
        return bindings[0]

    def revoke(self, identity_hash: str) -> bool:
        """Delete a whole link by its id (== identity hash). Used by
        DELETE /api/gateway/link/<id> and the `unlink` keyword."""
        try:
            os.remove(self._identity_path(identity_hash))
            return True
        except FileNotFoundError:
            return False

    def revoke_identity(self, channel_identity: str) -> bool:
        return self.revoke(hash_identity(channel_identity))

    def list_links(self) -> List[Dict[str, Any]]:
        """Redacted view for the dashboard: no raw number, no token — only the
        hash id, channel, and per-workspace metadata."""
        self.ensure_dirs()
        out: List[Dict[str, Any]] = []
        try:
            entries = sorted(os.listdir(self.identities_dir))
        except OSError:
            return out
        for name in entries:
            if not name.endswith('.json'):
                continue
            try:
                with open(os.path.join(self.identities_dir, name)) as f:
                    rec = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            out.append(self.public_view(rec))
        return out

    @staticmethod
    def public_view(rec: Dict[str, Any]) -> Dict[str, Any]:
        return {
            'id': rec.get('id'),
            'channel': rec.get('channel'),
            'created_at': rec.get('created_at'),
            'updated_at': rec.get('updated_at'),
            'bindings': [
                {
                    'workspace': b.get('workspace'),
                    'workspace_host': b.get('workspace_host'),
                    'is_default': bool(b.get('is_default')),
                    'has_thread': bool(b.get('default_thread_id')),
                    'token_set': bool(b.get('token')),  # redacted — never the value
                    'bound_at': b.get('bound_at'),
                }
                for b in rec.get('bindings', [])
            ],
        }

    # ── pairing codes (§4.1) ──────────────────────────────────────────────────
    def _pending_path(self, code: str) -> str:
        return os.path.join(self.pending_dir, f'{code}.json')

    def mint_pairing_code(self, *, workspace: str, workspace_host: str,
                          token: str, ttl_seconds: int = 600) -> str:
        """Mint a single-use 6-digit code bound to (workspace, token). The code —
        not the phone number — is the secret; nothing sensitive travels over the
        channel except this throwaway."""
        self.ensure_dirs()
        # Retry on the astronomically-unlikely collision so we never overwrite a
        # live pending code.
        for _ in range(10):
            code = f'{secrets.randbelow(1_000_000):06d}'
            path = self._pending_path(code)
            if os.path.exists(path):
                continue
            self._atomic_write(path, {
                'code': code,
                'workspace': workspace,
                'workspace_host': workspace_host,
                'token': token,
                'created_at': _now(),
                'expires_at': _now() + ttl_seconds,
            })
            return code
        raise RuntimeError('could not mint a unique pairing code')

    def _purge_expired_pending(self) -> None:
        try:
            names = os.listdir(self.pending_dir)
        except OSError:
            return
        now = _now()
        for name in names:
            if not name.endswith('.json'):
                continue
            p = os.path.join(self.pending_dir, name)
            try:
                with open(p) as f:
                    if json.load(f).get('expires_at', 0) < now:
                        os.remove(p)
            except (OSError, json.JSONDecodeError):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def try_bind_with_code(self, channel_identity: str, channel: str,
                           text: str) -> Optional[Dict[str, Any]]:
        """If `text` is a valid, unexpired, single-use pairing code, consume it
        and bind the identity; return the new record. Otherwise None."""
        self.ensure_dirs()
        self._purge_expired_pending()
        code = (text or '').strip()
        if not self._CODE_RE.match(code):
            return None
        path = self._pending_path(code)
        try:
            with open(path) as f:
                pending = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        if pending.get('expires_at', 0) < _now():
            try:
                os.remove(path)
            except OSError:
                pass
            return None
        # Single-use: consume before binding so a racing duplicate can't reuse it.
        try:
            os.remove(path)
        except OSError:
            return None
        return self.bind(
            channel_identity, channel,
            workspace=pending.get('workspace') or 'workspace',
            workspace_host=pending.get('workspace_host') or '',
            token=pending.get('token') or '',
            make_default=True,
        )


# ───────────────────────────────────────────────────────────────────────────
# §6 (B6) Template registry — pre-approved out-of-window notifications
# ───────────────────────────────────────────────────────────────────────────
class TemplateRegistry:
    """Maps a LOGICAL template name (e.g. "task_complete") to a provider
    template + language + approval status. Scaffolded now (Phase 2 fully wires
    approval) so the out-of-window path is real: the core SELECTS a template,
    the adapter EXECUTES it. Ships a default `task_complete` so tests and the
    policy engine have something to resolve."""

    DEFAULTS = {
        'task_complete': {
            'name': 'task_complete',
            'provider_name': 'kc_task_complete',
            'language': 'en',
            'status': 'approved',        # sandbox default; real approval is external
            'body': "✅ Your task '{title}' finished — reply to see the result.",
        },
        'not_linked': {
            'name': 'not_linked',
            'provider_name': 'kc_not_linked',
            'language': 'en',
            'status': 'approved',
            'body': "This number isn't linked to a workspace.",
        },
    }

    def __init__(self, base_dir: str = GATEWAY_DIR):
        self.dir = os.path.join(base_dir, 'templates')

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        path = os.path.join(self.dir, f'{name}.json')
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return dict(self.DEFAULTS.get(name)) if name in self.DEFAULTS else None

    def select(self, name: str) -> Optional[Dict[str, Any]]:
        """Return the template ONLY if it's approved — otherwise the caller must
        not attempt an out-of-window send (WhatsApp would reject it)."""
        tpl = self.get(name)
        if tpl and tpl.get('status') == 'approved':
            return tpl
        return None

    def render_body(self, name: str, **args: Any) -> str:
        tpl = self.get(name) or {}
        body = tpl.get('body', '')
        try:
            return body.format(**args)
        except (KeyError, IndexError):
            return body


# ───────────────────────────────────────────────────────────────────────────
# §5 Long-turn policy engine — channel-pluggable, pure decision function
# ───────────────────────────────────────────────────────────────────────────
# Actions the engine can choose. The gateway maps these onto adapter calls.
ACK = 'ack'
STREAM = 'stream'
BACKGROUND_NOTIFY = 'background_notify'
FINAL = 'final'
TEMPLATE = 'template'
DROP = 'drop'
WAIT = 'wait'


class LongTurnPolicy:
    """Decides what to do at each moment of a turn from ONLY
    (elapsed, capabilities, window_open, done). Because it never looks at the
    channel, a new channel (voice/SMS) supplies its Capabilities and inherits
    the behavior — that's the pluggability the issue's §5.2 calls for.

    * done=False, in progress:
        elapsed ≥ background_after → BACKGROUND_NOTIFY ("big one, I'll ping you")
        elapsed ≥ stream_after     → STREAM  (debounced progress)
        else                       → WAIT
    * done=True, finished:
        window_open                → FINAL   (free-form)
        else, caps.proactive       → TEMPLATE (out-of-window nudge)
        else                       → DROP    (can't reach them; runner log only)
    """

    def __init__(self, stream_after: float = 8.0, background_after: float = 60.0):
        self.stream_after = stream_after
        self.background_after = background_after

    def decide(self, *, elapsed: float, caps: Capabilities,
               window_open: bool, done: bool) -> str:
        if done:
            if window_open:
                return FINAL
            return TEMPLATE if caps.proactive else DROP
        if elapsed >= self.background_after:
            return BACKGROUND_NOTIFY
        if elapsed >= self.stream_after:
            return STREAM
        return WAIT


# ───────────────────────────────────────────────────────────────────────────
# Hypervisor client port — a CONSUMER of the existing facade operations
# ───────────────────────────────────────────────────────────────────────────
class HypervisorClient(Protocol):
    def create_thread(self) -> Optional[str]: ...
    def send(self, thread_id: str, text: str) -> bool: ...
    def status(self, thread_id: str) -> str: ...
    def last_seq(self, thread_id: str) -> int: ...
    def get_events(self, thread_id: str, since: int = 0) -> List[Dict[str, Any]]: ...
    def stop(self, thread_id: str) -> bool: ...
    def exists(self, thread_id: str) -> bool: ...


class LocalHypervisorClient:
    """In-process HypervisorClient over HypervisorSession — the same operations
    the /api/hypervisor/* facade performs (create + first message, follow-up,
    poll ?since, stop), just called directly since the gateway runs in the SAME
    process that owns the facade (issue D5). We deliberately create the thread
    WITHOUT a first message, then send() separately, so the caller can register
    for turn-completion before the (possibly very fast) turn can finish."""

    def __init__(self, session_cls, *, assistant: str = 'claude',
                 workdir: str = '/home/dev', cli_cmd: str = 'claude',
                 preamble: str = ''):
        self._S = session_cls
        self.assistant = assistant
        self.workdir = workdir
        self.cli_cmd = cli_cmd
        self.preamble = preamble

    def create_thread(self) -> Optional[str]:
        s = self._S.create(assistant=self.assistant, workdir=self.workdir,
                            cli_cmd=self.cli_cmd, preamble=self.preamble)
        return s.id

    def _get(self, thread_id: str):
        return self._S.get(thread_id)

    def send(self, thread_id: str, text: str) -> bool:
        s = self._get(thread_id)
        if s is None:
            return False
        s.send(text)
        return True

    def status(self, thread_id: str) -> str:
        s = self._get(thread_id)
        return s.status() if s else 'missing'

    def last_seq(self, thread_id: str) -> int:
        s = self._get(thread_id)
        if s is None:
            return 0
        events = s.read_events()
        return events[-1].get('seq', 0) if events else 0

    def get_events(self, thread_id: str, since: int = 0) -> List[Dict[str, Any]]:
        s = self._get(thread_id)
        return s.read_events(since) if s else []

    def stop(self, thread_id: str) -> bool:
        s = self._get(thread_id)
        return s.stop() if s else False

    def exists(self, thread_id: str) -> bool:
        return self._get(thread_id) is not None


# ───────────────────────────────────────────────────────────────────────────
# Per-sender rate limiting + outbound sequencing + replay dedupe
# ───────────────────────────────────────────────────────────────────────────
class RateLimiter:
    """Sliding-window per-key limiter (N events / window seconds). In-memory."""

    def __init__(self, max_events: int = 20, window_seconds: float = 60.0):
        self.max = max_events
        self.window = window_seconds
        self._events: Dict[str, collections.deque] = collections.defaultdict(
            collections.deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = _now()
        with self._lock:
            dq = self._events[key]
            while dq and now - dq[0] > self.window:
                dq.popleft()
            if len(dq) >= self.max:
                return False
            dq.append(now)
            return True


class OutboundSequencer:
    """Monotonic per-conversation outbound sequence so a retried send or a
    re-polled `message` event isn't delivered twice. dedupe() rejects a
    (conversation, logical-key) pair seen before."""

    def __init__(self):
        self._seq: Dict[str, int] = collections.defaultdict(int)
        self._sent: Dict[str, set] = collections.defaultdict(set)
        self._lock = threading.Lock()

    def next(self, conversation: str) -> int:
        with self._lock:
            self._seq[conversation] += 1
            return self._seq[conversation]

    def dedupe(self, conversation: str, key: str) -> bool:
        """True if this (conversation, key) is fresh (record it); False if a dup."""
        with self._lock:
            seen = self._sent[conversation]
            if key in seen:
                return False
            seen.add(key)
            return True


# ───────────────────────────────────────────────────────────────────────────
# Keyword / command parsing (channel-agnostic)
# ───────────────────────────────────────────────────────────────────────────
@dataclass
class ParsedCommand:
    command: Optional[str] = None      # new_chat | stop | unlink | workspaces | None
    workspace: Optional[str] = None    # from a leading @ws / "on ws:" prefix
    remainder: str = ''                # message text after stripping the above


_WS_PREFIX_RE = re.compile(r'^@(\S+)\s*(.*)$', re.DOTALL)
_ON_PREFIX_RE = re.compile(r'^on\s+(\S+)\s*:\s*(.*)$', re.DOTALL | re.IGNORECASE)


def parse_command(text: str) -> ParsedCommand:
    """Parse gateway keywords + a leading workspace selector out of an inbound.

    Keywords (whole-message, case-insensitive): `new chat`/`start over`,
    `stop`/`cancel`, `unlink`, `workspaces`. A leading `@<ws> …` or
    `on <ws>: …` selects a workspace and the rest is the message."""
    raw = (text or '').strip()
    low = raw.lower()
    if low in ('new chat', 'new', 'start over', 'reset'):
        return ParsedCommand(command='new_chat')
    if low in ('stop', 'cancel', 'abort'):
        return ParsedCommand(command='stop')
    if low in ('unlink', 'disconnect', 'forget me'):
        return ParsedCommand(command='unlink')
    if low in ('workspaces', 'list workspaces'):
        return ParsedCommand(command='workspaces')
    m = _WS_PREFIX_RE.match(raw) or _ON_PREFIX_RE.match(raw)
    if m:
        return ParsedCommand(workspace=m.group(1), remainder=m.group(2).strip())
    return ParsedCommand(remainder=raw)


# ───────────────────────────────────────────────────────────────────────────
# The gateway
# ───────────────────────────────────────────────────────────────────────────
@dataclass
class InboundResult:
    """What handle_inbound produced — for the HTTP layer to turn into a status
    code, and for tests to assert on without scraping the adapter."""
    status: int = 200
    action: str = ''                 # linked | not_linked | dispatched | queued |
                                    # stopped | new_chat | workspaces | unlink |
                                    # duplicate | rejected | error | no_op
    detail: str = ''
    thread_id: Optional[str] = None


class ConversationGateway:
    """The channel-agnostic core. One instance per workspace pod.

    * handle_inbound(adapter, raw)  — webhook entrypoint (returns InboundResult).
    * on_turn_complete(thread_id)   — the turn-complete observer; delivers the
      final projected message (registered via register_turn_observer()).

    `client_factory(binding) -> HypervisorClient` builds the client for a
    workspace binding (lets a shared-number router, Phase 2, target a different
    pod without touching this class). `token_verifier(token) -> bool` re-checks
    the stored token so a rotated/revoked token orphans the binding (§4.3).
    """

    ACK_TEXT = 'On it — working on that…'
    BIG_TURN_TEXT = "This is a big one — I'll message you when it's done."
    NOT_LINKED_TEXT = ("This number isn't linked to a workspace. Open the "
                       'dashboard, tap "Link WhatsApp", and send me the code.')
    LINKED_TEXT = '✅ Linked! Send me a message and I\'ll drive your workspace agent.'
    UNLINKED_TEXT = '🔌 Unlinked. This number can no longer reach your workspace.'
    EXPIRED_TEXT = 'Your link expired — re-link from the app.'
    RATE_LIMITED_TEXT = 'Slow down a moment — too many messages. Try again shortly.'

    def __init__(self, *, registry: Optional[IdentityRegistry] = None,
                 client_factory: Optional[Callable[[Dict[str, Any]], HypervisorClient]] = None,
                 token_verifier: Optional[Callable[[str], bool]] = None,
                 templates: Optional[TemplateRegistry] = None,
                 policy: Optional[LongTurnPolicy] = None,
                 rate_limiter: Optional[RateLimiter] = None,
                 window_seconds: float = 24 * 3600,
                 window_probe: Optional[Callable[[str], Optional[bool]]] = None,
                 send_ack: bool = True):
        self.registry = registry or IdentityRegistry()
        self.client_factory = client_factory
        self.token_verifier = token_verifier or (lambda _t: True)
        self.templates = templates or TemplateRegistry()
        self.policy = policy or LongTurnPolicy()
        self.rate_limiter = rate_limiter or RateLimiter()
        self.window_seconds = window_seconds
        # Optional per-identity override of the 24-h window decision, returning
        # True/False to force, or None to defer to the real last-inbound clock.
        # The Walkie-Talkie preview uses it to demo the out-of-window template
        # path locally without a real 24-h gap (issue #306 follow-up).
        self.window_probe = window_probe
        self.send_ack = send_ack
        self.sequencer = OutboundSequencer()
        # Idempotency for inbound (provider msg id), mirroring _ReplayCache.
        self._replay_lock = threading.Lock()
        self._replay: "collections.OrderedDict[str, float]" = collections.OrderedDict()
        self._replay_ttl = 300.0
        self._replay_cap = 2048
        # Per-thread conversation state so on_turn_complete knows who to answer.
        self._pending: Dict[str, Dict[str, Any]] = {}
        # Mid-turn message queue keyed by thread_id (D4: queue by default).
        self._queue: Dict[str, List[str]] = collections.defaultdict(list)
        self._state_lock = threading.Lock()
        # last inbound ts per channel_identity → the 24-h window.
        self._last_inbound: Dict[str, float] = {}

    # ── registration ─────────────────────────────────────────────────────────
    def install_turn_observer(self) -> None:
        """Wire on_turn_complete into hypervisor_session's single turn-complete
        hook. Call once at server startup."""
        if register_turn_observer is not None:
            register_turn_observer(self.on_turn_complete)

    # ── idempotency ──────────────────────────────────────────────────────────
    def _seen_before(self, key: str) -> bool:
        now = _now()
        with self._replay_lock:
            while self._replay:
                k, ts = next(iter(self._replay.items()))
                if now - ts > self._replay_ttl:
                    self._replay.popitem(last=False)
                else:
                    break
            if key in self._replay:
                self._replay.move_to_end(key)
                self._replay[key] = now
                return True
            self._replay[key] = now
            while len(self._replay) > self._replay_cap:
                self._replay.popitem(last=False)
            return False

    # ── window ───────────────────────────────────────────────────────────────
    def _window_open(self, channel_identity: str) -> bool:
        if self.window_probe is not None:
            forced = self.window_probe(channel_identity)
            if forced is not None:
                return bool(forced)
        last = self._last_inbound.get(channel_identity)
        return last is not None and (_now() - last) < self.window_seconds

    # ── inbound ──────────────────────────────────────────────────────────────
    def handle_inbound(self, adapter: ChannelAdapter,
                       raw: RawRequest) -> InboundResult:
        """Verify → parse → dedupe → identity → route → dispatch. Returns an
        InboundResult; the HTTP layer maps .status to a response (always fast)."""
        if not adapter.verify(raw):
            return InboundResult(status=403, action='rejected', detail='bad signature')
        try:
            inbound = adapter.inbound(raw)
        except Exception as e:
            return InboundResult(status=400, action='error', detail=str(e))
        if inbound is None:
            # A status callback / non-message webhook — nothing to do, ack it.
            return InboundResult(status=200, action='no_op')

        # Idempotency on the provider message id (Twilio MessageSid / Meta wamid).
        if inbound.provider_msg_id:
            key = f'{inbound.channel}:{inbound.provider_msg_id}'
            if self._seen_before(key):
                return InboundResult(status=200, action='duplicate')

        text = inbound.button_reply or inbound.text or ''
        identity = inbound.channel_identity

        # ── Per-sender rate limit (§4.3) — BEFORE the identity lookup so it also
        # throttles UNKNOWN senders: this caps pairing-code brute-force guesses
        # and stops an attacker eliciting an unbounded stream of "not linked"
        # replies (message-cost amplification). Silent 429, no reply. The key is
        # only ever a signature-verified sender, so the limiter map can't be
        # flooded with spoofed identities. Belt-and-suspenders with the edge
        # limit-rps/limit-connections on the public ingress.
        if not self.rate_limiter.allow(identity):
            return InboundResult(status=429, action='rejected', detail='rate limited')

        # ── Unknown sender → pairing candidate, else the one "not linked" reply.
        rec = self.registry.lookup(identity)
        if not rec:
            bound = self.registry.try_bind_with_code(identity, inbound.channel, text)
            if bound:
                self._send(adapter, identity, self.LINKED_TEXT)
                return InboundResult(status=200, action='linked')
            self._send(adapter, identity, self.NOT_LINKED_TEXT)
            return InboundResult(status=200, action='not_linked')

        self._last_inbound[identity] = _now()

        cmd = parse_command(text)
        binding = IdentityRegistry.select_binding(rec, cmd.workspace)
        if binding is None:
            names = ', '.join(b.get('workspace', '?') for b in rec.get('bindings', []))
            self._send(adapter, identity,
                       f'No workspace "{cmd.workspace}". You have: {names or "none"}.')
            return InboundResult(status=200, action='workspaces')

        # ── Token still valid? (rotation/revocation orphans the binding.)
        if not self.token_verifier(binding.get('token', '')):
            self._send(adapter, identity, self.EXPIRED_TEXT)
            return InboundResult(status=200, action='rejected', detail='token invalid')

        # ── Pure keyword commands (no message body needed).
        if cmd.command == 'unlink':
            self.registry.revoke(rec['identity_hash'])
            self._send(adapter, identity, self.UNLINKED_TEXT)
            return InboundResult(status=200, action='unlink')
        if cmd.command == 'workspaces':
            self._send(adapter, identity, self._workspaces_text(rec))
            return InboundResult(status=200, action='workspaces')

        client = self._client_for(binding)
        if client is None:
            self._send(adapter, identity, 'Workspace is offline. Try again shortly.')
            return InboundResult(status=503, action='error', detail='no client')

        thread_id = binding.get('default_thread_id')
        running = bool(thread_id and client.exists(thread_id)
                       and client.status(thread_id) == 'running')

        # ── stop / cancel → barge-in (§6).
        if cmd.command == 'stop':
            if running:
                client.stop(thread_id)
                return InboundResult(status=200, action='stopped', thread_id=thread_id)
            self._send(adapter, identity, 'Nothing is running right now.')
            return InboundResult(status=200, action='no_op')

        message = cmd.remainder
        if cmd.command == 'new_chat':
            thread_id = None  # force a fresh thread; the body (if any) is the msg

        # ── Mid-turn message → QUEUE (D4). The send facade 409s a concurrent
        # send; we buffer and dispatch on the idle transition. "In flight" is
        # keyed on the pending map (not a check-then-act on session status),
        # because on_turn_complete drains the queue under the SAME lock right
        # after clearing pending — so an enqueue can't be orphaned by a turn
        # that finishes concurrently.
        if message and thread_id:
            with self._state_lock:
                in_flight = thread_id in self._pending
                if in_flight:
                    self._queue[thread_id].append(message)
            if in_flight:
                self._send(adapter, identity,
                           "Got it — I'll get to that when the current step finishes.")
                return InboundResult(status=200, action='queued', thread_id=thread_id)

        if not message:
            # e.g. a bare "@ws" with no body, or a `new chat` with nothing after.
            self._send(adapter, identity, 'What would you like me to do?')
            return InboundResult(status=200, action='no_op', thread_id=thread_id)

        return self._dispatch(adapter, identity, binding, client, thread_id, message)

    def _dispatch(self, adapter, identity, binding, client,
                  thread_id, message) -> InboundResult:
        """Create/continue a thread and fire the turn. Registers the pending
        conversation BEFORE send() so a fast turn can't complete before we're
        listening (the observer would otherwise fire with nothing to deliver)."""
        new_thread = thread_id is None or not client.exists(thread_id)
        if new_thread:
            thread_id = client.create_thread()
            if thread_id is None:
                self._send(adapter, identity, 'Could not start a chat. Try again.')
                return InboundResult(status=500, action='error')
            self.registry.set_default_thread(identity, binding['workspace'], thread_id)
        since = client.last_seq(thread_id)
        with self._state_lock:
            self._pending[thread_id] = {
                'adapter': adapter,
                'identity': identity,
                'workspace': binding['workspace'],
                'since': since,
                'dispatched_at': _now(),
                'client': client,
            }
        if not client.send(thread_id, message):
            with self._state_lock:
                self._pending.pop(thread_id, None)
            self._send(adapter, identity, 'Could not deliver your message.')
            return InboundResult(status=500, action='error', thread_id=thread_id)
        if self.send_ack:
            self._send(adapter, identity, self.ACK_TEXT)
        return InboundResult(status=200, action='dispatched', thread_id=thread_id)

    # ── turn completion (the single hook) ─────────────────────────────────────
    def on_turn_complete(self, thread_id: str) -> None:
        """Registered on hypervisor_session's turn-complete hook. Projects the
        finished turn and delivers it per the long-turn policy, then dispatches
        any queued mid-turn message."""
        with self._state_lock:
            conv = self._pending.get(thread_id)
        if not conv:
            return  # not a thread we're driving
        adapter: ChannelAdapter = conv['adapter']
        identity: str = conv['identity']
        client: HypervisorClient = conv['client']
        since: int = conv['since']
        elapsed = _now() - conv.get('dispatched_at', _now())

        events = client.get_events(thread_id, since)
        projection = (build_messageable(events) if build_messageable
                      else {'text': '', 'choice': None, 'has_prose': False,
                            'counts': {}})
        window_open = self._window_open(identity)
        decision = self.policy.decide(elapsed=elapsed, caps=adapter.capabilities,
                                      window_open=window_open, done=True)

        body = projection.get('text') or ''
        if not projection.get('has_prose'):
            # Non-messageable transcript (only tool churn / no prose) — never send
            # an empty body; summarize what ran instead (§6).
            summary = (summarize_tool_activity(projection.get('counts', {}))
                       if summarize_tool_activity else '')
            body = f'Done ({summary}).' if summary else 'Done.'

        if decision == FINAL:
            self._deliver_final(adapter, identity, thread_id, since, body,
                                projection.get('choice'))
        elif decision == TEMPLATE:
            self._deliver_template(adapter, identity, thread_id, since)
        # decision == DROP → we can't reach them; the runner log already has it.

        # ── Drain one queued mid-turn message, re-arming completion for it.
        with self._state_lock:
            queued = self._queue.get(thread_id) or []
            next_msg = queued.pop(0) if queued else None
            if not queued:
                self._queue.pop(thread_id, None)
            self._pending.pop(thread_id, None)
        if next_msg:
            since2 = client.last_seq(thread_id)
            with self._state_lock:
                self._pending[thread_id] = {
                    'adapter': adapter, 'identity': identity,
                    'workspace': conv['workspace'], 'since': since2,
                    'dispatched_at': _now(), 'client': client,
                }
            client.send(thread_id, next_msg)

    def _deliver_final(self, adapter, identity, thread_id, since, body,
                       choice) -> None:
        chunks = chunk_text(body, adapter.capabilities.max_text_len)
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            quick = (choice.get('options') if (last and choice) else None) or []
            # `since` (the per-turn seq watermark) keys the dedupe to THIS turn,
            # so a later turn on the same thread isn't suppressed as a duplicate.
            key = f'{thread_id}:{since}:final:{i}'
            if not self.sequencer.dedupe(identity, key):
                continue
            self._send(adapter, identity, chunk, quick_replies=quick)

    def _deliver_template(self, adapter, identity, thread_id, since) -> None:
        tpl = self.templates.select('task_complete')
        if not tpl:
            return  # not approved → cannot send out-of-window (WhatsApp rejects)
        key = f'{thread_id}:{since}:template'
        if not self.sequencer.dedupe(identity, key):
            return
        msg = OutboundMessage(
            channel_identity=identity,
            text=self.templates.render_body('task_complete', title='your task'),
            template='task_complete',
            template_args={'title': 'your task'},
            seq=self.sequencer.next(identity),
        )
        try:
            adapter.outbound(msg)
        except Exception:
            pass

    # ── helpers ──────────────────────────────────────────────────────────────
    def _client_for(self, binding: Dict[str, Any]) -> Optional[HypervisorClient]:
        if self.client_factory is None:
            return None
        try:
            return self.client_factory(binding)
        except Exception:
            return None

    def _workspaces_text(self, rec: Dict[str, Any]) -> str:
        lines = []
        for b in rec.get('bindings', []):
            mark = ' (default)' if b.get('is_default') else ''
            lines.append(f'• {b.get("workspace")}{mark}')
        return 'Your linked workspaces:\n' + '\n'.join(lines) if lines \
            else 'No workspaces linked.'

    def _send(self, adapter: ChannelAdapter, identity: str, text: str,
              quick_replies: Optional[List[str]] = None) -> DeliveryResult:
        msg = OutboundMessage(
            channel_identity=identity, text=text,
            quick_replies=quick_replies or [],
            seq=self.sequencer.next(identity),
        )
        try:
            return adapter.outbound(msg)
        except Exception as e:
            return DeliveryResult(ok=False, error=str(e))


# ───────────────────────────────────────────────────────────────────────────
# Chunking (§6 message length) — pure, shared by the core + adapters
# ───────────────────────────────────────────────────────────────────────────
def chunk_text(text: str, limit: int = 4096) -> List[str]:
    """Split `text` into ≤limit-char chunks on safe boundaries (paragraph, then
    line, then word, then hard) so a long assistant answer arrives in order and
    never mid-word. Always returns at least one chunk (possibly empty→[''])."""
    text = text or ''
    if len(text) <= limit:
        return [text] if text else ['']
    chunks: List[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        # Prefer the last paragraph break, then line, then space.
        cut = window.rfind('\n\n')
        if cut < limit // 2:
            cut = window.rfind('\n')
        if cut < limit // 2:
            cut = window.rfind(' ')
        if cut <= 0:
            cut = limit  # no boundary — hard split
        chunks.append(remaining[:cut].rstrip('\n'))
        remaining = remaining[cut:].lstrip('\n')
    if remaining:
        chunks.append(remaining)
    return chunks or ['']


# ───────────────────────────────────────────────────────────────────────────
# EchoAdapter — the core's Phase-0 test harness
# ───────────────────────────────────────────────────────────────────────────
class EchoAdapter:
    """A trivial in-memory adapter. inbound() reads a fake {from, text[, button]}
    RawRequest.form; outbound() appends to `self.sent`. No signatures, no network
    — so the ENTIRE core (identity map, routing, projection, policy) is testable
    against a real HypervisorSession before any WhatsApp code exists.

    Capabilities intentionally advertise buttons + no proactive, matching the
    interesting core branches (choice → quick replies; out-of-window → DROP)."""

    name = 'echo'

    def __init__(self, secret: str = '', proactive: bool = False):
        self.secret = secret
        self.sent: List[OutboundMessage] = []
        self.capabilities = Capabilities(
            buttons=True, max_buttons=3, max_list_rows=10, media=True,
            typing_indicator=False, proactive=proactive, max_text_len=4096)

    def verify(self, raw: RawRequest) -> bool:
        if not self.secret:
            return True
        return secrets.compare_digest(self.secret,
                                      raw.headers.get('X-Echo-Secret', ''))

    def handshake(self, raw: RawRequest) -> Optional[str]:
        return None

    def inbound(self, raw: RawRequest) -> Optional[InboundMessage]:
        frm = raw.form.get('from') or raw.form.get('From')
        if not frm:
            return None
        return InboundMessage(
            channel='echo',
            channel_identity=frm,
            text=raw.form.get('text', ''),
            provider_msg_id=raw.form.get('id', ''),
            button_reply=raw.form.get('button', ''),
        )

    def outbound(self, msg: OutboundMessage) -> DeliveryResult:
        self.sent.append(msg)
        return DeliveryResult(ok=True, provider_msg_id=f'echo-{len(self.sent)}')

    # Test conveniences.
    def texts(self) -> List[str]:
        return [m.text for m in self.sent]

    def last(self) -> Optional[OutboundMessage]:
        return self.sent[-1] if self.sent else None


# ───────────────────────────────────────────────────────────────────────────
# Walkie-Talkie preview (in-app loopback) — see what WhatsApp would see, locally
#
# The preview drives the SAME gateway core through a LOOPBACK channel adapter
# (adapters/internal.py) whose transport is the dashboard instead of Twilio/Meta.
# It advertises WhatsApp's Capabilities, so the projection, choice→buttons,
# chunking, ack/final policy, and out-of-window template selection all behave
# exactly as they would on real WhatsApp. The transcript below is the durable-
# enough (in-memory, single-pod) buffer the UI polls / gets pushed.
# ───────────────────────────────────────────────────────────────────────────
INTERNAL_IDENTITY = 'internal:local'


class PreviewTranscript:
    """Bounded, monotonically-seq'd log of preview messages (both directions),
    each carrying the human text PLUS the raw provider "wire" payload so the UI
    can show exactly what WhatsApp would send/receive. In-memory: this is a local
    developer preview, not a system of record."""

    def __init__(self, capacity: int = 500):
        self._items: "collections.deque[Dict[str, Any]]" = collections.deque(maxlen=capacity)
        self._seq = 0
        self._lock = threading.Lock()

    def add(self, direction: str, text: str, *, wire: Any = None,
            quick_replies: Optional[List[str]] = None, kind: str = 'message',
            meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            self._seq += 1
            item = {
                'seq': self._seq,
                'ts': _now(),
                'direction': direction,          # 'in' (user) | 'out' (agent)
                'kind': kind,                     # message | template | notice
                'text': text or '',
                'quick_replies': quick_replies or [],
                'wire': wire,                     # provider payload(s) or parsed inbound
                'meta': meta or {},
            }
            self._items.append(item)
            return item

    def since(self, seq: int) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(i) for i in self._items if i['seq'] > seq]

    def cursor(self) -> int:
        with self._lock:
            return self._seq

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            # Keep seq monotonic across a clear so a mid-poll client that holds a
            # stale cursor doesn't re-see cleared rows as "new".


class GatewayPreview:
    """Orchestrates the Walkie-Talkie loopback: holds the transcript, the
    simulate-out-of-window toggle, the internal identity, and the window probe
    that lets the shared gateway force the out-of-window path for THIS identity
    only. The loopback adapter + control wiring live where the gateway/adapter
    instances are available (server.py); this class stays import-cycle-free."""

    def __init__(self, workspace: str = 'default'):
        self.transcript = PreviewTranscript()
        self.simulate_out_of_window = False
        self.workspace = workspace
        self.identity = INTERNAL_IDENTITY

    def window_probe(self, channel_identity: str) -> Optional[bool]:
        """Force the window CLOSED for the internal identity when simulating —
        so a finished turn takes the out-of-window TEMPLATE path. Returns None
        (defer to the real clock) for every other identity, so the real WhatsApp
        channel sharing this gateway is unaffected."""
        if self.simulate_out_of_window and channel_identity == self.identity:
            return False
        return None
