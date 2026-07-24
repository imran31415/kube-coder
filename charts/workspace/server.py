#!/usr/bin/env python3
import http.server
import html
import subprocess
import os
import sys
import json
import time
import re
import base64
import collections
import hmac
import hashlib
import secrets
import mimetypes
import shutil
import threading
import queue
import uuid
import urllib.parse
import urllib.request
import urllib.error
import http.client
import socket
import ipaddress
import fcntl

# Persistent memory subsystem — shared with mcp_memory.py via the colocated
# `memory` package. Importable because the workspace-entrypoint copies the
# package next to server.py at /tmp/browser/.
try:
    from memory.manager import (
        MemoryManager,
        MemoryError as MemError,
        NotFound as MemNotFound,
        Conflict as MemConflict,
        ValidationError as MemValidationError,
    )
    from memory.sync import ClaudeMemorySyncer
    from memory.embeddings_worker import EmbeddingWorker
    _MEMORY_AVAILABLE = True
except Exception as _mem_import_err:  # broken install shouldn't crash the server
    MemoryManager = None  # type: ignore
    ClaudeMemorySyncer = None  # type: ignore
    EmbeddingWorker = None  # type: ignore
    MemError = MemNotFound = MemConflict = MemValidationError = Exception  # type: ignore
    _MEMORY_AVAILABLE = False
    print(f'[memory] import failed: {_mem_import_err}', file=sys.stderr)

# hypervisor_session: structured agent sessions backing the Hypervisor chat.
# Same delivery as the memory package (copied next to server.py at /tmp/browser).
try:
    from hypervisor_session import (
        HypervisorSession, build_activity as hv_build_activity,
        hypervisor_health as hv_health, WATCHERS as hv_watchers,
        HYPERVISOR_DIR,
    )
    _HYPERVISOR_AVAILABLE = True
except Exception as _hv_import_err:  # broken install shouldn't crash the server
    HypervisorSession = None  # type: ignore
    hv_build_activity = None  # type: ignore
    hv_health = None  # type: ignore
    hv_watchers = None  # type: ignore
    HYPERVISOR_DIR = ''  # type: ignore
    _HYPERVISOR_AVAILABLE = False
    print(f'[hypervisor] import failed: {_hv_import_err}', file=sys.stderr)

# gateway: the Conversation Gateway (issue #306) — chat with the Hypervisor from
# outside the app over a channel (WhatsApp first). Channel-agnostic core here;
# the WhatsApp adapter lives in adapters/whatsapp.py. Same colocated-package
# delivery as memory/hypervisor_session.
try:
    from gateway import (ConversationGateway, IdentityRegistry,
                        LocalHypervisorClient, RawRequest, GatewayPreview,
                        INTERNAL_IDENTITY)
    from adapters.whatsapp import (WhatsAppAdapter, build_provider as
                                   gw_build_provider, list_providers as
                                   gw_list_providers, get_provider_spec as
                                   gw_get_provider_spec)
    from adapters.internal import LoopbackAdapter
    _GATEWAY_AVAILABLE = True
except Exception as _gw_import_err:  # broken install shouldn't crash the server
    ConversationGateway = None  # type: ignore
    IdentityRegistry = None  # type: ignore
    LocalHypervisorClient = None  # type: ignore
    RawRequest = None  # type: ignore
    GatewayPreview = None  # type: ignore
    INTERNAL_IDENTITY = 'internal:local'  # type: ignore
    WhatsAppAdapter = None  # type: ignore
    gw_build_provider = None  # type: ignore
    gw_list_providers = None  # type: ignore
    gw_get_provider_spec = None  # type: ignore
    LoopbackAdapter = None  # type: ignore
    _GATEWAY_AVAILABLE = False
    print(f'[gateway] import failed: {_gw_import_err}', file=sys.stderr)

# Multi-harness skills subsystem (issue #187) — same colocated-package
# convention as `memory`. Read-only surface over SKILL.md-style files
# discovered from every supported agent harness (Claude Code, OpenCode,
# Antigravity, …) via one provider class per harness.
try:
    from skills.sync import SkillsSyncer
    from skills.providers import PROVIDERS as SKILL_PROVIDERS
    from skills.model import SKILL_NAME_RE
    from skills.commands import discover_commands
    _SKILLS_AVAILABLE = True
except Exception as _skills_import_err:  # broken install shouldn't crash the server
    SkillsSyncer = None  # type: ignore
    SKILL_PROVIDERS = {}  # type: ignore
    SKILL_NAME_RE = None  # type: ignore
    discover_commands = None  # type: ignore
    _SKILLS_AVAILABLE = False
    print(f'[skills] import failed: {_skills_import_err}', file=sys.stderr)

# User-defined MCP servers (issue #353) — one canonical registry on the PVC,
# fanned out to every MCP-capable assistant's native config (Claude, OpenCode,
# Ante, Codex). Same colocated-module delivery as hypervisor_session.
try:
    import mcp_registry
    _MCP_REGISTRY_AVAILABLE = True
except Exception as _mcp_reg_import_err:  # broken install shouldn't crash the server
    mcp_registry = None  # type: ignore
    _MCP_REGISTRY_AVAILABLE = False
    print(f'[mcp-registry] import failed: {_mcp_reg_import_err}', file=sys.stderr)

# Alert thresholds for metrics
ALERT_THRESHOLDS = {
    'cpu': {'warning': 70, 'critical': 90},
    'memory': {'warning': 80, 'critical': 95},
    'disk': {'warning': 80, 'critical': 90}
}

# Public-demo / read-only mode. Set from helm values via env vars on the
# pod spec. READONLY_MODE gates every POST/DELETE/PUT in BrowserHandler;
# AUTH_MODE='none' short-circuits check_claude_auth for deployments without
# an oauth2-proxy in front. _check_safety_invariants() below refuses to
# start if AUTH_MODE=none without READONLY_MODE=true — no unauthed writes.
READONLY_MODE = os.environ.get('READONLY_MODE', 'false').lower() == 'true'
AUTH_MODE = os.environ.get('AUTH_MODE', 'basic').lower()
# DEMO_SHOW_ALL=true makes the SPA *render* every mutation control instead of
# hiding it (MutatorOnly), so the public demo shows the full UI surface — but
# the server still 403s every write via _readonly_block. Presentation-only
# hint surfaced through /api/mode; it does NOT relax any gate. Only meaningful
# alongside READONLY_MODE=true (the demo deploy); inert otherwise.
DEMO_SHOW_ALL = os.environ.get('DEMO_SHOW_ALL', 'false').lower() == 'true'
# Public-demo confidentiality controls (see finding 3 of the July 2026 review).
# AUTH_MODE=none serves an UNAUTHENTICATED workspace; READONLY_MODE blocks
# writes but NOT reads, so every readable file on the PVC — including dotfile
# credentials (.claude-tasks/.api-token, .config, .ssh, .git, task transcripts)
# — is otherwise publicly downloadable. Two operator opt-ins:
#   * PUBLIC_FILE_ROOT — confine public file reads to this subdir of /home/dev.
#   * PUBLIC_DEMO_ACK   — the operator has acknowledged that readable PVC files
#                         are public (silences the loud startup warning).
# Independent of either, none-mode always refuses hidden (dot) path segments
# for direct file download/preview/view (matching how listings hide dotfiles).
PUBLIC_FILE_ROOT = os.environ.get('PUBLIC_FILE_ROOT', '').strip()
PUBLIC_DEMO_ACK = os.environ.get('PUBLIC_DEMO_ACK', 'false').lower() == 'true'
# Hypervisor — the workspace-aware chat tab. A clean chat UI layered over the
# user's existing CLI agents (claude/ante/opencode/…) plus the dashboard MCP
# tools. Threads are hypervisor-flavoured tasks (source="hypervisor") reusing
# ClaudeTaskManager; there is no separate LLM/provider loop.
HYPERVISOR_ENABLED = os.environ.get('HYPERVISOR_ENABLED', 'true').lower() == 'true'
HYPERVISOR_DEFAULT_ASSISTANT = os.environ.get('HYPERVISOR_DEFAULT_ASSISTANT', 'claude')
HYPERVISOR_WORKDIR = os.environ.get('HYPERVISOR_WORKDIR', '/home/dev')
# Short context note pasted as the first message of a new chat, so the agent
# knows its role + that it has the dashboard tools. Kept terse on purpose —
# a big preamble front-loads noise and some CLIs handle it poorly.
HYPERVISOR_PREAMBLE = (
    "[System: You are the Workspace Hypervisor — a chat assistant embedded in "
    "this kube-coder developer workspace. You have `dashboard` MCP tools to read "
    "live workspace state (get_metrics, list_tasks, get_task, get_service_health, "
    "get_github_status, search_memory, list_memory, list_apps, list_triggers) and "
    "to act on it (create_task, send_task_message, add_memory, pin_app). "
    "Destructive tools (kill_task, delete_memory) require confirm=true — first "
    "tell the user exactly what you'll do and get their explicit approval in the "
    "chat, then call again with confirm=true. "
    "You can also render rich content inline in this chat: call show_app_preview "
    "with a running app's port to embed a LIVE preview of it, show_media to "
    "display an image or video (a workspace file path under /home/dev, or an http "
    "URL), and show_file to render a document/file for review (markdown, text, "
    "code, PDF, or HTML from a /home/dev path). Do this proactively — when you "
    "start or build an app, show its preview; when you produce a screenshot, show "
    "it; when you create or reference a doc (a plan, README, report), show_file it. "
    "Prefer these tools for any question about, or action on, the workspace. "
    "Answer from tool results, not memory. Be concise and conversational. "
    "IMPORTANT: background watchers armed inside your turn (Bash "
    "run_in_background, Monitor) do not survive it — each of your turns runs "
    "in a fresh headless CLI process, and a later 'no completion record / may "
    "have been stopped' notification means exactly that (not a user action). "
    "To wait on something across turns, arm a runner-owned watcher with the "
    "dashboard `watch` tool: kind 'task' with a task_id (fires when the task "
    "completes, errors, is killed, or goes waiting-for-input), kind 'command' "
    "with a shell predicate (fires when it exits 0), or kind 'file' with a "
    "path (fires when it appears/changes). The workspace runner polls it after "
    "your turn ends and posts the outcome into this chat as a new message — "
    "so after arming one, just end your turn; do not poll. Use list_watchers / "
    "cancel_watcher to inspect or disarm. Only use run_in_background for work "
    "you will collect within the same turn. "
    "If the user wants to connect their own GitHub account (push to their "
    "repos, use their identity), first check get_github_status; if they are "
    "not on a personal login, point them to Settings → GitHub & SSH and its "
    "one-click \"Connect GitHub account\" button, which walks them through the "
    "browser sign-in — no terminal or `gh auth login` needed. "
    "When you need the user to make a discrete choice between a few options, "
    "write your normal explanation, then END the message with a fenced choice "
    "block the chat renders as clickable buttons:\n"
    "```choice\n"
    "<optional one-line question>\n"
    "- First option\n"
    "- Second option\n"
    "```\n"
    "Use it only for genuine either/or decisions, keep each option short (a few "
    "words), and never put anything after the block. The user can always type a "
    "different answer instead of clicking one.]\n\n"
)
# Conversation Gateway (issue #306) — public host the gateway advertises when
# minting a pairing code, so the user knows which WhatsApp number to message.
# Purely informational; the number itself is configured on the provider side.
GATEWAY_WHATSAPP_NUMBER = os.environ.get('KC_WHATSAPP_NUMBER', '')

# Lazily-built singleton Conversation Gateway + one WhatsApp adapter instance.
# Built on first use (and at startup) so the turn-complete observer is installed
# before any gateway-dispatched turn can finish. Guarded so a broken install or a
# disabled hypervisor degrades to a 503 rather than a crash.
_GATEWAY = None            # type: ignore
_GATEWAY_ADAPTER = None    # type: ignore
_GATEWAY_PREVIEW = None     # type: ignore  # Walkie-Talkie preview orchestrator
_GATEWAY_LOOPBACK = None    # type: ignore  # in-app loopback ChannelAdapter
_GATEWAY_LOCK = threading.Lock()


def _gateway_client_factory(binding):
    """Build a HypervisorClient for a workspace binding. Phase 1 is one number
    per workspace, so every binding targets THIS pod's HypervisorSession — the
    same operations the /api/hypervisor/* facade performs (issue D5). A shared
    number router (Phase 2) would swap this to target a remote pod."""
    assistant = ClaudeTaskManager.resolve_assistant(HYPERVISOR_DEFAULT_ASSISTANT)
    cli_cmd = ClaudeTaskManager.assistant_command(assistant, auto_approve=True)
    return LocalHypervisorClient(
        HypervisorSession, assistant=assistant, workdir=HYPERVISOR_WORKDIR,
        cli_cmd=cli_cmd, preamble=HYPERVISOR_PREAMBLE)


def get_gateway():
    """The process-wide ConversationGateway, or None when unavailable. Installs
    the turn-complete observer exactly once on first construction, and wires the
    Walkie-Talkie preview (its window probe + loopback adapter) into the SAME
    core so the in-app preview behaves identically to real WhatsApp."""
    global _GATEWAY, _GATEWAY_ADAPTER, _GATEWAY_PREVIEW, _GATEWAY_LOOPBACK
    if not (_GATEWAY_AVAILABLE and _HYPERVISOR_AVAILABLE):
        return None
    with _GATEWAY_LOCK:
        if _GATEWAY is None:
            preview = GatewayPreview()
            gw = ConversationGateway(
                registry=IdentityRegistry(),
                client_factory=_gateway_client_factory,
                token_verifier=ClaudeTaskManager.verify_token,
                window_probe=preview.window_probe)
            gw.install_turn_observer()
            _GATEWAY = gw
            _GATEWAY_ADAPTER = _build_gateway_adapter()
            _GATEWAY_PREVIEW = preview
            _GATEWAY_LOOPBACK = LoopbackAdapter(
                preview.transcript, publish=EventBroker.publish,
                identity=INTERNAL_IDENTITY)
            print('[gateway] conversation gateway ready (whatsapp + loopback preview)')
        return _GATEWAY


def _build_gateway_adapter():
    """Construct the WhatsApp adapter from the per-workspace credential store
    (issue #329), falling back to env (issue #328 `_provider_from_env`) when the
    store is empty or names an unknown provider. This is what makes saving creds
    hot-swap the live provider with no pod restart."""
    if WhatsAppAdapter is None:
        return None
    raw = GatewayCredentialsManager.get_raw()
    if raw and raw.get('provider_id') and gw_build_provider is not None:
        try:
            provider = gw_build_provider(raw['provider_id'], raw.get('creds') or {})
            return WhatsAppAdapter(provider=provider)
        except ValueError:
            # Stored provider id is unknown (e.g. removed) — fall back to env so
            # the channel degrades gracefully rather than 500-ing.
            pass
    return WhatsAppAdapter()


def rebuild_gateway_adapter():
    """Rebuild the live adapter from the store after a credential change so the
    inbound webhook path and capability probes see the new provider immediately.
    No-op if the gateway subsystem was never constructed."""
    global _GATEWAY_ADAPTER
    with _GATEWAY_LOCK:
        if _GATEWAY is not None:
            _GATEWAY_ADAPTER = _build_gateway_adapter()


def get_gateway_adapter():
    get_gateway()
    return _GATEWAY_ADAPTER


def get_gateway_preview():
    get_gateway()
    return _GATEWAY_PREVIEW


def get_gateway_loopback():
    get_gateway()
    return _GATEWAY_LOOPBACK


# TRUSTED_PROXY=true tells check_claude_auth it's safe to honor
# X-Auth-Request-User / X-Auth-Request-Email / Remote-User headers from the
# request. Without it we ignore those headers — the only ways to authenticate
# become AUTH_MODE=none (gated to readonly) or a Bearer token. Set to true
# when an upstream proxy strips client-supplied auth headers (e.g. our
# oauth2-proxy + ingress).
TRUSTED_PROXY = os.environ.get('TRUSTED_PROXY', 'true').lower() == 'true'
# Hard cap on JSON request bodies. Without this, a single
# Content-Length: huge POST will allocate the body before parsing and OOM
# the pod. Override via MAX_REQUEST_BODY_BYTES.
MAX_REQUEST_BODY_BYTES = int(os.environ.get('MAX_REQUEST_BODY_BYTES', str(1024 * 1024)))
# Hard ceiling on /stream connection lifetime. Clients are expected to
# reconnect; without this an unbounded handler-thread leak is the path of
# least resistance to DoS. Override via STREAM_MAX_SECONDS.
STREAM_MAX_SECONDS = int(os.environ.get('STREAM_MAX_SECONDS', '1800'))
# SSRF guard for the completion-hook response_url. By default we refuse to
# POST to RFC1918 / link-local / loopback so a malicious caller cannot turn
# us into a probe of the cloud metadata service or in-cluster services.
# Set ALLOW_INTERNAL_HOOKS=true to opt back in (single-user trusted deploy).
ALLOW_INTERNAL_HOOKS = os.environ.get('ALLOW_INTERNAL_HOOKS', 'false').lower() == 'true'
# Defensive cap on the (unused) hook response body we read, so a hostile
# endpoint can't stream us unbounded data at delivery time.
HOOK_MAX_RESPONSE_BYTES = int(os.environ.get('KC_HOOK_MAX_RESPONSE_BYTES', str(64 * 1024)))


class _HookSSRFError(Exception):
    """Raised at completion-hook delivery time when the response_url resolves
    to a non-public address, cannot be resolved, or uses an unsupported
    scheme. Surfaced as an ordinary delivery error (retried/dead-lettered)
    rather than silently followed to an internal target."""


def _hook_public_ip(addr):
    """Return an ipaddress object for `addr` iff it is a *public* address,
    else None. Normalizes IPv4-mapped IPv6 (``::ffff:a.b.c.d``) to its IPv4
    form before classification so a mapped internal address is still caught.
    The reject set is loopback, RFC1918/private, link-local (covers cloud
    metadata 169.254.169.254), multicast, unspecified and reserved."""
    try:
        ip = ipaddress.ip_address(addr)
    except (ValueError, TypeError):
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_unspecified or ip.is_reserved):
        return None
    return ip


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject *all* redirects for completion-hook delivery.

    Following a 3xx would let an attacker-controlled endpoint that passes the
    public-IP check bounce us to 127.0.0.1 / RFC1918 / 169.254.169.254 / an
    in-cluster service (the redirect target is never re-validated). Returning
    None here makes urllib raise the 3xx as an HTTPError instead of following
    it, so it is surfaced as a delivery error rather than an SSRF."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that connects to a pre-validated, pinned IP while keeping
    the original hostname for the HTTP Host header — so the address we checked
    is the address we actually reach (no second, uncontrolled DNS lookup that
    a rebinding server could answer differently)."""
    def __init__(self, host, pinned_ip, **kw):
        super().__init__(host, **kw)
        self._pinned_ip = pinned_ip

    def connect(self):
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """As _PinnedHTTPConnection, but TLS: connect to the pinned IP yet keep the
    original hostname for SNI and certificate validation."""
    def __init__(self, host, pinned_ip, **kw):
        super().__init__(host, **kw)
        self._pinned_ip = pinned_ip

    def connect(self):
        sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
            sock = self.sock
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, pinned_ip):
        super().__init__()
        self._pinned_ip = pinned_ip

    def http_open(self, req):
        return self.do_open(
            lambda host, **kw: _PinnedHTTPConnection(host, self._pinned_ip, **kw), req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, pinned_ip, **kw):
        super().__init__(**kw)
        self._pinned_ip = pinned_ip

    def https_open(self, req):
        return self.do_open(
            lambda host, **kw: _PinnedHTTPSConnection(host, self._pinned_ip, **kw), req)


# Self-serve version updates are brokered to the workspace-controller, which
# owns the kube access this pod lacks. The controller exposes a token-gated
# self-serve listener reached over the in-cluster Service. Both are injected by
# the chart only when the operator opts in (names a shared Secret); empty => the
# dashboard's Updates section reports "self-serve unavailable".
CONTROLLER_SELF_SERVE_URL = os.environ.get('CONTROLLER_SELF_SERVE_URL', '').strip().rstrip('/')
CONTROLLER_SELF_SERVE_TOKEN = os.environ.get('CONTROLLER_SELF_SERVE_TOKEN', '').strip()

def _public_mode_active():
    """True when this is the unauthenticated public demo: AUTH_MODE=none, which
    _check_safety_invariants requires to be gated by READONLY_MODE."""
    return AUTH_MODE == 'none' and READONLY_MODE


def _public_demo_needs_ack():
    """True when public mode is active but the operator has NOT acknowledged that
    readable PVC files become public (no PUBLIC_DEMO_ACK and no PUBLIC_FILE_ROOT).
    Drives the loud startup warning below."""
    return _public_mode_active() and not (PUBLIC_DEMO_ACK or PUBLIC_FILE_ROOT)


def _check_safety_invariants():
    if AUTH_MODE == 'none' and not READONLY_MODE:
        print(
            '[server.py] FATAL: AUTH_MODE=none requires READONLY_MODE=true. '
            'Refusing to start an unauthed, writable workspace.',
            file=sys.stderr,
        )
        sys.exit(2)
    if READONLY_MODE:
        print('[server.py] READONLY_MODE active — mutating endpoints will 403.', file=sys.stderr)
    if AUTH_MODE == 'none':
        print('[server.py] AUTH_MODE=none — check_claude_auth short-circuits to True.', file=sys.stderr)
    if PUBLIC_FILE_ROOT:
        print(f'[server.py] PUBLIC_FILE_ROOT={PUBLIC_FILE_ROOT!r} — public file reads '
              'are confined to this subdir of /home/dev.', file=sys.stderr)
    if _public_demo_needs_ack():
        print(
            '[server.py] WARNING: public-demo mode (AUTH_MODE=none + READONLY_MODE=true) '
            'is active. READONLY_MODE blocks writes but NOT reads: every readable file on '
            'this PVC — including dotfile credentials like .claude-tasks/.api-token, '
            '.config, .ssh, .git — is otherwise reachable by UNAUTHENTICATED visitors. '
            'Hidden (dot) path segments are now refused for public download/preview/view, '
            'but you MUST still serve this demo from a FRESH, sanitized PVC and NEVER '
            'reuse a private workspace PVC. Set PUBLIC_DEMO_ACK=true (or PUBLIC_FILE_ROOT='
            '<subdir>) once you have done so to acknowledge and silence this warning.',
            file=sys.stderr,
        )
    if DEMO_SHOW_ALL:
        print('[server.py] DEMO_SHOW_ALL=true — SPA renders mutation UI (still 403-gated).', file=sys.stderr)
_check_safety_invariants()

# Strip ANSI escape sequences (CSI, OSC, single-char) from terminal output
# captured via `tmux pipe-pane`, so the dashboard chat view stays readable.
_ANSI_RE = re.compile(
    r'\x1b\[[0-9;?]*[ -/]*[@-~]'   # CSI
    r'|\x1b\][^\x07]*\x07'          # OSC ... BEL
    r'|\x1b[NOPYZ\\^_=>78<]'        # single-char escapes
)


def strip_ansi(text):
    return _ANSI_RE.sub('', text)


# --- Interactive-prompt screen parser (issue #204) ------------------------
# When a Claude Code task pauses on an in-terminal prompt — the numbered
# permission menu or a free-form yes/no question — we parse the captured tmux
# pane into a structured description the dashboard renders as tappable
# quick-reply buttons (one tap to approve, instead of typing into the raw ttyd
# iframe — the highest-value mobile win). The parser is deliberately server-side
# so web AND the native mobile app both benefit from one implementation.
#
# Parsing a TUI is brittle, so we anchor on Claude Code's real layout and fail
# safe: anything unrecognized returns None, and the plain composer stays fully
# in control. See parse_screen_prompt for the two recognized shapes.

# A numbered option line, e.g. "❯ 1. Yes" or "2. Yes, and don't ask again".
# The leading ❯ is Claude Code's selection caret (only the highlighted option
# carries it); we accept a plain '>' as a fallback for terminals that render it
# that way. The trailing "N. " requires a dot AND a space so version strings
# like "1.2.3" never match.
_PROMPT_OPTION_RE = re.compile(r'^(?:(❯|>)\s*)?(\d+)\.\s+(.+)$')

# A free-form yes/no marker: (y/n), [y/n], (yes/no), with tolerant spacing.
_YESNO_RE = re.compile(
    r'[\(\[]\s*y(?:es)?\s*/\s*n(?:o)?\s*[\)\]]', re.IGNORECASE)

# A line made up solely of box-drawing / rule characters (the borders tmux
# captures around Claude's prompt box). Treated as blank for question lookup.
_BOX_ONLY_RE = re.compile(r'^[\s─-╿|]+$')


def _normalize_prompt_line(line):
    """Strip trailing whitespace and the vertical box borders tmux captures so a
    bordered TUI line like '│ ❯ 1. Yes           │' becomes '❯ 1. Yes'. Lines
    that are only box-drawing rules collapse to '' so they're skipped."""
    s = line.strip().strip('│┃|').strip()
    if not s or _BOX_ONLY_RE.match(s):
        return ''
    return s


def parse_screen_prompt(text):
    """Parse the most-recent Claude Code TUI screen for an interactive prompt the
    user must answer, returning a structured description the dashboard renders as
    buttons — or None when no known prompt is on screen.

    Two shapes are recognized:

      1. A numbered choice block (the permission prompt):
             Do you want to proceed?
             ❯ 1. Yes
               2. Yes, and don't ask again
               3. No, and tell Claude what to do differently
         → {'kind': 'choice', 'question': 'Do you want to proceed?',
            'options': [{'index': 1, 'label': 'Yes'}, ...]}

      2. A free-form yes/no question carrying a (y/n) marker:
             Continue? (y/n)
         → {'kind': 'yesno', 'question': 'Continue?',
            'options': [{'index': 'y', 'label': 'Yes'},
                        {'index': 'n', 'label': 'No'}]}

    We only trust a numbered block that (a) starts at 1 and runs sequentially,
    (b) has >= 2 options, and (c) shows the ❯ selection caret — the caret is the
    anchor that distinguishes a live permission menu from an ordinary numbered
    list Claude may have printed in prose. Blocks are scanned bottom-up so the
    live prompt (always at the foot of the screen) wins.
    """
    if not text:
        return None

    norm = [_normalize_prompt_line(l) for l in text.splitlines()]

    # --- 1. Numbered choice block ---------------------------------------
    # Map each option line to (index, label, has_caret).
    opt_lines = {}
    for i, s in enumerate(norm):
        m = _PROMPT_OPTION_RE.match(s)
        if m:
            opt_lines[i] = (int(m.group(2)), m.group(3).strip(), m.group(1) is not None)

    # Group consecutive option-line indices into blocks.
    blocks = []
    cur = []
    prev = None
    for i in sorted(opt_lines):
        if prev is not None and i != prev + 1:
            blocks.append(cur)
            cur = []
        cur.append(i)
        prev = i
    if cur:
        blocks.append(cur)

    # Prefer the bottom-most valid block (the live prompt).
    for block in reversed(blocks):
        opts = [opt_lines[i] for i in block]
        indices = [o[0] for o in opts]
        has_caret = any(o[2] for o in opts)
        if len(opts) >= 2 and has_caret and indices == list(range(1, len(opts) + 1)):
            # Question = nearest non-blank normalized line above the block.
            question = None
            for j in range(block[0] - 1, -1, -1):
                if norm[j]:
                    question = norm[j]
                    break
            return {
                'kind': 'choice',
                'question': question,
                'options': [{'index': o[0], 'label': o[1]} for o in opts],
            }

    # --- 2. Free-form yes/no --------------------------------------------
    for i in range(len(norm) - 1, -1, -1):
        s = norm[i]
        if s and _YESNO_RE.search(s):
            q = _YESNO_RE.sub('', s).strip().rstrip('?').strip()
            question = f'{q}?' if q else None
            if not question:
                for j in range(i - 1, -1, -1):
                    if norm[j]:
                        question = norm[j]
                        break
            return {
                'kind': 'yesno',
                'question': question,
                'options': [
                    {'index': 'y', 'label': 'Yes'},
                    {'index': 'n', 'label': 'No'},
                ],
            }

    return None


# How long a task's rendered tmux screen must stay unchanged before we treat
# it as waiting-for-input. While an agent works it streams output / animates a
# spinner+timer, so the screen keeps changing; a static screen means it has
# finished its turn (or hit a prompt) and is awaiting the human. This replaced
# a regex scraper that never worked against the agents' full-screen TUIs.
# Env-overridable for tuning.
try:
    IDLE_WAITING_SECONDS = float(os.environ.get('KC_IDLE_WAITING_SECONDS', '90'))
except ValueError:
    IDLE_WAITING_SECONDS = 90.0

class MetricsCollector:
    """Collects system metrics from /proc filesystem and os.statvfs"""

    @staticmethod
    def get_cpu_usage():
        """Get CPU usage percentage using /proc/stat"""
        try:
            def read_cpu_times():
                with open('/proc/stat', 'r') as f:
                    line = f.readline()
                    parts = line.split()
                    # cpu user nice system idle iowait irq softirq steal guest guest_nice
                    if parts[0] == 'cpu':
                        times = [int(x) for x in parts[1:]]
                        idle = times[3] + times[4]  # idle + iowait
                        total = sum(times)
                        return idle, total
                return 0, 0

            idle1, total1 = read_cpu_times()
            time.sleep(0.5)
            idle2, total2 = read_cpu_times()

            idle_delta = idle2 - idle1
            total_delta = total2 - total1

            if total_delta == 0:
                usage_percent = 0.0
            else:
                usage_percent = ((total_delta - idle_delta) / total_delta) * 100

            # Count CPU cores
            cores = 0
            with open('/proc/stat', 'r') as f:
                for line in f:
                    if line.startswith('cpu') and line[3].isdigit():
                        cores += 1

            return {
                'usage_percent': round(usage_percent, 1),
                'cores': cores if cores > 0 else 1
            }
        except Exception as e:
            return {'usage_percent': 0.0, 'cores': 1, 'error': str(e)}

    @staticmethod
    def get_memory_usage():
        """Get memory usage from /proc/meminfo"""
        try:
            meminfo = {}
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    parts = line.split()
                    key = parts[0].rstrip(':')
                    value = int(parts[1])  # Value in kB
                    meminfo[key] = value

            total_kb = meminfo.get('MemTotal', 0)
            available_kb = meminfo.get('MemAvailable', meminfo.get('MemFree', 0))
            used_kb = total_kb - available_kb

            total_mb = total_kb / 1024
            used_mb = used_kb / 1024
            available_mb = available_kb / 1024

            percent = (used_kb / total_kb * 100) if total_kb > 0 else 0

            return {
                'total_mb': round(total_mb, 1),
                'used_mb': round(used_mb, 1),
                'available_mb': round(available_mb, 1),
                'percent': round(percent, 1)
            }
        except Exception as e:
            return {'total_mb': 0, 'used_mb': 0, 'available_mb': 0, 'percent': 0, 'error': str(e)}

    @staticmethod
    def get_disk_usage():
        """Get disk usage for /home/dev"""
        try:
            path = '/home/dev'
            if not os.path.exists(path):
                path = '/'

            stat = os.statvfs(path)
            total_bytes = stat.f_blocks * stat.f_frsize
            available_bytes = stat.f_bavail * stat.f_frsize
            used_bytes = total_bytes - available_bytes

            total_gb = total_bytes / (1024 ** 3)
            used_gb = used_bytes / (1024 ** 3)
            available_gb = available_bytes / (1024 ** 3)

            percent = (used_bytes / total_bytes * 100) if total_bytes > 0 else 0

            return {
                'total_gb': round(total_gb, 1),
                'used_gb': round(used_gb, 1),
                'available_gb': round(available_gb, 1),
                'percent': round(percent, 1),
                'path': path
            }
        except Exception as e:
            return {'total_gb': 0, 'used_gb': 0, 'available_gb': 0, 'percent': 0, 'path': '/home/dev', 'error': str(e)}

    @staticmethod
    def get_alerts(cpu, memory, disk):
        """Generate alerts based on current metrics"""
        alerts = []

        if cpu.get('usage_percent', 0) >= ALERT_THRESHOLDS['cpu']['critical']:
            alerts.append({'type': 'critical', 'resource': 'cpu', 'message': f"CPU usage at {cpu['usage_percent']}%"})
        elif cpu.get('usage_percent', 0) >= ALERT_THRESHOLDS['cpu']['warning']:
            alerts.append({'type': 'warning', 'resource': 'cpu', 'message': f"CPU usage at {cpu['usage_percent']}%"})

        if memory.get('percent', 0) >= ALERT_THRESHOLDS['memory']['critical']:
            alerts.append({'type': 'critical', 'resource': 'memory', 'message': f"Memory usage at {memory['percent']}%"})
        elif memory.get('percent', 0) >= ALERT_THRESHOLDS['memory']['warning']:
            alerts.append({'type': 'warning', 'resource': 'memory', 'message': f"Memory usage at {memory['percent']}%"})

        if disk.get('percent', 0) >= ALERT_THRESHOLDS['disk']['critical']:
            alerts.append({'type': 'critical', 'resource': 'disk', 'message': f"Disk usage at {disk['percent']}%"})
        elif disk.get('percent', 0) >= ALERT_THRESHOLDS['disk']['warning']:
            alerts.append({'type': 'warning', 'resource': 'disk', 'message': f"Disk usage at {disk['percent']}%"})

        return alerts

    @staticmethod
    def get_all_metrics():
        """Return all metrics as a dictionary"""
        cpu = MetricsCollector.get_cpu_usage()
        memory = MetricsCollector.get_memory_usage()
        disk = MetricsCollector.get_disk_usage()
        alerts = MetricsCollector.get_alerts(cpu, memory, disk)

        return {
            'cpu': cpu,
            'memory': memory,
            'disk': disk,
            'alerts': alerts,
            'timestamp': time.time()
        }


class GitHubManager:
    """Handles GitHub authentication and configuration"""

    SSH_DIR = os.path.expanduser('~/.ssh')
    GH_CONFIG_DIR = os.path.expanduser('~/.config/gh')
    # Persisted GitHub auth mode (issue #256). 'personal' = the user's own
    # gh/git login wins; anything else (incl. missing) = 'app', the managed
    # installation-token default. Read by start.sh, the refresh daemon and here.
    AUTH_MODE_FILE = '/home/dev/.credentials/.github-auth-mode'
    TOKEN_FILE = '/home/dev/.credentials/.github-token'

    @staticmethod
    def get_auth_mode():
        """Current GitHub auth mode: 'personal' or 'app' (default)."""
        try:
            with open(GitHubManager.AUTH_MODE_FILE) as f:
                return 'personal' if f.read().strip() == 'personal' else 'app'
        except OSError:
            return 'app'

    @staticmethod
    def app_available():
        """Whether the managed GitHub App flow is wired for this workspace —
        i.e. switching back to 'app' mode is meaningful. True when the App is
        configured (GITHUB_APP_ID) or a minted token file already exists."""
        return bool(os.environ.get('GITHUB_APP_ID')) or os.path.exists(GitHubManager.TOKEN_FILE)

    @staticmethod
    def _app_credential_helper():
        """The git credential helper that serves the App installation token."""
        return (
            "!f() { "
            'echo "protocol=https"; '
            'echo "host=github.com"; '
            'echo "username=x-access-token"; '
            'echo "password=$(cat /home/dev/.credentials/.github-token)"; '
            "}; f"
        )

    @staticmethod
    def _apply_auth_mode(mode):
        """Re-point git credential auth to match the mode, effective immediately
        for new git/gh operations (mirrors the refresh daemon's configure_git).
        app: install the App-token helper. personal: remove it and let the
        user's gh login drive git via `gh auth setup-git` (best-effort)."""
        if mode == 'personal':
            subprocess.run(['git', 'config', '--global', '--unset-all', 'credential.helper'],
                           capture_output=True)
            subprocess.run(['gh', 'auth', 'setup-git', '--hostname', 'github.com'],
                           capture_output=True)
        else:
            subprocess.run(['git', 'config', '--global', '--replace-all',
                            'credential.helper', GitHubManager._app_credential_helper()],
                           capture_output=True)

    @staticmethod
    def set_auth_mode(mode):
        """Persist + apply the GitHub auth mode. Raises ValueError on bad input.
        Returns the refreshed full status so the UI reflects the switch."""
        if mode not in ('app', 'personal'):
            raise ValueError("mode must be 'app' or 'personal'")
        os.makedirs(os.path.dirname(GitHubManager.AUTH_MODE_FILE), exist_ok=True)
        with open(GitHubManager.AUTH_MODE_FILE, 'w') as f:
            f.write(mode + '\n')
        os.chmod(GitHubManager.AUTH_MODE_FILE, 0o600)
        GitHubManager._apply_auth_mode(mode)
        return GitHubManager.get_full_status()

    # ── Browser-less "Connect GitHub" web login (issue #303) ──────────────
    # First-time users struggle to connect a *personal* GitHub account: it
    # means opening a terminal and driving the interactive `gh auth login
    # --web` device flow by hand. We drive that flow server-side instead —
    # spawn gh in a dedicated tmux session, scrape the one-time device code
    # so the dashboard can show it with a one-click "Open GitHub" link, and
    # poll to completion — then flip the workspace to 'personal' mode. No
    # terminal required.
    WEB_LOGIN_SESSION = 'kc-gh-web-login'
    # gh's one-time device code, e.g. "1A2B-3C4D".
    _DEVICE_CODE_RE = re.compile(r'\b([A-Z0-9]{4}-[A-Z0-9]{4})\b')
    _DEVICE_URL_RE = re.compile(r'(https://github\.com/login/device)')
    # Sentinel we append after gh exits so the pane can be classified even
    # after the process ends (the session lingers via `sleep`, see below).
    _GH_EXIT_RE = re.compile(r'__KC_GH_EXIT__:(\d+)')
    _LOGGED_IN_RE = re.compile(r'Logged in as\s+(\S+)', re.IGNORECASE)

    @staticmethod
    def _tmux(*args):
        return subprocess.run(['tmux', *args], capture_output=True, text=True)

    @staticmethod
    def web_login_running():
        """True while the background `gh auth login` session is still alive."""
        return GitHubManager._tmux(
            'has-session', '-t', GitHubManager.WEB_LOGIN_SESSION).returncode == 0

    @staticmethod
    def cancel_web_login():
        """Tear down the background login session (idempotent)."""
        GitHubManager._tmux('kill-session', '-t', GitHubManager.WEB_LOGIN_SESSION)

    @staticmethod
    def _capture_web_login_pane():
        r = GitHubManager._tmux(
            'capture-pane', '-p', '-t', GitHubManager.WEB_LOGIN_SESSION)
        return r.stdout if r.returncode == 0 else ''

    @staticmethod
    def parse_device_code(pane):
        """Pull gh's one-time device code + verification URL out of the captured
        tmux pane. Returns {'code','verification_uri'} once the code is on
        screen, else None. Pure/text-only so it unit-tests without a real gh."""
        if not pane:
            return None
        m = GitHubManager._DEVICE_CODE_RE.search(pane)
        if not m:
            return None
        url = GitHubManager._DEVICE_URL_RE.search(pane)
        return {
            'code': m.group(1),
            'verification_uri': url.group(1) if url else 'https://github.com/login/device',
        }

    @staticmethod
    def classify_web_login(pane):
        """Classify the login session from its pane: 'success' | 'failed' |
        'pending'. Anchored on the exit-code sentinel we print after gh, so a
        clean exit (0) is success and any non-zero is failure. Pure/testable."""
        m = GitHubManager._GH_EXIT_RE.search(pane or '')
        if not m:
            return 'pending'
        return 'success' if m.group(1) == '0' else 'failed'

    @staticmethod
    def start_web_login(timeout=25):
        """Kick off `gh auth login --web` in a background tmux session and return
        the one-time device code for the dashboard to display. We deliberately
        run the *real* gh binary with GH_TOKEN stripped (bypassing the mode
        shim) so the login writes a personal token to hosts.yml regardless of
        the current mode; the mode is only flipped to 'personal' once poll()
        confirms success. Raises RuntimeError if no code appears in `timeout`s."""
        GitHubManager.cancel_web_login()
        gh_bin = '/usr/bin/gh' if os.path.exists('/usr/bin/gh') else 'gh'
        # env -u strips the App token for THIS gh only, so gh stores a personal
        # login instead of refusing ("GH_TOKEN is being used for auth"). The
        # exit sentinel + trailing sleep keep the pane readable after gh ends.
        inner = (
            f'env -u GH_TOKEN -u GITHUB_TOKEN {gh_bin} auth login '
            '--hostname github.com --git-protocol https --web --skip-ssh-key; '
            "printf '__KC_GH_EXIT__:%s\\n' \"$?\"; sleep 600"
        )
        started = GitHubManager._tmux(
            'new-session', '-d', '-s', GitHubManager.WEB_LOGIN_SESSION,
            'bash', '-lc', inner)
        if started.returncode != 0:
            raise RuntimeError(
                (started.stderr or 'could not start login session').strip())
        deadline = time.time() + timeout
        while time.time() < deadline:
            parsed = GitHubManager.parse_device_code(
                GitHubManager._capture_web_login_pane())
            if parsed:
                # gh waits on "Press Enter to open in your browser" — answer it
                # so gh proceeds to poll GitHub for authorization.
                GitHubManager._tmux(
                    'send-keys', '-t', GitHubManager.WEB_LOGIN_SESSION, 'Enter')
                parsed['in_progress'] = True
                return parsed
            time.sleep(0.5)
        GitHubManager.cancel_web_login()
        raise RuntimeError(
            'Timed out waiting for GitHub to issue a sign-in code. Check the '
            "workspace's network access and try again.")

    @staticmethod
    def poll_web_login():
        """Report progress of the background login and, on success, switch the
        workspace to 'personal' mode + clean up. Returns the full GitHub status
        augmented with 'connected' (bool) and 'in_progress' (bool), plus an
        'error' string on failure."""
        running = GitHubManager.web_login_running()
        pane = GitHubManager._capture_web_login_pane() if running else ''
        state = GitHubManager.classify_web_login(pane)
        if state == 'success':
            if GitHubManager.get_auth_mode() != 'personal':
                GitHubManager.set_auth_mode('personal')
            user_m = GitHubManager._LOGGED_IN_RE.search(pane)
            GitHubManager.cancel_web_login()
            status = GitHubManager.get_full_status()
            status.update(connected=True, in_progress=False)
            if user_m:
                status['connected_user'] = user_m.group(1)
            return status
        if state == 'failed' or not running:
            GitHubManager.cancel_web_login()
            status = GitHubManager.get_full_status()
            status.update(connected=False, in_progress=False,
                          error='GitHub sign-in did not complete. Please try again.')
            return status
        status = GitHubManager.get_full_status()
        status.update(connected=False, in_progress=True)
        return status

    @staticmethod
    def get_ssh_status():
        """Check if SSH key exists and get its details"""
        key_path = os.path.join(GitHubManager.SSH_DIR, 'id_ed25519')
        pub_key_path = key_path + '.pub'

        if not os.path.exists(pub_key_path):
            return {'configured': False}

        try:
            with open(pub_key_path, 'r') as f:
                public_key = f.read().strip()

            # Get fingerprint
            result = subprocess.run(
                ['ssh-keygen', '-lf', pub_key_path],
                capture_output=True, text=True
            )
            fingerprint = result.stdout.split()[1] if result.returncode == 0 else 'unknown'

            return {
                'configured': True,
                'key_type': 'ed25519',
                'key_fingerprint': fingerprint,
                'public_key': public_key
            }
        except Exception as e:
            return {'configured': False, 'error': str(e)}

    @staticmethod
    def generate_ssh_key(email):
        """Generate new SSH key pair"""
        key_path = os.path.join(GitHubManager.SSH_DIR, 'id_ed25519')
        os.makedirs(GitHubManager.SSH_DIR, mode=0o700, exist_ok=True)

        # Remove existing key if present
        for ext in ['', '.pub']:
            path = key_path + ext
            if os.path.exists(path):
                os.remove(path)

        result = subprocess.run([
            'ssh-keygen', '-t', 'ed25519', '-C', email,
            '-f', key_path, '-N', ''
        ], capture_output=True, text=True)

        if result.returncode != 0:
            raise Exception(f"Failed to generate key: {result.stderr}")

        # Add GitHub config to SSH config file
        config_path = os.path.join(GitHubManager.SSH_DIR, 'config')
        github_config = """
Host github.com
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
"""
        # Check if config exists and already has github.com
        existing_config = ''
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                existing_config = f.read()

        if 'github.com' not in existing_config:
            with open(config_path, 'a') as f:
                f.write(github_config)
            os.chmod(config_path, 0o600)

        return GitHubManager.get_ssh_status()

    @staticmethod
    def get_gh_cli_status():
        """Check gh CLI authentication status"""
        try:
            result = subprocess.run(
                ['gh', 'auth', 'status', '--hostname', 'github.com'],
                capture_output=True, text=True
            )

            if result.returncode != 0:
                return {'installed': True, 'authenticated': False}

            # Parse output to get username (gh writes to stderr)
            output = result.stderr + result.stdout
            username = None
            for line in output.split('\n'):
                if 'Logged in to github.com' in line:
                    # Try to extract username
                    if 'account' in line:
                        parts = line.split('account')
                        if len(parts) > 1:
                            username = parts[1].strip().split()[0].strip('()')
                    break

            return {
                'installed': True,
                'authenticated': True,
                'username': username
            }
        except FileNotFoundError:
            return {'installed': False, 'authenticated': False}
        except Exception as e:
            return {'installed': True, 'authenticated': False, 'error': str(e)}

    @staticmethod
    def start_device_flow():
        """Start gh auth device flow - returns instructions for manual auth"""
        # We can't truly start interactive device flow from a server
        # Instead, provide instructions for the user
        return {
            'instructions': 'Run the following command in the terminal to authenticate:',
            'command': 'gh auth login --hostname github.com --git-protocol https --web',
            'manual_steps': [
                '1. Open Terminal from the dashboard',
                '2. Run: gh auth login',
                '3. Select GitHub.com',
                '4. Select HTTPS',
                '5. Authenticate with browser when prompted',
                '6. Return here and click "Check Status"'
            ]
        }

    @staticmethod
    def get_git_config():
        """Get git global config"""
        try:
            name_result = subprocess.run(
                ['git', 'config', '--global', 'user.name'],
                capture_output=True, text=True
            )
            email_result = subprocess.run(
                ['git', 'config', '--global', 'user.email'],
                capture_output=True, text=True
            )
            return {
                'user_name': name_result.stdout.strip() if name_result.returncode == 0 else '',
                'user_email': email_result.stdout.strip() if email_result.returncode == 0 else ''
            }
        except Exception as e:
            return {'user_name': '', 'user_email': '', 'error': str(e)}

    @staticmethod
    def set_git_config(name, email):
        """Set git global config"""
        try:
            subprocess.run(['git', 'config', '--global', 'user.name', name], check=True)
            subprocess.run(['git', 'config', '--global', 'user.email', email], check=True)
            return GitHubManager.get_git_config()
        except Exception as e:
            return {'error': str(e)}

    @staticmethod
    def get_full_status():
        """Get combined GitHub status"""
        return {
            'ssh': GitHubManager.get_ssh_status(),
            'gh_cli': GitHubManager.get_gh_cli_status(),
            'git_config': GitHubManager.get_git_config(),
            'auth_mode': GitHubManager.get_auth_mode(),
            'app_available': GitHubManager.app_available(),
        }


class ClaudeTaskManager:
    """Manages Claude Code tasks running in tmux sessions"""

    TASKS_DIR = '/home/dev/.claude-tasks'
    TOKEN_FILE = '/home/dev/.claude-tasks/.api-token'
    # Claude Code's per-user config; we pre-accept folder-trust here so a freshly
    # launched interactive task doesn't block on the trust dialog (see
    # _ensure_claude_trust).
    CLAUDE_CONFIG_PATH = os.path.expanduser('~/.claude.json')

    @staticmethod
    def ensure_tasks_dir():
        os.makedirs(ClaudeTaskManager.TASKS_DIR, mode=0o700, exist_ok=True)

    @staticmethod
    def get_or_create_token():
        ClaudeTaskManager.ensure_tasks_dir()
        if os.path.exists(ClaudeTaskManager.TOKEN_FILE):
            with open(ClaudeTaskManager.TOKEN_FILE, 'r') as f:
                token = f.read().strip()
                if token:
                    return token
        token = secrets.token_urlsafe(36)
        with open(ClaudeTaskManager.TOKEN_FILE, 'w') as f:
            f.write(token)
        os.chmod(ClaudeTaskManager.TOKEN_FILE, 0o600)
        return token

    @staticmethod
    def verify_token(token):
        if not os.path.exists(ClaudeTaskManager.TOKEN_FILE):
            return False
        with open(ClaudeTaskManager.TOKEN_FILE, 'r') as f:
            stored = f.read().strip()
        return secrets.compare_digest(token, stored)

    # ── App-proxy sessions (mobile WebView) ──────────────────────────────
    # A native WebView can attach an Authorization header to its FIRST request
    # only — every sub-resource (script/css/XHR/websocket) an embedded app
    # loads goes out headerless and would 401 against the app proxy. The web
    # dashboard doesn't have this problem because oauth2-proxy's session
    # cookie rides on every request. These sessions give the Bearer-token
    # client the same property: one Bearer-authenticated mint request sets a
    # short-lived, HMAC-signed cookie that check_app_proxy_auth() accepts —
    # for /api/apps + /api/app-proxy/* ONLY, never the general API. The HMAC
    # is keyed off the stored Bearer token, so regenerating the token also
    # invalidates every outstanding app session. Stateless: nothing to store
    # or clean up.
    APP_SESSION_TTL_SECONDS = 12 * 3600

    @staticmethod
    def _app_session_sig(expiry_ts):
        """HMAC for an app session, or None when no Bearer token exists yet
        (nothing to key off — mint is Bearer-gated, so this only happens for
        verify, which must then reject)."""
        if not os.path.exists(ClaudeTaskManager.TOKEN_FILE):
            return None
        with open(ClaudeTaskManager.TOKEN_FILE, 'r') as f:
            key = f.read().strip().encode('utf-8')
        if not key:
            return None
        msg = f'app-session:{expiry_ts}'.encode('utf-8')
        return hmac.new(key, msg, hashlib.sha256).hexdigest()

    @staticmethod
    def mint_app_session():
        """-> 'expiry.sig' cookie value, valid for APP_SESSION_TTL_SECONDS."""
        expiry = int(time.time()) + ClaudeTaskManager.APP_SESSION_TTL_SECONDS
        sig = ClaudeTaskManager._app_session_sig(expiry)
        if sig is None:
            # Bearer auth passed, so a token exists unless AUTH_MODE=none —
            # where the proxy is open anyway and the cookie value is inert.
            return f'{expiry}.none'
        return f'{expiry}.{sig}'

    @staticmethod
    def verify_app_session(value):
        try:
            expiry_s, sig = value.split('.', 1)
            expiry = int(expiry_s)
        except (ValueError, AttributeError):
            return False
        if expiry < time.time():
            return False
        expect = ClaudeTaskManager._app_session_sig(expiry)
        return expect is not None and hmac.compare_digest(sig, expect)

    @staticmethod
    def regenerate_token():
        ClaudeTaskManager.ensure_tasks_dir()
        token = secrets.token_urlsafe(36)
        with open(ClaudeTaskManager.TOKEN_FILE, 'w') as f:
            f.write(token)
        os.chmod(ClaudeTaskManager.TOKEN_FILE, 0o600)
        return token

    # ── Assistant selection ──────────────────────────────────────────────
    # Assistant options surfaced in the dashboard dropdown:
    #   1. Claude Code   — always available (anthropic-hosted)
    #   2. Ante CLI      — always available (pre-installed in the image)
    #   3. Antigravity   — `agy` CLI; listed when its binary is present (OAuth login)
    #   4. LibreFang     — agent-OS CLI; listed when its binary is present
    #   5. OpenRouter    — OpenCode CLI proxied through OpenRouter
    #   6. DeepSeek      — OpenCode CLI against DeepSeek's native API
    #   7. Opensource GPU — kc-harness against the configured Ollama endpoint
    # The legacy `opencode-fallback` assistant was retired in favour of
    # kc-harness: same endpoint, narrow tool surface, XML-aware parser, so
    # small local models actually execute tools instead of describing them.
    ASSISTANTS = {
        'claude': {
            'id': 'claude',
            'label': 'Claude Code',
        },
        'ante': {
            'id': 'ante',
            'label': 'Ante CLI',
        },
        # Antigravity — Google's `agy` CLI, pre-installed in the image. OAuth
        # login (no API key), so it's listed whenever its binary is resolvable.
        'antigravity': {
            'id': 'antigravity',
            'label': 'Antigravity',
        },
        # Codex — OpenAI's `codex` CLI, pre-installed in the image. ChatGPT
        # OAuth login (no API key: `codex login` once in the pod), so it's
        # listed whenever its binary is resolvable — same signal as Antigravity.
        'codex': {
            'id': 'codex',
            'label': 'Codex',
        },
        # LibreFang — open-source agent OS (https://librefang.ai). Tasks talk
        # to its registry-bundled "coder" agent via `librefang chat`; the CLI
        # picks up whatever provider key is in the environment
        # (ANTHROPIC_API_KEY, OPENROUTER_API_KEY, …).
        'librefang': {
            'id': 'librefang',
            'label': 'LibreFang',
        },
        'opencode-openrouter': {
            'id': 'opencode-openrouter',
            'label': 'OpenRouter',
        },
        'opencode-deepseek': {
            'id': 'opencode-deepseek',
            'label': 'DeepSeek',
        },
        # kc-harness — thin in-pod LLM tool-call loop at /tmp/browser/harness.py
        # See charts/workspace/harness.py for the design rationale.
        'kc-harness': {
            'id': 'kc-harness',
            'label': 'Opensource GPU',
        },
    }

    @staticmethod
    def available_assistants():
        out = [dict(ClaudeTaskManager.ASSISTANTS['claude'], default=True)]
        out.append(dict(ClaudeTaskManager.ASSISTANTS['ante']))
        # Antigravity — listed only when its `agy` CLI is actually resolvable
        # (older images predate it; /usr/local/bin/agy is a symlink to a PVC path
        # start.sh seeds). Auth is OAuth (`agy` login once in the pod), so there's
        # no key to gate on — binary presence is the right signal.
        if shutil.which('agy'):
            out.append(dict(
                ClaudeTaskManager.ASSISTANTS['antigravity'],
                model=os.environ.get('KC_ANTIGRAVITY_MODEL', ''),
            ))
        # Codex — listed only when its CLI is resolvable (older images predate
        # it). Auth is ChatGPT OAuth (`codex login` once in the pod), so binary
        # presence is the right signal — same as Antigravity. Optional model via
        # KC_CODEX_MODEL (codex picks its own default otherwise).
        if shutil.which('codex'):
            out.append(dict(
                ClaudeTaskManager.ASSISTANTS['codex'],
                model=os.environ.get('KC_CODEX_MODEL', ''),
            ))
        # LibreFang — listed only when its CLI is actually resolvable (older
        # images predate it, and /usr/local/bin/librefang is a symlink to a
        # PVC path that start.sh seeds), so the dropdown never advertises a
        # dead option.
        if shutil.which('librefang'):
            out.append(dict(ClaudeTaskManager.ASSISTANTS['librefang']))
        if os.environ.get('OPENROUTER_API_KEY'):
            out.append(dict(
                ClaudeTaskManager.ASSISTANTS['opencode-openrouter'],
                model=os.environ.get('KC_OPENROUTER_MODEL', 'anthropic/claude-sonnet-4'),
            ))
        if os.environ.get('DEEPSEEK_API_KEY'):
            out.append(dict(
                ClaudeTaskManager.ASSISTANTS['opencode-deepseek'],
                model=os.environ.get('KC_DEEPSEEK_MODEL', 'deepseek-chat'),
            ))
        if os.environ.get('KC_FALLBACK_BASE_URL'):
            out.append(dict(
                ClaudeTaskManager.ASSISTANTS['kc-harness'],
                model=os.environ.get('KC_HARNESS_MODEL')
                      or os.environ.get('KC_FALLBACK_MODEL', 'qwen3:32b-q4_K_M'),
            ))
        # Selectable models for the Hypervisor's in-chat model switcher (#308).
        # Only assistants whose adapter honours a per-thread `model` populate a
        # non-empty list; the frontend shows the switcher only then. First entry
        # is the default.
        for a in out:
            a['models'] = ClaudeTaskManager.available_models(a['id'])
        return out

    # Models the in-chat switcher offers per assistant (issue #308). An assistant
    # appears here only when its adapter threads a per-thread `--model`:
    # claude (aliases; `default` means "let the CLI pick" → the adapter omits
    # --model), and the two opencode providers (native model ids; the adapter
    # keeps the opencode provider prefix and swaps the model). Every list is
    # overridable per assistant via a comma-separated env var (below), so an
    # operator can curate exactly what their deployment offers. Codex/Antigravity
    # honour ctx['model'] too but ship no default list — their ids move fast, so
    # they only get a switcher when the operator sets the env var.
    #
    # Fable 5 (#361) rides as the full model id — Claude Code has no short
    # alias for it — and sits last so it can never become the fallback default
    # (resolve_model falls back to the first entry): it's priced above Opus
    # tier ($10/$50 per MTok) and behaves differently at the API (always-on
    # thinking, safety-classifier refusals, 30-day data-retention requirement),
    # so it must stay an explicit per-thread opt-in. Those API differences are
    # handled inside the Claude Code CLI itself; this layer only passes
    # `--model claude-fable-5` through.
    _CLAUDE_MODELS = ('default', 'opus', 'sonnet', 'haiku', 'claude-fable-5')

    # assistant id → env var that (when set) fully replaces its model list.
    _MODEL_LIST_ENV = {
        'claude': 'KC_CLAUDE_MODELS',
        'opencode-openrouter': 'KC_OPENROUTER_MODELS',
        'opencode-deepseek': 'KC_DEEPSEEK_MODELS',
        'codex': 'KC_CODEX_MODELS',
        'antigravity': 'KC_ANTIGRAVITY_MODELS',
    }

    @staticmethod
    def _builtin_models(assistant_id):
        """The default model list for an assistant, before any env override.
        Free/cheap options are surfaced but the assistant's existing configured
        default stays first so behaviour doesn't silently change (#308)."""
        if assistant_id == 'claude':
            return list(ClaudeTaskManager._CLAUDE_MODELS)
        if assistant_id == 'opencode-openrouter':
            # First = the workspace's configured default (unchanged behaviour);
            # then the free DeepSeek chat model so it's one tap away. These are
            # OpenRouter model ids; the opencode adapter prepends `openrouter/`.
            default = os.environ.get('KC_OPENROUTER_MODEL', 'anthropic/claude-sonnet-4')
            return _dedup_keep_order(
                [default, 'deepseek/deepseek-chat-v3-0324:free'])
        if assistant_id == 'opencode-deepseek':
            # Native DeepSeek API ids; the opencode adapter prepends `deepseek/`.
            default = os.environ.get('KC_DEEPSEEK_MODEL', 'deepseek-chat')
            return _dedup_keep_order([default, 'deepseek-chat', 'deepseek-reasoner'])
        return []

    @staticmethod
    def available_models(assistant_id):
        """Selectable model ids for `assistant_id`, default first. Empty when the
        assistant has no in-chat model choice (the switcher stays hidden). An env
        override (see _MODEL_LIST_ENV) fully replaces the built-in list."""
        env_key = ClaudeTaskManager._MODEL_LIST_ENV.get(assistant_id)
        if env_key:
            override = (os.environ.get(env_key) or '').strip()
            if override:
                return [m.strip() for m in override.split(',') if m.strip()]
        return ClaudeTaskManager._builtin_models(assistant_id)

    @staticmethod
    def resolve_model(assistant, requested):
        """Validate a requested model against the assistant's allow-list; fall
        back to the assistant's default (first entry). Returns '' when the
        assistant offers no model choice — the dashboard hides the switcher, but
        webhooks/CLI callers are free-form so we defend the boundary."""
        models = ClaudeTaskManager.available_models(assistant)
        if not models:
            return ''
        if requested and requested in models:
            return requested
        return models[0]

    @staticmethod
    def resolve_assistant(requested):
        """Validate the caller's choice; fall back to claude on anything
        unknown or disabled (the dashboard hides disabled options, but
        webhooks/crons/CLI clients are free-form so we defend the boundary)."""
        enabled = {a['id'] for a in ClaudeTaskManager.available_assistants()}
        if requested and requested in enabled:
            return requested
        return 'claude'

    # Unattended task sources — no human is watching the live terminal, so the
    # CLI must launch in auto-approve/skip-permissions mode or it stalls on the
    # API-key dialog and per-tool permission prompts (issue #296). The
    # interactive Build tab (source null / 'manual') is deliberately excluded:
    # a person watches that terminal and answers its prompts.
    _UNATTENDED_SOURCE_PREFIXES = ('webhook:', 'cron:', 'desktop:')
    _UNATTENDED_SOURCES = ('hypervisor-tool',)

    @staticmethod
    def _is_unattended_source(source):
        if not source:
            return False
        s = str(source)
        return (s in ClaudeTaskManager._UNATTENDED_SOURCES
                or s.startswith(ClaudeTaskManager._UNATTENDED_SOURCE_PREFIXES))

    @staticmethod
    def resolve_auto_approve(source, explicit=None):
        """Decide whether a new task launches its CLI with skip-permissions.

        An explicit request (the body's `auto_approve` flag) always wins, in
        both directions. When it is absent (None), fall back to the source
        default: unattended sources (hypervisor-tool, webhook:*, cron:*,
        desktop:*) auto-approve so they don't stall on prompts; the interactive
        Build tab (source null / 'manual') stays off. See issue #296."""
        if explicit is not None:
            return bool(explicit)
        return ClaudeTaskManager._is_unattended_source(source)

    @staticmethod
    def assistant_command(assistant, auto_approve=False):
        # auto_approve launches the interactive REPL with the CLI's
        # skip-permissions flag so it never blocks on an in-terminal approval
        # menu. This is required for surfaces that drive the agent purely by
        # pasting text (the Hypervisor chat, which has no way to answer an
        # arrow/number permission prompt) — mirrors the headless orchestrator's
        # per-CLI skip flags. The Build tab leaves this off so its live terminal
        # keeps prompting for approval as before. Only claude/ante/antigravity
        # expose a REPL-compatible skip flag; other CLIs are launched unchanged.
        if assistant == 'ante':
            return 'ante --yolo' if auto_approve else 'ante'
        if assistant == 'antigravity':
            # Interactive Antigravity (agy) REPL for the dashboard pane. Optional
            # model via KC_ANTIGRAVITY_MODEL (agy picks a sensible default
            # otherwise); quoted so a hostile env var can't break out of the
            # `bash -lc` shell_cmd built downstream in create_task().
            model = os.environ.get('KC_ANTIGRAVITY_MODEL', '')
            skip = '--dangerously-skip-permissions ' if auto_approve else ''
            model_flag = f'--model {_shell_quote(model)}' if model else ''
            return f'agy {skip}{model_flag}'.strip()
        if assistant == 'codex':
            # Interactive Codex TUI for the dashboard pane. Optional model via
            # KC_CODEX_MODEL (codex picks its own default otherwise); quoted so a
            # hostile env var can't break out of the `bash -lc` shell_cmd built
            # downstream in create_task(). The pod is externally sandboxed (k8s),
            # so auto_approve uses the bypass flag documented for exactly that.
            model = os.environ.get('KC_CODEX_MODEL', '')
            skip = '--dangerously-bypass-approvals-and-sandbox ' if auto_approve else ''
            model_flag = f'--model {_shell_quote(model)}' if model else ''
            return f'codex {skip}{model_flag}'.strip()
        if assistant == 'librefang':
            # Interactive chat REPL with the registry's "coder" agent (synced
            # into ~/.librefang by `librefang init`). KC_LIBREFANG_AGENT
            # overrides the agent name for users who ship their own manifest.
            # Quoted so a hostile env var can't break out of the `bash -lc`
            # shell_cmd built downstream in create_task().
            #
            # `librefang chat` needs the kernel daemon running — without it the
            # CLI panics ("there is no reactor running") and the tmux session
            # exits instantly. `librefang start` self-daemonizes and is a no-op
            # when already up; poll status briefly so the REPL doesn't attach
            # before the daemon's API binds. Mirrors the headless bootstrap in
            # mcp_agent_orchestrator.py.
            agent = _shell_quote(os.environ.get('KC_LIBREFANG_AGENT', 'coder'))
            return (
                'librefang status -q >/dev/null 2>&1 || { '
                'librefang start >/dev/null 2>&1 || true; '
                'for _ in 1 2 3 4 5 6 7 8 9 10; do '
                'librefang status -q >/dev/null 2>&1 && break; sleep 1; '
                'done; }; '
                f'librefang chat {agent}'
            )
        if assistant == 'opencode-openrouter':
            model = os.environ.get('KC_OPENROUTER_MODEL', 'anthropic/claude-sonnet-4')
            # Quote the model so a hostile env var can't break out of the
            # `bash -lc` shell_cmd built downstream in create_task().
            return f'opencode --model {_shell_quote(f"openrouter/{model}")}'
        if assistant == 'opencode-deepseek':
            model = os.environ.get('KC_DEEPSEEK_MODEL', 'deepseek-chat')
            return f'opencode --model {_shell_quote(f"deepseek/{model}")}'
        if assistant == 'kc-harness':
            # Reads stdin (tmux paste) and emits dashboard JSONL events.
            # KC_HARNESS_MODEL / KC_FALLBACK_MODEL pick the model; the
            # default lives in harness.py.
            return 'python3 /tmp/browser/harness.py'
        return 'claude --dangerously-skip-permissions' if auto_approve else 'claude'

    # Soft ceiling on concurrently-live tasks created through this manager
    # (dashboard / desktop / webhook / cron). Protects a small 2-3 CPU pod from
    # a webhook/cron storm — or a buggy POST loop — spawning unbounded tmux
    # sessions. The MCP orchestrator enforces its own KC_MAX_SUBAGENTS cap for
    # spawned sub-agents; this is the HTTP-create equivalent (issue #98).
    MAX_TASKS = int(os.environ.get('KC_MAX_TASKS', '12'))

    @staticmethod
    def count_live_tasks():
        """Count live tmux sessions for dashboard tasks (kube-coder-*)."""
        r = subprocess.run(
            ['tmux', 'list-sessions', '-F', '#{session_name}'],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return 0
        return sum(1 for n in r.stdout.splitlines() if n.startswith('kube-coder-'))

    @staticmethod
    def at_capacity():
        """(at_cap, live, max) — whether the live-task ceiling is reached."""
        live = ClaudeTaskManager.count_live_tasks()
        return live >= ClaudeTaskManager.MAX_TASKS, live, ClaudeTaskManager.MAX_TASKS

    @staticmethod
    def _capacity_rejection():
        _, live, cap = ClaudeTaskManager.at_capacity()
        return {
            'status': 'rejected',
            'task_id': None,
            'error': f'concurrent task limit reached ({live}/{cap}); '
                     'wait for a task to finish or raise KC_MAX_TASKS',
        }

    @staticmethod
    def create_task(prompt, workdir=None, response_url=None, response_secret=None,
                    source=None, disable_memory_injection=False, assistant=None,
                    parent_task_id=None, system_preamble=None, auto_approve=False):
        at_cap, _, _ = ClaudeTaskManager.at_capacity()
        if at_cap:
            return ClaudeTaskManager._capacity_rejection()
        ClaudeTaskManager.ensure_tasks_dir()
        assistant = ClaudeTaskManager.resolve_assistant(assistant)
        task_id = f"{int(time.time())}-{secrets.token_hex(4)}"
        session_id = str(uuid.uuid4())
        task_dir = os.path.join(ClaudeTaskManager.TASKS_DIR, task_id)
        os.makedirs(task_dir, mode=0o700)

        if workdir is None:
            workdir = '/home/dev'

        session_name = f'kube-coder-{task_id}'

        # ── Memory auto-injection (opt-in, OFF by default) ────────────────
        # Optionally compute a <workspace_memories> block from top-K relevant
        # memories and prepend it to the pasted prompt. This is now OFF by
        # default: it front-loaded a large block of memories into every new
        # session (especially noisy for Ante), and the agent can pull
        # memories on demand via the memory MCP tools — which CLAUDE.md
        # already documents. Set KC_MEMORY_PREINJECT=1 to restore the old
        # prepend behavior. `disable_memory_injection` still force-disables.
        _preinject = os.environ.get('KC_MEMORY_PREINJECT', '').strip().lower() \
            in ('1', 'true', 'yes', 'on')
        injected_memories = []
        injection_block = ''
        if _MEMORY_AVAILABLE and _preinject and not disable_memory_injection:
            try:
                injected_memories = MemoryManager.top_for_prompt(prompt or '')
                injection_block = MemoryManager.format_injection_block(injected_memories)
            except Exception as e:  # never fail task creation on memory errors
                print(f'[memory] auto-inject failed: {e}', file=sys.stderr)
                injected_memories = []
                injection_block = ''

        meta = {
            'task_id': task_id,
            'session_id': session_id,
            'prompt': prompt,
            'workdir': workdir,
            'status': 'running',
            'created_at': time.time(),
            'tmux_session': session_name,
            'assistant': assistant,
            'parent_task_id': parent_task_id,
            'sub_task_ids': [],
            'memory_injected': [
                {'namespace': m.get('namespace'), 'key': m.get('key')}
                for m in injected_memories
            ],
        }
        if disable_memory_injection:
            meta['memory_injection_disabled'] = True
        # Optional completion-hook fields. When response_url is set, the server
        # POSTs the final task state (status + tail output) to that URL once the
        # task reaches a terminal state. response_secret, if present, is used to
        # HMAC-SHA256-sign the body (X-Kube-Coder-Signature-256: sha256=...).
        # `source` is a free-form string ('webhook:<id>', 'cron:<id>', etc.)
        # used by the dashboard to badge triggered tasks.
        if response_url:
            meta['response_url'] = response_url
        if response_secret:
            meta['response_secret'] = response_secret
        if source:
            meta['source'] = source

        meta_path = os.path.join(task_dir, 'task.json')
        with open(meta_path, 'w') as f:
            json.dump(meta, f, indent=2)

        # Write prompt to a file so we can paste it cleanly via tmux. We
        # prepend the memory-injection block here so the model sees prior
        # context before the user's actual request.
        # system_preamble (e.g. the Hypervisor's role/context note) is pasted
        # ahead of the user's text but deliberately NOT stored in meta['prompt'],
        # so it never pollutes the task title / list.
        prompt_file = os.path.join(task_dir, 'prompt.txt')
        with open(prompt_file, 'w') as f:
            f.write(injection_block)
            if system_preamble:
                f.write(system_preamble)
            f.write(prompt)

        # Log read-access for every auto-injected memory (best-effort).
        if injected_memories and _MEMORY_AVAILABLE:
            for m in injected_memories:
                try:
                    MemoryManager.log_ref(
                        namespace=m['namespace'], key=m['key'],
                        ref_kind='task', ref_id=task_id, access_kind='read',
                    )
                except Exception:
                    pass

        # Launch the interactive assistant CLI in a tmux session. We export
        # KC_TASK_ID into the session env so the MCP memory server (spawned
        # by the assistant) can attribute writes to this task. The CLI is
        # chosen per task: Claude Code by default; OpenCode (via OpenRouter
        # or a custom fallback endpoint) when those providers are configured
        # on the workspace and the caller passes the matching `assistant`
        # value. See ClaudeTaskManager.assistant_command().
        # Pre-accept Claude's folder-trust dialog for this workdir, and
        # pre-answer No to the "Do you want to use this API key?" dialog a
        # pod-env ANTHROPIC_API_KEY triggers alongside a subscription login,
        # so the auto-pasted initial prompt below isn't swallowed by either
        # (see _ensure_claude_trust / _api_key_to_reject, #375). Only
        # relevant for the Claude CLI.
        if assistant == 'claude':
            ClaudeTaskManager._ensure_claude_trust(
                workdir,
                reject_api_key=ClaudeTaskManager._api_key_to_reject())

        cli_cmd = ClaudeTaskManager.assistant_command(assistant, auto_approve=auto_approve)
        shell_cmd = f'cd {_shell_quote(workdir)} && {cli_cmd}'
        # Overlay any user-set provider keys onto the new session's env, so a key
        # set in Settings (no redeploy) reaches the CLI subprocess. Store wins
        # over the pod env; when unset the pod/helm default carries through.
        provider_env = []
        for k, v in ProviderKeysManager.env_overlay().items():
            provider_env += ['-e', f'{k}={v}']
        tmux_cmd = [
            'tmux', 'new-session', '-d',
            '-s', session_name,
            '-x', '220', '-y', '50',
            '-e', f'KC_TASK_ID={task_id}',
            *provider_env,
            'bash', '-lc', shell_cmd,
        ]

        result = subprocess.run(tmux_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            meta['status'] = 'error'
            meta['error'] = result.stderr.strip()
            with open(meta_path, 'w') as f:
                json.dump(meta, f, indent=2)
            return meta

        # Mirror the tmux pane output to a log file so it survives session/pod restarts.
        # `pipe-pane -o` toggles output piping; the appended `cat >> ...` keeps writing
        # for the lifetime of the session.
        output_log = os.path.join(task_dir, 'output.log')
        subprocess.run(
            ['tmux', 'pipe-pane', '-o', '-t', session_name,
             f'cat >> {_shell_quote(output_log)}'],
            capture_output=True, text=True,
        )

        # Send the initial prompt to the interactive claude session after it starts
        # Use tmux load-buffer + paste-buffer for clean multi-line handling
        def send_prompt():
            # Wait for the assistant's TUI to finish drawing before pasting,
            # rather than a blind fixed delay. Pasting into a half-drawn screen
            # (banner, MCP download, or a leftover dialog) drops the prompt. For
            # Claude we insist on a real composer-ready signal (issue #288)
            # rather than mere screen stability, which its staggered startup
            # notices trip falsely.
            try:
                ClaudeTaskManager._wait_for_pane_ready(
                    session_name, expect_composer=(assistant == 'claude'))
                delivered = ClaudeTaskManager._deliver_prompt(
                    session_name, prompt_file, f'prompt-{task_id}')
            except Exception as e:
                print(f"[ClaudeTaskManager] Failed to send prompt: {e}")
                delivered = False
            # Surface delivery so the dashboard can flag an idle task whose
            # initial prompt never landed (composer stayed empty).
            try:
                ClaudeTaskManager._atomic_update_meta(
                    task_dir, lambda m: m.__setitem__('prompt_delivered', delivered))
            except Exception as e:
                print(f"[ClaudeTaskManager] prompt_delivered update failed: {e}",
                      file=sys.stderr)

        threading.Thread(target=send_prompt, daemon=True).start()

        EventBroker.publish('task.created', {
            'task_id': meta.get('task_id'),
            'status': meta.get('status'),
            'name': meta.get('name'),
            'assistant': meta.get('assistant'),
            'parent_task_id': meta.get('parent_task_id'),
        })
        # Record this child on its parent so the Subagents tab / list-by-parent
        # reflect API-created lineage (not just MCP-orchestrator-spawned ones).
        ClaudeTaskManager._append_sub_task_id(parent_task_id, task_id)
        return meta

    @staticmethod
    def _append_sub_task_id(parent_task_id, child_task_id):
        """Append a child task id to its parent's sub_task_ids (best-effort).

        Mirrors mcp_agent_orchestrator._append_sub_task_id but uses the meta
        file lock (_atomic_update_meta) so concurrent creates can't clobber the
        list. No-op if there's no parent or the parent task is gone (issue #111).
        """
        if not parent_task_id:
            return
        parent_dir = os.path.join(ClaudeTaskManager.TASKS_DIR, parent_task_id)
        if not os.path.isfile(os.path.join(parent_dir, 'task.json')):
            return

        def mutate(m):
            subs = m.get('sub_task_ids') or []
            if child_task_id not in subs:
                subs.append(child_task_id)
                m['sub_task_ids'] = subs

        try:
            ClaudeTaskManager._atomic_update_meta(parent_dir, mutate)
        except Exception as e:
            print(f'[ClaudeTaskManager] sub_task_id append failed: {e}',
                  file=sys.stderr)

    @staticmethod
    def create_terminal_task(workdir=None):
        """Create a task that runs an interactive bash session under tmux.

        Mirrors create_task() but skips launching claude and pasting a prompt —
        useful so the dashboard's Terminal button leaves a row in the task
        list that can be re-attached later, even if the original browser tab
        is closed.
        """
        at_cap, _, _ = ClaudeTaskManager.at_capacity()
        if at_cap:
            return ClaudeTaskManager._capacity_rejection()
        ClaudeTaskManager.ensure_tasks_dir()
        task_id = f"{int(time.time())}-{secrets.token_hex(4)}"
        session_id = str(uuid.uuid4())
        task_dir = os.path.join(ClaudeTaskManager.TASKS_DIR, task_id)
        os.makedirs(task_dir, mode=0o700)

        if workdir is None:
            workdir = '/home/dev'

        session_name = f'kube-coder-{task_id}'

        meta = {
            'task_id': task_id,
            'session_id': session_id,
            'kind': 'terminal',
            'prompt': f'Terminal · {workdir}',
            'workdir': workdir,
            'status': 'running',
            'created_at': time.time(),
            'tmux_session': session_name,
        }

        meta_path = os.path.join(task_dir, 'task.json')
        with open(meta_path, 'w') as f:
            json.dump(meta, f, indent=2)

        shell_cmd = f'cd {_shell_quote(workdir)} && exec bash -l'
        tmux_cmd = [
            'tmux', 'new-session', '-d',
            '-s', session_name,
            '-x', '220', '-y', '50',
            'bash', '-lc', shell_cmd,
        ]
        result = subprocess.run(tmux_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            meta['status'] = 'error'
            meta['error'] = result.stderr.strip()
            with open(meta_path, 'w') as f:
                json.dump(meta, f, indent=2)
            return meta

        output_log = os.path.join(task_dir, 'output.log')
        subprocess.run(
            ['tmux', 'pipe-pane', '-o', '-t', session_name,
             f'cat >> {_shell_quote(output_log)}'],
            capture_output=True, text=True,
        )

        EventBroker.publish('task.created', {
            'task_id': meta.get('task_id'),
            'status': meta.get('status'),
            'name': meta.get('name'),
            'kind': meta.get('kind'),
        })
        return meta

    @staticmethod
    def list_tasks(parent=None):
        ClaudeTaskManager.ensure_tasks_dir()
        tasks = []
        try:
            entries = sorted(os.listdir(ClaudeTaskManager.TASKS_DIR), reverse=True)
        except OSError:
            return tasks

        for entry in entries:
            task_dir = os.path.join(ClaudeTaskManager.TASKS_DIR, entry)
            meta_path = os.path.join(task_dir, 'task.json')
            if not os.path.isfile(meta_path):
                continue
            try:
                with open(meta_path, 'r') as f:
                    meta = json.load(f)
                ClaudeTaskManager._reconcile_status(meta, task_dir)

                # Filter by parent_task_id when requested
                task_parent = meta.get('parent_task_id')
                if parent is not None and task_parent != parent:
                    continue

                tasks.append({
                    'task_id': meta.get('task_id', entry),
                    'name': meta.get('name'),
                    'prompt': meta.get('prompt', '')[:120],
                    'status': meta.get('status', 'unknown'),
                    'created_at': meta.get('created_at'),
                    'finished_at': meta.get('finished_at') or meta.get('killed_at'),
                    # Moment the rendered screen last changed — drives the
                    # dashboard's idle-duration label + stale escalation.
                    'last_activity_at': meta.get('last_activity_at'),
                    'source': meta.get('source'),
                    'kind': meta.get('kind', 'claude'),
                    'assistant': meta.get('assistant'),
                    'parent_task_id': task_parent,
                    'sub_task_ids': meta.get('sub_task_ids', []),
                    'memory_injected': meta.get('memory_injected', []),
                    'memory_injection_disabled':
                        bool(meta.get('memory_injection_disabled')),
                })
            except (json.JSONDecodeError, OSError):
                continue
        return tasks

    @staticmethod
    def reconcile_running(max_tasks=1000):
        """Reconcile every non-terminal task once; return the count touched.

        This is what the background TaskReconciler calls so a finished task's
        completion hook fires (and finished_at / waiting-for-input update) even
        when no client is reading it. Without it, _reconcile_status only runs
        lazily on list/get/stream, so a headless webhook/cron callback can be
        arbitrarily delayed — or never fire if nothing polls (issue #96).

        Best-effort: a bad task dir is skipped, never raised. Terminal tasks
        are skipped cheaply (no tmux subprocess).
        """
        ClaudeTaskManager.ensure_tasks_dir()
        try:
            entries = sorted(os.listdir(ClaudeTaskManager.TASKS_DIR), reverse=True)
        except OSError:
            return 0
        reconciled = 0
        for entry in entries[:max_tasks]:
            task_dir = os.path.join(ClaudeTaskManager.TASKS_DIR, entry)
            meta_path = os.path.join(task_dir, 'task.json')
            if not os.path.isfile(meta_path):
                continue
            try:
                with open(meta_path, 'r') as f:
                    meta = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            # Cheap skip for terminal tasks — avoids the tmux has-session call.
            if meta.get('status') not in ('running', 'waiting-for-input'):
                continue
            try:
                ClaudeTaskManager._reconcile_status(meta, task_dir)
                reconciled += 1
            except Exception as e:
                print(f'[task-reconciler] reconcile {entry} failed: {e}',
                      file=sys.stderr)
        return reconciled

    @staticmethod
    def get_task(task_id):
        task_dir = os.path.join(ClaudeTaskManager.TASKS_DIR, task_id)
        meta_path = os.path.join(task_dir, 'task.json')
        if not os.path.isfile(meta_path):
            return None
        with open(meta_path, 'r') as f:
            meta = json.load(f)
        ClaudeTaskManager._reconcile_status(meta, task_dir)

        # Get recent output from live tmux pane or fallback to log file
        recent_output = ''
        session_name = meta.get('tmux_session', f'kube-coder-{task_id}')
        result = subprocess.run(
            ['tmux', 'capture-pane', '-J', '-t', session_name, '-p', '-S', '-50'],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            recent_output = result.stdout
        meta['recent_output'] = recent_output
        # Structured interactive prompt (numbered permission menu / yes-no) the
        # dashboard renders as tappable quick-reply buttons (issue #204). Wrapped
        # so a parser hiccup never breaks task-detail; None means "no buttons".
        try:
            meta['pending_prompt'] = parse_screen_prompt(recent_output)
        except Exception:
            meta['pending_prompt'] = None
        return meta

    @staticmethod
    def get_task_output(task_id, tail=None, ansi=False):
        task_dir = os.path.join(ClaudeTaskManager.TASKS_DIR, task_id)
        meta_path = os.path.join(task_dir, 'task.json')
        if not os.path.isfile(meta_path):
            return None

        with open(meta_path, 'r') as f:
            meta = json.load(f)

        # For live sessions, capture the tmux pane content. `-e` preserves the
        # SGR color escape sequences so a client that renders ANSI (the mobile
        # app) gets syntax-highlighted output; without it tmux emits plain text.
        session_name = meta.get('tmux_session', f'kube-coder-{task_id}')
        # -J joins wrapped lines, so URLs the assistant prints that overflow the
        # 220-col pane come back as one logical line — critical for the SPA's
        # URL-detection strip in the Terminal tab.
        cmd = ['tmux', 'capture-pane', '-J', '-t', session_name, '-p', '-S', '-200']
        if ansi:
            cmd.insert(1, '-e')
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            output = result.stdout
            if tail:
                lines = output.split('\n')
                return '\n'.join(lines[-tail:])
            return output

        # Fallback to output.log if session is gone (raw stream — has ANSI; strip
        # unless the caller asked to keep it).
        output_path = os.path.join(task_dir, 'output.log')
        if os.path.exists(output_path):
            with open(output_path, 'r', errors='replace') as f:
                raw = ''.join(f.readlines()[-tail:]) if tail else f.read()
            return raw if ansi else strip_ansi(raw)
        return '(no output available)'

    @staticmethod
    def send_followup(task_id, prompt, submit=True):
        # submit=False pastes the text into the live session's input box WITHOUT
        # pressing Enter — used by the dashboard's "Paste from clipboard" action
        # so a mobile user can drop text in, review it, and submit themselves.
        task_dir = os.path.join(ClaudeTaskManager.TASKS_DIR, task_id)
        meta_path = os.path.join(task_dir, 'task.json')
        if not os.path.isfile(meta_path):
            return None, 'Task not found'

        with open(meta_path, 'r') as f:
            meta = json.load(f)

        session_name = meta.get('tmux_session', f'kube-coder-{task_id}')

        # Check if tmux session is still alive
        check = subprocess.run(
            ['tmux', 'has-session', '-t', session_name],
            capture_output=True, text=True,
        )
        if check.returncode != 0:
            return None, 'Session is no longer running'

        # Send the follow-up prompt into the interactive claude session
        # Use load-buffer + paste-buffer for clean multi-line handling
        prompt_file = os.path.join(task_dir, 'followup.txt')
        with open(prompt_file, 'w') as f:
            f.write(prompt)

        # Reuse the shared paste+verify path (issue #288). The TUI is already
        # settled for a follow-up, but the verify+retry still guards against a
        # dropped paste, and Claude/OpenCode's bracketed-paste Enter-absorption
        # is handled by the helper's nudge (the "it just sets the input" bug).
        buf_name = f'followup-{task_id}'
        delivered = ClaudeTaskManager._deliver_prompt(
            session_name, prompt_file, buf_name, submit=submit)
        if not delivered:
            return None, 'Failed to send follow-up'

        # A paste (no submit) leaves the text sitting in the input box — nothing
        # was actually sent, so don't record a followup or flip status. Return
        # the current meta so the caller still gets a 200.
        if not submit:
            with open(meta_path, 'r') as f:
                return json.load(f), None

        # Update metadata under an exclusive lock so concurrent /message calls
        # don't drop each other's appends to followups[].
        sent_at = time.time()

        def mutate(m):
            m['status'] = 'running'
            fps = m.get('followups', [])
            fps.append({'prompt': prompt, 'sent_at': sent_at})
            m['followups'] = fps

        updated = ClaudeTaskManager._atomic_update_meta(task_dir, mutate)
        return updated, None

    @staticmethod
    def delete_task(task_id):
        task_dir = os.path.join(ClaudeTaskManager.TASKS_DIR, task_id)
        meta_path = os.path.join(task_dir, 'task.json')
        if not os.path.isfile(meta_path):
            return None

        with open(meta_path, 'r') as f:
            meta = json.load(f)

        session_name = meta.get('tmux_session', f'kube-coder-{task_id}')

        # Kill the tmux session if alive
        subprocess.run(
            ['tmux', 'kill-session', '-t', session_name],
            capture_output=True, text=True,
        )

        killed_at = time.time()
        fire_hook = False

        def mutate(m):
            nonlocal fire_hook
            m['status'] = 'killed'
            m['killed_at'] = killed_at
            if m.get('response_url') and not m.get('hook_fired_at'):
                m['hook_fired_at'] = killed_at
                fire_hook = True

        updated = ClaudeTaskManager._atomic_update_meta(task_dir, mutate)
        if updated is not None and fire_hook:
            ClaudeTaskManager._fire_completion_hook(updated)
        return updated

    @staticmethod
    def rename_task(task_id, body):
        """Rename a task. Returns (meta, error).

        Empty/whitespace-only name clears the field. Cap 100 chars after
        stripping control characters.
        """
        if 'name' not in body:
            return None, 'name field required'
        raw = body['name']
        if not isinstance(raw, str):
            return None, 'name must be a string'
        cleaned = ''.join(
            ch for ch in raw if ch == ' ' or ch.isprintable()
        ).strip()
        if len(cleaned) > 100:
            return None, 'name too long (max 100)'

        task_dir = os.path.join(ClaudeTaskManager.TASKS_DIR, task_id)
        if not os.path.isdir(task_dir):
            return None, 'not_found'

        renamed_at = time.time()

        def mutate(m):
            if cleaned:
                m['name'] = cleaned
                m['renamed_at'] = renamed_at
            else:
                m.pop('name', None)
                m.pop('renamed_at', None)

        updated = ClaudeTaskManager._atomic_update_meta(task_dir, mutate)
        if updated is None:
            return None, 'not_found'
        return updated, None

    @staticmethod
    def _atomic_update_meta(task_dir, mutate_fn):
        """Atomically read-modify-write task.json under an exclusive flock.

        mutate_fn(meta) may modify meta in place. Returning False skips the write
        (used when the mutator decides the update is no longer needed after seeing
        fresh state). Returns the post-mutation meta dict, or None if the task
        directory is gone.
        """
        meta_path = os.path.join(task_dir, 'task.json')
        if not os.path.isfile(meta_path):
            return None
        lock_path = os.path.join(task_dir, '.meta.lock')
        with open(lock_path, 'a') as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            try:
                with open(meta_path, 'r') as f:
                    meta = json.load(f)
                should_write = mutate_fn(meta)
                if should_write is False:
                    return meta
                tmp_path = meta_path + '.tmp'
                with open(tmp_path, 'w') as f:
                    json.dump(meta, f, indent=2)
                os.rename(tmp_path, meta_path)
                return meta
            finally:
                fcntl.flock(lockf, fcntl.LOCK_UN)

    @staticmethod
    def _api_key_to_reject():
        """The env ANTHROPIC_API_KEY worth pre-rejecting at launch, or None.

        With a subscription (OAuth) login active, a pod-env ANTHROPIC_API_KEY
        makes Claude Code ask "Do you want to use this API key?" on launch —
        a third startup dialog that swallows the auto-pasted prompt exactly
        like the trust dialog (#375). Subscription is the default auth on
        these workspaces, so the answer is No (using the key would also
        silently shift billing from the subscription to API credits).

        Two cases keep the key: a key the user explicitly pasted in Settings
        (ProviderKeysManager) is an opt-in to API-key auth, and without a
        subscription login the env key may be the only working auth.
        """
        env_key = os.environ.get('ANTHROPIC_API_KEY', '')
        if not env_key:
            return None
        if 'ANTHROPIC_API_KEY' in ProviderKeysManager.env_overlay():
            return None
        if SubscriptionStatusManager._claude_status().get('kind') != 'subscription':
            return None
        return env_key

    @staticmethod
    def _ensure_claude_trust(workdir, config_path=None, reject_api_key=None):
        """Pre-accept Claude Code's folder-trust + onboarding for `workdir`.

        A freshly launched interactive `claude` shows "Do you trust the files
        in this folder?" the first time it runs in a directory. The initial
        prompt is auto-pasted shortly after launch (send_prompt), so without
        this the paste lands in the trust dialog and the following Enter just
        dismisses it — the prompt is silently lost.

        We seed top-level `hasCompletedOnboarding` and
        `projects[workdir].hasTrustDialogAccepted` in ~/.claude.json. When
        `reject_api_key` is given (see _api_key_to_reject) we additionally
        pre-answer No to the "Do you want to use this API key?" dialog:
        Claude Code records answers as the key's LAST 20 CHARACTERS (verified
        against a live config — not a hash) under
        `customApiKeyResponses.{approved,rejected}`; an existing answer in
        either list is the user's and is respected. Idempotent:
        only writes when a value is actually missing, so steady-state launches
        do zero writes and don't race a live Claude rewriting the same file.
        Best-effort — never raises, never clobbers an unreadable/invalid config.

        Returns True if the file was written, False otherwise.
        """
        path = config_path or ClaudeTaskManager.CLAUDE_CONFIG_PATH
        lock_path = path + '.kc.lock'
        try:
            with open(lock_path, 'a') as lockf:
                fcntl.flock(lockf, fcntl.LOCK_EX)
                try:
                    try:
                        with open(path, 'r') as f:
                            cfg = json.load(f)
                    except FileNotFoundError:
                        cfg = {}
                    if not isinstance(cfg, dict):
                        # Don't overwrite a config we don't understand.
                        return False

                    changed = False
                    if cfg.get('hasCompletedOnboarding') is not True:
                        cfg['hasCompletedOnboarding'] = True
                        changed = True
                    projects = cfg.get('projects')
                    if not isinstance(projects, dict):
                        projects = {}
                        cfg['projects'] = projects
                    proj = projects.get(workdir)
                    if not isinstance(proj, dict):
                        proj = {}
                        projects[workdir] = proj
                    if proj.get('hasTrustDialogAccepted') is not True:
                        proj['hasTrustDialogAccepted'] = True
                        changed = True

                    if reject_api_key:
                        tail = reject_api_key[-20:]
                        resp = cfg.get('customApiKeyResponses')
                        if not isinstance(resp, dict):
                            resp = {}
                        approved = resp.get('approved')
                        approved = approved if isinstance(approved, list) else []
                        rejected = resp.get('rejected')
                        rejected = rejected if isinstance(rejected, list) else []
                        if tail not in approved and tail not in rejected:
                            cfg['customApiKeyResponses'] = {
                                **resp,
                                'approved': approved,
                                'rejected': rejected + [tail],
                            }
                            changed = True

                    if not changed:
                        return False
                    tmp = path + '.kc.tmp'
                    with open(tmp, 'w') as f:
                        json.dump(cfg, f, indent=2)
                    os.replace(tmp, path)
                    return True
                finally:
                    fcntl.flock(lockf, fcntl.LOCK_UN)
        except (OSError, json.JSONDecodeError) as e:
            print(f'[ClaudeTaskManager] trust-seed for {workdir} failed: {e}',
                  file=sys.stderr)
            return False

    @staticmethod
    def _capture_pane(session_name):
        """Return the rendered tmux pane text, or None if capture failed."""
        r = subprocess.run(
            ['tmux', 'capture-pane', '-p', '-t', session_name],
            capture_output=True, text=True,
        )
        return r.stdout if r.returncode == 0 else None

    @staticmethod
    def _pane_input_ready(pane):
        """True when the pane shows a live REPL composer ready for input.

        Detects Claude Code / OpenCode's interactive state: the shortcuts
        footer ('for shortcuts') plus an input-prompt affordance (the box
        composer's `> ` / `❯`). This is the signal that the TUI has finished
        its staggered startup paint (banner, promos, plan-limit / auto-update
        notices) and will actually accept a pasted prompt. Screen *stability*
        alone is not enough — a quiet gap between two async notices looks
        settled while the composer still isn't accepting input, which silently
        drops the initial paste (issue #288).
        """
        if not pane:
            return False
        if 'for shortcuts' not in pane:
            return False
        return '> ' in pane or '❯' in pane

    @staticmethod
    def _screen_advanced(before, after):
        """True if the pane visibly changed between two captures.

        Used to confirm a paste actually landed (empty composer → content)
        and that Enter registered as a submit (composer cleared / assistant
        started working). If either capture is unavailable we can't tell, so
        we assume it advanced — better than re-pasting into a session we
        can't observe (which would duplicate the text).
        """
        if before is None or after is None:
            return True
        return after != before

    @staticmethod
    def _deliver_prompt(session_name, prompt_file, buf_name, submit=True,
                        retries=3):
        """Paste a prompt file into a live tmux TUI and verify delivery.

        Shared path for the initial prompt (create_task) and follow-ups
        (send_followup). Loads `prompt_file` into a tmux buffer, pastes it
        into the session's composer, and — when `submit` — presses Enter.

        Claude/OpenCode wrap pasted text in bracketed-paste escapes; a paste
        into a still-initializing TUI is silently *dropped*, and re-sending
        Enter cannot recover a dropped paste (the composer is empty). So we
        verify the paste landed by comparing pane captures; if it didn't, we
        retry the whole load+paste — safe precisely because a dropped paste
        left the composer empty, so there's nothing to duplicate. Once the
        paste lands we send Enter and, if the submit doesn't register (Enter
        absorbed into the bracketed paste), nudge Enter once more.

        Returns True once the prompt is delivered (and submitted, when
        `submit`), else False after exhausting `retries`.
        """
        for _ in range(max(1, retries)):
            try:
                subprocess.run(
                    ['tmux', 'load-buffer', '-b', buf_name, prompt_file],
                    capture_output=True, text=True, check=True,
                )
                before = ClaudeTaskManager._capture_pane(session_name)
                subprocess.run(
                    ['tmux', 'paste-buffer', '-b', buf_name, '-t', session_name],
                    capture_output=True, text=True, check=True,
                )
                # Settle so the bracketed paste is fully ingested before we
                # look (and before Enter — otherwise Enter is absorbed into
                # the paste and the prompt never submits).
                time.sleep(0.4)
                pasted = ClaudeTaskManager._capture_pane(session_name)
            except subprocess.CalledProcessError as e:
                print(f'[ClaudeTaskManager] paste failed: {e}', file=sys.stderr)
                ClaudeTaskManager._delete_buffer(buf_name)
                continue

            if not ClaudeTaskManager._screen_advanced(before, pasted):
                # Paste was dropped (composer unchanged) — retry the whole
                # load+paste. The empty composer means no risk of duplication.
                ClaudeTaskManager._delete_buffer(buf_name)
                continue

            if not submit:
                ClaudeTaskManager._delete_buffer(buf_name)
                return True

            subprocess.run(
                ['tmux', 'send-keys', '-t', session_name, 'Enter'],
                capture_output=True, text=True,
            )
            time.sleep(0.8)
            after = ClaudeTaskManager._capture_pane(session_name)
            if ClaudeTaskManager._screen_advanced(pasted, after):
                ClaudeTaskManager._delete_buffer(buf_name)
                return True
            # Enter likely absorbed into the paste — nudge once more. An
            # extra Enter on an empty input is a harmless no-op.
            subprocess.run(
                ['tmux', 'send-keys', '-t', session_name, 'Enter'],
                capture_output=True, text=True,
            )
            time.sleep(0.6)
            after2 = ClaudeTaskManager._capture_pane(session_name)
            ClaudeTaskManager._delete_buffer(buf_name)
            return ClaudeTaskManager._screen_advanced(pasted, after2)
        return False

    @staticmethod
    def _delete_buffer(buf_name):
        subprocess.run(
            ['tmux', 'delete-buffer', '-b', buf_name],
            capture_output=True, text=True,
        )

    @staticmethod
    def _wait_for_pane_ready(session_name, floor=2.0, ceiling=12.0, interval=0.6,
                             expect_composer=False):
        """Block until the session's TUI is ready for a pasted prompt.

        Prefers a real input-readiness signal — the composer affordance (see
        _pane_input_ready) — over mere screen stability. A freshly spawned CLI
        paints staggered async startup notices (banner, promos, plan-limit /
        auto-update warnings); a quiet gap between them can look 'settled'
        while the composer still isn't accepting input, silently dropping the
        pasted prompt (issue #288). Returns True once the composer is
        detected.

        When `expect_composer` is False (non-Claude UIs without that footer),
        falls back to the old settle heuristic — two identical captures
        `interval` apart — so those launches don't stall. Gives up after
        `ceiling` seconds either way so a perpetually-animating UI still gets
        the prompt (the paste path then verifies + retries delivery).
        Best-effort; safe if capture fails.
        """
        time.sleep(floor)
        deadline = time.time() + max(0.0, ceiling - floor)
        prev = ClaudeTaskManager._capture_pane(session_name)
        if ClaudeTaskManager._pane_input_ready(prev):
            return True
        while time.time() < deadline:
            time.sleep(interval)
            cur = ClaudeTaskManager._capture_pane(session_name)
            if ClaudeTaskManager._pane_input_ready(cur):
                return True
            if not expect_composer and cur is not None and cur == prev:
                return False
            prev = cur
        return False

    @staticmethod
    def _is_safe_response_url(url):
        """Allow only http(s) URLs to public IPs. Reject:
          - non-http(s) schemes (file://, gopher://) — they turn urlopen() into
            an SSRF / local-file primitive
          - hosts that resolve to RFC1918, link-local, loopback or unspecified
            ranges — would let an attacker probe the cloud metadata service
            (169.254.169.254), in-cluster services (10.x), or the workspace
            itself (localhost)
        Set ALLOW_INTERNAL_HOOKS=true to opt back in (single-user / trusted
        deploys that need to POST hook results into the cluster)."""
        if not url or not isinstance(url, str):
            return False
        try:
            parsed = urllib.parse.urlparse(url)
        except (ValueError, TypeError):
            return False
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            return False
        if ALLOW_INTERNAL_HOOKS:
            return True
        host = parsed.hostname or ''
        if not host:
            return False
        # Resolve to *all* addresses; reject if any one is internal. This is a
        # cheap pre-check at task-creation time; the authoritative check (and
        # DNS pinning that closes the TOCTOU / rebinding window) happens at
        # delivery time in _resolve_and_pin. Fail CLOSED: an unresolvable host
        # is rejected rather than deferred to urlopen — deferring lets the name
        # resolve to an internal target at fire time.
        try:
            infos = socket.getaddrinfo(host, None)
        except (socket.gaierror, UnicodeError):
            return False
        if not infos:
            return False
        for info in infos:
            sockaddr = info[4]
            try:
                addr = sockaddr[0]
            except (IndexError, TypeError):
                return False
            if _hook_public_ip(addr) is None:
                return False
        return True

    # Bounded retries for completion-hook delivery (issue #97).
    HOOK_MAX_ATTEMPTS = int(os.environ.get('KC_HOOK_MAX_ATTEMPTS', '4'))

    @staticmethod
    def _build_hook_request(meta):
        """Build (url, body_bytes, headers) for the completion hook, or
        (None, None, None) when the URL is missing/unsafe."""
        url = meta.get('response_url')
        if not ClaudeTaskManager._is_safe_response_url(url):
            return None, None, None
        try:
            tail_output = ClaudeTaskManager.get_task_output(meta.get('task_id', ''), tail=200) or ''
        except Exception:
            tail_output = ''
        payload = {
            'task_id': meta.get('task_id'),
            'status': meta.get('status'),
            'prompt': meta.get('prompt'),
            'workdir': meta.get('workdir'),
            'source': meta.get('source'),
            'created_at': meta.get('created_at'),
            'finished_at': meta.get('finished_at') or meta.get('killed_at'),
            'output': tail_output,
        }
        body = json.dumps(payload).encode('utf-8')
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'kube-coder-completion-hook/1.0',
        }
        secret = meta.get('response_secret')
        if secret:
            sig = hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()
            headers['X-Kube-Coder-Signature-256'] = f'sha256={sig}'
        return url, body, headers

    @staticmethod
    def _record_hook_delivery(task_id, delivery):
        """Persist completion-hook delivery state on the task meta (best-effort)."""
        task_dir = os.path.join(ClaudeTaskManager.TASKS_DIR, task_id)
        if not os.path.isfile(os.path.join(task_dir, 'task.json')):
            return
        try:
            ClaudeTaskManager._atomic_update_meta(
                task_dir, lambda m: m.__setitem__('hook_delivery', delivery))
        except Exception:
            pass

    @staticmethod
    def _resolve_and_pin(host, port):
        """Resolve `host` exactly ONCE and return a single validated IP string
        to connect to. Every returned address (IPv4 and IPv6) must be public,
        or we raise _HookSSRFError — so a rebinding server cannot pass one
        address at check time and serve a different internal one at connect
        time (there is no second lookup). Fails CLOSED on resolution failure.

        When ALLOW_INTERNAL_HOOKS is set we still resolve-and-pin (single
        lookup) but skip the public-only classification — the deliberate,
        documented relaxation for single-user / trusted in-cluster deploys."""
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, UnicodeError) as e:
            raise _HookSSRFError(f'DNS resolution failed for {host!r}: {e}')
        if not infos:
            raise _HookSSRFError(f'no addresses for {host!r}')
        chosen = None
        for info in infos:
            addr = info[4][0]
            if not ALLOW_INTERNAL_HOOKS and _hook_public_ip(addr) is None:
                raise _HookSSRFError(
                    f'{host!r} resolves to non-public address {addr}')
            if chosen is None:
                chosen = addr
        return chosen

    @staticmethod
    def _hook_urlopen(req, timeout=10):
        """urlopen replacement for completion-hook delivery that pins the
        connection to a validated IP and rejects redirects (see
        _resolve_and_pin / _NoRedirectHandler). Keeps the original hostname
        for Host header and TLS SNI. stdlib-only. Raises _HookSSRFError for an
        unsafe/unresolvable/unsupported target; other errors propagate as
        usual so _deliver_hook's retry/dead-letter logic is unchanged."""
        parsed = urllib.parse.urlparse(req.full_url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise _HookSSRFError(f'unsupported hook URL: {req.full_url!r}')
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        pinned = ClaudeTaskManager._resolve_and_pin(host, port)
        if parsed.scheme == 'https':
            pinned_handler = _PinnedHTTPSHandler(pinned)
        else:
            pinned_handler = _PinnedHTTPHandler(pinned)
        # Empty ProxyHandler disables any ambient HTTP(S)_PROXY: a proxy would
        # route around our pinned IP and reopen the SSRF hole.
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirectHandler, pinned_handler)
        resp = opener.open(req, timeout=timeout)
        # Defensive: drain at most HOOK_MAX_RESPONSE_BYTES; the body is unused
        # but we don't want a hostile endpoint streaming us unbounded data.
        try:
            resp.read(HOOK_MAX_RESPONSE_BYTES)
        except Exception:
            pass
        return resp

    @staticmethod
    def _deliver_hook(task_id, url, body, headers, max_attempts=None):
        """POST with bounded exponential-backoff retry; record delivery state.

        On success persists hook_delivery={state:'delivered',...}; on exhaustion
        persists {state:'failed', last_error,...} (the dead-letter the redeliver
        endpoint re-attempts). A permanent 4xx (except 429) is not retried.
        Runs in a daemon thread; never raises.
        """
        attempts = max_attempts or ClaudeTaskManager.HOOK_MAX_ATTEMPTS
        last_err = ''
        for attempt in range(1, attempts + 1):
            try:
                req = urllib.request.Request(url, data=body, headers=headers, method='POST')
                with ClaudeTaskManager._hook_urlopen(req, timeout=10) as resp:
                    status = getattr(resp, 'status', 200)
                ClaudeTaskManager._record_hook_delivery(task_id, {
                    'state': 'delivered', 'attempts': attempt,
                    'status': status, 'delivered_at': time.time(),
                })
                print(f'[completion-hook] task={task_id} -> {url} ({status}) attempt {attempt}')
                return
            except urllib.error.HTTPError as e:
                last_err = f'HTTP {e.code}'
                if 400 <= e.code < 500 and e.code != 429:
                    break  # permanent client error — don't waste attempts
            except Exception as e:
                last_err = f'{type(e).__name__}: {e}'
            if attempt < attempts:
                time.sleep(min(30.0, 0.5 * (2 ** (attempt - 1))))
        ClaudeTaskManager._record_hook_delivery(task_id, {
            'state': 'failed', 'attempts': attempts,
            'last_error': last_err, 'last_attempt_at': time.time(),
        })
        print(f'[completion-hook] task={task_id} -> {url} FAILED after {attempts}: {last_err}',
              file=sys.stderr)

    @staticmethod
    def _fire_completion_hook(meta):
        """Deliver the task's terminal state to meta['response_url'] with
        bounded retries, from a daemon thread.

        Idempotent: callers set meta['hook_fired_at'] under the meta lock before
        invoking this, so duplicate transitions (e.g. concurrent reconciles)
        don't re-send. Delivery is retried with backoff and dead-lettered on
        exhaustion (see _deliver_hook / redeliver_hook). HMAC signing via
        response_secret is unchanged.
        """
        url, body, headers = ClaudeTaskManager._build_hook_request(meta)
        if url is None:
            if meta.get('response_url'):
                print(f'[completion-hook] task={meta.get("task_id")} skipped: '
                      'unsafe URL scheme', file=sys.stderr)
            return
        threading.Thread(
            target=ClaudeTaskManager._deliver_hook,
            args=(meta.get('task_id', '?'), url, body, headers),
            daemon=True,
        ).start()

    @staticmethod
    def redeliver_hook(task_id):
        """Re-attempt a task's completion hook (used by the redeliver endpoint).
        Returns (ok: bool, message: str)."""
        task_dir = os.path.join(ClaudeTaskManager.TASKS_DIR, task_id)
        meta_path = os.path.join(task_dir, 'task.json')
        if not os.path.isfile(meta_path):
            return False, 'task not found'
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError):
            return False, 'task metadata unreadable'
        url, body, headers = ClaudeTaskManager._build_hook_request(meta)
        if url is None:
            return False, 'task has no (valid) response_url'
        threading.Thread(
            target=ClaudeTaskManager._deliver_hook,
            args=(task_id, url, body, headers),
            daemon=True,
        ).start()
        return True, 'redelivery started'

    @staticmethod
    def _reconcile_status(meta, task_dir):
        """If task.json says running but tmux session is gone, update status.
        Also check for waiting-for-input patterns in running tasks."""
        current_status = meta.get('status', 'unknown')
        
        # If task is already finished, no need to check further
        if current_status not in ('running', 'waiting-for-input'):
            return

        session_name = meta.get('tmux_session', '')
        if not session_name:
            return

        check = subprocess.run(
            ['tmux', 'has-session', '-t', session_name],
            capture_output=True, text=True,
        )
        
        # If tmux session is gone, mark as completed
        if check.returncode != 0:
            finished_at = time.time()
            fire_hook = False

            def mutate(m):
                nonlocal fire_hook
                # Re-check inside the lock; another reconcile may have run already.
                if m.get('status') not in ('running', 'waiting-for-input'):
                    return False
                m['status'] = 'completed'
                m['finished_at'] = finished_at
                # Clear waiting state fields
                m.pop('waiting_for_input', None)
                m.pop('last_input_prompt', None)
                # Decide-and-mark-fired atomically. If we mark hook_fired_at here, a
                # concurrent reconciler reading the same task.json will see it and
                # skip firing — so we get at-most-once delivery without needing a
                # second lock acquire.
                if m.get('response_url') and not m.get('hook_fired_at'):
                    m['hook_fired_at'] = finished_at
                    fire_hook = True

            updated = ClaudeTaskManager._atomic_update_meta(task_dir, mutate)
            if updated is not None:
                meta['status'] = updated.get('status', meta.get('status'))
                meta['finished_at'] = updated.get('finished_at', meta.get('finished_at'))
                meta.pop('waiting_for_input', None)
                meta.pop('last_input_prompt', None)
                if fire_hook:
                    ClaudeTaskManager._fire_completion_hook(updated)
                EventBroker.publish('task.status', {
                    'task_id': meta.get('task_id'),
                    'status': 'completed',
                    'finished_at': meta.get('finished_at'),
                })
            return
        
        # Session is alive — derive waiting-for-input from render *quiescence*
        # rather than scraping prompt text (which never worked across the
        # full-screen TUIs Claude/Ante/OpenCode render). While an agent works
        # it streams output / animates a spinner+timer, so the captured screen
        # keeps changing; once it finishes a turn or hits a prompt the screen
        # goes static. Stable for >= IDLE_WAITING_SECONDS ⇒ waiting-for-input.
        # `last_activity_at` (the moment the screen last changed) also lets the
        # dashboard show idle duration and escalate long-idle ("stale") tasks.
        capture_cmd = subprocess.run(
            ['tmux', 'capture-pane', '-t', session_name, '-p'],
            capture_output=True, text=True,
        )
        if capture_cmd.returncode != 0:
            return
        screen = strip_ansi(capture_cmd.stdout or '')
        digest = hashlib.sha1(screen.encode('utf-8', 'replace')).hexdigest()
        now = time.time()

        if digest != meta.get('pane_hash'):
            # Screen changed → activity. Record it, reset the idle clock, and
            # if we had flagged waiting, return to running.
            def mutate(m):
                m['pane_hash'] = digest
                m['last_activity_at'] = now
                if m.get('status') == 'waiting-for-input':
                    m['status'] = 'running'
                    m.pop('waiting_for_input', None)
                    m.pop('last_input_prompt', None)

            updated = ClaudeTaskManager._atomic_update_meta(task_dir, mutate)
            if updated is not None:
                meta['pane_hash'] = digest
                meta['last_activity_at'] = now
                if meta.get('status') == 'waiting-for-input':
                    meta['status'] = 'running'
                    meta.pop('waiting_for_input', None)
                    meta.pop('last_input_prompt', None)
                    EventBroker.publish('task.status', {
                        'task_id': meta.get('task_id'), 'status': 'running',
                    })
            return

        # Screen unchanged since the previous capture.
        if current_status == 'running':
            stable_since = meta.get('last_activity_at') or now
            if now - stable_since >= IDLE_WAITING_SECONDS:
                def mutate(m):
                    if m.get('status') == 'running':  # re-check inside lock
                        m['status'] = 'waiting-for-input'
                        m['waiting_for_input'] = True

                updated = ClaudeTaskManager._atomic_update_meta(task_dir, mutate)
                if updated is not None:
                    meta['status'] = 'waiting-for-input'
                    meta['waiting_for_input'] = True
                    EventBroker.publish('task.status', {
                        'task_id': meta.get('task_id'), 'status': 'waiting-for-input',
                    })


def _shell_quote(s):
    """Quote a string for safe use in a shell command."""
    import shlex
    return shlex.quote(s)


def _dedup_keep_order(items):
    """Drop duplicates and empties, preserving first-seen order. Used to build
    model lists where a configured default is prepended to a curated set and may
    already appear in it (#308)."""
    seen = set()
    out = []
    for it in items:
        it = (it or '').strip()
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


class WorkspaceManager:
    """Lists candidate working directories under /home/dev for the
    new-task picker. Skips hidden tooling dirs (.config, .credentials,
    .claude-tasks, etc.) and obvious non-projects (node_modules, vendor)."""

    HOME_DIR = '/home/dev'
    PROJECT_MARKERS = (
        'package.json', 'pyproject.toml', 'Cargo.toml',
        'go.mod', 'Gemfile', 'Makefile', 'requirements.txt',
    )
    SKIP_NAMES = {'node_modules', 'vendor', 'target', 'dist', 'build', '__pycache__'}

    @staticmethod
    def list_dirs():
        results = []
        try:
            entries = os.listdir(WorkspaceManager.HOME_DIR)
        except OSError:
            return results
        for name in entries:
            if name.startswith('.'):
                continue
            if name in WorkspaceManager.SKIP_NAMES:
                continue
            path = os.path.join(WorkspaceManager.HOME_DIR, name)
            if not os.path.isdir(path):
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0
            is_git = os.path.isdir(os.path.join(path, '.git'))
            has_project_marker = any(
                os.path.exists(os.path.join(path, m))
                for m in WorkspaceManager.PROJECT_MARKERS
            )
            results.append({
                'path': path,
                'label': name,
                'is_git_repo': is_git,
                'is_project': is_git or has_project_marker,
                'mtime': mtime,
            })
        results.sort(key=lambda d: d['mtime'], reverse=True)
        return results


class _ReplayCache:
    """Bounded LRU+TTL set of (webhook_id, body_sha256) keys for replay
    protection. In-memory only — fine for a single-pod workspace; if we ever
    horizontal-scale the IDE pod, this moves to Redis.

    The size cap (default 1024) protects against memory growth under a flood
    of distinct payloads; the TTL (default 5 min) is the replay window. A key
    is rejected if seen before its TTL expires."""

    def __init__(self, capacity=1024, ttl_seconds=300, clock=time.time):
        self._cap = capacity
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        # OrderedDict so we can evict oldest via popitem(last=False).
        self._seen = collections.OrderedDict()

    def check_and_record(self, key):
        """Return True if this key is fresh (record it); False if it's a replay
        within the TTL window."""
        now = self._clock()
        with self._lock:
            # Lazy TTL eviction at the head; OrderedDict is insertion-ordered.
            while self._seen:
                k, ts = next(iter(self._seen.items()))
                if now - ts > self._ttl:
                    self._seen.popitem(last=False)
                else:
                    break
            if key in self._seen:
                # Refresh position to LRU-end so an actively-replayed key stays hot
                # (and continues to be rejected) instead of aging out.
                self._seen.move_to_end(key)
                self._seen[key] = now
                return False
            self._seen[key] = now
            # Size cap — drop oldest after insert
            while len(self._seen) > self._cap:
                self._seen.popitem(last=False)
            return True


class WebhookManager:
    """Inbound HTTP webhooks that spawn Claude tasks.

    A webhook config is a JSON file at /home/dev/.claude-triggers/webhooks/<id>.json:

        {
          "id":               "github-pr-review",
          "prompt_template":  "Review the PR titled '{{ payload.pull_request.title }}'",
          "workdir":          "/home/dev/myproject",
          "interpolate_mode": "attach",     // "attach" (default, safe) or "interpolate"
          "hmac_secret":      "<random>",   // optional but recommended
          "signature_header": "X-Hub-Signature-256",  // header name to verify
          "signature_algo":   "sha256",     // sha256 (default) or sha1
          "response_url":     "https://...", // optional — POST task result back here
          "response_secret":  "...",         // optional HMAC for the response POST
          "created_at":       <epoch>
        }

    The receiver endpoint POST /api/webhooks/<id> is unauthenticated by bearer
    token on purpose — it's meant to be called by external services (GitHub,
    Stripe, Slack, etc.). Auth is via HMAC of the raw body against hmac_secret.
    If hmac_secret is omitted, the webhook is open — only do that for testing.
    """

    WEBHOOKS_DIR = '/home/dev/.claude-triggers/webhooks'
    _ID_RE = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')
    _INTERP_RE = re.compile(r'\{\{\s*payload((?:\.[\w]+)*)\s*\}\}')
    PROVIDERS = ('generic', 'github', 'slack', 'stripe')
    # Module-level so the cache survives across requests (each request gets a
    # fresh handler instance). 5-minute window matches Slack/Stripe convention.
    REPLAY_CACHE = _ReplayCache(capacity=1024, ttl_seconds=300)
    # Tolerated clock skew for providers that sign a timestamp (Slack, Stripe).
    # Matches Slack's documented 5-minute drift allowance.
    TIMESTAMP_TOLERANCE = 300

    @staticmethod
    def ensure_dir():
        os.makedirs(WebhookManager.WEBHOOKS_DIR, mode=0o700, exist_ok=True)

    @staticmethod
    def _config_path(webhook_id):
        return os.path.join(WebhookManager.WEBHOOKS_DIR, f'{webhook_id}.json')

    @staticmethod
    def valid_id(webhook_id):
        return bool(webhook_id) and bool(WebhookManager._ID_RE.match(webhook_id))

    @staticmethod
    def list_webhooks():
        WebhookManager.ensure_dir()
        out = []
        try:
            entries = sorted(os.listdir(WebhookManager.WEBHOOKS_DIR))
        except OSError:
            return out
        for name in entries:
            if not name.endswith('.json'):
                continue
            path = os.path.join(WebhookManager.WEBHOOKS_DIR, name)
            try:
                with open(path) as f:
                    cfg = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            out.append(WebhookManager._public_view(cfg))
        return out

    @staticmethod
    def get_webhook(webhook_id, include_secrets=False):
        if not WebhookManager.valid_id(webhook_id):
            return None
        try:
            with open(WebhookManager._config_path(webhook_id)) as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        return cfg if include_secrets else WebhookManager._public_view(cfg)

    @staticmethod
    def _public_view(cfg):
        """Return a copy of the config safe to expose over the dashboard API:
        secret material is replaced with a boolean indicator so the UI can
        show 'configured' without revealing the value."""
        view = dict(cfg)
        for k in ('hmac_secret', 'response_secret'):
            if view.get(k):
                view[k + '_set'] = True
                view.pop(k)
        # Flag a secret-less webhook so the UI can warn: it's unauthenticated
        # and will reject POSTs (fail-closed) unless KC_ALLOW_UNSIGNED_WEBHOOKS
        # is set. See verify_signature / issue #99.
        view['unsigned'] = not cfg.get('hmac_secret')
        return view

    @staticmethod
    def create_or_update(data, existing_id=None):
        """Validate and persist a webhook config. Returns (cfg, error_str)."""
        WebhookManager.ensure_dir()
        webhook_id = existing_id or data.get('id', '')
        if not WebhookManager.valid_id(webhook_id):
            return None, 'invalid id (1-64 chars, [a-zA-Z0-9_-])'
        prompt_template = (data.get('prompt_template') or '').strip()
        if not prompt_template:
            return None, 'prompt_template is required'

        mode = data.get('interpolate_mode', 'attach')
        if mode not in ('attach', 'interpolate'):
            return None, "interpolate_mode must be 'attach' or 'interpolate'"

        algo = data.get('signature_algo', 'sha256')
        if algo not in ('sha256', 'sha1'):
            return None, "signature_algo must be 'sha256' or 'sha1'"

        provider = data.get('provider', 'generic')
        if provider not in WebhookManager.PROVIDERS:
            return None, f'provider must be one of {WebhookManager.PROVIDERS}'

        response_url = data.get('response_url')
        if response_url and not ClaudeTaskManager._is_safe_response_url(response_url):
            return None, 'response_url must be http(s)'

        # Default signature_header by provider. Users can override, but the
        # defaults match what each platform documents so most setups are
        # zero-config.
        default_header = {
            'github': 'X-Hub-Signature-256',
            'slack': 'X-Slack-Signature',
            'stripe': 'Stripe-Signature',
            'generic': 'X-Hub-Signature-256',
        }[provider]
        cfg = {
            'id': webhook_id,
            'prompt_template': prompt_template,
            'workdir': data.get('workdir') or '/home/dev',
            'interpolate_mode': mode,
            'provider': provider,
            'signature_header': data.get('signature_header') or default_header,
            'signature_algo': algo,
            'created_at': time.time(),
        }
        # Optional secret-bearing fields. Auto-mint hmac_secret on create if
        # caller didn't provide one — never want to silently land an open webhook.
        if data.get('hmac_secret'):
            cfg['hmac_secret'] = data['hmac_secret']
        elif not existing_id:
            cfg['hmac_secret'] = secrets.token_urlsafe(32)
        if data.get('response_url'):
            cfg['response_url'] = data['response_url']
        if data.get('response_secret'):
            cfg['response_secret'] = data['response_secret']

        # Preserve created_at on update
        if existing_id:
            prior = WebhookManager.get_webhook(existing_id, include_secrets=True) or {}
            if prior.get('created_at'):
                cfg['created_at'] = prior['created_at']
            # If caller didn't pass hmac_secret on update, keep the prior one.
            if 'hmac_secret' not in cfg and prior.get('hmac_secret'):
                cfg['hmac_secret'] = prior['hmac_secret']

        path = WebhookManager._config_path(webhook_id)
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(cfg, f, indent=2)
        os.chmod(tmp, 0o600)
        os.rename(tmp, path)
        return cfg, None

    @staticmethod
    def delete(webhook_id):
        if not WebhookManager.valid_id(webhook_id):
            return False
        path = WebhookManager._config_path(webhook_id)
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return False

    @staticmethod
    def _allow_unsigned():
        """Opt-in escape hatch for secret-less ('open') webhooks. Off by
        default so production fails closed; intended only for local/testing."""
        return os.environ.get('KC_ALLOW_UNSIGNED_WEBHOOKS', '').strip().lower() in (
            '1', 'true', 'yes', 'on',
        )

    @staticmethod
    def verify_signature(cfg, raw_body, headers):
        """Provider-aware signature verification.

        ``headers`` accepts either:
          * a dict-like with case-insensitive ``.get(name, default)`` — typically
            ``BaseHTTPRequestHandler.headers``. Required for Slack/Stripe which
            read multiple headers.
          * a plain string, treated as the value of ``cfg['signature_header']``.
            Kept as a backwards-compat path for the original generic-HMAC tests
            and for callers that already extracted the one header they need.

        If the webhook has no ``hmac_secret`` configured it is unauthenticated,
        so this **fails closed** (returns False) — a secret-less webhook would
        let anonymous POSTs spawn an AI assistant with tool access. create()
        auto-mints a secret, so this only bites hand-written / migrated configs
        or a cleared secret. Set KC_ALLOW_UNSIGNED_WEBHOOKS=1 to opt back into
        open mode for local/testing (issue #99).
        """
        secret = cfg.get('hmac_secret')
        if not secret:
            if WebhookManager._allow_unsigned():
                return True
            print(
                f"[webhook] rejecting unsigned POST to webhook "
                f"'{cfg.get('id', '?')}' — no hmac_secret configured "
                f"(set one, or KC_ALLOW_UNSIGNED_WEBHOOKS=1 to allow)",
                file=sys.stderr,
            )
            return False

        provider = cfg.get('provider', 'generic')

        # Normalize headers into a uniform `get(name, default)`. For the str
        # form (or None for "no header sent"), only the configured
        # signature_header resolves; everything else returns ''.
        if headers is None or isinstance(headers, str):
            target = (cfg.get('signature_header') or '').lower()
            value = headers or ''

            def _get(name, default=''):
                return value if name.lower() == target else default
        else:
            def _get(name, default=''):
                v = headers.get(name, default)
                return v if v is not None else default

        if provider == 'slack':
            return WebhookManager._verify_slack(secret, raw_body, _get)
        if provider == 'stripe':
            return WebhookManager._verify_stripe(secret, raw_body, _get)
        # 'generic' and 'github' use the same shape: hex HMAC, optional
        # algo-prefix, configured header name.
        return WebhookManager._verify_generic(cfg, secret, raw_body, _get)

    @staticmethod
    def _verify_generic(cfg, secret, raw_body, get_header):
        """HMAC of body in the configured header, prefixed 'sha256=' or 'sha1='
        (GitHub style) or bare hex. Constant-time compare."""
        header_name = cfg.get('signature_header', 'X-Hub-Signature-256')
        provided = get_header(header_name, '')
        if not provided:
            return False
        algo = cfg.get('signature_algo', 'sha256')
        hasher = hashlib.sha256 if algo == 'sha256' else hashlib.sha1
        expected = hmac.new(secret.encode('utf-8'), raw_body, hasher).hexdigest()
        provided = provided.strip()
        if '=' in provided:
            _, _, provided = provided.partition('=')
        try:
            return hmac.compare_digest(expected, provided.strip())
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _verify_slack(secret, raw_body, get_header):
        """Slack signs ``v0:<ts>:<body>`` with HMAC-SHA256, hex result in
        ``X-Slack-Signature`` as ``v0=<hex>``. Timestamp is in
        ``X-Slack-Request-Timestamp`` and must be within ±5 minutes to thwart
        offline replay of captured requests."""
        sig = (get_header('X-Slack-Signature', '') or '').strip()
        ts = (get_header('X-Slack-Request-Timestamp', '') or '').strip()
        if not sig.startswith('v0=') or not ts:
            return False
        try:
            ts_int = int(ts)
        except ValueError:
            return False
        if abs(time.time() - ts_int) > WebhookManager.TIMESTAMP_TOLERANCE:
            return False
        base = f'v0:{ts}:'.encode('utf-8') + raw_body
        expected = 'v0=' + hmac.new(secret.encode('utf-8'), base, hashlib.sha256).hexdigest()
        try:
            return hmac.compare_digest(expected, sig)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _verify_stripe(secret, raw_body, get_header):
        """Stripe signs ``<ts>.<body>`` with HMAC-SHA256. The header
        ``Stripe-Signature`` is a comma-separated list of ``k=v`` pairs:
        ``t=<unix>,v1=<hex>,v0=<hex>``. We accept any v1 that matches; if a
        request has multiple v1 entries (during a secret rotation window),
        Stripe sends both and the receiver should accept either."""
        header = get_header('Stripe-Signature', '') or ''
        if not header:
            return False
        pairs = {}
        v1s = []
        for part in header.split(','):
            if '=' not in part:
                continue
            k, _, v = part.partition('=')
            k, v = k.strip(), v.strip()
            if k == 'v1':
                v1s.append(v)
            else:
                pairs[k] = v
        ts = pairs.get('t')
        if not ts or not v1s:
            return False
        try:
            ts_int = int(ts)
        except ValueError:
            return False
        if abs(time.time() - ts_int) > WebhookManager.TIMESTAMP_TOLERANCE:
            return False
        base = f'{ts}.'.encode('utf-8') + raw_body
        expected = hmac.new(secret.encode('utf-8'), base, hashlib.sha256).hexdigest()
        return any(
            hmac.compare_digest(expected, v) for v in v1s
        )

    @staticmethod
    def render_prompt(cfg, payload):
        """Apply the prompt template to the inbound payload.

        Two modes, chosen by the config:
          * 'attach' (default, safe): prompt = template + fenced JSON of payload.
            No interpolation, so payload contents can't smuggle instructions
            into the rendered prompt — they appear as data in a code fence.
          * 'interpolate': substitute {{ payload.x.y }} references with the
            matching JSON value. Caller-controlled values land verbatim in the
            instruction line — only use when the payload source is trusted.
        """
        template = cfg.get('prompt_template', '')
        mode = cfg.get('interpolate_mode', 'attach')
        if mode == 'interpolate':
            return WebhookManager._INTERP_RE.sub(
                lambda m: WebhookManager._lookup(payload, m.group(1)),
                template,
            )
        # attach mode
        try:
            pretty = json.dumps(payload, indent=2, default=str)
        except (TypeError, ValueError):
            pretty = repr(payload)
        return f'{template}\n\nWebhook payload:\n```json\n{pretty}\n```'

    @staticmethod
    def _lookup(payload, dotted):
        """Resolve a '.a.b.c' path against payload (dict-only). Returns '' if
        any segment is missing or the payload isn't traversable. Stringifies
        non-string leaves so the substitution always produces a string."""
        cur = payload
        # dotted is e.g. ".pull_request.title" — leading dot, may be empty
        parts = [p for p in dotted.split('.') if p]
        for p in parts:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return ''
        if cur is None:
            return ''
        if isinstance(cur, (str, int, float, bool)):
            return str(cur)
        try:
            return json.dumps(cur, default=str)
        except (TypeError, ValueError):
            return ''


class ProviderKeysManager:
    """User-settable provider API keys, persisted on the PVC and injected into
    every CLI subprocess's env at spawn — so a user can set their own OpenRouter
    / DeepSeek / Anthropic key from the workspace Settings UI (no redeploy), the
    way Claude's oauth login is self-service. The helm value / pod env stays the
    default; a stored key overrides it only when set. Model: WebhookManager
    (one JSON on the PVC, atomic 0600 write, masked public view).
    """

    KEYS_FILE = '/home/dev/.claude-tasks/provider-keys.json'
    # ONLY these env var names may ever be set/injected — never arbitrary env.
    # Keep in sync with hypervisor_session._PROVIDER_KEY_VARS.
    # OPENAI_API_KEY (issue #396) also powers the voice interface's server-side
    # transcription (SpeechTranscriber) besides riding CLI spawns like the rest.
    ALLOWED = ('OPENROUTER_API_KEY', 'DEEPSEEK_API_KEY', 'ANTHROPIC_API_KEY',
               'OPENAI_API_KEY')

    @classmethod
    def _read(cls):
        try:
            with open(cls.KEYS_FILE) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    @classmethod
    def _write(cls, data):
        os.makedirs(os.path.dirname(cls.KEYS_FILE), mode=0o700, exist_ok=True)
        tmp = cls.KEYS_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, 0o600)
        os.rename(tmp, cls.KEYS_FILE)

    @classmethod
    def set(cls, provider, key):
        if provider not in cls.ALLOWED:
            return False, 'unknown provider'
        key = (key or '').strip()
        if not key:
            return False, 'key is required'
        data = cls._read()
        data[provider] = key
        cls._write(data)
        return True, None

    @classmethod
    def delete(cls, provider):
        if provider not in cls.ALLOWED:
            return False
        data = cls._read()
        if provider in data:
            del data[provider]
            cls._write(data)
            return True
        return False

    @classmethod
    def public_view(cls):
        """Masked view for the UI — reports set/unset + a last-4 hint, NEVER the
        key itself (same idea as WebhookManager stripping hmac_secret)."""
        data = cls._read()
        out = {}
        for p in cls.ALLOWED:
            v = data.get(p)
            out[p] = {
                'set': bool(v),
                'hint': (f'…{v[-4:]}' if isinstance(v, str) and len(v) >= 4 else ''),
            }
        return out

    @classmethod
    def env_overlay(cls):
        """{VAR: value} for the keys the user has set — applied over the pod env
        at every CLI spawn (Build tab tmux + Hypervisor subprocess)."""
        data = cls._read()
        return {p: data[p] for p in cls.ALLOWED
                if isinstance(data.get(p), str) and data[p].strip()}


class SpeechTranscriber:
    """Server-side speech-to-text for the voice interface (issue #396, tier 1).

    The web dashboard's tier-0 path uses the browser's own SpeechRecognition
    and never touches the server; native mobile (React Native) has no such
    API, so the Expo app records audio and POSTs it to
    /api/hypervisor/transcribe, which forwards to an OpenAI-compatible
    transcription endpoint. The key comes from the per-workspace provider-key
    store (Settings → Provider keys) with the pod env as fallback — same
    precedence as every CLI spawn's env overlay.
    """

    # Both overridable for self-hosted / compatible providers (e.g. a local
    # whisper.cpp server exposing the OpenAI transcriptions API shape).
    API_URL = os.environ.get(
        'HYPERVISOR_STT_URL', 'https://api.openai.com/v1/audio/transcriptions')
    MODEL = os.environ.get('HYPERVISOR_STT_MODEL', 'whisper-1')
    MAX_AUDIO_BYTES = 25 * 1024 * 1024  # the OpenAI API's own per-file cap

    # Extension by MIME type so the provider can sniff the container format —
    # Expo records m4a on iOS/Android; browsers upload webm/ogg.
    _EXT = {
        'audio/m4a': 'm4a', 'audio/x-m4a': 'm4a', 'audio/mp4': 'm4a',
        'audio/aac': 'aac', 'audio/mpeg': 'mp3', 'audio/mp3': 'mp3',
        'audio/wav': 'wav', 'audio/x-wav': 'wav', 'audio/webm': 'webm',
        'audio/ogg': 'ogg', 'audio/flac': 'flac',
    }

    @classmethod
    def api_key(cls):
        key = ProviderKeysManager.env_overlay().get('OPENAI_API_KEY')
        return key or os.environ.get('OPENAI_API_KEY', '').strip() or None

    @classmethod
    def available(cls):
        """Whether a client mic should even be offered the server STT path."""
        return bool(cls.api_key())

    @classmethod
    def transcribe(cls, audio, content_type='application/octet-stream',
                   filename=None):
        """Returns (text, None) on success or (None, (status, message)) on
        failure — the handler maps the tuple straight onto the response."""
        key = cls.api_key()
        if not key:
            return None, (503, 'No speech-to-text provider is configured — '
                               'set an OpenAI API key under Settings → '
                               'Provider keys (or OPENAI_API_KEY in the pod '
                               'env).')
        if not filename:
            filename = 'audio.' + cls._EXT.get(content_type, 'm4a')
        boundary = uuid.uuid4().hex
        parts = []
        parts.append(f'--{boundary}\r\n'
                     'Content-Disposition: form-data; name="model"\r\n\r\n'
                     f'{cls.MODEL}\r\n'.encode())
        parts.append(f'--{boundary}\r\n'
                     'Content-Disposition: form-data; name="file"; '
                     f'filename="{filename}"\r\n'
                     f'Content-Type: {content_type}\r\n\r\n'.encode())
        parts.append(audio)
        parts.append(f'\r\n--{boundary}--\r\n'.encode())
        body = b''.join(parts)
        req = urllib.request.Request(
            cls.API_URL, data=body, method='POST', headers={
                'Authorization': f'Bearer {key}',
                'Content-Type': f'multipart/form-data; boundary={boundary}',
            })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            detail = ''
            try:
                detail = e.read().decode('utf-8', 'replace')[:300]
            except Exception:
                pass
            return None, (502, f'transcription provider error ({e.code}): '
                               f'{detail}')
        except Exception as e:
            return None, (502, f'transcription request failed: {e}')
        text = (data.get('text') or '').strip() if isinstance(data, dict) else ''
        return text, None


class GatewayCredentialsManager:
    """Per-workspace messaging-provider credentials (issue #329), persisted on
    the PVC and read by the WhatsApp adapter at construction so saving new creds
    hot-swaps the live provider — no pod restart. Same discipline as
    ProviderKeysManager (one JSON on the PVC, atomic 0600 write, redacted public
    view) but the field set is DRIVEN by the provider registry (issue #328): a
    provider's spec declares which fields exist and which are secret, so this
    store never hardcodes provider knowledge.

    Stored shape: {"provider_id": "twilio", "creds": {<field_key>: value, ...}}.
    `creds` is flat and keyed by the provider spec's field_keys() (credential
    fields + the sender field), so it drops straight into build_provider().
    """

    CREDS_FILE = '/home/dev/.claude-tasks/gateway-credentials.json'

    @classmethod
    def _read(cls):
        try:
            with open(cls.CREDS_FILE) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    @classmethod
    def _write(cls, data):
        os.makedirs(os.path.dirname(cls.CREDS_FILE), mode=0o700, exist_ok=True)
        tmp = cls.CREDS_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, 0o600)
        # os.replace is atomic on both POSIX and Windows (os.rename raises on
        # Windows when the target exists), so re-saving credentials never fails.
        os.replace(tmp, cls.CREDS_FILE)

    @staticmethod
    def _spec(provider_id):
        """The provider spec from the registry, or None if unknown/unavailable."""
        if gw_get_provider_spec is None:
            return None
        return gw_get_provider_spec(provider_id)

    @classmethod
    def get_raw(cls):
        """The stored {provider_id, creds} incl. secret values, or None when the
        channel is unconfigured. The ONLY getter that returns secrets — read by
        _build_gateway_adapter() at construction, never logged or returned to a
        client."""
        data = cls._read()
        pid = data.get('provider_id')
        if not pid:
            return None
        creds = data.get('creds')
        return {'provider_id': pid, 'creds': creds if isinstance(creds, dict) else {}}

    @classmethod
    def set(cls, provider_id, creds, sender_number=None):
        """Persist provider + creds. Validates the provider against the registry,
        keeps only fields the spec declares (allowlist), folds sender_number into
        its spec field, and preserves an existing secret when the incoming value
        is blank AND the provider is unchanged (so the form needn't re-enter a
        masked secret). Switching providers starts fresh. Returns (ok, err)."""
        provider_id = (provider_id or '').strip().lower()
        spec = cls._spec(provider_id)
        if spec is None:
            return False, 'unknown provider'
        incoming = dict(creds) if isinstance(creds, dict) else {}
        if sender_number is not None:
            incoming[spec.sender_field.key] = sender_number
        allowed = set(spec.field_keys())
        prev = cls._read()
        same_provider = prev.get('provider_id') == provider_id
        prev_creds = prev.get('creds') if isinstance(prev.get('creds'), dict) else {}
        out = {}
        for key in allowed:
            val = incoming.get(key)
            if isinstance(val, str) and val.strip():
                out[key] = val.strip()
            elif same_provider and isinstance(prev_creds.get(key), str) and prev_creds[key]:
                # Blank/absent on update → keep the previously-stored value.
                out[key] = prev_creds[key]
        cls._write({'provider_id': provider_id, 'creds': out})
        return True, None

    @classmethod
    def clear(cls):
        """Remove the store — disables the channel (adapter falls back to env).
        Idempotent."""
        try:
            os.remove(cls.CREDS_FILE)
        except OSError:
            pass
        return True

    @classmethod
    def public_view(cls):
        """Redacted view for the UI, spec-driven. Per declared field: `set` bool
        always; secret fields add a last-4 `hint` and NEVER the value; non-secret
        identifiers (account SID, verify token, sender number) may include
        `value`. Empty store → {configured: false}."""
        data = cls._read()
        pid = data.get('provider_id')
        spec = cls._spec(pid) if pid else None
        if not pid or spec is None:
            return {'configured': False, 'provider_id': None, 'fields': {}}
        creds = data.get('creds') if isinstance(data.get('creds'), dict) else {}
        fields = {}
        for f in list(spec.credential_fields) + [spec.sender_field]:
            v = creds.get(f.key)
            is_set = isinstance(v, str) and bool(v)
            entry = {'set': is_set}
            if f.secret:
                entry['hint'] = (f'…{v[-4:]}' if is_set and len(v) >= 4 else '')
            elif is_set:
                entry['value'] = v
            fields[f.key] = entry
        return {
            'configured': True,
            'provider_id': pid,
            'sender_field': spec.sender_field.key,
            'fields': fields,
        }

    @classmethod
    def validate_stored(cls):
        """Test-connection over the stored creds: build the provider and probe it
        WITHOUT sending to a real user. Returns (ok, detail); detail never
        contains secret material."""
        raw = cls.get_raw()
        if raw is None:
            return False, 'no credentials configured'
        if gw_build_provider is None:
            return False, 'gateway unavailable'
        try:
            provider = gw_build_provider(raw['provider_id'], raw['creds'])
        except ValueError:
            return False, 'unknown provider'
        return provider.validate()


class SubscriptionStatusManager:
    """Read-only view of subscription-based CLI logins (Claude Max/Pro OAuth,
    Codex ChatGPT OAuth) so the Settings UI can show "logged in via subscription"
    next to the pasted-API-key rows (ProviderKeysManager). Two rules, mirroring
    ProviderKeysManager.public_view():
      * status is read from the credential file (cheap, no subprocess on every
        poll, and richer — gives plan + expiry). Logout shells out to the CLI's
        own subcommand so cleanup stays authoritative.
      * NEVER return the token / refresh token / raw JWT — only derived,
        non-secret fields (plan, expiry, expired-flag).
    """

    CLAUDE_CREDS = os.path.expanduser('~/.claude/.credentials.json')
    CODEX_AUTH = os.path.expanduser('~/.codex/auth.json')

    # provider -> CLI logout argv. Only these providers may be logged out, and
    # only via the CLI's own subcommand (never by us rm-ing credential files).
    LOGOUT_CMDS = {
        'claude': ['claude', 'auth', 'logout'],
        'codex': ['codex', 'logout'],
    }

    @staticmethod
    def _load_json(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    @classmethod
    def _claude_status(cls):
        data = cls._load_json(cls.CLAUDE_CREDS) or {}
        oauth = data.get('claudeAiOauth')
        if not isinstance(oauth, dict) or not oauth.get('accessToken'):
            return {'logged_in': False}
        expires_at = oauth.get('expiresAt')
        expires_at = expires_at if isinstance(expires_at, (int, float)) else None
        # A pasted ANTHROPIC_API_KEY (user overlay or pod env) takes precedence
        # over the subscription at spawn — surface that so the UI can explain it.
        overridden = ('ANTHROPIC_API_KEY' in ProviderKeysManager.env_overlay()
                      or bool(os.environ.get('ANTHROPIC_API_KEY')))
        return {
            'logged_in': True,
            'kind': 'subscription',
            'plan': oauth.get('subscriptionType') or '',
            'expires_at': expires_at,
            'expired': bool(expires_at is not None and expires_at < time.time() * 1000),
            'overridden_by_key': overridden,
        }

    @classmethod
    def _codex_status(cls):
        # ~/.codex/auth.json is absent until `codex login`. A `tokens` object
        # means ChatGPT OAuth (subscription); an OPENAI_API_KEY-only file is an
        # api-key login, not a subscription.
        if not shutil.which('codex'):
            return {'logged_in': False, 'available': False}
        data = cls._load_json(cls.CODEX_AUTH)
        if not isinstance(data, dict):
            return {'logged_in': False}
        if isinstance(data.get('tokens'), dict) and data['tokens']:
            return {'logged_in': True, 'kind': 'subscription', 'plan': 'ChatGPT'}
        if data.get('OPENAI_API_KEY'):
            return {'logged_in': True, 'kind': 'api_key'}
        return {'logged_in': False}

    @classmethod
    def public_view(cls):
        """Masked status for the UI — never includes any token material."""
        return {
            'claude': cls._claude_status(),
            'codex': cls._codex_status(),
        }

    @classmethod
    def logout(cls, provider):
        argv = cls.LOGOUT_CMDS.get(provider)
        if not argv:
            return False, 'unknown provider'
        if not shutil.which(argv[0]):
            return False, f'{argv[0]} not available'
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        except (subprocess.SubprocessError, OSError) as e:
            return False, str(e)[:200]
        if proc.returncode != 0:
            return False, (proc.stderr or proc.stdout or 'logout failed').strip()[:200]
        return True, None


class CronManager:
    """Scheduled triggers backed by Kubernetes CronJob objects.

    Each cron has TWO pieces of state:
      * Local config JSON at /home/dev/.claude-triggers/crons/<id>.json
        (prompt_template, payload, response_url, fire_token, …)
      * A Kubernetes CronJob named cron-<user>-<id> + a matching Secret
        cron-<user>-<id>-token with the bearer the CronJob uses to call back.

    Why CronJob rather than an in-pod scheduler thread:
      * Native suspend/resume via `spec.suspend`
      * Native run-history via successful/failedJobsHistoryLimit
      * `kubectl get cronjobs -n coder` lists everything
      * Schedule fires even if the IDE pod is briefly down (the CronJob's
        curl will retry per the Job's backoffLimit)

    The CronJob's container is just curlimages/curl POSTing to the workspace
    service. The IDE pod is the actual executor; the CronJob is the timer.
    """

    CRONS_DIR = '/home/dev/.claude-triggers/crons'
    NAMESPACE_FILE = '/var/run/secrets/kubernetes.io/serviceaccount/namespace'
    _ID_RE = re.compile(r'^[a-z0-9-]{1,40}$')  # tighter than webhooks because used in k8s names
    # Cron schedule: 5 space-separated fields restricted to characters that
    # appear in real cron expressions (digits, *, /, -, ,). Restricting the
    # character class — vs. \S+ — closes off YAML injection via quote chars,
    # since the schedule is interpolated into the kubectl-apply manifest.
    _CRON_FIELD = r'[0-9*/,-]+'
    _SCHEDULE_RE = re.compile(
        r'^@(yearly|annually|monthly|weekly|daily|hourly)$|'
        r'^' + r'\s+'.join([_CRON_FIELD] * 5) + r'$')
    # IANA timezone names: letters, digits, _, /, +, -. Same anti-injection
    # reasoning as the schedule above.
    _TIMEZONE_RE = re.compile(r'^[A-Za-z0-9_/+\-]{1,64}$')

    @staticmethod
    def ensure_dir():
        os.makedirs(CronManager.CRONS_DIR, mode=0o700, exist_ok=True)

    @staticmethod
    def _config_path(cron_id):
        return os.path.join(CronManager.CRONS_DIR, f'{cron_id}.json')

    @staticmethod
    def valid_id(cron_id):
        return bool(cron_id) and bool(CronManager._ID_RE.match(cron_id))

    @staticmethod
    def detect_user():
        """Workspace username. Prefer the authoritative WORKSPACE_USER env (set
        by the chart from user.name); otherwise parse the pod hostname. A
        Deployment names pods ws-<user>-<replicaset-hash>-<pod-suffix> (TWO hash
        segments) — strip both — falling back to the single-suffix form used by
        bare pods / the kaniko wrapper."""
        u = os.environ.get('WORKSPACE_USER', '').strip()
        if u:
            return u
        host = os.uname().nodename
        for pat in (r'^ws-([a-z0-9-]+?)-[a-z0-9]+-[a-z0-9]+$',
                    r'^ws-([a-z0-9-]+?)-[a-z0-9]+$'):
            m = re.match(pat, host)
            if m:
                return m.group(1)
        return 'unknown'

    @staticmethod
    def detect_namespace():
        try:
            with open(CronManager.NAMESPACE_FILE) as f:
                return f.read().strip()
        except OSError:
            return os.environ.get('POD_NAMESPACE', 'coder')

    @staticmethod
    def k8s_object_name(cron_id):
        """Stable name for both the CronJob and its companion Secret.
        Length-limited because k8s caps object names at 253 but Job names
        get a suffix appended at trigger time (~63 chars practical max)."""
        user = CronManager.detect_user()
        return f'cron-{user}-{cron_id}'[:50]

    @staticmethod
    def list_crons():
        CronManager.ensure_dir()
        out = []
        try:
            entries = sorted(os.listdir(CronManager.CRONS_DIR))
        except OSError:
            return out
        for name in entries:
            if not name.endswith('.json'):
                continue
            try:
                with open(os.path.join(CronManager.CRONS_DIR, name)) as f:
                    cfg = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            out.append(CronManager._public_view(cfg))
        return out

    @staticmethod
    def get_cron(cron_id, include_secrets=False):
        if not CronManager.valid_id(cron_id):
            return None
        try:
            with open(CronManager._config_path(cron_id)) as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        return cfg if include_secrets else CronManager._public_view(cfg)

    @staticmethod
    def _public_view(cfg):
        view = dict(cfg)
        for k in ('fire_token', 'response_secret'):
            if view.get(k):
                view[k + '_set'] = True
                view.pop(k)
        return view

    @staticmethod
    def create_or_update(data, existing_id=None):
        CronManager.ensure_dir()
        cron_id = existing_id or data.get('id', '')
        if not CronManager.valid_id(cron_id):
            return None, 'invalid id (1-40 chars, [a-z0-9-])'

        schedule = (data.get('schedule') or '').strip()
        if not CronManager._SCHEDULE_RE.match(schedule):
            return None, 'invalid schedule (5-field cron or @daily/@hourly/etc)'

        timezone = (data.get('timezone') or 'UTC').strip()
        if not CronManager._TIMEZONE_RE.match(timezone):
            return None, 'invalid timezone (IANA name like UTC or America/Los_Angeles)'

        prompt_template = (data.get('prompt_template') or '').strip()
        if not prompt_template:
            return None, 'prompt_template is required'

        mode = data.get('interpolate_mode', 'attach')
        if mode not in ('attach', 'interpolate'):
            return None, "interpolate_mode must be 'attach' or 'interpolate'"

        payload = data.get('payload')
        if payload is not None and not isinstance(payload, (dict, list)):
            return None, 'payload must be a JSON object or array'

        response_url = data.get('response_url')
        if response_url and not ClaudeTaskManager._is_safe_response_url(response_url):
            return None, 'response_url must be http(s)'

        cfg = {
            'id': cron_id,
            'schedule': schedule,
            'prompt_template': prompt_template,
            'workdir': data.get('workdir') or '/home/dev',
            'payload': payload if payload is not None else {},
            'interpolate_mode': mode,
            'timezone': timezone,
            'suspended': bool(data.get('suspended', False)),
            'created_at': time.time(),
        }
        if data.get('response_url'):
            cfg['response_url'] = data['response_url']
        if data.get('response_secret'):
            cfg['response_secret'] = data['response_secret']

        # Preserve created_at + fire_token across update
        prior = None
        if existing_id:
            prior = CronManager.get_cron(existing_id, include_secrets=True) or {}
            if prior.get('created_at'):
                cfg['created_at'] = prior['created_at']
            if prior.get('fire_token'):
                cfg['fire_token'] = prior['fire_token']

        # Mint the fire_token on first create; the CronJob pod uses it to auth
        # back into the workspace service.
        if not cfg.get('fire_token'):
            cfg['fire_token'] = secrets.token_urlsafe(32)

        # Persist local config
        path = CronManager._config_path(cron_id)
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(cfg, f, indent=2)
        os.chmod(tmp, 0o600)
        os.rename(tmp, path)

        # Apply (or re-apply) the K8s CronJob + Secret. If this fails, the
        # local config still lives — the user can re-apply by editing.
        try:
            CronManager._apply_k8s(cfg)
        except Exception as e:
            return cfg, f'config saved but kubectl apply failed: {e}'

        return cfg, None

    @staticmethod
    def delete(cron_id):
        if not CronManager.valid_id(cron_id):
            return False
        # Best-effort: tear down k8s objects even if local config is gone
        name = CronManager.k8s_object_name(cron_id)
        ns = CronManager.detect_namespace()
        for kind in ('cronjob', 'secret'):
            subprocess.run(
                ['kubectl', 'delete', kind, name, '-n', ns, '--ignore-not-found'],
                capture_output=True, text=True, timeout=30,
            )
        try:
            os.remove(CronManager._config_path(cron_id))
            return True
        except FileNotFoundError:
            # Still report success if we cleaned up k8s objects above
            return False

    @staticmethod
    def set_suspended(cron_id, suspended):
        cfg = CronManager.get_cron(cron_id, include_secrets=True)
        if cfg is None:
            return None
        cfg['suspended'] = bool(suspended)
        path = CronManager._config_path(cron_id)
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(cfg, f, indent=2)
        os.chmod(tmp, 0o600)
        os.rename(tmp, path)

        # Patch the CronJob's spec.suspend in place — cheaper than full re-apply.
        name = CronManager.k8s_object_name(cron_id)
        ns = CronManager.detect_namespace()
        patch = json.dumps({'spec': {'suspend': bool(suspended)}})
        subprocess.run(
            ['kubectl', 'patch', 'cronjob', name, '-n', ns, '--type=merge', '-p', patch],
            capture_output=True, text=True, timeout=30,
        )
        return cfg

    @staticmethod
    def rotate_token(cron_id):
        """Mint a fresh fire_token and re-apply the companion Secret.

        The CronJob references the Secret by name, so the next pod that
        spawns reads the new token. In-flight jobs that were already pulled
        from the API will fail their next call (intended — that's the
        rotation point). Returns (cfg, new_token) or (None, None) if the
        cron doesn't exist or the k8s apply failed (in which case the
        on-disk config is restored to the previous token to keep parity
        with the k8s Secret)."""
        cfg = CronManager.get_cron(cron_id, include_secrets=True)
        if cfg is None:
            return None, None
        # Remember the previous token so we can revert the on-disk config
        # if the k8s apply fails — without this the file would have the
        # new token while the Secret still has the old, and legitimate
        # CronJob fires would reject until the next successful rotation.
        old_token = cfg.get('fire_token')
        old_rotated_at = cfg.get('fire_token_rotated_at')
        new_token = secrets.token_urlsafe(32)
        cfg['fire_token'] = new_token
        cfg['fire_token_rotated_at'] = time.time()

        path = CronManager._config_path(cron_id)

        def _write_atomic(data):
            tmp = path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=2)
            os.chmod(tmp, 0o600)
            os.rename(tmp, path)

        _write_atomic(cfg)

        try:
            CronManager._apply_k8s(cfg)
        except Exception as e:
            # Revert the on-disk config so the local file and the k8s Secret
            # agree on the same (old) token.
            cfg['fire_token'] = old_token
            if old_rotated_at is None:
                cfg.pop('fire_token_rotated_at', None)
            else:
                cfg['fire_token_rotated_at'] = old_rotated_at
            try:
                _write_atomic(cfg)
            except Exception as rollback_err:
                print(
                    f'[cron] rotate-token ROLLBACK FAILED for {cron_id}: {rollback_err}; '
                    f'disk has new token but k8s Secret still has the old one',
                    file=sys.stderr,
                )
            print(f'[cron] rotate-token kubectl apply failed for {cron_id}: {e}', file=sys.stderr)
            return None, None
        return cfg, new_token

    @staticmethod
    def run_now(cron_id):
        """Create a one-shot Job from the cron's CronJob — same effect as if
        the schedule had just fired."""
        if not CronManager.valid_id(cron_id):
            return False, 'invalid id'
        name = CronManager.k8s_object_name(cron_id)
        ns = CronManager.detect_namespace()
        # Suffix with timestamp so repeated 'run now' clicks don't collide
        job_name = f"{name}-manual-{int(time.time())}"[:50]
        r = subprocess.run(
            ['kubectl', 'create', 'job', job_name,
             '--from', f'cronjob/{name}', '-n', ns],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return False, r.stderr.strip() or 'kubectl create failed'
        return True, job_name

    @staticmethod
    def kubectl_status(cron_id):
        """Return k8s-side state for a cron: suspended flag, last-schedule-time,
        next-schedule-time. Returns {} if the CronJob isn't found or kubectl
        isn't available — never raises, so the dashboard stays usable."""
        if not CronManager.valid_id(cron_id):
            return {}
        name = CronManager.k8s_object_name(cron_id)
        ns = CronManager.detect_namespace()
        r = subprocess.run(
            ['kubectl', 'get', 'cronjob', name, '-n', ns, '-o', 'json'],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return {}
        try:
            obj = json.loads(r.stdout)
        except json.JSONDecodeError:
            return {}
        spec = obj.get('spec', {}) or {}
        status = obj.get('status', {}) or {}
        return {
            'k8s_suspended': bool(spec.get('suspend', False)),
            'k8s_schedule': spec.get('schedule'),
            'k8s_last_schedule_time': status.get('lastScheduleTime'),
            'k8s_active': len(status.get('active', []) or []),
        }

    @staticmethod
    def render_prompt(cfg):
        """Cron's payload field plays the role of the inbound payload for
        webhooks — same rendering pipeline."""
        return WebhookManager.render_prompt(cfg, cfg.get('payload') or {})

    @staticmethod
    def _apply_k8s(cfg):
        """kubectl apply -f - for the Secret + CronJob. Raises on failure.
        Re-applies are safe (server-side merge semantics).

        The Secret holds the fire_token and is mounted as an env var into the
        curl pod. We intentionally do NOT pass the token via command-line args
        (would leak in `ps`) or via the URL (would leak in nginx access logs)."""
        name = CronManager.k8s_object_name(cfg['id'])
        ns = CronManager.detect_namespace()
        user = CronManager.detect_user()
        # base64 the token for the Secret (kubectl apply requires base64 for `data:`)
        token_b64 = base64.b64encode(cfg['fire_token'].encode('utf-8')).decode('ascii')

        # The receiver URL: in-cluster service DNS. Using cluster.local is the
        # safe default; if the cluster uses a different DNS suffix, override
        # via the WORKSPACE_INTERNAL_URL env var.
        internal_url = os.environ.get(
            'WORKSPACE_INTERNAL_URL',
            f'http://ws-{user}.{ns}.svc.cluster.local:6080',
        )

        manifest = f"""
apiVersion: v1
kind: Secret
metadata:
  name: {name}
  namespace: {ns}
  labels:
    app: kube-coder-cron
    workspace-user: {user}
    cron-id: {cfg['id']}
type: Opaque
data:
  token: {token_b64}
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: {name}
  namespace: {ns}
  labels:
    app: kube-coder-cron
    workspace-user: {user}
    cron-id: {cfg['id']}
spec:
  schedule: "{cfg['schedule']}"
  timeZone: "{cfg.get('timezone', 'UTC')}"
  suspend: {str(cfg.get('suspended', False)).lower()}
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      backoffLimit: 2
      ttlSecondsAfterFinished: 3600
      template:
        spec:
          restartPolicy: Never
          containers:
          - name: trigger
            image: curlimages/curl:8.10.1
            command: ["/bin/sh", "-c"]
            args:
            - 'curl -fsS --max-time 30 -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "{{}}" "{internal_url}/api/triggers/cron-fire/{cfg['id']}"'
            env:
            - name: TOKEN
              valueFrom:
                secretKeyRef:
                  name: {name}
                  key: token
"""
        r = subprocess.run(
            ['kubectl', 'apply', '-f', '-'],
            input=manifest, capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or 'kubectl apply failed')

    @staticmethod
    def verify_fire_token(cron_id, provided):
        """Constant-time compare an inbound bearer token against the cron's
        fire_token. False on any mismatch or unknown id."""
        cfg = CronManager.get_cron(cron_id, include_secrets=True)
        if cfg is None:
            return False, None
        expected = cfg.get('fire_token') or ''
        if not provided or not expected:
            return False, None
        try:
            ok = hmac.compare_digest(expected, provided)
        except (TypeError, ValueError):
            ok = False
        return ok, cfg


class UpdateManager:
    """Brokers workspace version checks/updates to the workspace-controller.

    The workspace pod has no Kubernetes access, so it cannot read its own image
    tag or patch its Deployment. The controller can; it exposes a token-gated
    self-serve listener (a separate port from its admin API) that authorizes
    actions on the workspace the caller names. We always name OUR OWN user, so a
    user can only ever update their own workspace. Returns (status, payload)
    tuples mirroring the controller's responses; never raises on a network
    error (degrades to a 502 payload)."""

    TIMEOUT = int(os.environ.get('CONTROLLER_TIMEOUT', '15'))

    @staticmethod
    def enabled():
        return bool(CONTROLLER_SELF_SERVE_URL and CONTROLLER_SELF_SERVE_TOKEN)

    @staticmethod
    def _request(method, suffix, body=None):
        user = CronManager.detect_user()
        url = f'{CONTROLLER_SELF_SERVE_URL}/api/self/workspaces/{user}/{suffix}'
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header('X-KC-Service-Token', CONTROLLER_SELF_SERVE_TOKEN)
        req.add_header('Accept', 'application/json')
        if data is not None:
            req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=UpdateManager.TIMEOUT) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                payload = json.load(exc)
            except (ValueError, OSError):
                payload = {'error': f'controller HTTP {exc.code}'}
            return exc.code, payload
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return 502, {'error': f'controller unreachable: {exc}'}

    @staticmethod
    def get_version():
        return UpdateManager._request('GET', 'version')

    @staticmethod
    def do_update(version=None):
        body = {'version': version} if version else {}
        return UpdateManager._request('POST', 'update', body=body)

    @staticmethod
    def do_restart():
        return UpdateManager._request('POST', 'restart', body={})


class DesktopManager:
    """Backs the /api/desktop endpoints — the customizable launcher grid on
    the Desktop tab. Single JSON file at /home/dev/.kube-coder/desktop.json
    holds the full ordered icon list. One file (not one file per icon)
    keeps reordering trivial and avoids transient inconsistency on a
    cold pod read.

    Schema:
        {
          "version": 1,
          "items": [
            {
              "id":     "<8-hex>",
              "label":  "Refactor auth",
              "icon":   "📝",             # any single grapheme cluster
              "hotkey": "cmd+shift+1",     # optional
              "action": {
                "type":      "task",
                "prompt":    "...",
                "workdir":   "/home/dev/kube-coder",
                "assistant": "claude"      # or "opencode-openrouter" / "kc-harness"
              }
            }
          ]
        }

    Action types:
      task  — server creates a Claude/OpenCode task via ClaudeTaskManager
      url   — client opens in a new tab; server is just bookkeeping
      shell — server runs a one-shot bash command, returns stdout/stderr

    All shell commands run as the workspace user with the workspace's own
    environment + cwd — no privilege escalation, no setuid. The owner of
    the workspace also owns the desktop config so anyone who can edit a
    `shell` action can already run arbitrary commands via the terminal.
    """

    CONFIG_PATH = '/home/dev/.kube-coder/desktop.json'
    CONFIG_DIR = '/home/dev/.kube-coder'
    SHELL_TIMEOUT_DEFAULT = 30   # seconds
    SHELL_TIMEOUT_MAX = 300
    _ID_RE = re.compile(r'^[a-z0-9]{4,16}$')
    _ALLOWED_ACTION_TYPES = ('task', 'url', 'shell')

    @staticmethod
    def _ensure_dir():
        os.makedirs(DesktopManager.CONFIG_DIR, mode=0o755, exist_ok=True)

    # Seed icons rendered the first time a workspace opens the Desktop
    # tab. Each user can delete or edit any of these; the seed only fires
    # when desktop.json doesn't exist (first-ever load on the PVC).
    _SEED_ITEMS = [
        {
            'id': 'seedclaud',
            'label': 'New build',
            'icon': 'icon:chat',
            'hotkey': 'cmd+shift+c',
            'action': {
                'type': 'task',
                'prompt': '',
                'workdir': '/home/dev',
                'assistant': 'claude',
            },
        },
        {
            'id': 'seedbuild',
            'label': 'Builds',
            'icon': 'icon:tasks',
            'action': {
                'type': 'url',
                'url': '/tasks',
                'target': 'self',
            },
        },
        {
            'id': 'seedmem01',
            'label': 'Memory',
            'icon': 'icon:memory',
            'action': {
                'type': 'url',
                'url': '/memory',
                'target': 'self',
            },
        },
        {
            'id': 'seeddocs1',
            'label': 'Docs',
            'icon': 'icon:docs',
            'action': {
                'type': 'url',
                'url': '/docs',
                'target': 'self',
            },
        },
        {
            'id': 'seedfiles',
            'label': 'Files',
            'icon': 'icon:files',
            'action': {
                'type': 'url',
                'url': '/files',
                'target': 'self',
            },
        },
        {
            'id': 'seedapps1',
            'label': 'Apps',
            'icon': 'icon:apps',
            'action': {
                'type': 'url',
                'url': '/apps',
                'target': 'self',
            },
        },
        {
            'id': 'seedsett1',
            'label': 'Settings',
            'icon': 'icon:settings',
            'action': {
                'type': 'url',
                'url': '/settings',
                'target': 'self',
            },
        },
    ]

    @staticmethod
    def _load_all():
        DesktopManager._ensure_dir()
        if not os.path.exists(DesktopManager.CONFIG_PATH):
            # First-ever load on this PVC — seed defaults so the Desktop
            # tab isn't an empty page on a fresh workspace. The user can
            # delete/edit/reorder them like any other icon.
            seeded = {'version': 1, 'items': list(DesktopManager._SEED_ITEMS)}
            try:
                DesktopManager._save_all(seeded)
            except OSError:
                pass  # If we can't write, just return the in-memory copy.
            return seeded
        try:
            with open(DesktopManager.CONFIG_PATH) as f:
                data = json.load(f)
            if not isinstance(data, dict) or 'items' not in data:
                return {'version': 1, 'items': []}
            if not isinstance(data['items'], list):
                data['items'] = []
            return data
        except (OSError, json.JSONDecodeError):
            return {'version': 1, 'items': []}

    @staticmethod
    def _save_all(data):
        DesktopManager._ensure_dir()
        # Atomic write — tmp + rename — so a crashed write can't leave the
        # config file empty / half-written and brick the Desktop route.
        tmp = DesktopManager.CONFIG_PATH + f'.tmp.{os.getpid()}'
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, DesktopManager.CONFIG_PATH)

    @staticmethod
    def _new_id():
        return secrets.token_hex(4)

    @staticmethod
    def _validate(item):
        """Validate an item submitted by the client. Returns the cleaned
        dict or raises ValueError. Server is the trust boundary; the SPA
        validates too but a curl client can post anything."""
        if not isinstance(item, dict):
            raise ValueError('item must be an object')
        label = str(item.get('label', '')).strip()
        if not label or len(label) > 80:
            raise ValueError('label must be 1-80 chars')
        icon = str(item.get('icon', '')).strip()
        # Accept either a short emoji/text (≤8 chars) or the named-icon
        # prefix form "icon:NAME" (up to 32 chars) which the SPA renders
        # via its built-in Icon component for the clean line-icon look.
        if not icon or len(icon) > 32:
            raise ValueError('icon must be 1-32 chars (emoji or "icon:NAME")')
        hotkey = item.get('hotkey')
        if hotkey is not None:
            hotkey = str(hotkey).strip().lower()
            if hotkey and not re.match(r'^[a-z0-9+\- ]{1,40}$', hotkey):
                raise ValueError('hotkey must be a short modifier expression e.g. "cmd+shift+1"')
            if not hotkey:
                hotkey = None
        action = item.get('action')
        if not isinstance(action, dict):
            raise ValueError('action must be an object')
        action_type = action.get('type')
        if action_type not in DesktopManager._ALLOWED_ACTION_TYPES:
            raise ValueError(f'action.type must be one of {DesktopManager._ALLOWED_ACTION_TYPES}')
        cleaned_action = {'type': action_type}
        if action_type == 'task':
            # Empty prompt is intentional — boots the assistant CLI into
            # interactive REPL mode (NewTaskForm sends '' too for the
            # "open a Claude session" flow). Just cap the upper bound.
            prompt = str(action.get('prompt', ''))
            if len(prompt) > 8000:
                raise ValueError('action.prompt must be <= 8000 chars')
            cleaned_action['prompt'] = prompt
            workdir = str(action.get('workdir', '')).strip() or '/home/dev'
            cleaned_action['workdir'] = workdir
            assistant = action.get('assistant')
            if assistant:
                cleaned_action['assistant'] = str(assistant).strip()
        elif action_type == 'url':
            url = str(action.get('url', '')).strip()
            if not url or not re.match(r'^(https?://|/)[\S]+$', url):
                raise ValueError('action.url must be http(s) or an absolute path')
            cleaned_action['url'] = url
            target = str(action.get('target', 'blank')).strip()
            if target not in ('blank', 'self'):
                raise ValueError('action.target must be "blank" or "self"')
            cleaned_action['target'] = target
        elif action_type == 'shell':
            command = str(action.get('command', '')).strip()
            if not command or len(command) > 4000:
                raise ValueError('action.command must be 1-4000 chars')
            cleaned_action['command'] = command
            try:
                timeout = int(action.get('timeout') or DesktopManager.SHELL_TIMEOUT_DEFAULT)
            except (TypeError, ValueError):
                raise ValueError('action.timeout must be an integer (seconds)')
            if timeout < 1 or timeout > DesktopManager.SHELL_TIMEOUT_MAX:
                raise ValueError(f'action.timeout must be 1-{DesktopManager.SHELL_TIMEOUT_MAX}s')
            cleaned_action['timeout'] = timeout
        cleaned = {
            'label': label,
            'icon': icon,
            'action': cleaned_action,
        }
        if hotkey:
            cleaned['hotkey'] = hotkey
        return cleaned

    @staticmethod
    def list_items():
        return DesktopManager._load_all().get('items', [])

    @staticmethod
    def create(item):
        cleaned = DesktopManager._validate(item)
        cleaned['id'] = DesktopManager._new_id()
        data = DesktopManager._load_all()
        data['items'].append(cleaned)
        DesktopManager._save_all(data)
        return cleaned

    @staticmethod
    def update(item_id, item):
        if not DesktopManager._ID_RE.match(item_id or ''):
            raise ValueError('invalid id')
        cleaned = DesktopManager._validate(item)
        cleaned['id'] = item_id
        data = DesktopManager._load_all()
        for i, existing in enumerate(data['items']):
            if existing.get('id') == item_id:
                data['items'][i] = cleaned
                DesktopManager._save_all(data)
                return cleaned
        raise ValueError('item not found')

    @staticmethod
    def delete(item_id):
        if not DesktopManager._ID_RE.match(item_id or ''):
            raise ValueError('invalid id')
        data = DesktopManager._load_all()
        before = len(data['items'])
        data['items'] = [it for it in data['items'] if it.get('id') != item_id]
        if len(data['items']) == before:
            raise ValueError('item not found')
        DesktopManager._save_all(data)

    @staticmethod
    def reorder(ordered_ids):
        if not isinstance(ordered_ids, list):
            raise ValueError('order must be an array of ids')
        data = DesktopManager._load_all()
        by_id = {it.get('id'): it for it in data['items']}
        new_items = []
        seen = set()
        for item_id in ordered_ids:
            if item_id in by_id and item_id not in seen:
                new_items.append(by_id[item_id])
                seen.add(item_id)
        # Append any items not mentioned (defensive — client should send all).
        for it in data['items']:
            if it.get('id') not in seen:
                new_items.append(it)
        data['items'] = new_items
        DesktopManager._save_all(data)
        return data['items']

    @staticmethod
    def get(item_id):
        for it in DesktopManager.list_items():
            if it.get('id') == item_id:
                return it
        return None


class DocsManager:
    """Backs the /api/docs endpoints used by the in-app documentation site.

    Source of truth is /home/dev/kube-coder/docs (cloned into every pod by
    start.sh, see CLAUDE.md). The manifest at docs/_manifest.json declares
    the nav tree; pages are plain markdown files. Per-file content is
    mtime-cached so polling clients don't re-read disk every request.
    """

    DOCS_DIR = os.environ.get(
        'DOCS_DIR', '/home/dev/kube-coder/docs'
    )
    # (path → (mtime, decoded_text)). Small enough that we never bother evicting.
    _PAGE_CACHE: dict = {}
    # Manifest is similarly mtime-cached.
    _MANIFEST_CACHE: tuple = (0.0, None)

    @classmethod
    def _safe_join(cls, rel: str) -> str:
        """Resolve `rel` under DOCS_DIR; raise on traversal."""
        rel = (rel or '').lstrip('/')
        # Reject backslashes and absolute drives outright — defensive only.
        if '\x00' in rel or rel.startswith('..'):
            raise ValueError('invalid path')
        base = os.path.realpath(cls.DOCS_DIR)
        target = os.path.realpath(os.path.join(base, rel))
        if target != base and not target.startswith(base + os.sep):
            raise ValueError('path escapes docs root')
        return target

    @classmethod
    def load_manifest(cls) -> dict:
        path = cls._safe_join('_manifest.json')
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return {'version': 1, 'sections': []}
        cached_mtime, cached = cls._MANIFEST_CACHE
        if cached and cached_mtime == mtime:
            return cached
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        cls._MANIFEST_CACHE = (mtime, data)
        return data

    @classmethod
    def index(cls) -> dict:
        """Return the manifest plus a flat id→{title,file,summary,breadcrumbs} map."""
        manifest = cls.load_manifest()
        flat = {}
        for sec in manifest.get('sections', []):
            for page in sec.get('pages', []):
                flat[page['id']] = {
                    'id': page['id'],
                    'title': page.get('title', page['id']),
                    'file': page.get('file', ''),
                    'summary': page.get('summary', ''),
                    'section_id': sec.get('id'),
                    'section_title': sec.get('title'),
                }
        return {'manifest': manifest, 'pages': flat}

    @classmethod
    def get_page(cls, page_id: str) -> dict:
        index = cls.index()
        meta = index['pages'].get(page_id)
        if not meta:
            raise KeyError(page_id)
        path = cls._safe_join(meta['file'])
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            raise KeyError(page_id)
        cached = cls._PAGE_CACHE.get(path)
        if cached and cached[0] == mtime:
            markdown = cached[1]
        else:
            with open(path, 'r', encoding='utf-8') as f:
                markdown = f.read()
            cls._PAGE_CACHE[path] = (mtime, markdown)
        return {
            'id': meta['id'],
            'title': meta['title'],
            'summary': meta['summary'],
            'section_id': meta['section_id'],
            'section_title': meta['section_title'],
            'file': meta['file'],
            'edited_at': mtime,
            'markdown': markdown,
        }

    @classmethod
    def search(cls, q: str, limit: int = 25) -> list:
        """Substring + title-weighted search across all pages. Returns
        [{id,title,snippet,score}] sorted by score desc. Cheap O(N*M) scan
        — adequate for ~20 docs. Phase-6 work upgrades to SQLite FTS5."""
        needle = (q or '').strip().lower()
        if not needle:
            return []
        results = []
        index = cls.index()
        for page_id in index['pages']:
            try:
                page = cls.get_page(page_id)
            except KeyError:
                continue
            title_l = page['title'].lower()
            body_l = page['markdown'].lower()
            score = 0
            if needle in title_l:
                score += 10
            count = body_l.count(needle)
            score += min(count, 20)  # diminishing returns
            if score == 0:
                continue
            idx = body_l.find(needle)
            start = max(0, idx - 60)
            end = min(len(page['markdown']), idx + len(needle) + 80)
            snippet = page['markdown'][start:end].strip()
            results.append({
                'id': page['id'],
                'title': page['title'],
                'section_title': page['section_title'],
                'snippet': snippet,
                'score': score,
            })
        results.sort(key=lambda r: r['score'], reverse=True)
        return results[:limit]


class AppsManager:
    """Backs the Applications page in the dashboard SPA.

    Discovers locally-listening TCP services (from /proc/net/tcp[6]) and
    merges them with a user-curated list of "pinned" ports persisted on
    the workspace PVC. A pinned port gives the user a friendly name and
    stays in the list even when the underlying process is stopped, so the
    UI can show "my Django app — stopped" instead of an entry that
    disappears every time the server restarts.

    The proxy itself (BrowserHandler._proxy_app_request) calls is_proxyable
    to confirm the requested port is currently listening on loopback before
    forwarding — that prevents bearer-authed callers from probing arbitrary
    pod-external ports through the dashboard.
    """

    PINS_PATH = os.path.expanduser('~/.claude-tasks/apps.json')

    # Ports the workspace itself owns. Hidden from the auto-list and
    # refused by the proxy even if the user tries to pin them.
    INTERNAL_PORTS = frozenset({22, 2376, 5900, 6080, 6081, 7681, 8080})

    # Bind addresses we accept as "on loopback". 0.0.0.0 / :: are
    # "all interfaces" which includes loopback, so anything bound that
    # way is reachable from the pod and safe to proxy.
    LOOPBACK_ADDRS = frozenset({'127.0.0.1', '::1', '0.0.0.0', '::'})

    _NAME_RE = re.compile(r'^[\w \-./@:]{1,80}$')

    # --- /proc/net/tcp parsing ---

    @staticmethod
    def parse_listen_ports(tcp_path='/proc/net/tcp', tcp6_path='/proc/net/tcp6'):
        """Return [{port, addr, inode}] for every LISTEN socket bound to a
        loopback address. Parameters allow injecting fixture files in tests."""
        out = []
        for path, family in ((tcp_path, 4), (tcp6_path, 6)):
            try:
                with open(path) as f:
                    lines = f.read().splitlines()[1:]
            except (FileNotFoundError, PermissionError):
                continue
            for line in lines:
                parts = line.split()
                if len(parts) < 10 or parts[3] != '0A':  # 0A = TCP_LISTEN
                    continue
                local = parts[1]
                if ':' not in local:
                    continue
                ip_hex, port_hex = local.rsplit(':', 1)
                try:
                    port = int(port_hex, 16)
                except ValueError:
                    continue
                if family == 4:
                    addr = AppsManager._decode_ipv4_hex(ip_hex)
                else:
                    addr = AppsManager._decode_ipv6_hex(ip_hex)
                if addr is None or not AppsManager._is_loopback(addr):
                    continue
                try:
                    inode = int(parts[9])
                except ValueError:
                    inode = 0
                out.append({'port': port, 'addr': addr, 'inode': inode})
        # Dedupe by port (a service bound on both v4 and v6 shows up twice).
        seen = {}
        for entry in out:
            seen.setdefault(entry['port'], entry)
        return list(seen.values())

    @staticmethod
    def _decode_ipv4_hex(s):
        if len(s) != 8:
            return None
        try:
            return '.'.join(str(int(s[i:i + 2], 16)) for i in (6, 4, 2, 0))
        except ValueError:
            return None

    @staticmethod
    def _decode_ipv6_hex(s):
        """/proc/net/tcp6 IPv6: 32 hex chars, little-endian per 32-bit word.
        Decode to a canonical lowercase form so the loopback check matches."""
        if len(s) != 32:
            return None
        try:
            groups = []
            for i in range(0, 32, 8):
                word = s[i:i + 8]
                # Reverse bytes within the 32-bit word.
                be = word[6:8] + word[4:6] + word[2:4] + word[0:2]
                groups.append(be[:4].lower())
                groups.append(be[4:8].lower())
            full = ':'.join(groups)
        except ValueError:
            return None
        # Canonicalize the addresses we care about; leave others as-is.
        if full == '0000:0000:0000:0000:0000:0000:0000:0000':
            return '::'
        if full == '0000:0000:0000:0000:0000:0000:0000:0001':
            return '::1'
        # IPv4-mapped (::ffff:a.b.c.d) — present the dotted form so the
        # loopback check below can match the v4 string directly.
        if full.startswith('0000:0000:0000:0000:0000:ffff:'):
            tail = full.split(':')[-2:]  # ['7f00', '0001']
            try:
                packed = int(tail[0] + tail[1], 16).to_bytes(4, 'big')
                return f'::ffff:{packed[0]}.{packed[1]}.{packed[2]}.{packed[3]}'
            except ValueError:
                return full
        return full

    @staticmethod
    def _is_loopback(addr):
        if addr in AppsManager.LOOPBACK_ADDRS:
            return True
        # IPv4-mapped IPv6 loopback (::ffff:127.0.0.1).
        if addr.startswith('::ffff:') and addr.endswith('.127.0.0.1'):
            return True
        if addr.startswith('::ffff:127.'):
            return True
        return False

    # --- pin persistence ---

    @staticmethod
    def _ensure_dir():
        os.makedirs(os.path.dirname(AppsManager.PINS_PATH), mode=0o700, exist_ok=True)

    @staticmethod
    def _load_pins():
        if not os.path.exists(AppsManager.PINS_PATH):
            return {}
        try:
            with open(AppsManager.PINS_PATH) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        out = {}
        for k, v in data.items():
            if not isinstance(v, dict):
                continue
            try:
                out[int(k)] = v
            except (TypeError, ValueError):
                continue
        return out

    @staticmethod
    def _save_pins(pins):
        AppsManager._ensure_dir()
        # JSON keys must be strings; sort for stable diffs.
        on_disk = {str(p): pins[p] for p in sorted(pins.keys())}
        tmp = AppsManager.PINS_PATH + f'.tmp.{os.getpid()}'
        with open(tmp, 'w') as f:
            json.dump(on_disk, f, indent=2)
        os.replace(tmp, AppsManager.PINS_PATH)

    @staticmethod
    def _validate_port(port):
        try:
            p = int(port)
        except (TypeError, ValueError):
            raise ValueError('port must be an integer')
        if not (1 <= p <= 65535):
            raise ValueError('port must be between 1 and 65535')
        return p

    @staticmethod
    def _validate_name(name):
        s = str(name or '').strip()
        if not s:
            raise ValueError('name is required')
        if not AppsManager._NAME_RE.match(s):
            raise ValueError(
                'name must be 1-80 chars; letters, digits, space, _-./@: only'
            )
        return s

    @classmethod
    def add_pin(cls, port, name, strip_prefix=False):
        port = cls._validate_port(port)
        name = cls._validate_name(name)
        pins = cls._load_pins()
        pins[port] = {
            'name': name,
            'strip_prefix': bool(strip_prefix),
            'created_at': time.time(),
        }
        cls._save_pins(pins)
        return pins[port]

    @classmethod
    def remove_pin(cls, port):
        port = cls._validate_port(port)
        pins = cls._load_pins()
        if port in pins:
            del pins[port]
            cls._save_pins(pins)
            return True
        return False

    @classmethod
    def get_pin(cls, port):
        try:
            port = cls._validate_port(port)
        except ValueError:
            return None
        return cls._load_pins().get(port)

    # --- merged view ---

    @classmethod
    def list_apps(cls):
        """Merged list shown on the Applications page.

        Order: pinned entries first (sorted by name), then discovered
        entries that aren't pinned (sorted by port).
        """
        listeners = {entry['port']: entry for entry in cls.parse_listen_ports()}
        pins = cls._load_pins()
        rows = []
        seen = set()
        for port in sorted(pins.keys(), key=lambda p: (pins[p].get('name', '').lower(), p)):
            seen.add(port)
            pin = pins[port]
            listening = port in listeners
            if port in cls.INTERNAL_PORTS:
                rows.append({
                    'port': port, 'name': pin.get('name', ''),
                    'pinned': True, 'status': 'blocked',
                    'strip_prefix': bool(pin.get('strip_prefix')),
                    'addr': listeners.get(port, {}).get('addr', ''),
                })
                continue
            rows.append({
                'port': port, 'name': pin.get('name', ''),
                'pinned': True,
                'status': 'running' if listening else 'stopped',
                'strip_prefix': bool(pin.get('strip_prefix')),
                'addr': listeners.get(port, {}).get('addr', ''),
            })
        for port in sorted(listeners.keys()):
            if port in seen or port in cls.INTERNAL_PORTS:
                continue
            rows.append({
                'port': port, 'name': '',
                'pinned': False, 'status': 'running',
                'strip_prefix': False,
                'addr': listeners[port].get('addr', ''),
            })
        return rows

    @classmethod
    def is_proxyable(cls, port):
        """(ok, reason). Called by the proxy before forwarding."""
        try:
            port = cls._validate_port(port)
        except ValueError as e:
            return False, str(e)
        if port in cls.INTERNAL_PORTS:
            return False, f'port {port} is reserved for the workspace'
        listeners = {e['port']: e for e in cls.parse_listen_ports()}
        if port not in listeners:
            return False, f'port {port} is not currently listening on loopback'
        return True, ''


# ── Mission Control (issue #425) ─────────────────────────────────────────────
# Normalizes builds (~/.claude-tasks tasks), hypervisor chats and orchestrator
# sub-agents into one card list grouped by what needs the human:
#
#   running  — agent actively working
#   waiting  — blocked on the user (waiting-for-input, quick-reply prompt)
#   done     — all terminals (completed, failed, killed) from the recent
#              window, plus idle (parked, resumable) chats. Terminals older
#              than the window are excluded — the board is a working set,
#              not an archive.

# How far back terminal work stays on the board.
MC_RECENT_SECONDS = int(os.environ.get('KC_MISSIONCONTROL_RECENT_S', 48 * 3600))
# Bytes tailed from output.log / events.jsonl for headline derivation.
_MC_TAIL_BYTES = 6144
_MC_HEADLINE_MAX = 140


def _mc_tail(path, max_bytes=_MC_TAIL_BYTES):
    """Last max_bytes of a file as text ('' on any error)."""
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode('utf-8', errors='replace')
    except OSError:
        return ''


def _mc_clip(text, limit=_MC_HEADLINE_MAX):
    text = ' '.join((text or '').split())
    if len(text) > limit:
        return text[:limit - 1].rstrip() + '…'
    return text


def _mc_headline_from_log(task_dir, fallback=''):
    """Derived one-liner: the last meaningful output line of a task.

    Phase-1 headlines are honest rather than clever — the latest thing the
    agent printed, ANSI-stripped, box-drawing and status-bar chrome skipped.
    (Classifier-written headlines are a later phase of #425.)
    """
    text = strip_ansi(_mc_tail(os.path.join(task_dir, 'output.log')))
    for line in reversed(text.splitlines()):
        s = _normalize_prompt_line(line)
        # Skip prompt-menu options and pure chrome; keep real content.
        if not s or _PROMPT_OPTION_RE.match(s):
            continue
        if len(s) < 4:
            continue
        return _mc_clip(s)
    return _mc_clip(fallback)


def _mc_headline_from_events(events_path, fallback=''):
    """Latest human-meaningful event of a hypervisor thread."""
    best = ''
    for line in _mc_tail(events_path).splitlines():
        try:
            e = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        etype = e.get('type')
        if etype == 'message' and e.get('role') == 'assistant':
            best = e.get('text') or best
        elif etype == 'tool_call':
            tool = e.get('tool') or e.get('name') or 'tool'
            best = f'Using {tool}'
        elif etype == 'error':
            best = e.get('text') or best
    return _mc_clip(best or fallback)


def _mc_git_branch(workdir):
    """Current branch of workdir, '' when unknown. Pure file reads (no git
    subprocess — this runs per card on a list endpoint). Follows one level of
    `gitdir:` indirection so linked worktrees resolve too."""
    if not workdir:
        return ''
    try:
        git_path = os.path.join(workdir, '.git')
        if os.path.isfile(git_path):  # linked worktree: .git is a pointer file
            with open(git_path) as f:
                first = f.readline().strip()
            if not first.startswith('gitdir:'):
                return ''
            head_path = os.path.join(first.split(':', 1)[1].strip(), 'HEAD')
        else:
            head_path = os.path.join(git_path, 'HEAD')
        with open(head_path) as f:
            head = f.read().strip()
        if head.startswith('ref: '):
            return head.rsplit('/', 1)[-1]
        return head[:12]  # detached
    except OSError:
        return ''


def _mc_task_card(meta, task_dir, now):
    """One queue card from a task's meta (already status-reconciled), or None
    when the task is outside the board's working set."""
    status = meta.get('status', 'unknown')
    finished_at = meta.get('finished_at') or meta.get('killed_at')
    if status == 'running':
        state = 'running'
    elif status == 'waiting-for-input':
        state = 'waiting'
    elif status in ('completed', 'error', 'killed'):
        if not finished_at or (now - finished_at) > MC_RECENT_SECONDS:
            return None
        state = 'done'
    else:
        return None

    task_id = meta.get('task_id') or os.path.basename(task_dir)
    kind = 'subagent' if meta.get('parent_task_id') else 'build'
    prompt = meta.get('prompt', '')
    workdir = meta.get('workdir') or ''

    card = {
        'id': f'{kind}:{task_id}',
        'ref_id': task_id,
        'kind': kind,
        'state': state,
        'title': meta.get('name') or _mc_clip(prompt, 60) or task_id,
        'headline': _mc_headline_from_log(task_dir, fallback=prompt),
        'assistant': meta.get('assistant'),
        'model': '',
        'workdir': workdir,
        'repo': os.path.basename(workdir.rstrip('/')) if workdir else '',
        'branch': _mc_git_branch(workdir),
        'created_at': meta.get('created_at'),
        'updated_at': meta.get('last_activity_at') or meta.get('created_at'),
        'finished_at': finished_at,
        'waiting_since': meta.get('last_activity_at') if state == 'waiting' else None,
        'waiting_prompt': None,
        'outcome': None,
        'parent_id': (f"build:{meta['parent_task_id']}"
                      if meta.get('parent_task_id') else None),
        'children': [],  # filled by the assembler from sub_task_ids
        '_sub_task_ids': meta.get('sub_task_ids', []),
    }

    if state == 'waiting':
        # Mirror get_task's pending_prompt so quick-reply buttons render on
        # the board itself (#204/#276). One tmux capture per *waiting* task
        # only — bounded, and only these rows need it.
        session_name = meta.get('tmux_session', f'kube-coder-{task_id}')
        try:
            result = subprocess.run(
                ['tmux', 'capture-pane', '-J', '-t', session_name,
                 '-p', '-S', '-50'],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                card['waiting_prompt'] = parse_screen_prompt(result.stdout)
                if card['waiting_prompt'] and not card['headline']:
                    card['headline'] = _mc_clip(
                        card['waiting_prompt'].get('question') or '')
        except (OSError, subprocess.SubprocessError):
            pass
    elif state == 'done':
        if status == 'completed':
            card['outcome'] = {'ok': True, 'detail': 'completed'}
        elif status == 'killed':
            card['outcome'] = {'ok': False, 'detail': 'killed'}
        else:
            exit_code = meta.get('exit_code')
            detail = ('error' if exit_code in (None, '')
                      else f'error · exit {exit_code}')
            card['outcome'] = {'ok': False, 'detail': detail}
    return card


def _mc_thread_card(summary, now):
    """One queue card from a hypervisor thread summary, or None when the
    thread is outside the working set (deleted, or idle beyond the window)."""
    if summary.get('deleted_at'):
        return None
    status = summary.get('status')
    updated_at = summary.get('updated_at') or summary.get('created_at') or 0
    if status == 'running':
        state = 'running'
    elif status == 'error':
        if (now - updated_at) > MC_RECENT_SECONDS:
            return None
        state = 'done'
    else:  # idle → parked, resumable
        if (now - updated_at) > MC_RECENT_SECONDS:
            return None
        state = 'done'

    thread_id = summary.get('id')
    thread_dir = os.path.join(HYPERVISOR_DIR, thread_id) \
        if _HYPERVISOR_AVAILABLE else ''
    card = {
        'id': f'chat:{thread_id}',
        'ref_id': thread_id,
        'kind': 'chat',
        'state': state,
        'title': summary.get('title') or 'New chat',
        'headline': _mc_headline_from_events(
            os.path.join(thread_dir, 'events.jsonl'),
            fallback=summary.get('title') or '') if thread_dir else '',
        'assistant': summary.get('assistant'),
        'model': summary.get('model') or '',
        'workdir': '',
        'repo': '',
        'branch': '',
        'created_at': summary.get('created_at'),
        'updated_at': updated_at,
        'finished_at': None,
        'waiting_since': None,
        'waiting_prompt': None,
        'outcome': None,
        'parent_id': None,
        'children': [],
        '_sub_task_ids': [],
    }
    if state == 'done':
        card['outcome'] = ({'ok': False, 'detail': 'error'}
                           if status == 'error'
                           else {'ok': True, 'detail': 'idle — resumable'})
    return card


def missioncontrol_queue():
    """Assemble the normalized queue: cards + pulse. Pure read — safe to poll."""
    now = time.time()
    cards = []

    # Builds + sub-agents (sub-agents are ordinary tasks with parent_task_id).
    ClaudeTaskManager.ensure_tasks_dir()
    try:
        entries = sorted(os.listdir(ClaudeTaskManager.TASKS_DIR), reverse=True)
    except OSError:
        entries = []
    for entry in entries:
        task_dir = os.path.join(ClaudeTaskManager.TASKS_DIR, entry)
        meta_path = os.path.join(task_dir, 'task.json')
        if not os.path.isfile(meta_path):
            continue
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            ClaudeTaskManager._reconcile_status(meta, task_dir)
            card = _mc_task_card(meta, task_dir, now)
            if card:
                cards.append(card)
        except (json.JSONDecodeError, OSError):
            continue

    # Hypervisor chats.
    if _HYPERVISOR_AVAILABLE:
        try:
            for summary in HypervisorSession.list():
                card = _mc_thread_card(summary, now)
                if card:
                    cards.append(card)
        except Exception as e:
            print(f'[missioncontrol] thread listing failed: {e}',
                  file=sys.stderr)

    # Lineage: resolve children shallowly from sub_task_ids.
    by_ref = {c['ref_id']: c for c in cards if c['kind'] != 'chat'}
    for card in cards:
        for child_id in card.pop('_sub_task_ids'):
            child = by_ref.get(child_id)
            if child:
                card['children'].append({
                    'id': child['id'],
                    'title': child['title'],
                    'state': child['state'],
                })

    # Urgency first (waiting → running → done), newest within a group.
    order = {'waiting': 0, 'running': 1, 'done': 2}
    cards.sort(key=lambda c: (order.get(c['state'], 9), -(c['updated_at'] or 0)))

    waiting = [c for c in cards if c['state'] == 'waiting']
    oldest_wait_s = 0
    if waiting:
        stamps = [c['waiting_since'] or c['updated_at'] or now for c in waiting]
        oldest_wait_s = int(max(0, now - min(stamps)))
    day_ago = now - 24 * 3600
    pulse = {
        'running': sum(1 for c in cards if c['state'] == 'running'),
        'waiting': len(waiting),
        'done_today': sum(
            1 for c in cards
            if c['state'] == 'done'
            and (c['finished_at'] or c['updated_at'] or 0) >= day_ago),
        'oldest_wait_s': oldest_wait_s,
        'generated_at': now,
    }
    return {'cards': cards, 'pulse': pulse}


# Detail drawer (#425 phase 3): the tail is a window, not a transcript.
_MC_DETAIL_TAIL_LINES = 80
_MC_DETAIL_TIMELINE_MAX = 100


def _mc_primary_arg(tool_input):
    """Best-effort "primary argument" of a tool call for a timeline detail
    line — mirrors routes/hypervisor/activity.ts primaryArg so web and API
    consumers describe a call the same way."""
    if tool_input is None:
        return ''
    if isinstance(tool_input, str):
        return tool_input
    if not isinstance(tool_input, dict):
        return str(tool_input)
    for key in ('command', 'prompt', 'query', 'path', 'file_path', 'name',
                'message', 'namespace', 'url', 'port'):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return ''


# Raw task status → board state, matching _mc_task_card's mapping so the
# drawer's lineage chips read the same as the queue's.
_MC_STATUS_STATE = {'running': 'running', 'waiting-for-input': 'waiting',
                    'completed': 'done', 'error': 'done', 'killed': 'done'}


def _mc_entry(at, kind, text, detail='', link=None, status='ok'):
    """One normalized timeline entry — the drawer's unit of history."""
    return {'at': at, 'kind': kind, 'text': _mc_clip(text, 140),
            'detail': _mc_clip(detail, 140), 'link': link, 'status': status}


def _mc_task_timeline(meta, card, children):
    """Coarse activity timeline for a task card, derived from what the task
    metadata records (no transcript parsing): start, sub-agent spawns,
    waiting-on-input, and the terminal transition."""
    entries = [_mc_entry(meta.get('created_at'), 'start', 'Started',
                         detail=meta.get('prompt', ''))]
    for child_id, child in children:
        entries.append(_mc_entry(
            child.get('created_at'), 'subagent',
            f"Spawned sub-agent — {child.get('name') or child_id}",
            detail=child.get('prompt', ''),
            link=f'subagent:{child_id}'))
    if card['state'] == 'waiting':
        prompt = card.get('waiting_prompt') or {}
        entries.append(_mc_entry(
            card.get('waiting_since'), 'waiting', 'Waiting on your input',
            detail=prompt.get('question') or '', status='pending'))
    outcome = card.get('outcome')
    if outcome and card.get('finished_at'):
        entries.append(_mc_entry(
            card['finished_at'], 'end', outcome['detail'],
            status='ok' if outcome['ok'] else 'error'))
    entries.sort(key=lambda e: e['at'] or 0)
    return entries


def _mc_chat_timeline(session, summary):
    """Chat card timeline: the hypervisor activity classifier's view (#298),
    re-normalized to the same entry shape task timelines use."""
    entries = [_mc_entry(summary.get('created_at'), 'start', 'Started',
                         detail=summary.get('title') or '')]
    if hv_build_activity is None:
        return entries
    for e in hv_build_activity(session.read_events())['timeline']:
        kind = e.get('kind')
        if kind == 'tool':
            is_sub = e.get('category') == 'subagent'
            if is_sub:
                text = (f"Sub-agent · {e['subagent_type']}"
                        if e.get('subagent_type') else 'Sub-agent')
                detail = e.get('description') or ''
            else:
                text = f"Using {e.get('label') or e.get('tool') or 'tool'}"
                detail = _mc_primary_arg(e.get('input'))
            # A sub-build carries the created task id — cross-link the cards.
            link = (f"build:{e['task_id']}"
                    if e.get('category') == 'build' and e.get('task_id')
                    else None)
            entries.append(_mc_entry(
                e.get('ts'), 'subagent' if is_sub else 'tool', text,
                detail=detail, link=link, status=e.get('status') or 'ok'))
        elif kind == 'error':
            entries.append(_mc_entry(e.get('ts'), 'error',
                                     e.get('text') or 'Error',
                                     status='error'))
        elif kind == 'status':
            entries.append(_mc_entry(e.get('ts'), 'status',
                                     str(e.get('status') or 'status'),
                                     status='muted'))
    if len(entries) > _MC_DETAIL_TIMELINE_MAX:
        entries = entries[:1] + entries[-(_MC_DETAIL_TIMELINE_MAX - 1):]
    return entries


def missioncontrol_card_detail(card_id):
    """Drawer payload for one board card (#425 phase 3): the card itself plus
    a normalized activity timeline and, for tasks, a bounded ANSI-stripped
    output tail. Returns None when the card is not on the board (unknown id,
    deleted thread, or a task aged out of the working set). Pure read."""
    kind, _, ref_id = card_id.partition(':')
    now = time.time()

    if kind in ('build', 'subagent'):
        task_dir = os.path.join(ClaudeTaskManager.TASKS_DIR, ref_id)
        meta_path = os.path.join(task_dir, 'task.json')
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            ClaudeTaskManager._reconcile_status(meta, task_dir)
        except (OSError, json.JSONDecodeError):
            return None
        card = _mc_task_card(meta, task_dir, now)
        if card is None:
            return None
        card.pop('_sub_task_ids', None)
        # Resolve children once, shared by the card's lineage line and the
        # timeline's spawn entries (the queue assembler does this globally;
        # here the scope is just this card's sub_task_ids).
        children = []
        for child_id in meta.get('sub_task_ids', []):
            child_path = os.path.join(
                ClaudeTaskManager.TASKS_DIR, child_id, 'task.json')
            try:
                with open(child_path) as f:
                    child = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            children.append((child_id, child))
            card['children'].append({
                'id': f'subagent:{child_id}',
                'title': child.get('name') or child_id,
                'state': _MC_STATUS_STATE.get(
                    child.get('status'), child.get('status', 'unknown')),
            })
        tail = strip_ansi(_mc_tail(os.path.join(task_dir, 'output.log')))
        tail = '\n'.join(tail.splitlines()[-_MC_DETAIL_TAIL_LINES:])
        return {
            'card': card,
            'timeline': _mc_task_timeline(meta, card, children),
            'output_tail': tail,
        }

    if kind == 'chat':
        if not _HYPERVISOR_AVAILABLE:
            return None
        session = HypervisorSession.get(ref_id)
        if session is None:
            return None
        summary = session.summary()
        card = _mc_thread_card(summary, now)
        if card is None:
            return None
        card.pop('_sub_task_ids', None)
        return {
            'card': card,
            'timeline': _mc_chat_timeline(session, summary),
            'output_tail': '',
        }

    return None


class BrowserHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # Force browsers (especially mobile Safari) to revalidate the
        # dashboard on each visit. Without this, SimpleHTTPRequestHandler
        # sends no Cache-Control and Safari can pin a stale SPA index.html
        # for days, hiding bundle updates behind a manual cache-clear.
        # Applied to HTML AND to SPA routes (which don't end in .html but
        # serve index.html via serve_next_spa) — the previous .html-only
        # check missed /, /memory, /tasks etc., leading to users stuck on
        # months-old bundles. Static hashed assets keep default heuristics.
        path = (self.path or '').split('?', 1)[0].lower()
        is_html = path.endswith('.html')
        is_spa_route = (
            path in ('/', '/dashboard', '/dashboard/', '/browser', '/browser/', '/next', '/next/')
            or path.startswith('/next/')
            or any(path == r or path.startswith(r + '/')
                   for r in ('/tasks', '/memory', '/apps', '/triggers', '/files', '/docs', '/settings', '/desktop', '/hypervisor', '/walkie', '/mission'))
        )
        if is_html or is_spa_route:
            self.send_header('Cache-Control', 'no-cache, must-revalidate')
            self.send_header('Pragma', 'no-cache')
        super().end_headers()

    def do_GET(self):
        self._consume_bearer_marker()
        # Normalize path: strip /oauth and /browser prefixes from ingress
        # rewrites, AND strip the query string before matching routes.
        # Without dropping the query string, deep-link URLs like
        # /oauth/?task=<id>&chat=open never match the "/" dashboard route
        # and fall through to the static-file 404.
        path_no_query = self.path.split('?', 1)[0]
        normalized_path = path_no_query.replace('/oauth', '').replace('/browser', '')
        if normalized_path == '' or normalized_path == '/':
            normalized_path = '/'

        # Sub-resources an embedded app loaded that escaped the proxy prefix
        # (lazy route chunks, @font-face fonts, …) land at the dashboard root.
        # If the Referer is an app-proxy iframe, send them back to that app.
        # Runs before dashboard routing so an escaped /tasks etc. goes to the
        # app rather than serving it the dashboard SPA.
        if self._dispatch_referer_proxy('GET'):
            return

        # All SPA routes serve the new dashboard. /next/* is the explicit form
        # (kept for backward compat after cutover) and the bare top-level
        # routes (/, /tasks, /memory, …) all serve the same SPA index.html so
        # client-side routing handles deep links. The legacy dashboard.html
        # has been removed; if /opt/dashboard-dist is missing we return 503
        # rather than fall back to anything stale.
        SPA_TOP_LEVEL = {'/', '/tasks', '/memory', '/apps', '/triggers', '/files', '/docs', '/settings', '/desktop', '/hypervisor', '/walkie', '/mission'}
        first_seg = '/' + normalized_path.split('/')[1] if normalized_path != '/' else '/'
        if normalized_path == "/next" or normalized_path == "/next/" or normalized_path.startswith("/next/"):
            rel = normalized_path[len("/next"):] if normalized_path.startswith("/next") else ""
            self.serve_next_spa(rel)
            return
        elif (
            normalized_path in ["/", "/dashboard", "/dashboard/"]
            or normalized_path in ["/browser", "/browser/"]
            or first_seg in SPA_TOP_LEVEL
        ):
            # SPA at root. /dashboard and /browser kept for back-compat URLs.
            self.serve_next_spa('/')
            return
        elif self.path == "/livez":
            self.send_livez()
            return
        elif self.path == "/health":
            self.send_health_check()
            return
        elif self.path == "/health/vscode":
            self.send_vscode_health()
            return
        elif self.path == "/health/terminal":
            self.send_terminal_health()
            return
        elif self.path == "/health/browser":
            self.send_browser_health()
            return
        elif self.path == "/metrics":
            self.send_metrics()
            return
        # These /api/* reads match on normalized_path (the /oauth- and
        # /browser-stripped path) rather than raw self.path: the SPA prefixes
        # every /api/ call with /oauth in oauth2 mode, so a raw `self.path`
        # match would 404 the prefixed request. (Peers like /api/mode and
        # /api/desktop already route via the normalized path below.)
        elif normalized_path == "/api/github/status":
            self.send_github_status()
            return
        elif normalized_path == "/api/github/config":
            self.send_git_config()
            return
        elif normalized_path == "/api/workspace/version":
            self.send_workspace_version()
            return
        elif self.path == "/vnc" or self.path == "/vnc/":
            self.send_vnc_viewer()
            return
        elif self.path == "/vnc-proxy" or self.path == "/vnc-proxy/":
            self.redirect_to_vnc()
            return
        elif self.path.startswith("/vnc/"):
            self.proxy_vnc_request()
            return

        # --- Claude Task API (GET) ---
        # Query string is already stripped at the top; handlers re-parse it from self.path when needed.
        claude_path = normalized_path

        # /api/events — Server-Sent Events firehose of dashboard events
        # (task.created / task.status). Lets the SPA replace per-route polling
        # with push (issue #93).
        if claude_path == '/api/events':
            self.handle_events_stream()
            return

        # /api/mode — public deployment-mode probe used by the SPA at boot to
        # decide whether to hide mutation UI. Intentionally unauthenticated so
        # the read-only public demo can fetch it without an auth proxy in
        # front. Returns the two flags server.py was started with — never
        # derived per-request, never user-controllable.
        if claude_path == '/api/mode':
            self.send_json({
                'readOnly': READONLY_MODE,
                'authed': AUTH_MODE != 'none',
                'authMode': AUTH_MODE,
                'demoShowAll': DEMO_SHOW_ALL,
            })
            return
        # /api/apps — Applications page list endpoint.
        if claude_path == '/api/apps':
            self._handle_apps_list()
            return
        # Reverse-proxy to a locally-listening web app.
        if self._dispatch_app_proxy(claude_path, 'GET'):
            return
        # /api/missioncontrol/cards/{kind}:{id} — drawer detail (#425 ph. 3).
        m = re.match(
            r'^/api/missioncontrol/cards/'
            r'((?:build|chat|subagent):[A-Za-z0-9_-]+)$', claude_path)
        if m:
            self.handle_missioncontrol_card(m.group(1))
            return
        if claude_path == '/api/claude/tasks':
            self.handle_claude_list_tasks()
            return
        elif claude_path == '/api/missioncontrol/queue':
            self.handle_missioncontrol_queue()
            return
        elif claude_path == '/api/claude/auth/token':
            self.handle_claude_get_token()
            return
        elif claude_path == '/api/claude/apps/session':
            self.handle_app_session_mint()
            return
        elif claude_path == '/api/claude/assistants':
            self.handle_claude_list_assistants()
            return
        elif claude_path == '/api/hypervisor/config':
            self.handle_hypervisor_config()
            return
        elif claude_path == '/api/hypervisor/threads':
            self.handle_hypervisor_list_threads()
            return
        elif claude_path == '/api/hypervisor/health':
            self.handle_hypervisor_health()
            return
        elif claude_path == '/api/workspace/dirs':
            self.handle_workspace_dirs()
            return
        # Conversation Gateway (issue #306): Meta GET verify handshake (NO auth —
        # the provider can't carry an OAuth session) + link list (bearer).
        elif claude_path == '/api/gateway/whatsapp/webhook':
            self.handle_gateway_whatsapp_verify()
            return
        elif claude_path == '/api/gateway/links':
            self.handle_gateway_link_list()
            return
        elif claude_path == '/api/gateway/providers':
            self.handle_gateway_providers()
            return
        elif claude_path == '/api/gateway/credentials':
            self.handle_gateway_credentials_get()
            return
        elif claude_path == '/api/gateway/internal/transcript':
            self.handle_gateway_internal_transcript()
            return

        # /api/claude/tasks/{id}/stream — Server-Sent Events
        m = re.match(r'^/api/claude/tasks/([A-Za-z0-9_-]+)/stream$', claude_path)
        if m:
            self._claude_task_id = m.group(1)
            self.handle_claude_stream_output()
            return

        # --- Hypervisor chat threads ---
        # Threads are structured agent sessions (hypervisor_session.py); the
        # frontend polls this endpoint with ?since=<seq> for new canonical
        # events. No SSE/tmux stream — there is no terminal to stream.
        # /api/hypervisor/threads/{id}/activity — observability timeline +
        # bounded runner.log tail (must precede the plain threads/{id} route).
        m = re.match(r'^/api/hypervisor/threads/([A-Za-z0-9_-]+)/activity$', claude_path)
        if m:
            self.handle_hypervisor_get_activity(m.group(1))
            return
        # /api/hypervisor/threads/{id}/watchers — cross-turn watchers (#402).
        m = re.match(r'^/api/hypervisor/threads/([A-Za-z0-9_-]+)/watchers$', claude_path)
        if m:
            self.handle_hypervisor_list_watchers(m.group(1))
            return
        m = re.match(r'^/api/hypervisor/threads/([A-Za-z0-9_-]+)$', claude_path)
        if m:
            self.handle_hypervisor_get_thread(m.group(1))
            return
        # /api/claude/tasks/{id} and /api/claude/tasks/{id}/output
        m = re.match(r'^/api/claude/tasks/([A-Za-z0-9_-]+)/output$', claude_path)
        if m:
            self._claude_task_id = m.group(1)
            self.handle_claude_get_output()
            return
        m = re.match(r'^/api/claude/tasks/([A-Za-z0-9_-]+)$', claude_path)
        if m:
            self._claude_task_id = m.group(1)
            self.handle_claude_get_task()
            return

        # --- Provider keys (dashboard Settings) ---
        if claude_path == '/api/provider-keys':
            self.handle_provider_keys_list()
            return

        # --- User MCP servers (dashboard Settings, issue #353) ---
        if claude_path == '/api/mcp-servers':
            self.handle_mcp_servers_list()
            return

        # --- Subscription-login status (dashboard Settings) ---
        if claude_path == '/api/subscriptions':
            self.handle_subscriptions_list()
            return

        # --- Webhook CRUD (dashboard) ---
        if claude_path == '/api/webhooks':
            self.handle_webhook_list()
            return
        m = re.match(r'^/api/webhooks/([a-zA-Z0-9_-]+)$', claude_path)
        if m:
            self._webhook_id = m.group(1)
            self.handle_webhook_get()
            return

        # --- Cron CRUD (dashboard) ---
        if claude_path == '/api/crons':
            self.handle_cron_list()
            return
        m = re.match(r'^/api/crons/([a-z0-9-]+)$', claude_path)
        if m:
            self._cron_id = m.group(1)
            self.handle_cron_get()
            return

        # --- Desktop launcher (dashboard) ---
        if claude_path == '/api/desktop':
            self.handle_desktop_list()
            return
        m = re.match(r'^/api/desktop/([a-z0-9]+)$', claude_path)
        if m:
            item = DesktopManager.get(m.group(1))
            if item is None:
                self.send_json({'error': 'item not found'}, 404)
            else:
                self.send_json(item)
            return

        # --- Memory API (dashboard surface; backs the Memory tab) ---
        # Parse the query string once so list/search can use it.
        query_string = self.path.split('?', 1)[1] if '?' in self.path else ''
        memory_query = urllib.parse.parse_qs(query_string)
        if claude_path == '/api/memory':
            self.handle_memory_list(memory_query)
            return
        if claude_path == '/api/memory/stats':
            self.handle_memory_stats()
            return
        if claude_path == '/api/memory/export':
            self.handle_memory_export()
            return
        m = re.match(r'^/api/memory/([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+)/history$', claude_path)
        if m:
            self.handle_memory_history(m.group(1), m.group(2))
            return
        m = re.match(r'^/api/memory/([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+)/refs$', claude_path)
        if m:
            self.handle_memory_refs(m.group(1), m.group(2))
            return
        m = re.match(r'^/api/memory/([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+)/relations$', claude_path)
        if m:
            self.handle_memory_relations(m.group(1), m.group(2))
            return
        m = re.match(r'^/api/memory/([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+)/neighbors$', claude_path)
        if m:
            self.handle_memory_neighbors(m.group(1), m.group(2), memory_query)
            return
        m = re.match(r'^/api/memory/([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+)$', claude_path)
        if m:
            self.handle_memory_get(m.group(1), m.group(2))
            return

        # --- Skills API (multi-harness SKILL.md surface; backs the Skills tab) ---
        if claude_path == '/api/skills':
            self.handle_skills_list(memory_query)
            return
        if claude_path == '/api/skills/stats':
            self.handle_skills_stats()
            return
        m = re.match(r'^/api/skills/([a-zA-Z0-9._-]+)$', claude_path)
        if m:
            self.handle_skills_get(m.group(1))
            return

        # --- Subagents (read-only view over Claude's transcripts) ---
        if claude_path == '/api/subagents':
            self.handle_subagents_list()
            return

        # --- Docs (in-app documentation site) ---
        if claude_path == '/api/docs':
            self.handle_docs_manifest()
            return
        if claude_path == '/api/docs/search':
            self.handle_docs_search(memory_query)
            return
        m = re.match(r'^/api/docs/([a-zA-Z0-9_-]+)$', claude_path)
        if m:
            self.handle_docs_page(m.group(1))
            return

        # --- File browser (lists /home/dev and child directories) ---
        if claude_path == '/api/files/list':
            self.handle_files_list()
            return
        # Stream a workspace media file (Hypervisor image/video rendering).
        if claude_path == '/api/files/raw':
            self.handle_file_raw()
            return
        # Download any workspace file as an attachment (Files manager).
        if claude_path == '/api/files/download':
            self.handle_file_download()
            return
        # Inline preview descriptor for the Files pane (text/image/binary).
        if claude_path == '/api/files/preview':
            self.handle_file_preview()
            return
        # Stream a document inline for the Hypervisor chat's sandboxed viewer
        # (PDF/HTML/SVG). Markdown/text render client-side via /preview.
        if claude_path == '/api/files/view':
            self.handle_file_view()
            return

        super().do_GET()

    def serve_next_spa(self, rel_path):
        """Serve the new Preact SPA built into /opt/dashboard-dist/.

        rel_path is the path *after* /next (e.g. '' for /next, '/assets/x.js').
        SPA history fallback: if the path has no extension and the file is
        missing, fall back to index.html so client-routed deep links work
        after a refresh.

        DASHBOARD_DIST_DIR overrides the default location so tests + local
        dev can point at charts/workspace/web/dist.
        """
        import mimetypes
        base = os.environ.get('DASHBOARD_DIST_DIR') or '/opt/dashboard-dist'
        if not os.path.isdir(base):
            self.send_error(
                404,
                'New dashboard is not built. Run `yarn --cwd charts/workspace/web build` '
                'or set DASHBOARD_DIST_DIR to a built dist/ directory.',
            )
            return
        # Strip leading slash, decode percent-escapes, refuse traversal.
        rel = urllib.parse.unquote(rel_path).lstrip('/')
        if rel == '' or rel.endswith('/'):
            rel = 'index.html'
        target = os.path.normpath(os.path.join(base, rel))
        base_real = os.path.realpath(base)
        target_real = os.path.realpath(target)
        if not (target_real == base_real or target_real.startswith(base_real + os.sep)):
            self.send_error(403, 'Forbidden')
            return
        # History fallback for client-side routes: no extension + not found.
        if not os.path.isfile(target_real) and '.' not in os.path.basename(target_real):
            target_real = os.path.join(base_real, 'index.html')
            rel = 'index.html'
        if not os.path.isfile(target_real):
            self.send_error(404, 'Not found')
            return
        ctype, _ = mimetypes.guess_type(target_real)
        if ctype is None:
            ctype = 'application/octet-stream'
        try:
            with open(target_real, 'rb') as fh:
                body = fh.read()
        except OSError as exc:
            self.send_error(500, f'Read error: {exc}')
            return
        # Tell the SPA which ingress auth prefix to use for API and embedded-
        # service (terminal/vscode/vnc/metrics) URLs. In oauth2 mode only the
        # /oauth/* ingress paths inject the x-auth-request-user header; the bare
        # /api/* paths don't, so the SPA must call /oauth/api/*. The SPA is
        # served at '/' in EVERY mode, so it can't infer this from the URL —
        # AUTH_MODE here is the source of truth. client.ts authPrefix() reads
        # window.__KC_AUTH_PREFIX__ ('/oauth' for oauth2, '' for basic/none).
        if rel == 'index.html':
            spa_prefix = '/oauth' if AUTH_MODE == 'oauth2' else ''
            inject = ('<script>window.__KC_AUTH_PREFIX__=%s;</script>'
                      % json.dumps(spa_prefix)).encode('utf-8')
            body = (body.replace(b'</head>', inject + b'</head>', 1)
                    if b'</head>' in body else inject + body)
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        # Vite emits hashed filenames into /assets/, so those are safe to cache
        # for a year. index.html and other top-level files must revalidate so
        # deploys take effect on next request.
        if rel.startswith('assets/'):
            self.send_header('Cache-Control', 'public, max-age=31536000, immutable')
        else:
            self.send_header('Cache-Control', 'no-cache, must-revalidate')
        self.end_headers()
        self.wfile.write(body)

    def check_auth(self):
        """Legacy auth check used by the (deprecated) pre-SPA endpoints.
        Honors Remote-User only when TRUSTED_PROXY=true so a misconfigured
        ingress cannot be exploited by client-supplied headers."""
        if self.headers.get('Authorization', ''):
            return True
        if TRUSTED_PROXY and not getattr(self, '_bearer_only', False) \
                and self.headers.get('Remote-User', ''):
            return True
        return False
    
    # --- Claude Task API helpers ---

    @staticmethod
    def _strip_route_prefix(path):
        """Strip the SPA's leading `/oauth/` and `/browser/` route prefixes.

        A naive `path.replace('/oauth', '').replace('/browser', '')` (which
        this method replaces) corrupts paths that contain the substrings
        mid-string — e.g. `/api/oauth/foo` becomes `/api//foo` and may not
        match any registered route. Only prefix matches are stripped, and
        the bare `/oauth` or `/browser` route maps to `/`. Order matters
        because both wraps are valid (`/oauth/browser/api/x` -> `/api/x`).
        """
        if path.startswith('/oauth/'):
            path = path[len('/oauth'):]
        elif path == '/oauth':
            path = '/'
        if path.startswith('/browser/'):
            path = path[len('/browser'):]
        elif path == '/browser':
            path = '/'
        return path

    def _consume_bearer_marker(self):
        """Detect + strip the Bearer-only ingress marker and flag the request.

        The dedicated Bearer-token API ingress (ingress-claude-api.yaml) routes
        through a leading `/bearer-api/` marker that NO oauth2-proxy-fronted
        path ever uses. Its presence means the request arrived via the ingress
        that is *not* authenticated by oauth2-proxy — so upstream identity
        headers (X-Auth-Request-*, Remote-User) must never be trusted for it,
        regardless of ingress header hygiene. The Bearer token is then the only
        accepted credential (see check_claude_auth / check_oauth_only).

        This is defense-in-depth for the trusted-proxy header model: even if an
        operator's ingress-nginx has `allow-snippet-annotations` disabled (so the
        header-stripping configuration-snippet on that ingress is a no-op), a
        forged X-Auth-Request-User on the Bearer path is still rejected here.

        We strip the marker from self.path so all existing routing / prefix
        logic is unchanged. Requests without the marker (dashboard via
        /oauth/*, in-pod localhost calls, k8s probes) are untouched.
        """
        p = self.path or ''
        if p.startswith('/bearer-api/'):
            self.path = p[len('/bearer-api'):]
            self._bearer_only = True
        elif p == '/bearer-api':
            self.path = '/'
            self._bearer_only = True

    def check_claude_auth(self, allow_none_mode=True):
        """Returns True if request is authenticated via OAuth2 headers OR valid bearer token.

        Short-circuits to True when AUTH_MODE=none — the public-demo
        deployment runs without an auth proxy in front of it. Guarded at
        startup (see _check_safety_invariants below) so this combo is only
        allowed when READONLY_MODE=true.

        Set allow_none_mode=False on endpoints that must always require a
        real identity (e.g. anything that returns PII or workspace secrets
        — the public demo must not leak the operator's git config / SSH
        key fingerprint just because the demo runs unauth'd).

        Upstream-auth headers (X-Auth-Request-*, Remote-User) are only
        honored when TRUSTED_PROXY=true — otherwise a misconfigured
        ingress that doesn't strip client-supplied headers becomes a
        trivial auth bypass.
        """
        if AUTH_MODE == 'none' and allow_none_mode:
            return True
        # AUTH_MODE=basic: the nginx-ingress http-basic-auth gate is the sole
        # authenticator. It validates credentials in front of the pod but
        # deliberately strips the `Authorization` header before proxying
        # (`proxy_set_header Authorization "";`), and re-forwarding it is
        # blocked by the controller's admission webhook — so server.py has no
        # forwarded proof to re-check and trusts the edge. This is what lets
        # the SPA's /api/* calls work under basic auth; without it the
        # dashboard loads but every data fetch 401s.
        #
        # Security: basic auth is a single shared password with no per-user
        # identity to enforce, intended for local / single-tenant use where
        # the only path to the pod is through the authenticating ingress. For
        # multi-tenant clusters use AUTH_MODE=oauth2, where server.py is the
        # enforcer (validated proxy headers / Bearer tokens) — see the
        # TRUSTED_PROXY / Bearer paths below.
        if AUTH_MODE == 'basic':
            return True
        # _bearer_only requests arrived via the Bearer-token ingress (marked by
        # _consume_bearer_marker), which is NOT fronted by oauth2-proxy — so
        # identity headers on that path are untrusted and only a Bearer token
        # authenticates. This holds even if the ingress failed to strip them.
        if TRUSTED_PROXY and not getattr(self, '_bearer_only', False):
            if self.headers.get('X-Auth-Request-User') or self.headers.get('X-Auth-Request-Email'):
                return True
            if self.headers.get('Remote-User', ''):
                return True
        auth_header = self.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:].strip()
            return ClaudeTaskManager.verify_token(token)
        return False

    def check_oauth_only(self):
        """Returns True only if request has OAuth2 proxy headers (not bearer token).
        Only honored when TRUSTED_PROXY=true; otherwise returns False.

        Never honored for _bearer_only requests (Bearer-token ingress): that path
        is not fronted by oauth2-proxy, so its identity headers are untrusted."""
        if not TRUSTED_PROXY or getattr(self, '_bearer_only', False):
            return False
        if self.headers.get('X-Auth-Request-User') or self.headers.get('X-Auth-Request-Email'):
            return True
        if self.headers.get('Remote-User', ''):
            return True
        return False

    APP_SESSION_COOKIE = 'kc_app_session'

    def _app_session_cookie_value(self):
        """The kc_app_session cookie's value from the request, or ''."""
        for part in self.headers.get('Cookie', '').split(';'):
            name, _, value = part.strip().partition('=')
            if name == self.APP_SESSION_COOKIE:
                return value
        return ''

    def check_app_proxy_auth(self):
        """Auth for the apps list + app proxy ONLY: everything
        check_claude_auth accepts, plus a valid short-lived app-session
        cookie (minted by /api/claude/apps/session for the mobile WebView,
        whose sub-resource requests can't carry an Authorization header).
        The cookie is deliberately NOT accepted anywhere else — an exfiltrated
        session grants the embedded-app surface, not the workspace API."""
        if self.check_claude_auth():
            return True
        value = self._app_session_cookie_value()
        return bool(value) and ClaudeTaskManager.verify_app_session(value)

    def send_json(self, data, status=200):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self, max_bytes=None):
        """Read + parse a JSON request body, refusing anything over the cap.
        Without the cap a single Content-Length: big POST will OOM the pod.
        Raises ValueError on oversized bodies; handlers should treat the
        same way they treat JSONDecodeError (400)."""
        cap = max_bytes if max_bytes is not None else MAX_REQUEST_BODY_BYTES
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length == 0:
            return {}
        if content_length > cap:
            raise ValueError(f'request body too large ({content_length} > {cap})')
        body = self.rfile.read(content_length).decode('utf-8')
        return json.loads(body) if body else {}

    def _readonly_block(self):
        """Reject mutating requests when READONLY_MODE=true. Single chokepoint
        called at the top of do_POST/do_DELETE/do_PUT so individual handlers
        don't each need to remember to gate themselves."""
        if not READONLY_MODE:
            return False
        self.send_json({
            'error': 'This workspace is a read-only public demo. '
                     'Sign in to a personal workspace at https://github.com/imran31415/kube-coder '
                     'for full read-write access.',
            'code': 'readonly',
        }, 403)
        return True

    def do_DELETE(self):
        self._consume_bearer_marker()
        if self._readonly_block():
            return
        try:
            path = self._strip_route_prefix(self.path)
            # /api/apps/pins/<port> — remove a pinned port. Match before the
            # generic app-proxy dispatcher so the proxy doesn't swallow it.
            m = re.match(r'^/api/apps/pins/(\d+)$', path)
            if m:
                self._handle_apps_pin_delete(int(m.group(1)))
                return
            if self._dispatch_app_proxy(path, 'DELETE'):
                return
            # Delete a file / empty dir (Files manager). Path arrives as a query
            # param, so match the route portion before '?'.
            if path.split('?', 1)[0] == '/api/files':
                self.handle_file_delete()
                return
            m = re.match(r'^/api/claude/tasks/([A-Za-z0-9_-]+)$', path)
            if m:
                self._claude_task_id = m.group(1)
                self.handle_claude_delete_task()
                return
            # /api/hypervisor/threads/{id}/watchers/{wid} — cancel one
            # cross-turn watcher (#402). Must precede the plain threads/{id}
            # delete route.
            m = re.match(r'^/api/hypervisor/threads/([A-Za-z0-9_-]+)/watchers/'
                         r'([A-Za-z0-9_-]+)$', path)
            if m:
                self.handle_hypervisor_cancel_watcher(m.group(1), m.group(2))
                return
            m = re.match(r'^/api/hypervisor/threads/([A-Za-z0-9_-]+)$', path)
            if m:
                self.handle_hypervisor_delete_thread(m.group(1))
                return
            m = re.match(r'^/api/provider-keys/([A-Z_]+)$', path)
            if m:
                self.handle_provider_keys_delete(m.group(1))
                return
            # User MCP servers (issue #353): remove + fan-out cleanup.
            m = re.match(r'^/api/mcp-servers/([A-Za-z0-9_-]+)$', path)
            if m:
                self.handle_mcp_servers_delete(m.group(1))
                return
            # Log out of a subscription CLI (claude|codex) — DELETE the login.
            m = re.match(r'^/api/subscriptions/([a-z]+)$', path)
            if m:
                self.handle_subscriptions_logout(m.group(1))
                return
            m = re.match(r'^/api/webhooks/([a-zA-Z0-9_-]+)$', path)
            if m:
                self._webhook_id = m.group(1)
                self.handle_webhook_delete()
                return
            # Conversation Gateway (issue #306): revoke a link by id (== the
            # sha256 identity hash).
            m = re.match(r'^/api/gateway/link/([a-f0-9]{64})$', path)
            if m:
                self.handle_gateway_link_delete(m.group(1))
                return
            # Messaging provider credentials (issue #329): clear the store.
            if path == '/api/gateway/credentials':
                self.handle_gateway_credentials_delete()
                return
            m = re.match(r'^/api/crons/([a-z0-9-]+)$', path)
            if m:
                self._cron_id = m.group(1)
                self.handle_cron_delete()
                return
            m = re.match(r'^/api/desktop/([a-z0-9]+)$', path)
            if m:
                self.handle_desktop_delete(m.group(1))
                return
            m = re.match(r'^/api/memory/([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+)/relations/(\d+)$', path)
            if m:
                self.handle_memory_unlink(m.group(1), m.group(2), int(m.group(3)))
                return
            m = re.match(r'^/api/memory/([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+)$', path)
            if m:
                self.handle_memory_delete(m.group(1), m.group(2))
                return
            self.send_json({'error': 'Not found'}, 404)
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    # --- Claude Task API handlers ---

    def handle_claude_list_tasks(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        # Parse ?parent=<task_id> filter from query string
        parent = None
        if '?' in self.path:
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            parent_val = params.get('parent', [None])[0]
            if parent_val:
                parent = parent_val
        tasks = ClaudeTaskManager.list_tasks(parent=parent)
        self.send_json({'tasks': tasks})

    def handle_missioncontrol_queue(self):
        """Mission Control board (#425): builds + chats + sub-agents as one
        normalized card queue, grouped by what needs the human. Read-only."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        self.send_json(missioncontrol_queue())

    def handle_missioncontrol_card(self, card_id):
        """Drawer detail for one Mission Control card (#425 phase 3): the
        card, a normalized activity timeline, and an output tail. Read-only."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        detail = missioncontrol_card_detail(card_id)
        if detail is None:
            self.send_json({'error': 'Card not found'}, 404)
            return
        self.send_json(detail)

    def handle_workspace_dirs(self):
        """List candidate working directories under /home/dev for the
        new-task picker. Reuses Claude auth (OAuth header OR bearer token)."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        self.send_json({'dirs': WorkspaceManager.list_dirs()})

    def handle_claude_get_task(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        task = ClaudeTaskManager.get_task(self._claude_task_id)
        if task is None:
            self.send_json({'error': 'Task not found'}, 404)
            return
        self.send_json(task)

    def handle_claude_get_output(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        # Parse ?tail=N and ?ansi=1 from query string
        tail = None
        ansi = False
        if '?' in self.path:
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            tail_val = params.get('tail', [None])[0]
            if tail_val and tail_val.isdigit():
                tail = int(tail_val)
            ansi = params.get('ansi', ['0'])[0] in ('1', 'true')
        output = ClaudeTaskManager.get_task_output(self._claude_task_id, tail=tail, ansi=ansi)
        if output is None:
            self.send_json({'error': 'Task or output not found'}, 404)
            return
        # JSON-wrap the body so the SPA's typed fetch client (which expects
        # `{output: string}`) deserializes correctly. The legacy dashboard's
        # raw-text consumer was retired with the dashboard.html removal.
        self.send_json({'output': output})

    def handle_claude_stream_output(self):
        """Server-Sent Events stream of a task's rendered tmux output.

        Polls `tmux capture-pane` every ~1.5s and emits the diff vs the previous
        capture. We use capture-pane (not raw pipe-pane bytes) because claude-code
        is an interactive TUI: it emits cursor-moves, \\r-redraws, and spinner
        animations that look like garbage when streamed raw. tmux maintains the
        rendered screen state, so capture-pane gives us clean text.

        For completed tasks (tmux session gone) falls back to output.log.

        Query params:
          from=start (default) — send the current full capture once, then diffs.
          from=live            — skip initial capture, only send what changes after.
        """
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return

        task_id = self._claude_task_id
        task_dir = os.path.join(ClaudeTaskManager.TASKS_DIR, task_id)
        meta_path = os.path.join(task_dir, 'task.json')
        if not os.path.isfile(meta_path):
            self.send_json({'error': 'Task not found'}, 404)
            return

        from_param = 'start'
        if '?' in self.path:
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            from_param = params.get('from', ['start'])[0]

        try:
            with open(meta_path, 'r') as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError):
            self.send_json({'error': 'Task metadata unreadable'}, 500)
            return
        session_name = meta.get('tmux_session', f'kube-coder-{task_id}')
        output_log = os.path.join(task_dir, 'output.log')

        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        # Tell ingress-nginx not to buffer — otherwise SSE chunks won't flush in real time.
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()

        def write_raw(payload_bytes):
            try:
                self.wfile.write(payload_bytes)
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        def write_sse(data):
            # SSE framing: prefix every line with "data: ", terminate event with blank line.
            lines = data.split('\n')
            block = ''.join(f'data: {line}\n' for line in lines) + '\n'
            return write_raw(block.encode('utf-8'))

        def capture():
            """Return the current pane content (with history), or fall back to
            output.log if the tmux session is gone (task completed/killed)."""
            r = subprocess.run(
                ['tmux', 'capture-pane', '-J', '-t', session_name, '-p', '-S', '-2000'],
                capture_output=True, text=True,
            )
            if r.returncode == 0 and r.stdout:
                return strip_ansi(r.stdout).rstrip('\n') + '\n'
            if os.path.exists(output_log):
                try:
                    with open(output_log, 'r', errors='replace') as f:
                        return strip_ansi(f.read()).rstrip('\n') + '\n'
                except OSError:
                    pass
            return ''

        last_capture = ''
        if from_param == 'start':
            initial = capture()
            if initial:
                if not write_sse(initial):
                    return
                last_capture = initial
        else:
            # Live mode: prime last_capture so we don't replay history.
            last_capture = capture()

        last_heartbeat = time.time()
        last_change = time.time()
        started = time.time()
        poll_interval = 1.5
        heartbeat_interval = 15
        idle_grace = 5  # seconds after task stops with no diff before we close

        while True:
            # Hard cap so a client that never disconnects can't pin a
            # handler thread forever (combined with ThreadingHTTPServer's
            # unbounded thread spawn this is the easiest path to DoS).
            # Emit a graceful end event so the SPA reconnects cleanly.
            if time.time() - started > STREAM_MAX_SECONDS:
                write_raw(b'event: end\ndata: timeout\n\n')
                return
            new_capture = capture()
            if new_capture != last_capture:
                if new_capture.startswith(last_capture):
                    # Pure append — emit just the new tail.
                    diff = new_capture[len(last_capture):]
                elif last_capture.startswith(new_capture):
                    # Capture shrank (rare; pane cleared). Wait for next snapshot.
                    diff = ''
                else:
                    # History scrolled off / pane cleared. Send a marker plus the
                    # new content so the user sees something without a giant replay.
                    diff = '\n[output buffer rewound]\n' + new_capture
                last_capture = new_capture
                if diff:
                    if not write_sse(diff):
                        return
                    last_change = time.time()
                    last_heartbeat = last_change

            if time.time() - last_heartbeat > heartbeat_interval:
                if not write_raw(b': keep-alive\n\n'):
                    return
                last_heartbeat = time.time()

            try:
                with open(meta_path, 'r') as f:
                    meta = json.load(f)
                ClaudeTaskManager._reconcile_status(meta, task_dir)
                status = meta.get('status', 'unknown')
            except (OSError, json.JSONDecodeError):
                status = 'unknown'

            if status not in ('running', 'waiting-for-input') and time.time() - last_change > idle_grace:
                write_raw(f'event: end\ndata: {status}\n\n'.encode('utf-8'))
                return

            time.sleep(poll_interval)

    def handle_events_stream(self):
        """Server-Sent Events firehose of dashboard events (task.created /
        task.status). Subscribes to EventBroker and forwards each event as a
        named SSE frame so the SPA can replace per-route polling (issue #93).

        Framing mirrors handle_claude_stream_output: heartbeat comments keep
        proxies from closing an idle connection, and STREAM_MAX_SECONDS caps
        the lifetime so a never-disconnecting client can't pin a handler
        thread forever (the SPA reconnects on the `end` event).
        """
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return

        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()

        def write_raw(payload_bytes):
            try:
                self.wfile.write(payload_bytes)
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        q = EventBroker.subscribe()
        started = time.time()
        # Greet so the client can flip to "connected" and stop its poll fallback.
        if not write_raw(b'event: ready\ndata: {}\n\n'):
            EventBroker.unsubscribe(q)
            return
        try:
            while True:
                if time.time() - started > STREAM_MAX_SECONDS:
                    write_raw(b'event: end\ndata: timeout\n\n')
                    return
                try:
                    event = q.get(timeout=15)
                except queue.Empty:
                    if not write_raw(b': keep-alive\n\n'):
                        return
                    continue
                payload = json.dumps(event.get('data', {}))
                frame = f"event: {event.get('type', 'message')}\ndata: {payload}\n\n"
                if not write_raw(frame.encode('utf-8')):
                    return
        finally:
            EventBroker.unsubscribe(q)

    def handle_claude_create_task(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        # Prompt is optional — the SPA's "Create build" flow drops the textarea
        # and spawns an interactive Claude/OpenCode session the user can type
        # into directly. Empty prompt → no-op Enter after the 3s assistant init.
        prompt = data.get('prompt', '').strip()
        workdir = data.get('workdir')
        response_url = data.get('response_url') or None
        response_secret = data.get('response_secret') or None
        source = data.get('source') or None
        disable_memory_injection = bool(data.get('disable_memory_injection'))
        assistant = data.get('assistant') or None
        parent_task_id = data.get('parent_task_id') or None
        # Skip-permissions default: honor an explicit body flag; otherwise let
        # the source decide — unattended sources auto-approve, the interactive
        # Build tab does not (issue #296).
        auto_approve = ClaudeTaskManager.resolve_auto_approve(
            source, data.get('auto_approve'))
        if response_url and not ClaudeTaskManager._is_safe_response_url(response_url):
            self.send_json({'error': 'response_url must be http(s)'}, 400)
            return
        task = ClaudeTaskManager.create_task(
            prompt,
            workdir=workdir,
            response_url=response_url,
            response_secret=response_secret,
            source=source,
            disable_memory_injection=disable_memory_injection,
            assistant=assistant,
            parent_task_id=parent_task_id,
            auto_approve=auto_approve,
        )
        if task.get('status') == 'rejected':
            self.send_json({'error': task.get('error')}, 429)
            return
        self.send_json(task, 201)

    def handle_claude_redeliver_hook(self):
        """POST /api/claude/tasks/{id}/redeliver-hook — re-attempt a failed
        completion hook (issue #97). Auth + readonly gated."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._readonly_block():
            return
        ok, msg = ClaudeTaskManager.redeliver_hook(self._claude_task_id)
        if not ok:
            code = 404 if msg == 'task not found' else 400
            self.send_json({'error': msg}, code)
            return
        self.send_json({'task_id': self._claude_task_id, 'status': msg}, 202)

    def handle_claude_create_terminal_task(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        # Body is optional — accept {} or no body at all.
        workdir = None
        try:
            data = self.read_json_body()
            if isinstance(data, dict):
                workdir = data.get('workdir') or None
        except (json.JSONDecodeError, ValueError):
            pass
        task = ClaudeTaskManager.create_terminal_task(workdir=workdir)
        if task.get('status') == 'rejected':
            self.send_json({'error': task.get('error')}, 429)
            return
        # Pre-arm the ttyd entry script so the next /oauth/terminal/ load
        # attaches to this session instead of dropping to a fresh bash.
        if task.get('status') != 'error':
            session_name = task.get('tmux_session', '')
            try:
                with open('/tmp/.claude-terminal-pending', 'w') as f:
                    f.write(session_name)
            except OSError:
                pass
        self.send_json(task, 201)

    def handle_claude_followup(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        prompt = data.get('prompt', '').strip()
        if not prompt:
            self.send_json({'error': 'prompt is required'}, 400)
            return
        # submit defaults to True (normal send). False = paste-only (no Enter).
        submit = data.get('submit', True) is not False
        task, err = ClaudeTaskManager.send_followup(self._claude_task_id, prompt, submit=submit)
        if task is None:
            self.send_json({'error': err or 'Task not found'}, 404)
            return
        self.send_json(task)

    def handle_claude_rename_task(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        if not isinstance(data, dict):
            self.send_json({'error': 'Body must be a JSON object'}, 400)
            return
        meta, err = ClaudeTaskManager.rename_task(self._claude_task_id, data)
        if meta is None:
            status = 404 if err == 'not_found' else 400
            self.send_json({'error': err or 'Task not found'}, status)
            return
        self.send_json(meta)

    def handle_claude_delete_task(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        task = ClaudeTaskManager.delete_task(self._claude_task_id)
        if task is None:
            self.send_json({'error': 'Task not found'}, 404)
            return
        self.send_json(task)

    def handle_claude_get_token(self):
        if not self.check_oauth_only():
            self.send_json({'error': 'This endpoint requires OAuth2 authentication (browser session)'}, 401)
            return
        token = ClaudeTaskManager.get_or_create_token()
        self.send_json({'token': token})

    # Only ever bounce the WebView into the app proxy or the terminal proxy —
    # anything else would be an open redirect on an authenticated endpoint.
    _APP_SESSION_NEXT_RE = re.compile(r'^/api/(app-proxy/\d+|terminal-proxy)(/.*)?$')

    def handle_app_session_mint(self):
        """GET /api/claude/apps/session?next=/api/app-proxy/<port>/

        Bearer-authenticated bootstrap for embedding an app in a native
        WebView: validates the caller, mints a short-lived app-session cookie
        (see ClaudeTaskManager.mint_app_session) and 302s to `next`. The
        WebView attaches its Authorization header to this one request, stores
        the Set-Cookie, follows the redirect, and every sub-resource the
        embedded app loads from then on authenticates via the cookie."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        qs = urllib.parse.urlsplit(self.path).query
        next_path = (urllib.parse.parse_qs(qs).get('next') or [''])[0]
        if not self._APP_SESSION_NEXT_RE.match(next_path):
            self.send_json({'error': 'next must be an /api/app-proxy/<port>/ path'}, 400)
            return
        value = ClaudeTaskManager.mint_app_session()
        # Secure only when the edge says HTTPS — a hard Secure flag would break
        # local http (kubectl port-forward) development.
        secure = '; Secure' if self.headers.get('X-Forwarded-Proto', '') == 'https' else ''
        cookie = (f'{self.APP_SESSION_COOKIE}={value}; Path=/; HttpOnly; SameSite=Lax; '
                  f'Max-Age={ClaudeTaskManager.APP_SESSION_TTL_SECONDS}{secure}')
        self.send_response(302)
        self.send_header('Set-Cookie', cookie)
        self.send_header('Location', next_path)
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()

    def handle_claude_list_assistants(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        self.send_json({'assistants': ClaudeTaskManager.available_assistants()})

    # ── Hypervisor: structured agent-session chat ───────────────────────────
    # Each thread is a HypervisorSession (see hypervisor_session.py): the
    # selected CLI is run in its machine-readable streaming mode over pipes (no
    # tmux, no TTY) and normalized into a canonical event stream persisted as
    # events.jsonl. The frontend renders those events — it never sees a
    # terminal, so there are no interactive dialogs to answer and no rendered
    # pane to un-scrape. Adding an assistant means adding one adapter; this
    # facade and the frontend don't change.

    def _hv_session_or_404(self, thread_id):
        s = HypervisorSession.get(thread_id) if _HYPERVISOR_AVAILABLE else None
        if s is None:
            self.send_json({'error': 'Thread not found'}, 404)
            return None
        return s

    def handle_hypervisor_config(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        self.send_json({
            'enabled': HYPERVISOR_ENABLED and _HYPERVISOR_AVAILABLE,
            'defaultAssistant': HYPERVISOR_DEFAULT_ASSISTANT,
            'workdir': HYPERVISOR_WORKDIR,
            'readOnly': READONLY_MODE,
            'assistants': ClaudeTaskManager.available_assistants(),
            # Invocable skills + custom slash commands the composer's `/` picker
            # offers (issue #302). Claude-scoped: the Hypervisor runs Claude at
            # /home/dev and that adapter is the one confirmed to expand `/name`
            # inline in headless print mode — so the frontend shows the picker
            # only for the `claude` assistant. Provider expansion (ante/opencode)
            # is a follow-up: the source can grow a `systems` filter without a
            # client redesign.
            'commands': self._hypervisor_commands(),
            # Whether POST /api/hypervisor/transcribe has a provider key to
            # work with (issue #396) — clients without a browser SpeechRecognition
            # (the mobile app) show the mic only when this is true.
            'stt': SpeechTranscriber.available(),
        })

    @staticmethod
    def _hypervisor_commands():
        """Composer picker source: custom `/commands` + invocable skills.

        Each entry: {name, kind: 'command'|'skill', description,
        argument_hint, scope}. Deduped by name (a custom command shadows a
        same-named skill). Never raises — a discovery hiccup degrades to
        fewer picker entries, never a broken config response."""
        out = []
        seen = set()
        # Custom slash commands (.claude/commands/*.md) — Claude-native, not in
        # the skills registry.
        if _SKILLS_AVAILABLE and discover_commands is not None:
            try:
                for c in discover_commands():
                    if c['name'] in seen:
                        continue
                    seen.add(c['name'])
                    out.append({**c, 'kind': 'command'})
            except Exception as e:
                print(f'[hypervisor] command discovery failed: {e}',
                      file=sys.stderr)
        # Invocable skills the registry already tracks, filtered to those Claude
        # can actually run (systems includes 'claude').
        if _SKILLS_AVAILABLE and SkillsSyncer is not None:
            try:
                for r in SkillsSyncer.snapshot():
                    if not r.user_invocable or 'claude' not in r.systems:
                        continue
                    if r.name in seen:
                        continue
                    seen.add(r.name)
                    out.append({
                        'name': r.name,
                        'kind': 'skill',
                        'description': r.description,
                        'argument_hint': r.argument_hint,
                        'scope': r.scope,
                    })
            except Exception as e:
                print(f'[hypervisor] skill picker snapshot failed: {e}',
                      file=sys.stderr)
        out.sort(key=lambda c: c['name'])
        return out

    def handle_hypervisor_list_threads(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        # ?deleted=1 → the "Recently deleted" trash view (soft-deleted only).
        # The query string is stripped from the route match, so re-parse it.
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        only_deleted = (qs.get('deleted') or [''])[0] in ('1', 'true')
        if not _HYPERVISOR_AVAILABLE:
            self.send_json({'threads': []})
            return
        threads = HypervisorSession.list(only_deleted=only_deleted)
        self.send_json({'threads': threads})

    def handle_hypervisor_create_thread(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if not (HYPERVISOR_ENABLED and _HYPERVISOR_AVAILABLE):
            self.send_json({'error': 'Hypervisor is disabled'}, 404)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        message = (data.get('message') or '').strip()
        assistant = ClaudeTaskManager.resolve_assistant(
            data.get('assistant') or HYPERVISOR_DEFAULT_ASSISTANT)
        workdir = data.get('workdir') or HYPERVISOR_WORKDIR
        # Per-thread model choice (#308) — validated against the assistant's
        # allow-list; '' when the assistant offers no choice (adapter default).
        model = ClaudeTaskManager.resolve_model(assistant, data.get('model'))
        # cli_cmd is only consumed by the non-structured fallback adapter; the
        # Claude adapter builds its own argv. auto_approve keeps any fallback
        # CLI from blocking on an approval it can't answer.
        cli_cmd = ClaudeTaskManager.assistant_command(assistant, auto_approve=True)
        try:
            session = HypervisorSession.create(
                assistant=assistant, workdir=workdir, cli_cmd=cli_cmd,
                preamble=HYPERVISOR_PREAMBLE, title=message, model=model)
        except Exception as e:
            self.send_json({'error': f'failed to start chat: {e}'}, 500)
            return
        if message:
            session.send(message)
        self.send_json({'thread': session.summary()}, 201)

    def handle_hypervisor_get_thread(self, thread_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        session = self._hv_session_or_404(thread_id)
        if session is None:
            return
        qs = self.path.split('?', 1)[1] if '?' in self.path else ''
        try:
            since = int((urllib.parse.parse_qs(qs).get('since') or ['0'])[0])
        except (TypeError, ValueError):
            since = 0
        # Prefer Claude Code's own JSONL session log (structured, complete,
        # restart-proof) for the transcript; fall back to the live events.jsonl
        # capture when it's unavailable. `source` tells the client which won.
        tx = session.transcript(since_seq=since)
        self.send_json({
            'thread': session.summary(),
            'events': tx['events'],
            'source': tx['source'],
        })

    def handle_hypervisor_get_activity(self, thread_id):
        """Per-thread observability view: a normalized activity timeline (tool
        calls + results + durations, errors, status transitions) derived from
        events.jsonl, plus a bounded tail of the runner.log (subprocess stderr +
        runner diagnostics). Read-only; behind the same auth gate as the thread
        endpoint."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        session = self._hv_session_or_404(thread_id)
        if session is None:
            return
        if hv_build_activity is None:
            self.send_json({'error': 'Hypervisor unavailable'}, 503)
            return
        activity = hv_build_activity(session.read_events())
        activity['thread'] = session.summary()
        activity['runner_log'] = session.read_runner_log()
        self.send_json(activity)

    def handle_hypervisor_health(self):
        """Global hypervisor runner health: live turn/subprocess counts and a
        per-thread status + recent-error snapshot. Read-only, auth-gated."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if not _HYPERVISOR_AVAILABLE or hv_health is None:
            self.send_json({'error': 'Hypervisor unavailable'}, 503)
            return
        self.send_json(hv_health())

    def handle_hypervisor_send_message(self, thread_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        session = self._hv_session_or_404(thread_id)
        if session is None:
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        message = (data.get('message') or '').strip()
        if not message:
            self.send_json({'error': 'message is required'}, 400)
            return
        if session.status() == 'running':
            self.send_json({'error': 'assistant is still responding'}, 409)
            return
        session.send(message)
        self.send_json({'ok': True})

    def handle_hypervisor_transcribe(self):
        """POST /api/hypervisor/transcribe (issue #396, tier 1) — raw audio in
        the body, transcript out. Serves clients without a browser SpeechRecognition
        (the Expo mobile app); the transcript feeds the ordinary send path
        client-side, so this endpoint never touches a thread itself."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            content_length = int(self.headers.get('Content-Length', 0) or 0)
        except ValueError:
            self.send_json({'error': 'invalid Content-Length'}, 400)
            return
        if content_length <= 0:
            self.send_json({'error': 'empty audio body'}, 400)
            return
        if content_length > SpeechTranscriber.MAX_AUDIO_BYTES:
            self.send_json({'error': 'audio too large '
                            f'(max {SpeechTranscriber.MAX_AUDIO_BYTES} bytes)'}, 413)
            return
        audio = self.rfile.read(content_length)
        ctype = (self.headers.get('Content-Type')
                 or 'application/octet-stream').split(';')[0].strip()
        # URL-encoded like the file-upload headers (header values are ISO-8859-1).
        filename = urllib.parse.unquote(
            (self.headers.get('X-Filename') or '').strip()) or None
        text, err = SpeechTranscriber.transcribe(audio, ctype, filename)
        if err is not None:
            self.send_json({'error': err[1]}, err[0])
            return
        self.send_json({'text': text})

    def handle_hypervisor_rename_thread(self, thread_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        session = self._hv_session_or_404(thread_id)
        if session is None:
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        title = (data.get('title') or '').strip()
        if not title:
            self.send_json({'error': 'title is required'}, 400)
            return
        summary = session.set_title(title)
        if summary is None:
            self.send_json({'error': 'not found'}, 404)
            return
        self.send_json({'thread': summary})

    def handle_hypervisor_set_model(self, thread_id):
        """Switch a live thread's model (#308). Takes effect on the next turn —
        the Claude adapter reads ctx['model'] each build, and `--resume` carries
        the same session across the change. Validated against the thread's own
        assistant so a client can't smuggle in an off-list model."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        session = self._hv_session_or_404(thread_id)
        if session is None:
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        meta = session.read_meta() or {}
        model = ClaudeTaskManager.resolve_model(
            meta.get('assistant') or '', data.get('model'))
        summary = session.set_model(model)
        if summary is None:
            self.send_json({'error': 'not found'}, 404)
            return
        self.send_json({'thread': summary})

    def handle_hypervisor_stop(self, thread_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        session = self._hv_session_or_404(thread_id)
        if session is None:
            return
        stopped = session.stop()
        # Idle threads are a safe no-op — report 'idle' rather than erroring so
        # the client can fire-and-forget without racing the turn's completion.
        self.send_json({'ok': True, 'stopped': stopped})

    # ── Cross-turn watchers (issue #402) ──────────────────────────────────
    # The runner-owned watch primitive: an in-turn agent arms a watcher here
    # (via the dashboard MCP `watch` tool); hypervisor_session.WATCHERS polls
    # the condition after the turn ends and injects the outcome back into the
    # thread as a follow-up turn. These endpoints are thin wrappers over it.
    def handle_hypervisor_list_watchers(self, thread_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        session = self._hv_session_or_404(thread_id)
        if session is None:
            return
        if hv_watchers is None:
            self.send_json({'error': 'Hypervisor unavailable'}, 503)
            return
        self.send_json({'watchers': hv_watchers.list(thread_id)})

    def handle_hypervisor_create_watcher(self, thread_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        session = self._hv_session_or_404(thread_id)
        if session is None:
            return
        if hv_watchers is None:
            self.send_json({'error': 'Hypervisor unavailable'}, 503)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        try:
            watcher = hv_watchers.arm(
                thread_id,
                kind=data.get('kind') or '',
                target=data.get('target') or '',
                note=data.get('note') or '',
                interval=data.get('interval'),
                timeout=data.get('timeout'))
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        self.send_json({'watcher': watcher}, 201)

    def handle_hypervisor_cancel_watcher(self, thread_id, watcher_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        session = self._hv_session_or_404(thread_id)
        if session is None:
            return
        if hv_watchers is None:
            self.send_json({'error': 'Hypervisor unavailable'}, 503)
            return
        cancelled = hv_watchers.cancel(thread_id, watcher_id)
        # Cancelling an already-finished/unknown watcher is a reported no-op,
        # mirroring stop()'s fire-and-forget shape.
        self.send_json({'ok': True, 'cancelled': cancelled})

    def handle_hypervisor_delete_thread(self, thread_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        session = self._hv_session_or_404(thread_id)
        if session is None:
            return
        # Soft-delete: the thread drops out of the default listing but its files
        # survive so it can be restored from "Recently deleted".
        session.delete()
        self.send_json({'ok': True})

    def handle_hypervisor_restore_thread(self, thread_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        session = self._hv_session_or_404(thread_id)
        if session is None:
            return
        revived = session.revive()
        self.send_json({'ok': True, 'restored': revived})

    def handle_claude_regenerate_token(self):
        if not self.check_oauth_only():
            self.send_json({'error': 'This endpoint requires OAuth2 authentication (browser session)'}, 401)
            return
        token = ClaudeTaskManager.regenerate_token()
        self.send_json({'token': token})

    def handle_claude_prepare_terminal(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        task_id = self._claude_task_id
        task = ClaudeTaskManager.get_task(task_id)
        if task is None:
            self.send_json({'error': 'Task not found'}, 404)
            return
        session_name = task.get('tmux_session', f'kube-coder-{task_id}')
        # Wait up to ~3s for the tmux session to be visible to has-session
        # before declaring ourselves ready. Without this, the SPA can load
        # the iframe and run terminal-entry.sh while the session that
        # create_task spawned is still mid-registration; the entry script
        # falls through to bash and the user sees a fresh shell instead
        # of their task. Cheap on the happy path — has-session is ~1ms.
        deadline = time.time() + 3.0
        session_ready = False
        while time.time() < deadline:
            check = subprocess.run(
                ['tmux', 'has-session', '-t', session_name],
                capture_output=True,
            )
            if check.returncode == 0:
                session_ready = True
                break
            time.sleep(0.1)
        try:
            with open('/tmp/.claude-terminal-pending', 'w') as f:
                f.write(session_name)
            self.send_json({
                'ok': True,
                'session': session_name,
                'session_ready': session_ready,
            })
        except OSError as e:
            self.send_json({'error': str(e)}, 500)

    # --- Webhook handlers ---
    # CRUD endpoints (list/get/create/delete) reuse check_claude_auth — they
    # manage webhook *configs* and require the same trust as creating tasks.
    # The receiver endpoint (handle_webhook_receive) is intentionally NOT
    # behind that auth: external services authenticate via HMAC of the body.

    def handle_claude_scroll_mode(self):
        """Toggle tmux copy-mode for a task's pane.
        Replaces the user holding Ctrl+B [ to scroll and `q` to exit —
        instead the SPA shows a single Scroll-mode button that POSTs here.
        Once in copy-mode, arrow keys / Page Up / mouse wheel all navigate
        the scrollback (xterm.js's alt-screen wheel→arrow conversion lands
        on copy-mode's own arrow bindings, which is what we want here)."""
        if self._readonly_block():
            return
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        action = (data.get('action') or '').strip().lower()
        # enter/exit toggle copy-mode. The scroll directions drive copy-mode
        # navigation server-side so touch clients (mobile sends no wheel events,
        # so xterm's wheel->arrow conversion inside the ttyd iframe never fires)
        # can scroll the scrollback by POSTing here instead.
        SCROLL_CMDS = {
            'up': 'scroll-up', 'down': 'scroll-down',
            'page-up': 'page-up', 'page-down': 'page-down',
        }
        if action not in ('enter', 'exit') and action not in SCROLL_CMDS:
            self.send_json(
                {'error': "action must be 'enter', 'exit', or a scroll direction"},
                400,
            )
            return
        task_id = self._claude_task_id
        task = ClaudeTaskManager.get_task(task_id)
        if task is None:
            self.send_json({'error': 'Task not found'}, 404)
            return
        session_name = task.get('tmux_session', f'kube-coder-{task_id}')
        if action == 'enter':
            cmd = ['tmux', 'copy-mode', '-t', session_name]
        elif action == 'exit':
            cmd = ['tmux', 'send-keys', '-t', session_name, '-X', 'cancel']
        else:
            # Repeat the copy-mode motion `lines` times so one touch gesture can
            # scroll several lines. Clamp so a fling can't send a runaway count.
            try:
                lines = int(data.get('lines') or 1)
            except (TypeError, ValueError):
                lines = 1
            lines = max(1, min(lines, 40))
            cmd = ['tmux', 'send-keys', '-t', session_name,
                   '-X', '-N', str(lines), SCROLL_CMDS[action]]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            self.send_json({
                'error': result.stderr.strip() or 'tmux command failed',
            }, 500)
            return
        self.send_json({'ok': True, 'mode': action})

    # Named tmux keys a mobile client can send without a physical keyboard.
    # Whitelisted so the request body can never become an arbitrary tmux command.
    _KEYMAP = {
        'shift-tab': 'BTab',  # Claude Code's mode switch (auto-accept etc.)
        'tab': 'Tab',
        'escape': 'Escape',
        'enter': 'Enter',
        'up': 'Up', 'down': 'Down', 'left': 'Left', 'right': 'Right',
        'ctrl-c': 'C-c',
        'space': 'Space',
    }

    def handle_claude_send_key(self):
        """Send a single control key (Shift-Tab, Esc, arrows, Ctrl-C, …) to the
        live tmux session, for mobile clients with no physical keyboard."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body()
        except ValueError:
            self.send_json({'error': 'invalid body'}, 400)
            return
        key = str(data.get('key', '')).strip().lower()
        tmux_key = self._KEYMAP.get(key)
        if not tmux_key:
            self.send_json({'error': 'unsupported key; allowed: ' + ', '.join(sorted(self._KEYMAP))}, 400)
            return
        task = ClaudeTaskManager.get_task(self._claude_task_id)
        if task is None:
            self.send_json({'error': 'Task not found'}, 404)
            return
        session_name = task.get('tmux_session', f'kube-coder-{self._claude_task_id}')
        result = subprocess.run(
            ['tmux', 'send-keys', '-t', session_name, tmux_key],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            self.send_json({'error': result.stderr.strip() or 'tmux send-keys failed'}, 500)
            return
        self.send_json({'ok': True, 'key': key})

    # ── Desktop launcher handlers ──────────────────────────────────────
    # All reads (GET /api/desktop, /api/desktop/{id}) pass through
    # check_claude_auth + allow_none_mode=True so the public-demo can
    # show the seeded launcher. Writes go through _readonly_block first so
    # the public-demo can't add/edit/delete icons.

    def handle_desktop_list(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        self.send_json({'items': DesktopManager.list_items()})

    def handle_desktop_create(self):
        if self._readonly_block():
            return
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            body = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        try:
            item = DesktopManager.create(body)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        self.send_json(item, 201)

    def handle_desktop_update(self, item_id):
        if self._readonly_block():
            return
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            body = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        try:
            item = DesktopManager.update(item_id, body)
        except ValueError as e:
            code = 404 if 'not found' in str(e) else 400
            self.send_json({'error': str(e)}, code)
            return
        self.send_json(item)

    def handle_desktop_delete(self, item_id):
        if self._readonly_block():
            return
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            DesktopManager.delete(item_id)
        except ValueError as e:
            code = 404 if 'not found' in str(e) else 400
            self.send_json({'error': str(e)}, code)
            return
        self.send_json({'ok': True})

    def handle_desktop_reorder(self):
        if self._readonly_block():
            return
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            body = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        ids = body.get('order') if isinstance(body, dict) else None
        try:
            items = DesktopManager.reorder(ids or [])
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        self.send_json({'items': items})

    def handle_desktop_launch(self, item_id):
        """Execute the icon's action server-side. `task` returns the
        created task_id; `shell` returns stdout/stderr/exit_code; `url`
        rejects (client opens the URL directly, server is just bookkeeping).
        Mutations gated by _readonly_block — viewing the launcher in the
        public demo is fine, firing it isn't."""
        if self._readonly_block():
            return
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        item = DesktopManager.get(item_id)
        if not item:
            self.send_json({'error': 'item not found'}, 404)
            return
        action = item.get('action', {})
        kind = action.get('type')
        if kind == 'task':
            task = ClaudeTaskManager.create_task(
                action.get('prompt', ''),
                workdir=action.get('workdir') or '/home/dev',
                source=f'desktop:{item_id}',
                assistant=action.get('assistant'),
            )
            if task.get('status') == 'rejected':
                self.send_json({'error': task.get('error')}, 429)
                return
            if task.get('status') == 'error':
                self.send_json({'error': task.get('error') or 'task spawn failed'}, 500)
                return
            self.send_json({'kind': 'task', 'task_id': task.get('task_id')}, 201)
        elif kind == 'shell':
            try:
                result = subprocess.run(
                    ['bash', '-lc', action.get('command', 'true')],
                    capture_output=True,
                    text=True,
                    timeout=int(action.get('timeout') or DesktopManager.SHELL_TIMEOUT_DEFAULT),
                    cwd='/home/dev',
                )
                self.send_json({
                    'kind': 'shell',
                    'exit_code': result.returncode,
                    'stdout': (result.stdout or '')[-8000:],
                    'stderr': (result.stderr or '')[-2000:],
                })
            except subprocess.TimeoutExpired:
                self.send_json({'error': 'command timed out', 'kind': 'shell'}, 504)
        elif kind == 'url':
            # The client opens URLs directly — no server work needed.
            # Return ok so the client can still report a launch event.
            self.send_json({'kind': 'url', 'url': action.get('url'), 'target': action.get('target', 'blank')})
        else:
            self.send_json({'error': f'unknown action type: {kind}'}, 400)

    def handle_webhook_list(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        # Public view — secrets are stripped out by WebhookManager._public_view
        self.send_json({'webhooks': WebhookManager.list_webhooks()})

    def handle_webhook_get(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        cfg = WebhookManager.get_webhook(self._webhook_id)
        if cfg is None:
            self.send_json({'error': 'Webhook not found'}, 404)
            return
        # Include the receive URL so the dashboard can render a copy button.
        cfg['receive_url'] = self._build_receive_url(self._webhook_id)
        self.send_json(cfg)

    def handle_webhook_create(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        cfg, err = WebhookManager.create_or_update(data)
        if err:
            self.send_json({'error': err}, 400)
            return
        # On create we surface the hmac_secret ONCE so the user can copy it
        # into the upstream service (GitHub/Stripe/etc.). After this, it's
        # only ever returned as hmac_secret_set: true.
        response = WebhookManager._public_view(cfg)
        if cfg.get('hmac_secret'):
            response['hmac_secret_once'] = cfg['hmac_secret']
        response['receive_url'] = self._build_receive_url(cfg['id'])
        self.send_json(response, 201)

    def handle_webhook_delete(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        ok = WebhookManager.delete(self._webhook_id)
        if not ok:
            self.send_json({'error': 'Webhook not found'}, 404)
            return
        self.send_json({'ok': True})

    def handle_provider_keys_list(self):
        # Secrets endpoint — must not be reachable in the unauth public demo
        # (same posture as send_git_config), so allow_none_mode=False.
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        # Masked view only — never returns the key itself.
        self.send_json({'providers': ProviderKeysManager.public_view()})

    def handle_provider_keys_set(self):
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        provider = (data.get('provider') or '').strip()
        ok, err = ProviderKeysManager.set(provider, data.get('key'))
        if not ok:
            self.send_json({'error': err}, 400)
            return
        self.send_json({'ok': True, 'provider': provider})

    def handle_provider_keys_delete(self, provider):
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        ProviderKeysManager.delete(provider)
        self.send_json({'ok': True})

    # --- User MCP servers (issue #353) ---
    # Env values may hold API keys, so gate like provider-keys: never
    # reachable in the unauth public demo (allow_none_mode=False), and the
    # list view is redacted (mcp_registry.public_view — hints, never values).

    def _mcp_registry_gate(self):
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return False
        if not _MCP_REGISTRY_AVAILABLE:
            self.send_json({'error': 'MCP registry unavailable'}, 503)
            return False
        return True

    def handle_mcp_servers_list(self):
        if not self._mcp_registry_gate():
            return
        self.send_json({'servers': mcp_registry.public_view()})

    def handle_mcp_servers_set(self):
        if not self._mcp_registry_gate():
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        name = (data.get('name') or '').strip()
        ok, err = mcp_registry.set_server(
            name, data.get('command') or '',
            args=data.get('args'), env=data.get('env'),
            enabled=data.get('enabled', True))
        if not ok:
            self.send_json({'error': err}, 400)
            return
        self.send_json({'ok': True, 'name': name, 'sync': mcp_registry.sync_all()})

    def handle_mcp_servers_delete(self, name):
        if not self._mcp_registry_gate():
            return
        if not mcp_registry.delete_server(name):
            self.send_json({'error': 'MCP server not found'}, 404)
            return
        self.send_json({'ok': True, 'sync': mcp_registry.sync_all()})

    def handle_subscriptions_list(self):
        # Reports subscription-login status only (plan/expiry) — no token
        # material — but still a secrets-adjacent endpoint, so gate it like
        # provider-keys (never reachable in the unauth public demo).
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        self.send_json({'subscriptions': SubscriptionStatusManager.public_view()})

    def handle_subscriptions_logout(self, provider):
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        ok, err = SubscriptionStatusManager.logout(provider)
        if not ok:
            self.send_json({'error': err or 'logout failed'}, 400)
            return
        self.send_json({'ok': True})

    def handle_webhook_receive(self):
        """Inbound receiver. Auth via HMAC of the raw body — NO bearer token.
        Triggers a Claude task and returns the task_id."""
        cfg = WebhookManager.get_webhook(self._webhook_id, include_secrets=True)
        if cfg is None:
            # Don't leak existence: same response as a real auth failure.
            self.send_json({'error': 'Not found or unauthorized'}, 404)
            return

        # Read the raw body for HMAC verification BEFORE JSON parsing.
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length < 0 or content_length > 1 * 1024 * 1024:  # 1 MiB cap
            self.send_json({'error': 'payload too large'}, 413)
            return
        raw_body = self.rfile.read(content_length) if content_length else b''

        # Pass full headers — Slack/Stripe verifiers read multiple of them
        # (e.g. X-Slack-Request-Timestamp alongside X-Slack-Signature).
        if not WebhookManager.verify_signature(cfg, raw_body, self.headers):
            # Same shape as the not-found response to avoid leaking which is which.
            self.send_json({'error': 'Not found or unauthorized'}, 404)
            return

        # Replay protection: reject identical signed bodies seen within the
        # 5-minute window. Provider-level timestamp checks (Slack/Stripe) and
        # this cache are belt-and-suspenders — Slack/Stripe alone allow up to
        # 5 minutes of replay; this cache closes that window to "exactly once".
        replay_key = (cfg['id'], hashlib.sha256(raw_body).hexdigest())
        if not WebhookManager.REPLAY_CACHE.check_and_record(replay_key):
            self.send_json({'error': 'duplicate request (replay)'}, 409)
            return

        try:
            payload = json.loads(raw_body.decode('utf-8')) if raw_body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json({'error': 'invalid JSON payload'}, 400)
            return

        self._fire_webhook(cfg, payload, status=202)

    def handle_webhook_test(self):
        """Dashboard 'Test' button: fire as if a real call came in, but with
        bearer auth instead of HMAC. Payload comes from the JSON body."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        cfg = WebhookManager.get_webhook(self._webhook_id, include_secrets=True)
        if cfg is None:
            self.send_json({'error': 'Webhook not found'}, 404)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        payload = data.get('payload', {}) if isinstance(data, dict) else {}
        self._fire_webhook(cfg, payload, status=202)

    def _fire_webhook(self, cfg, payload, status=202):
        prompt = WebhookManager.render_prompt(cfg, payload)
        task = ClaudeTaskManager.create_task(
            prompt,
            workdir=cfg.get('workdir') or '/home/dev',
            response_url=cfg.get('response_url'),
            response_secret=cfg.get('response_secret'),
            source=f"webhook:{cfg['id']}",
        )
        if task.get('status') == 'rejected':
            self.send_json({
                'error': task.get('error'),
                'webhook_id': cfg['id'],
            }, 429)
            return
        # Propagate task-creation failures (tmux unreachable, fs error) so
        # the upstream sees a 5xx and can retry, rather than a 202 with a
        # task_id that never runs.
        if task.get('status') == 'error':
            self.send_json({
                'error': task.get('error') or 'failed to spawn task',
                'webhook_id': cfg['id'],
                'task_id': task.get('task_id'),
            }, 502)
            return
        EventBroker.publish('trigger.fired', {
            'trigger_type': 'webhook',
            'trigger_id': cfg['id'],
            'task_id': task['task_id'],
        })
        self.send_json({
            'task_id': task['task_id'],
            'webhook_id': cfg['id'],
            'status': task['status'],
        }, status)

    def _build_receive_url(self, webhook_id):
        """Construct the public URL the upstream service should POST to.
        Uses the Host header; ingress strips /oauth so we don't prepend it."""
        host = self.headers.get('Host', '')
        proto = self.headers.get('X-Forwarded-Proto', 'https')
        if not host:
            return f'/api/webhooks/{webhook_id}'
        return f'{proto}://{host}/api/webhooks/{webhook_id}'

    # --- Conversation Gateway handlers (issue #306) ---
    # The inbound webhook + Meta GET handshake are intentionally NOT behind
    # check_claude_auth: external providers (Twilio/Meta) can't carry an OAuth
    # session, so they authenticate via the provider signature verified in the
    # adapter — exactly the handle_webhook_receive posture. The link CRUD
    # endpoints DO require bearer/OAuth (they manage identity bindings).

    def _gateway_raw_request(self, method):
        """Build a gateway.RawRequest from this HTTP request: raw body (capped),
        parsed form (Twilio), full external URL (Twilio signs over it), headers,
        and query. Returns None if the body exceeds the 1 MiB cap."""
        try:
            content_length = int(self.headers.get('Content-Length', 0) or 0)
        except (TypeError, ValueError):
            content_length = 0
        if content_length < 0 or content_length > 1 * 1024 * 1024:
            return None
        raw_body = self.rfile.read(content_length) if content_length else b''
        ctype = self.headers.get('Content-Type', '') or ''
        form = {}
        if 'application/x-www-form-urlencoded' in ctype and raw_body:
            parsed = urllib.parse.parse_qs(
                raw_body.decode('utf-8', 'replace'), keep_blank_values=True)
            form = {k: v[0] for k, v in parsed.items()}
        host = self.headers.get('Host', '')
        proto = self.headers.get('X-Forwarded-Proto', 'https')
        path = self.path.split('?', 1)[0]
        url = f'{proto}://{host}{path}' if host else path
        query = {k: v[0] for k, v in urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query).items()}
        headers = {k: v for k, v in self.headers.items()}
        return RawRequest(method=method, url=url, headers=headers,
                          raw_body=raw_body, form=form, query=query)

    def handle_gateway_whatsapp_webhook(self):
        """Inbound WhatsApp webhook. Provider-signature authed (in the adapter),
        idempotent on the provider message id, fast 200 so retries stop."""
        gw = get_gateway()
        adapter = get_gateway_adapter()
        if gw is None or adapter is None:
            self.send_json({'error': 'gateway unavailable'}, 503)
            return
        raw = self._gateway_raw_request('POST')
        if raw is None:
            self.send_json({'error': 'payload too large'}, 413)
            return
        result = gw.handle_inbound(adapter, raw)
        self.send_json({'status': result.action}, result.status)

    def handle_gateway_whatsapp_verify(self):
        """Meta Cloud API GET verification handshake — echo hub.challenge on a
        verify-token match, else 403. No-op (403) for providers without a
        handshake (Twilio)."""
        adapter = get_gateway_adapter()
        if adapter is None:
            self.send_response(503)
            self.end_headers()
            return
        raw = self._gateway_raw_request('GET')
        challenge = adapter.handshake(raw) if raw is not None else None
        if challenge is not None:
            self.send_response(200)
            self.send_header('Content-type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(str(challenge).encode('utf-8'))
        else:
            self.send_response(403)
            self.end_headers()

    def _current_bearer_token(self):
        """The workspace Bearer token the app already holds — stored with the
        pairing so revoking/rotating the token also orphans the WhatsApp link."""
        try:
            with open(ClaudeTaskManager.TOKEN_FILE) as f:
                return f.read().strip()
        except OSError:
            return ''

    def handle_gateway_link_create(self):
        """Mint a single-use pairing code (dashboard 'Link WhatsApp'). The user
        sends this code once over WhatsApp to bind their number."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        gw = get_gateway()
        if gw is None:
            self.send_json({'error': 'gateway unavailable'}, 503)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        workspace = (data.get('workspace') or 'workspace').strip()[:64] or 'workspace'
        host = (data.get('workspace_host') or self.headers.get('Host', '')).strip()
        token = self._current_bearer_token()
        if not token:
            self.send_json({'error': 'workspace has no API token yet'}, 409)
            return
        try:
            code = gw.registry.mint_pairing_code(
                workspace=workspace, workspace_host=host, token=token,
                ttl_seconds=600)
        except Exception as e:
            self.send_json({'error': f'could not mint pairing code: {e}'}, 500)
            return
        self.send_json({
            'code': code,
            'expires_in': 600,
            'whatsapp_number': GATEWAY_WHATSAPP_NUMBER,
            'workspace': workspace,
        }, 201)

    def handle_gateway_link_list(self):
        """List the identity bindings — redacted (no raw number, no token)."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        gw = get_gateway()
        if gw is None:
            self.send_json({'links': [], 'available': False})
            return
        self.send_json({
            'links': gw.registry.list_links(),
            'available': True,
            'whatsapp_number': GATEWAY_WHATSAPP_NUMBER,
            'proactive': get_gateway_adapter().capabilities.proactive
                if get_gateway_adapter() else False,
        })

    def handle_gateway_link_delete(self, link_id):
        """Revoke a link by id (== identity hash). The `unlink` keyword does the
        same over WhatsApp."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        gw = get_gateway()
        if gw is None:
            self.send_json({'error': 'gateway unavailable'}, 503)
            return
        ok = gw.registry.revoke(link_id)
        self.send_json({'ok': ok}, 200 if ok else 404)

    # --- Messaging provider config (issue #329) ---
    # The data-driven catalog + per-workspace credential store the Settings
    # "Messaging / WhatsApp" section (stage 3) drives. Credentials are stored on
    # the PVC (0600, redacted in every read), and saving hot-swaps the live
    # adapter so the inbound webhook uses the new provider with no pod restart.

    def handle_gateway_providers(self):
        """The provider catalog + each provider's field spec (issue #328), so the
        Settings form is entirely data-driven. No secrets — just the schema."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if gw_list_providers is None:
            self.send_json({'providers': [], 'available': False})
            return
        self.send_json({
            'providers': [s.to_dict() for s in gw_list_providers()],
            'available': True,
        })

    def handle_gateway_credentials_get(self):
        """Current selection, redacted (never a secret value)."""
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        self.send_json({'credentials': GatewayCredentialsManager.public_view()})

    def handle_gateway_credentials_put(self):
        """Set provider + creds + sender number, then hot-swap the live adapter.
        Responds with the redacted view (so the client never round-trips a
        secret back)."""
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        provider_id = (data.get('provider_id') or '').strip()
        creds = data.get('creds')
        if creds is not None and not isinstance(creds, dict):
            self.send_json({'error': 'creds must be an object'}, 400)
            return
        ok, err = GatewayCredentialsManager.set(
            provider_id, creds or {}, sender_number=data.get('sender_number'))
        if not ok:
            self.send_json({'error': err}, 400)
            return
        rebuild_gateway_adapter()
        self.send_json({'ok': True,
                        'credentials': GatewayCredentialsManager.public_view()})

    def handle_gateway_credentials_delete(self):
        """Clear the store (disables the channel) and hot-swap back to the env
        fallback."""
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        GatewayCredentialsManager.clear()
        rebuild_gateway_adapter()
        self.send_json({'ok': True})

    def handle_gateway_test(self):
        """Validate the stored creds against the provider — no message to a real
        user. 400 when nothing is configured yet."""
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        ok, detail = GatewayCredentialsManager.validate_stored()
        if not ok and detail == 'no credentials configured':
            self.send_json({'ok': False, 'detail': detail}, 400)
            return
        self.send_json({'ok': ok, 'detail': detail})

    # --- Walkie-Talkie preview (in-app loopback) ---
    # Bearer-authed (it's the app user). Drives the SAME gateway core through the
    # loopback adapter so the preview shows exactly what WhatsApp would see —
    # projection, choice→buttons, chunking, ack/final, out-of-window template —
    # while running a real Hypervisor turn locally.

    def _gw_preview_bundle(self):
        """(gateway, preview, loopback) or None if the gateway is unavailable."""
        gw = get_gateway()
        preview = get_gateway_preview()
        loop = get_gateway_loopback()
        if gw is None or preview is None or loop is None:
            return None
        return gw, preview, loop

    def _gw_internal_status(self, gw):
        """(thread_id, busy) for the internal identity's active thread."""
        rec = gw.registry.lookup(INTERNAL_IDENTITY)
        if not rec:
            return None, False
        binding = gw.registry.select_binding(rec)
        thread_id = binding.get('default_thread_id') if binding else None
        busy = False
        if thread_id and _HYPERVISOR_AVAILABLE:
            s = HypervisorSession.get(thread_id)
            busy = bool(s and s.status() == 'running')
        return thread_id, busy

    def handle_gateway_internal_inbound(self):
        """Send a message into the loopback as if it arrived over WhatsApp."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        bundle = self._gw_preview_bundle()
        if bundle is None:
            self.send_json({'error': 'gateway unavailable'}, 503)
            return
        gw, preview, loop = bundle
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        text = (data.get('text') or '').strip()
        button = (data.get('button') or '').strip()
        display = button or text
        if not display:
            self.send_json({'error': 'text is required'}, 400)
            return
        # Record the user's own bubble + the inbound "wire" (the provider webhook
        # shape WhatsApp would POST) so the UI can show both sides of the wire.
        preview.transcript.add('in', display, kind='message', wire={
            'inbound': {'from': INTERNAL_IDENTITY, 'text': text, 'button': button}})
        raw = RawRequest(method='POST', form={
            'from': INTERNAL_IDENTITY, 'text': text, 'button': button})
        result = gw.handle_inbound(loop, raw)
        self.send_json({'ok': True, 'action': result.action,
                        'cursor': preview.transcript.cursor()})

    def handle_gateway_internal_transcript(self):
        """Poll the preview transcript (both directions, each with its wire
        payload) since a cursor, plus link/simulate/thread status."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        bundle = self._gw_preview_bundle()
        if bundle is None:
            self.send_json({'available': False, 'messages': []})
            return
        gw, preview, loop = bundle
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            since = int((qs.get('since') or ['0'])[0])
        except (TypeError, ValueError):
            since = 0
        thread_id, busy = self._gw_internal_status(gw)
        self.send_json({
            'available': True,
            'messages': preview.transcript.since(since),
            'cursor': preview.transcript.cursor(),
            'linked': gw.registry.is_linked(INTERNAL_IDENTITY),
            'simulate_out_of_window': preview.simulate_out_of_window,
            'provider': loop.wire_provider.name,
            'identity': INTERNAL_IDENTITY,
            'busy': busy,
            'thread_id': thread_id,
        })

    def handle_gateway_internal_control(self):
        """Link (mint+inject a pairing code so the real enrollment path shows in
        the transcript), toggle the out-of-window simulation, or reset."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        bundle = self._gw_preview_bundle()
        if bundle is None:
            self.send_json({'error': 'gateway unavailable'}, 503)
            return
        gw, preview, loop = bundle
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        action = (data.get('action') or '').strip()
        if action == 'link':
            if gw.registry.is_linked(INTERNAL_IDENTITY):
                self.send_json({'ok': True, 'linked': True})
                return
            token = self._current_bearer_token()
            if not token:
                self.send_json({'error': 'workspace has no API token yet'}, 409)
                return
            code = gw.registry.mint_pairing_code(
                workspace=preview.workspace,
                workspace_host=self.headers.get('Host', ''), token=token)
            # Inject the code as a loopback inbound so the REAL pairing path runs
            # and the code→"✅ Linked" exchange is visible in the transcript.
            preview.transcript.add('in', code, kind='notice', wire={
                'inbound': {'from': INTERNAL_IDENTITY, 'text': code}})
            gw.handle_inbound(loop, RawRequest(form={
                'from': INTERNAL_IDENTITY, 'text': code}))
            self.send_json({'ok': True,
                            'linked': gw.registry.is_linked(INTERNAL_IDENTITY)})
        elif action == 'simulate':
            preview.simulate_out_of_window = bool(data.get('on'))
            self.send_json({'ok': True,
                            'simulate_out_of_window': preview.simulate_out_of_window})
        elif action == 'reset':
            gw.registry.revoke_identity(INTERNAL_IDENTITY)
            preview.transcript.clear()
            preview.simulate_out_of_window = False
            self.send_json({'ok': True})
        else:
            self.send_json({'error': "action must be 'link', 'simulate', or 'reset'"}, 400)

    # --- Cron handlers ---
    # CRUD uses check_claude_auth (dashboard / scripts). The cron-fire receiver
    # uses the per-cron fire_token instead — k8s CronJob pods are not part of
    # the OAuth session.

    def handle_cron_list(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        crons = CronManager.list_crons()
        # Decorate with k8s status — best-effort, won't fail the request.
        for c in crons:
            try:
                c.update(CronManager.kubectl_status(c['id']))
            except Exception:
                pass
        self.send_json({'crons': crons})

    def handle_cron_get(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        cfg = CronManager.get_cron(self._cron_id)
        if cfg is None:
            self.send_json({'error': 'Cron not found'}, 404)
            return
        cfg.update(CronManager.kubectl_status(self._cron_id))
        self.send_json(cfg)

    def handle_cron_create(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        cfg, err = CronManager.create_or_update(data)
        # err may be a "soft" error (k8s apply failed but config saved); still 4xx.
        if cfg is None:
            self.send_json({'error': err}, 400)
            return
        response = CronManager._public_view(cfg)
        if err:
            response['warning'] = err
            self.send_json(response, 202)
            return
        self.send_json(response, 201)

    def handle_cron_delete(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        ok = CronManager.delete(self._cron_id)
        if not ok:
            self.send_json({'error': 'Cron not found'}, 404)
            return
        self.send_json({'ok': True})

    def handle_cron_action(self):
        """suspend / resume / run — dashboard buttons."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        action = self._cron_action
        if action == 'suspend':
            cfg = CronManager.set_suspended(self._cron_id, True)
            if cfg is None:
                self.send_json({'error': 'Cron not found'}, 404)
                return
            self.send_json(CronManager._public_view(cfg))
        elif action == 'resume':
            cfg = CronManager.set_suspended(self._cron_id, False)
            if cfg is None:
                self.send_json({'error': 'Cron not found'}, 404)
                return
            self.send_json(CronManager._public_view(cfg))
        elif action == 'run':
            ok, info = CronManager.run_now(self._cron_id)
            if not ok:
                self.send_json({'error': info}, 500)
                return
            self.send_json({'ok': True, 'job': info})
        elif action == 'rotate-token':
            cfg, new_token = CronManager.rotate_token(self._cron_id)
            if cfg is None:
                self.send_json({'error': 'rotate failed (see pod logs)'}, 500)
                return
            response = CronManager._public_view(cfg)
            # One-time reveal of the new token, matching webhook secret-reveal UX
            response['fire_token_once'] = new_token
            self.send_json(response)
        else:
            self.send_json({'error': 'unknown action'}, 400)

    def handle_cron_fire(self):
        """Receiver called by the k8s CronJob pod with the per-cron fire_token.
        Renders the cron's prompt template against its static payload and
        spawns a Claude task. Never touches OAuth headers — this is an
        internal-cluster call."""
        auth = self.headers.get('Authorization', '')
        token = auth[7:].strip() if auth.startswith('Bearer ') else ''
        ok, cfg = CronManager.verify_fire_token(self._cron_id, token)
        if not ok or cfg is None:
            # Don't leak existence; same response for unknown id vs bad token.
            self.send_json({'error': 'Not found or unauthorized'}, 404)
            return
        # Refuse to spawn tasks for suspended crons. Belt-and-suspenders: the
        # CronJob shouldn't fire when suspended, but if someone hits this
        # endpoint manually we want the suspend flag to be authoritative.
        if cfg.get('suspended'):
            self.send_json({'error': 'cron is suspended'}, 409)
            return
        prompt = CronManager.render_prompt(cfg)
        task = ClaudeTaskManager.create_task(
            prompt,
            workdir=cfg.get('workdir') or '/home/dev',
            response_url=cfg.get('response_url'),
            response_secret=cfg.get('response_secret'),
            source=f"cron:{cfg['id']}",
        )
        if task.get('status') == 'rejected':
            self.send_json({'error': task.get('error'), 'cron_id': cfg['id']}, 429)
            return
        EventBroker.publish('trigger.fired', {
            'trigger_type': 'cron',
            'trigger_id': cfg['id'],
            'task_id': task['task_id'],
        })
        self.send_json({
            'task_id': task['task_id'],
            'cron_id': cfg['id'],
            'status': task['status'],
        }, 202)

    # --- Memory API handlers ---------------------------------------------
    # The dashboard's Memory tab consumes these endpoints. They mirror the
    # MCP tool surface (mcp_memory.py) so the dashboard and Claude share a
    # single SQLite store with consistent semantics.

    def _memory_unavailable(self):
        if _MEMORY_AVAILABLE:
            return False
        self.send_json({
            'error': 'memory subsystem unavailable',
            'detail': 'memory.manager failed to import; check server logs',
        }, 503)
        return True

    def _memory_actor(self):
        """Derive a stable `source` string for memory writes."""
        email = self.headers.get('X-Auth-Request-Email') or ''
        if email:
            return f'dashboard:{email}'
        user = self.headers.get('X-Auth-Request-User') or self.headers.get('Remote-User') or ''
        if user:
            return f'dashboard:{user}'
        auth_header = self.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            tok = auth_header[7:].strip()
            fp = hashlib.sha256(tok.encode()).hexdigest()[:8]
            return f'api:{fp}'
        return 'unknown'

    def _memory_error(self, e):
        if isinstance(e, MemNotFound):
            self.send_json({'error': str(e), 'code': 'not_found'}, 404)
            return
        if isinstance(e, MemConflict):
            self.send_json({'error': str(e), 'code': 'conflict'}, 409)
            return
        if isinstance(e, MemValidationError):
            self.send_json({'error': str(e), 'code': 'validation'}, 400)
            return
        if isinstance(e, MemError):
            self.send_json({'error': str(e), 'code': e.code}, 400)
            return
        print(f'[memory] internal error: {e}', file=sys.stderr)
        self.send_json({'error': str(e), 'code': 'internal'}, 500)

    def handle_memory_list(self, query):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            rows = MemoryManager.list(
                namespace=(query.get('namespace') or [None])[0],
                kind=(query.get('kind') or [None])[0],
                q=(query.get('q') or [None])[0],
                limit=int((query.get('limit') or ['500'])[0]),
            )
        except Exception as e:
            self._memory_error(e); return
        self.send_json({'memories': rows, 'count': len(rows)})

    def handle_memory_get(self, namespace, key):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            row = MemoryManager.get(namespace=namespace, key=key)
        except Exception as e:
            self._memory_error(e); return
        if row is None:
            self.send_json({'error': 'not found', 'code': 'not_found'}, 404)
            return
        # Log read access.
        try:
            actor = self._memory_actor()
            kind = actor.split(':', 1)[0]
            ident = actor.split(':', 1)[1] if ':' in actor else actor
            MemoryManager.log_ref(
                namespace=namespace, key=key,
                ref_kind=kind if kind in ('dashboard', 'api', 'cron', 'task') else 'api',
                ref_id=ident, access_kind='read',
            )
        except Exception:
            pass
        self.send_json({'memory': row})

    def handle_memory_history(self, namespace, key):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            rows = MemoryManager.history(namespace=namespace, key=key)
        except Exception as e:
            self._memory_error(e); return
        self.send_json({'revisions': rows, 'count': len(rows)})

    def handle_memory_refs(self, namespace, key):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            rows = MemoryManager.refs(namespace=namespace, key=key)
        except Exception as e:
            self._memory_error(e); return
        self.send_json({'refs': rows, 'count': len(rows)})

    def handle_memory_relations(self, namespace, key):
        """GET /api/memory/{ns}/{key}/relations — the memory's graph edges with
        ids, so the dashboard can render + unlink them (#134)."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            rows = MemoryManager.relations(namespace=namespace, key=key)
        except Exception as e:
            self._memory_error(e); return
        self.send_json({'relations': rows, 'count': len(rows)})

    def handle_memory_neighbors(self, namespace, key, query):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        depth = int((query.get('depth') or ['1'])[0])
        kinds = query.get('kind') or query.get('kinds') or None
        try:
            rows = MemoryManager.neighbors(
                namespace=namespace, key=key, depth=depth,
                kinds=kinds,
            )
        except Exception as e:
            self._memory_error(e); return
        self.send_json({'neighbors': rows, 'count': len(rows)})

    def handle_memory_stats(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            stats = MemoryManager.stats()
            # Surface syncer status so the dashboard can show "last
            # imported N from Claude's auto-memory · X minutes ago".
            try:
                stats['claude_sync'] = ClaudeMemorySyncer.status()
            except Exception:
                pass
            # Surface embedding-worker status so the Memory tab can show the
            # semantic-search backlog draining (or that it's disabled).
            try:
                stats['embedding_worker'] = EmbeddingWorker.status()
            except Exception:
                pass
            self.send_json(stats)
        except Exception as e:
            self._memory_error(e)

    def handle_memory_sync_claude(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            res = ClaudeMemorySyncer.trigger_sync()
        except Exception as e:
            self._memory_error(e); return
        self.send_json({'status': 'ok', 'result': res})

    def handle_memory_upsert(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        if not isinstance(data, dict):
            self.send_json({'error': 'body must be an object'}, 400)
            return
        try:
            row = MemoryManager.upsert(
                namespace=data.get('namespace', ''),
                key=data.get('key', ''),
                value=data.get('value', ''),
                kind=data.get('kind', 'semantic'),
                tags=data.get('tags', '') or '',
                importance=float(data.get('importance', 0.5)),
                confidence=float(data.get('confidence', 1.0)),
                source=self._memory_actor(),
                expires_at=data.get('expires_at'),
            )
        except Exception as e:
            self._memory_error(e); return
        try:
            actor = self._memory_actor()
            kind = actor.split(':', 1)[0]
            ident = actor.split(':', 1)[1] if ':' in actor else actor
            MemoryManager.log_ref(
                namespace=row['namespace'], key=row['key'],
                ref_kind=kind if kind in ('dashboard', 'api') else 'api',
                ref_id=ident, access_kind='write',
            )
        except Exception:
            pass
        EventBroker.publish('memory.changed', {
            'op': 'upsert', 'namespace': row['namespace'], 'key': row['key'],
        })
        self.send_json({'memory': row}, 200)

    def handle_memory_delete(self, namespace, key):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            row = MemoryManager.soft_delete(
                namespace=namespace, key=key,
                source=self._memory_actor(),
            )
        except Exception as e:
            self._memory_error(e); return
        EventBroker.publish('memory.changed', {
            'op': 'delete', 'namespace': namespace, 'key': key,
        })
        self.send_json({'memory': row, 'deleted': True})

    def handle_memory_link(self, namespace, key):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        if not isinstance(data, dict):
            self.send_json({'error': 'body must be an object'}, 400)
            return
        try:
            rel = MemoryManager.link(
                src_namespace=namespace, src_key=key,
                dst_namespace=data.get('dst_namespace', ''),
                dst_key=data.get('dst_key', ''),
                kind=data.get('kind', 'related-to'),
                weight=float(data.get('weight', 1.0)),
                created_by=self._memory_actor(),
            )
        except Exception as e:
            self._memory_error(e); return
        EventBroker.publish('memory.changed', {
            'op': 'link', 'namespace': namespace, 'key': key,
        })
        self.send_json({'relation': rel}, 201)

    def handle_memory_unlink(self, namespace, key, relation_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            deleted = MemoryManager.unlink_by_id(
                relation_id=relation_id, namespace=namespace, key=key)
        except Exception as e:
            self._memory_error(e); return
        if deleted:
            EventBroker.publish('memory.changed', {
                'op': 'unlink', 'namespace': namespace, 'key': key,
            })
        self.send_json({'deleted': deleted}, 200 if deleted else 404)

    def handle_memory_consolidate(self):
        """Phase 1 stub. Returns 202 with a no-op so the dashboard button can
        be wired ahead of Phase 3 landing the real worker."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        self.send_json({
            'status': 'queued',
            'detail': 'consolidation worker activates in Phase 3',
        }, 202)

    def handle_memory_export(self):
        """GET /api/memory/export — JSON dump of live memories + relations,
        suitable for backup or moving a corpus between workspaces (#107)."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            payload = MemoryManager.export_json()
        except Exception as e:
            self._memory_error(e); return
        self.send_json(payload)

    def handle_memory_import(self):
        """POST /api/memory/_import — load a corpus produced by export. Body:
        {memories:[...], relations:[...], mode?:'merge'|'skip'}. Mutating, so
        the do_POST readonly chokepoint already gates it (#107)."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        if not isinstance(data, dict):
            self.send_json({'error': 'body must be an object'}, 400)
            return
        try:
            res = MemoryManager.import_json(
                data, mode=data.get('mode', 'merge'),
                source=self._memory_actor())
        except Exception as e:
            self._memory_error(e); return
        EventBroker.publish('memory.changed', {'op': 'import'})
        self.send_json({'status': 'ok', 'result': res})

    def handle_memory_purge(self):
        """POST /api/memory/_purge — hard-delete soft-deleted memories and
        VACUUM. Body: {older_than_days?: number}. Mutating; readonly-gated by
        the do_POST chokepoint (#107)."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._memory_unavailable():
            return
        older = None
        try:
            data = self.read_json_body()  # {} when body is empty
            if isinstance(data, dict) and data.get('older_than_days') is not None:
                older = float(data['older_than_days'])
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        try:
            res = MemoryManager.purge_deleted(older_than_days=older)
        except Exception as e:
            self._memory_error(e); return
        EventBroker.publish('memory.changed', {'op': 'purge'})
        self.send_json({'status': 'ok', 'result': res})

    # ── Skills (multi-harness SKILL.md surface — issue #187) ─────────────
    # Read-only in this phase: list / detail / stats come from the
    # SkillsSyncer's in-memory snapshot (files are the source of truth);
    # POST /api/skills/_scan forces a synchronous rescan.

    def _skills_unavailable(self):
        if _SKILLS_AVAILABLE:
            return False
        self.send_json({'error': 'skills subsystem unavailable',
                        'code': 'skills_unavailable',
                        'detail': 'skills package failed to import; check server logs'},
                       503)
        return True

    def handle_skills_list(self, query):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._skills_unavailable():
            return
        if (query.get('refresh') or [''])[0] in ('1', 'true'):
            try:
                SkillsSyncer.trigger_sync()
            except Exception as e:
                print(f'[skills] refresh failed: {e}', file=sys.stderr)
        records = SkillsSyncer.snapshot()
        system = (query.get('system') or [None])[0]
        scope = (query.get('scope') or [None])[0]
        if system:
            records = [r for r in records if system in r.systems]
        if scope:
            records = [r for r in records if r.scope == scope]
        self.send_json({'skills': [r.to_dict() for r in records],
                        'count': len(records)})

    def handle_skills_get(self, name):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._skills_unavailable():
            return
        variants = SkillsSyncer.get(name)
        if not variants:
            self.send_json({'error': 'not found', 'code': 'not_found'}, 404)
            return
        # One logical skill normally; 2+ entries when copies have diverged
        # across systems (same name, different content fingerprint).
        self.send_json({'skill': variants[0].to_dict(),
                        'variants': [v.to_dict() for v in variants],
                        'divergent': len(variants) > 1})

    def handle_skills_stats(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._skills_unavailable():
            return
        records = SkillsSyncer.snapshot()
        by_system, by_scope = {}, {}
        for r in records:
            for s in r.systems:
                by_system[s] = by_system.get(s, 0) + 1
            by_scope[r.scope] = by_scope.get(r.scope, 0) + 1
        self.send_json({'total': len(records),
                        'by_system': by_system,
                        'by_scope': by_scope,
                        'syncer': SkillsSyncer.status()})

    def handle_skills_scan(self):
        """POST /api/skills/_scan — synchronous forced rescan."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._skills_unavailable():
            return
        try:
            res = SkillsSyncer.trigger_sync()
        except Exception as e:
            self.send_json({'error': f'scan failed: {e}'}, 500)
            return
        self.send_json({'status': 'ok', 'result': res})

    def handle_skills_sync(self, name):
        """POST /api/skills/{name}/sync — cross-harness install (PR2).

        Body: {source_system, source_scope?, targets:[{system, scope?}],
        force?}. Translates one logical skill's source variant into each
        target harness's native dir so every agent can use it. Any-to-any:
        source_system is a parameter, not fixed to Claude.

        Guarded: readonly (do_POST chokepoint), name charset, unknown/
        disabled targets (400), and 409 when a target already holds a
        DIVERGENT copy (different fingerprint) unless {"force": true} —
        so a sync never silently clobbers a locally-edited skill.
        """
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._skills_unavailable():
            return
        # Belt-and-suspenders: the route regex already restricts the charset,
        # but re-check against the canonical gate before any path is built.
        if not (SKILL_NAME_RE and SKILL_NAME_RE.match(name)):
            self.send_json({'error': 'invalid skill name', 'code': 'bad_name'}, 400)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        if not isinstance(data, dict):
            self.send_json({'error': 'body must be an object'}, 400)
            return

        source_system = (data.get('source_system') or '').strip()
        source_scope = (data.get('source_scope') or '').strip() or None
        force = bool(data.get('force'))
        targets = data.get('targets')
        if not isinstance(targets, list) or not targets:
            self.send_json({'error': 'targets[] required'}, 400)
            return

        # --- resolve the SOURCE variant from the live snapshot ---
        variants = SkillsSyncer.get(name)
        if not variants:
            self.send_json({'error': 'not found', 'code': 'not_found'}, 404)
            return
        candidates = variants
        if source_system:
            candidates = [v for v in candidates if source_system in v.systems]
        if source_scope:
            candidates = [v for v in candidates if v.scope == source_scope]
        if not candidates:
            self.send_json({'error': 'source variant not found',
                            'code': 'source_not_found'}, 404)
            return
        if len(candidates) > 1:
            # Divergent skill and the caller didn't pin it down.
            self.send_json({'error': 'ambiguous source; specify source_system',
                            'code': 'ambiguous_source'}, 400)
            return
        source = candidates[0]

        # --- validate every target up front (fail fast, install nothing) ---
        norm_targets = []
        for t in targets:
            if not isinstance(t, dict):
                self.send_json({'error': 'each target must be an object'}, 400)
                return
            sys_key = (t.get('system') or '').strip()
            scope = (t.get('scope') or 'user').strip()
            provider = SKILL_PROVIDERS.get(sys_key)
            if provider is None:
                self.send_json({'error': f'unknown target system: {sys_key!r}',
                                'code': 'bad_target'}, 400)
                return
            if not provider.enabled:
                self.send_json({'error': f'target {sys_key!r} is disabled',
                                'code': 'target_disabled'}, 400)
                return
            try:
                provider.install_path(name, scope)  # validates scope writable
            except ValueError as e:
                self.send_json({'error': str(e), 'code': 'bad_target'}, 400)
                return
            norm_targets.append((sys_key, scope, provider))

        # --- conflict check: refuse to overwrite a DIVERGENT target copy ---
        conflicts = []
        for sys_key, scope, _provider in norm_targets:
            existing = [v for v in variants if sys_key in v.systems]
            for ex in existing:
                if ex.fingerprint != source.fingerprint:
                    conflicts.append({'system': sys_key,
                                      'existing_fingerprint': ex.fingerprint})
                    break
        if conflicts and not force:
            self.send_json({'error': 'target has a divergent copy',
                            'code': 'conflict', 'conflicts': conflicts,
                            'hint': 'retry with {"force": true} to overwrite'},
                           409)
            return

        # --- install into every target (atomic per file) ---
        installed, failed = [], []
        for sys_key, scope, provider in norm_targets:
            try:
                path = provider.install(source, scope)
                installed.append({'system': sys_key, 'scope': scope, 'path': path})
            except Exception as e:
                failed.append({'system': sys_key, 'scope': scope, 'error': str(e)})
        # Refresh the snapshot so the collapsed row is visible immediately
        # and clients get a fresh list on their next poll / SSE tick.
        try:
            SkillsSyncer.trigger_sync()
        except Exception:
            pass
        status = 200 if installed and not failed else (207 if installed else 500)
        self.send_json({'name': name, 'source_system': source.systems[0],
                        'installed': installed, 'failed': failed}, status)

    # ── Subagents (spawned child tasks) ──────────────────────────
    # Lists real spawned sub-tasks filtered by parent_task_id.
    # Replaces the old read-only transcript scanner which was fragile
    # and version-dependent.

    def handle_subagents_list(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        # Parse ?parent=<task_id> filter from query string
        parent = None
        if '?' in self.path:
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            parent_val = params.get('parent', [None])[0]
            if parent_val:
                parent = parent_val
        if not parent:
            self.send_json({'subagents': [], 'count': 0,
                            'running_count': 0, 'completed_count': 0,
                            'error_count': 0, 'note': 'pass ?parent=<task_id> to list sub-agents'})
            return
        tasks = ClaudeTaskManager.list_tasks(parent=parent)
        subagents = []
        running = 0
        completed = 0
        errored = 0
        for t in tasks:
            status = t['status']
            sa = {
                'tool_use_id': t['task_id'],
                'tool': 'spawn_agent',
                'timestamp': t['created_at'],
                'session_id': t['task_id'],
                'project': 'kube-coder',
                'description': t.get('prompt', '')[:200],
                'subagent_type': t.get('assistant', 'claude'),
                'prompt': t.get('prompt', ''),
                'status': status,
                'ended_at': t.get('finished_at'),
                'is_error': status == 'error',
            }
            subagents.append(sa)
            if status == 'running':
                running += 1
            elif status in ('completed',):
                completed += 1
            elif status in ('error', 'killed'):
                errored += 1
        self.send_json({
            'subagents': subagents,
            'count': len(subagents),
            'running_count': running,
            'completed_count': completed,
            'error_count': errored,
        })

    # ── Docs (in-app documentation site) ────────────────────────────────
    def handle_docs_manifest(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            manifest = DocsManager.load_manifest()
            self.send_json(manifest)
        except Exception as e:
            print(f'[docs] manifest error: {e}', file=sys.stderr)
            self.send_json({'error': str(e), 'code': 'internal'}, 500)

    def handle_docs_page(self, page_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            page = DocsManager.get_page(page_id)
            self.send_json(page)
        except KeyError:
            self.send_json({'error': f'Unknown doc page: {page_id}'}, 404)
        except Exception as e:
            print(f'[docs] page {page_id} error: {e}', file=sys.stderr)
            self.send_json({'error': str(e), 'code': 'internal'}, 500)

    def handle_docs_search(self, query):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        q = ''
        try:
            q = (query.get('q', [''])[0] or '').strip()
            try:
                limit = int(query.get('limit', ['25'])[0])
            except (ValueError, TypeError):
                limit = 25
            limit = max(1, min(100, limit))
            results = DocsManager.search(q, limit=limit)
            self.send_json({'q': q, 'results': results})
        except Exception as e:
            print(f'[docs] search {q!r} error: {e}', file=sys.stderr)
            self.send_json({'error': str(e), 'code': 'internal'}, 500)

    # ── File upload + browse (rooted at /home/dev) ─────────────────────
    # We deliberately keep these endpoints scoped to /home/dev with a
    # realpath-based traversal check so a crafted X-Dest-Path can't escape
    # the user's home directory (e.g. via ".." or absolute paths).
    HOME_DEV = '/home/dev'
    MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MiB

    @classmethod
    def _resolve_under_home_dev(cls, rel_path: str) -> str:
        rel = (rel_path or '').strip()
        # Treat leading slashes as relative to /home/dev — users naturally
        # type "/screenshots" or "screenshots" interchangeably.
        rel = rel.lstrip('/')
        abs_path = os.path.realpath(os.path.join(cls.HOME_DEV, rel))
        if abs_path != cls.HOME_DEV and not abs_path.startswith(cls.HOME_DEV + os.sep):
            raise ValueError('path escapes /home/dev')
        return abs_path

    @classmethod
    def _path_has_hidden_segment(cls, abs_path: str) -> bool:
        """True if any path segment of abs_path *below* HOME_DEV starts with a
        dot (e.g. .claude-tasks, .config, .ssh, .git). HOME_DEV itself is never
        counted — only the portion a request can address."""
        try:
            rel = os.path.relpath(abs_path, cls.HOME_DEV)
        except ValueError:
            return True  # different drive / unrelatable → treat as hidden
        if rel in ('', '.'):
            return False
        return any(seg.startswith('.') for seg in rel.split(os.sep)
                   if seg not in ('', '.', '..'))

    def _reject_public_read(self, target: str) -> bool:
        """Public-demo confidentiality gate for direct file reads. In
        AUTH_MODE=none (the unauthenticated public demo) refuse to serve a file
        whose resolved path contains a hidden (dot) segment, and — when the
        operator set PUBLIC_FILE_ROOT — anything outside that subdir. This closes
        finding 3: READONLY_MODE protects integrity, not confidentiality, so the
        credential/config dotfiles that directory listings already hide must not
        be reachable via a direct download/preview/view/raw request. Authed modes
        (oauth2/basic) are untouched, so a logged-in user keeps full access to
        their own dotfiles. Returns True — and sends a 404, matching a
        nonexistent file so existence isn't confirmed — when the read must be
        refused; the caller then stops. Traversal/symlink-escape protection is
        handled earlier by _resolve_under_home_dev and stays intact."""
        if AUTH_MODE != 'none':
            return False
        if PUBLIC_FILE_ROOT:
            try:
                root = self._resolve_under_home_dev(PUBLIC_FILE_ROOT)
            except ValueError:
                root = self.HOME_DEV
            if target != root and not target.startswith(root + os.sep):
                self.send_json({'error': 'not a file'}, 404)
                return True
        if self._path_has_hidden_segment(target):
            self.send_json({'error': 'not a file'}, 404)
            return True
        return False

    @staticmethod
    def _safe_filename(name: str) -> bool:
        if not name or name in ('.', '..'):
            return False
        if '/' in name or '\\' in name or '\x00' in name:
            return False
        if len(name) > 255:
            return False
        return True

    def handle_files_list(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        rel_dir = (params.get('path', [''])[0] or '').strip()
        try:
            target = self._resolve_under_home_dev(rel_dir)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        if not os.path.isdir(target):
            self.send_json({'error': 'not a directory'}, 404)
            return
        entries = []
        try:
            for name in sorted(os.listdir(target), key=str.lower):
                if name.startswith('.'):  # hide dotfiles
                    continue
                full = os.path.join(target, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                entries.append({
                    'name': name,
                    'kind': 'dir' if os.path.isdir(full) else 'file',
                    'size': st.st_size,
                    'mtime': int(st.st_mtime),
                })
        except OSError as e:
            self.send_json({'error': f'list failed: {e}'}, 500)
            return
        # Surface dirs first so the UI can render a sensible tree.
        entries.sort(key=lambda e: (0 if e['kind'] == 'dir' else 1, e['name'].lower()))
        rel = os.path.relpath(target, self.HOME_DEV)
        if rel == '.':
            rel = ''
        self.send_json({'path': rel, 'entries': entries})

    def handle_file_raw(self):
        """GET /api/files/raw?path=<rel> — stream a workspace MEDIA file.

        Auth-gated + traversal-guarded (reuses _resolve_under_home_dev). Powers
        the Hypervisor chat's image/video rendering: the agent saves a file under
        /home/dev and shows it via show_media(path=...), and the client fetches
        the bytes here.

        SECURITY: restricted to image/* and video/* by content type. The
        dashboard is served from this same origin with no CSP, so serving an
        arbitrary /home/dev HTML/JS file here would be stored XSS — refuse
        anything that isn't media, and send nosniff + inline disposition.
        Supports a single Range request so <video> seeking works.
        """
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        qs = urllib.parse.urlparse(self.path).query
        rel = (urllib.parse.parse_qs(qs).get('path', [''])[0] or '').strip()
        try:
            target = self._resolve_under_home_dev(rel)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        if not os.path.isfile(target):
            self.send_json({'error': 'not a file'}, 404)
            return
        if self._reject_public_read(target):
            return
        ctype = mimetypes.guess_type(target)[0] or ''
        if not (ctype.startswith('image/') or ctype.startswith('video/')):
            self.send_json(
                {'error': 'only image/* and video/* files can be served here'}, 415)
            return
        try:
            size = os.path.getsize(target)
            start, end = 0, size - 1
            is_range = False
            rng = self.headers.get('Range', '')
            m = re.match(r'^bytes=(\d*)-(\d*)$', rng.strip()) if rng else None
            if m and size > 0:
                s, e = m.group(1), m.group(2)
                if s:
                    start = int(s)
                    end = int(e) if e else size - 1
                elif e:  # suffix range: last N bytes
                    start = max(0, size - int(e))
                    end = size - 1
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.end_headers()
                    return
                is_range = True
            length = end - start + 1
            with open(target, 'rb') as fh:
                self.send_response(206 if is_range else 200)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(length))
                self.send_header('Accept-Ranges', 'bytes')
                if is_range:
                    self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                self.send_header('Content-Disposition', 'inline')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Cache-Control', 'private, max-age=60')
                self.end_headers()
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away mid-stream; headers already sent
        except OSError as e:
            try:
                self.send_json({'error': f'read failed: {e}'}, 500)
            except Exception:
                pass

    def handle_file_upload(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        # Client URL-encodes both headers because HTTP header values are
        # ISO-8859-1; Unicode filenames (smart quotes, emoji, CJK, accented
        # letters) would otherwise trip fetch() in the browser. Decode here
        # before applying the safe-filename / under-home checks so the
        # validation runs on the actual intended path.
        rel_dir = urllib.parse.unquote((self.headers.get('X-Dest-Path') or '').strip())
        filename = urllib.parse.unquote((self.headers.get('X-Filename') or '').strip())
        if not self._safe_filename(filename):
            self.send_json({'error': 'invalid X-Filename header'}, 400)
            return
        try:
            dest_dir = self._resolve_under_home_dev(rel_dir)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        try:
            content_length = int(self.headers.get('Content-Length', 0) or 0)
        except ValueError:
            self.send_json({'error': 'invalid Content-Length'}, 400)
            return
        if content_length <= 0:
            self.send_json({'error': 'empty body'}, 400)
            return
        if content_length > self.MAX_UPLOAD_BYTES:
            self.send_json({'error': f'file too large (max {self.MAX_UPLOAD_BYTES} bytes)'}, 413)
            return
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as e:
            self.send_json({'error': f'mkdir failed: {e}'}, 500)
            return
        final_path = os.path.join(dest_dir, filename)
        # Stream to disk in 64 KiB chunks so a 200 MiB upload doesn't have to
        # fully buffer in memory before we touch the filesystem.
        try:
            with open(final_path, 'wb') as fh:
                remaining = content_length
                while remaining > 0:
                    chunk = self.rfile.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    fh.write(chunk)
                    remaining -= len(chunk)
        except OSError as e:
            self.send_json({'error': f'write failed: {e}'}, 500)
            return
        try:
            size = os.path.getsize(final_path)
        except OSError:
            size = 0
        # Match perms to existing files in the target dir — workspace pods
        # run as a non-root user, so this is mostly a safety net.
        try:
            os.chmod(final_path, 0o644)
        except OSError:
            pass
        rel_out = os.path.relpath(final_path, self.HOME_DEV)
        self.send_json({
            'ok': True,
            'path': rel_out,
            'absolute_path': final_path,
            'size': size,
        }, 201)

    def handle_file_mkdir(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            content_length = int(self.headers.get('Content-Length', 0) or 0)
            raw = self.rfile.read(content_length).decode('utf-8') if content_length else '{}'
            body = json.loads(raw) if raw else {}
        except (ValueError, json.JSONDecodeError):
            self.send_json({'error': 'invalid JSON body'}, 400)
            return
        rel_dir = (body.get('path') or '').strip()
        if not rel_dir:
            self.send_json({'error': 'path is required'}, 400)
            return
        try:
            target = self._resolve_under_home_dev(rel_dir)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as e:
            self.send_json({'error': f'mkdir failed: {e}'}, 500)
            return
        rel_out = os.path.relpath(target, self.HOME_DEV)
        if rel_out == '.':
            rel_out = ''
        self.send_json({'ok': True, 'path': rel_out}, 201)

    # Text preview is capped so a multi-gigabyte log can't be slurped into
    # memory (and shipped to the browser) by a single click. Larger files fall
    # back to download.
    PREVIEW_MAX_BYTES = 256 * 1024

    def handle_file_download(self):
        """GET /api/files/download?path=<rel> — stream ANY workspace file as an
        attachment (traversal-guarded via _resolve_under_home_dev).

        SECURITY: unlike /api/files/raw (which serves image/* + video/* inline
        for the Hypervisor chat), this can serve arbitrary bytes — including an
        HTML/JS file that would be stored XSS if rendered on this same (CSP-less)
        origin. We defuse that by ALWAYS sending `Content-Disposition:
        attachment` + `X-Content-Type-Options: nosniff` + a neutral
        application/octet-stream type, so the browser downloads rather than
        renders. Directories can't be downloaded."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        qs = urllib.parse.urlparse(self.path).query
        rel = (urllib.parse.parse_qs(qs).get('path', [''])[0] or '').strip()
        try:
            target = self._resolve_under_home_dev(rel)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        if not os.path.isfile(target):
            self.send_json({'error': 'not a file'}, 404)
            return
        if self._reject_public_read(target):
            return
        name = os.path.basename(target)
        # RFC 6266: ASCII fallback (with quotes/backslashes/control chars
        # stripped) plus a UTF-8 filename* so non-ASCII names survive.
        ascii_name = re.sub(r'[^\x20-\x7e]', '_', name).replace('\\', '_').replace('"', '_')
        star = urllib.parse.quote(name, safe='')
        try:
            size = os.path.getsize(target)
            with open(target, 'rb') as fh:
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(size))
                self.send_header(
                    'Content-Disposition',
                    f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{star}",
                )
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Cache-Control', 'private, no-store')
                self.end_headers()
                while True:
                    chunk = fh.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away mid-stream; headers already sent
        except OSError as e:
            try:
                self.send_json({'error': f'read failed: {e}'}, 500)
            except Exception:
                pass

    def handle_file_preview(self):
        """GET /api/files/preview?path=<rel> — JSON preview descriptor for the
        Files pane. Text files (size-capped) return their content with a
        truncation marker; images signal the client to render via
        /api/files/raw; everything else (binary / oversized) signals download."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        qs = urllib.parse.urlparse(self.path).query
        rel = (urllib.parse.parse_qs(qs).get('path', [''])[0] or '').strip()
        try:
            target = self._resolve_under_home_dev(rel)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        if not os.path.isfile(target):
            self.send_json({'error': 'not a file'}, 404)
            return
        if self._reject_public_read(target):
            return
        rel_out = os.path.relpath(target, self.HOME_DEV)
        try:
            size = os.path.getsize(target)
        except OSError as e:
            self.send_json({'error': f'stat failed: {e}'}, 500)
            return
        ctype = mimetypes.guess_type(target)[0] or ''
        if ctype.startswith('image/'):
            self.send_json({'kind': 'image', 'path': rel_out, 'mime': ctype, 'size': size})
            return
        if ctype.startswith('video/'):
            self.send_json({'kind': 'video', 'path': rel_out, 'mime': ctype, 'size': size})
            return
        # Peek only the first PREVIEW_MAX_BYTES: a huge text log still previews
        # (truncated, with a marker), while a huge binary never gets slurped in.
        try:
            with open(target, 'rb') as fh:
                raw = fh.read(self.PREVIEW_MAX_BYTES + 1)
        except OSError as e:
            self.send_json({'error': f'read failed: {e}'}, 500)
            return
        # A NUL byte is the classic "this is binary" tell; also treat anything
        # that isn't valid UTF-8 as binary rather than mangling it.
        if b'\x00' in raw:
            self.send_json({'kind': 'binary', 'path': rel_out, 'mime': ctype, 'size': size,
                            'reason': 'binary'})
            return
        truncated = len(raw) > self.PREVIEW_MAX_BYTES
        body = raw[:self.PREVIEW_MAX_BYTES]
        try:
            text = body.decode('utf-8')
        except UnicodeDecodeError:
            self.send_json({'kind': 'binary', 'path': rel_out, 'mime': ctype, 'size': size,
                            'reason': 'binary'})
            return
        self.send_json({'kind': 'text', 'path': rel_out, 'mime': ctype or 'text/plain',
                        'size': size, 'content': text, 'truncated': truncated})

    # Document types the Hypervisor chat renders in a sandboxed <iframe>/WebView
    # (markdown/text/code are rendered client-side from /api/files/preview, and
    # image/video stream from /api/files/raw, so only these need serving inline).
    VIEW_INLINE_MIME = {
        'application/pdf',
        'text/html', 'application/xhtml+xml',
        'image/svg+xml',
        'text/xml', 'application/xml',
    }

    def handle_file_view(self):
        """GET /api/files/view?path=<rel> — stream a workspace DOCUMENT inline so
        the Hypervisor chat can render it in a sandboxed frame (PDF, HTML, SVG).

        Auth-gated + traversal-guarded (reuses _resolve_under_home_dev), like
        /api/files/raw. Restricted to a small document allowlist (VIEW_INLINE_MIME).

        SECURITY: the dashboard is served from this same origin with no CSP, so
        serving an arbitrary HTML/JS file inline would be stored XSS. We defuse
        that with `Content-Security-Policy: sandbox` (no allow-scripts, no
        allow-same-origin) on every response *except* application/pdf — the
        browser's PDF viewer already isolates any embedded JS, and the sandbox
        directive would break it. Always send nosniff + inline. PDFs support a
        single Range so large-file seeking works.
        """
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        qs = urllib.parse.urlparse(self.path).query
        rel = (urllib.parse.parse_qs(qs).get('path', [''])[0] or '').strip()
        try:
            target = self._resolve_under_home_dev(rel)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        if not os.path.isfile(target):
            self.send_json({'error': 'not a file'}, 404)
            return
        if self._reject_public_read(target):
            return
        ctype = mimetypes.guess_type(target)[0] or ''
        if ctype not in self.VIEW_INLINE_MIME:
            self.send_json(
                {'error': f'{ctype or "this file type"} cannot be viewed inline; '
                          'use /api/files/preview (text) or /api/files/download'}, 415)
            return
        is_pdf = ctype == 'application/pdf'
        try:
            size = os.path.getsize(target)
            start, end = 0, size - 1
            is_range = False
            # Range only matters for the PDF viewer; text/HTML docs are small.
            rng = self.headers.get('Range', '') if is_pdf else ''
            m = re.match(r'^bytes=(\d*)-(\d*)$', rng.strip()) if rng else None
            if m and size > 0:
                s, e = m.group(1), m.group(2)
                if s:
                    start = int(s)
                    end = int(e) if e else size - 1
                elif e:  # suffix range: last N bytes
                    start = max(0, size - int(e))
                    end = size - 1
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.end_headers()
                    return
                is_range = True
            length = end - start + 1
            with open(target, 'rb') as fh:
                self.send_response(206 if is_range else 200)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(length))
                if is_pdf:
                    self.send_header('Accept-Ranges', 'bytes')
                    if is_range:
                        self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                else:
                    # Neutralize any active content in HTML/SVG/XML: unique origin,
                    # no scripts, no access to the dashboard's cookies/DOM.
                    self.send_header('Content-Security-Policy', 'sandbox')
                self.send_header('Content-Disposition', 'inline')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Cache-Control', 'private, max-age=60')
                self.end_headers()
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away mid-stream; headers already sent
        except OSError as e:
            try:
                self.send_json({'error': f'read failed: {e}'}, 500)
            except Exception:
                pass

    def handle_file_delete(self):
        """DELETE /api/files?path=<rel> — remove a file or an EMPTY directory
        (traversal-guarded). Refuses to recurse into a non-empty directory so a
        stray click can't wipe a subtree; refuses to delete /home/dev itself."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        qs = urllib.parse.urlparse(self.path).query
        rel = (urllib.parse.parse_qs(qs).get('path', [''])[0] or '').strip()
        try:
            target = self._resolve_under_home_dev(rel)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        if target == self.HOME_DEV:
            self.send_json({'error': 'refusing to delete /home/dev'}, 400)
            return
        # lexists so a dangling/relative symlink can still be removed (a symlink
        # ESCAPING /home/dev is already rejected above: realpath resolved its
        # target and failed the containment check).
        if not os.path.lexists(target):
            self.send_json({'error': 'not found'}, 404)
            return
        try:
            if os.path.isdir(target) and not os.path.islink(target):
                if os.listdir(target):
                    self.send_json({'error': 'directory not empty'}, 409)
                    return
                os.rmdir(target)
            else:
                os.remove(target)
        except OSError as e:
            self.send_json({'error': f'delete failed: {e}'}, 500)
            return
        self.send_json({'ok': True})

    def handle_file_rename(self):
        """POST /api/files/rename {from,to} — move/rename within /home/dev. Both
        endpoints are traversal-guarded; `to` may name a new path but must not
        already exist (no silent overwrite) and its parent must exist."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            content_length = int(self.headers.get('Content-Length', 0) or 0)
            raw = self.rfile.read(content_length).decode('utf-8') if content_length else '{}'
            body = json.loads(raw) if raw else {}
        except (ValueError, json.JSONDecodeError):
            self.send_json({'error': 'invalid JSON body'}, 400)
            return
        src_rel = (body.get('from') or '').strip()
        dst_rel = (body.get('to') or '').strip()
        if not src_rel or not dst_rel:
            self.send_json({'error': 'both from and to are required'}, 400)
            return
        try:
            src = self._resolve_under_home_dev(src_rel)
            dst = self._resolve_under_home_dev(dst_rel)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        if src == self.HOME_DEV:
            self.send_json({'error': 'refusing to move /home/dev'}, 400)
            return
        if not os.path.lexists(src):
            self.send_json({'error': 'source not found'}, 404)
            return
        if os.path.lexists(dst):
            self.send_json({'error': 'destination already exists'}, 409)
            return
        if not os.path.isdir(os.path.dirname(dst)):
            self.send_json({'error': 'destination directory does not exist'}, 400)
            return
        try:
            os.rename(src, dst)
        except OSError as e:
            self.send_json({'error': f'rename failed: {e}'}, 500)
            return
        rel_out = os.path.relpath(dst, self.HOME_DEV)
        self.send_json({'ok': True, 'path': rel_out})

    def send_vnc_viewer(self):
        # Defense-in-depth: oauth2-proxy should already have rejected an
        # unauth'd visitor, but if this handler is ever reached directly
        # (e.g. a misconfigured ingress) refuse rather than render the
        # iframe URL anyway. The deeper /vnc/<path> proxy IS authed; this
        # wrapper page used to slip through.
        if not self.check_claude_auth():
            self.send_response(401)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Unauthorized')
            return
        # Instead of embedding, redirect to the noVNC URL directly
        host = self.headers.get('Host', 'localhost').split(':')[0]
        vnc_url = f"https://{host}/vnc-direct/vnc.html?host={host}&port=6081&autoconnect=true&resize=scale"
        
        vnc_html = f'''<!DOCTYPE html>
<html>
<head>
    <title>VNC Viewer</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; text-align: center; }}
        .container {{ max-width: 600px; margin: 0 auto; }}
        .btn {{ background: #007cba; color: white; border: none; padding: 12px 24px; margin: 10px; border-radius: 4px; text-decoration: none; display: inline-block; }}
        .btn:hover {{ background: #005a8b; }}
        .warning {{ background: #fff3cd; border: 1px solid #ffeaa7; color: #856404; padding: 10px; border-radius: 4px; margin: 10px 0; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🖥️ Remote Desktop Viewer</h1>
        <div class="warning">
            <strong>🔒 Secure Access:</strong> This VNC viewer is protected by authentication.
            You must be logged into this workspace to access the remote desktop.
        </div>
        <p>Click the button below to open the VNC viewer in a new window:</p>
        <a href="{vnc_url}" target="_blank" class="btn">Open VNC Viewer</a>
        <p><small>If the VNC viewer doesn't load, make sure you've launched a browser first.</small></p>
        <p><a href="/browser/">← Back to Browser Controls</a></p>
    </div>
</body>
</html>'''
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(vnc_html.encode())
    
    def redirect_to_vnc(self):
        # Defense-in-depth — see send_vnc_viewer above. This handler
        # actually proxies localhost:6081 content, so unauth'd access
        # would have exposed the VNC HTML directly.
        if not self.check_claude_auth():
            self.send_response(401)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Unauthorized')
            return
        # Redirect to the noVNC URL running on localhost:6081
        import urllib.request
        try:
            # Proxy the request to the local noVNC server
            vnc_url = "http://localhost:6081/vnc.html?autoconnect=true&resize=scale"
            with urllib.request.urlopen(vnc_url, timeout=10) as response:
                content = response.read()
                self.send_response(200)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                self.wfile.write(content)
        except Exception as e:
            # Escape so a crafted upstream error message can't inject HTML
            # into this authenticated origin (reflected XSS).
            error_html = f'''<!DOCTYPE html>
<html>
<head><title>VNC Connection Error</title></head>
<body>
    <h1>VNC Connection Error</h1>
    <p>Unable to connect to VNC server: {html.escape(str(e))}</p>
    <p><a href="/browser/">← Back to Browser Controls</a></p>
    <p>Make sure a browser is launched first, then try again.</p>
</body>
</html>'''
            self.send_response(500)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(error_html.encode())

    # Per-CSP-directive splitter — used to strip frame-ancestors while keeping
    # the rest of the policy intact.
    _CSP_FRAME_ANCESTORS_RE = re.compile(r'(?:^|;)\s*frame-ancestors[^;]*', re.IGNORECASE)
    # Root-absolute src/href values in a proxied HTML body, e.g. src="/assets/x"
    # or href='/main.css'. The lookbehind skips data-src / srcset and similar
    # (no attr-boundary match); the value class stops at the closing quote,
    # whitespace or tag end. Bytes-mode so we never have to decode the body.
    _ABS_ASSET_URL_RE = re.compile(rb'(?<![\w-])((?:src|href)\s*=\s*)(["\'])(/[^"\'<>\s]*)')
    # Opening <head> tag — where we inject the runtime base-path shim.
    _HEAD_OPEN_RE = re.compile(rb'<head\b[^>]*>', re.IGNORECASE)
    # Injected into proxied HTML so an app's *runtime* requests (built in JS,
    # not in the HTML we rewrite) reach the right service through the proxy.
    # Runs in the browser, where the full client-visible prefix — including the
    # external /oauth auth segment that oauth2-proxy strips before requests
    # reach this server — IS visible via location.pathname. For fetch / XHR /
    # EventSource / WebSocket it rewrites:
    #   - root-absolute paths (`/api/x`)           → <prefix>/api/x  (this app's port)
    #   - same-origin absolute URLs                → same, via their path
    #   - localhost:<port> / 127.0.0.1:<port> URLs → /…/api-app-proxy/<port>/…
    #     so a separate backend ("API on :8086") the app talks to over loopback
    #     is reached through the proxy too — and becomes same-origin (no CORS).
    # Protocol-relative (//cdn), already-proxied, and port-less / external URLs
    # pass through. A classic inline <script> runs at parse time, before the
    # app's deferred module scripts, so the patches are in place first.
    _APP_PROXY_SHIM = (
        b'<script>(function(){'
        b'var p=location.pathname,k="/api/app-proxy/",ix=p.indexOf(k);if(ix<0)return;'
        b'var r=p.slice(ix+k.length),j=r.indexOf("/"),port=j<0?r:r.slice(0,j);'
        b'if(!port)return;'
        b'var P=p.slice(0,ix+k.length+port.length);'   # this app: /…/api-app-proxy/<port>
        b'var root=p.slice(0,ix+k.length-1);'          # proxy root: /…/api-app-proxy
        b'function pp(pt,pa){return root+"/"+pt+(pa||"/");}'
        b'function wsx(pt,pa){return (location.protocol==="https:"?"wss://":"ws://")+location.host+pp(pt,pa);}'
        b'var LH=/^https?:\\/\\/(?:localhost|127\\.0\\.0\\.1):(\\d+)(\\/[^\\s]*)?$/i;'
        b'var LW=/^wss?:\\/\\/(?:localhost|127\\.0\\.0\\.1):(\\d+)(\\/[^\\s]*)?$/i;'
        b'function fix(u){if(typeof u!=="string"||!u)return u;'
        b'var m=u.match(LH);if(m)return pp(m[1],m[2]);'                       # localhost:<port> → that port
        b'var o=location.origin+"/";if(u.indexOf(o)===0)u=u.slice(location.origin.length);'  # same-origin abs → path
        b'if(u.charAt(0)==="/"&&u.charAt(1)!=="/"&&u.indexOf(P+"/")!==0&&u.indexOf("/api/app-proxy/")!==0)return P+u;'
        b'return u;}'
        # fetch must be invoked with this===window; a bare call on a saved
        # reference throws "Illegal invocation", so bind it.
        b'var _f=window.fetch&&window.fetch.bind(window);if(_f){window.fetch=function(q,n){'
        b'if(typeof q==="string")return _f(fix(q),n);'
        b'if(q&&q.url){try{return _f(new Request(fix(q.url),q),n)}catch(e){}}'
        b'return _f(q,n)};}'
        b'var _x=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(){'
        b'if(arguments.length>1)arguments[1]=fix(arguments[1]);return _x.apply(this,arguments)};'
        b'if(window.EventSource){var E=window.EventSource;window.EventSource=function(u,c){return new E(fix(u),c)};'
        b'window.EventSource.prototype=E.prototype;}'
        b'if(window.WebSocket){var W=window.WebSocket;window.WebSocket=function(u,pr){'
        b'try{if(typeof u==="string"){var m=u.match(LW);'
        b'if(m)u=wsx(m[1],m[2]);'                                             # ws://localhost:<port> → that port
        b'else if(u.charAt(0)==="/"&&u.charAt(1)!=="/")u=wsx(port,u);}}catch(e){}'  # root-relative → this app
        b'return pr!==undefined?new W(u,pr):new W(u)};window.WebSocket.prototype=W.prototype;'
        # Preserve the readyState constants apps read as WebSocket.OPEN etc.
        b'window.WebSocket.CONNECTING=W.CONNECTING;window.WebSocket.OPEN=W.OPEN;'
        b'window.WebSocket.CLOSING=W.CLOSING;window.WebSocket.CLOSED=W.CLOSED;}'
        # Dynamically-injected <link>/<script> (modulepreload, the rel=stylesheet
        # CSS-preload links a Vite/Rolldown SPA appends at runtime, prefetch) build
        # their href/src from the app's absolute base (e.g. /dashboard/assets/x.css)
        # — fetch/XHR patching doesn't cover element insertion, so those escape the
        # proxy prefix and 404/HTML-fall-through ("Unable to preload CSS for ..."").
        # Rewrite href/src through fix() as the node is inserted (before the browser
        # fetches it), so the request goes through the authed proxy path.
        # Primary: intercept the href/src *property setter* on freshly-created
        # link/script elements. The bundler's CSS preloader does `o.href=t`
        # (a property assignment, not setAttribute) then document.head.append —
        # patching appendChild alone misses it. Redefining the setter on the
        # instance catches the absolute path at the moment it is assigned.
        b'function fxp(el,prop){try{var pr=Object.getPrototypeOf(el);'
        b'var d=pr&&Object.getOwnPropertyDescriptor(pr,prop);'
        b'if(d&&d.set&&d.get){Object.defineProperty(el,prop,{configurable:true,enumerable:d.enumerable,'
        b'get:function(){return d.get.call(this)},'
        b'set:function(v){try{v=fix(v)}catch(e){}return d.set.call(this,v)}});}}catch(e){}}'
        b'var _ce=document.createElement;document.createElement=function(t){'
        b'var el=_ce.apply(document,arguments);try{var tg=(""+t).toLowerCase();'
        b'if(tg==="link")fxp(el,"href");else if(tg==="script")fxp(el,"src");}catch(e){}return el;};'
        # Fallback: fix href/src as a node is inserted, covering elements built
        # via innerHTML / cloneNode that bypass our createElement override.
        b'function fxn(n){try{if(!n||!n.tagName)return;var t=n.tagName;'
        b'if(t==="LINK"){var h=n.getAttribute&&n.getAttribute("href");if(h)n.setAttribute("href",fix(h));}'
        b'else if(t==="SCRIPT"){var s=n.getAttribute&&n.getAttribute("src");if(s)n.setAttribute("src",fix(s));}}catch(e){}}'
        b'var _ap=Node.prototype.appendChild;Node.prototype.appendChild=function(n){fxn(n);return _ap.call(this,n)};'
        b'var _ib=Node.prototype.insertBefore;Node.prototype.insertBefore=function(n,r){fxn(n);return _ib.call(this,n,r)};'
        b'})();</script>'
    )
    # Hop-by-hop headers that must not be forwarded between client and
    # upstream. RFC 7230 §6.1 plus the usual extras.
    _HOP_BY_HOP_HEADERS = frozenset({
        'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
        'te', 'trailers', 'transfer-encoding', 'upgrade',
        'host', 'content-length',  # we re-derive these
    })

    def _dispatch_referer_proxy(self, method):
        """Recover a sub-resource that escaped the proxy prefix to the dashboard
        origin root — @font-face icon fonts (loaded by the CSS engine), lazy
        route chunks (dynamic import()), <img> srcs — i.e. requests the client
        shim can't rewrite. They arrive here as root-absolute paths and would
        404.

        When the Referer is one of our /api/app-proxy/<port>/ iframes (and the
        path isn't already a proxy path), 302-redirect it to the proxy path,
        reusing the Referer's own prefix — including the /oauth segment — so
        the redirect re-enters through oauth2-proxy and authenticates normally.

        We redirect rather than proxy inline on purpose: Referer is forgeable
        by non-browser clients, so proxying here would be an unauthenticated
        read path to loopback ports. The redirect target still enforces auth
        (a real browser carries the session cookie and follows the 3xx for
        fonts/images/modules; an unauthenticated client just gets bounced to
        login by oauth2-proxy).
        """
        ref = self.headers.get('Referer') or ''
        m = re.search(r'(/(?:oauth/|browser/)?api/app-proxy/(\d+))', ref)
        if not m:
            # TEMP diagnostic: an escaped navigation/sub-resource with no
            # usable app-proxy Referer would fall through to a 404. Log what
            # we got so we can see why (only for likely-escaped requests).
            dest = self.headers.get('Sec-Fetch-Dest', '')
            if ref or dest in ('document', 'iframe', 'empty'):
                try:
                    self.log_message('[app-escape] %s path=%s dest=%s mode=%s referer=%r',
                                     method, self.path, dest,
                                     self.headers.get('Sec-Fetch-Mode', ''), ref[:160])
                except Exception:
                    pass
            return False
        norm = self.path.split('?', 1)[0].replace('/oauth', '').replace('/browser', '')
        if norm.startswith('/api/app-proxy/'):
            return False  # already a proxy path — _dispatch_app_proxy handles it
        ok, _reason = AppsManager.is_proxyable(int(m.group(2)))
        if not ok:
            return False
        target = m.group(1) + (self.path if self.path.startswith('/') else '/' + self.path)
        self.send_response(302)
        self.send_header('Location', target)
        self.send_header('Content-Length', '0')
        self.end_headers()
        return True

    def _dispatch_app_proxy(self, claude_path, method):
        """Match /api/app-proxy/<port>/... and forward to the upstream.

        Centralizes the dispatch so every HTTP verb (GET/POST/PUT/DELETE/
        HEAD/OPTIONS) shares the same matching + auth + proxy code. The
        verb-specific do_* methods call this near the top of their /api
        routing chain; returns True if the request was handled.
        """
        m = re.match(r'^/api/app-proxy/(\d+)(/.*)?$', claude_path)
        if not m:
            return self._dispatch_terminal_proxy(claude_path, method)
        port = int(m.group(1))
        upstream_path = m.group(2) or '/'
        # Preserve the original query string (stripped from claude_path
        # by the caller before normalization).
        qs = self.path.split('?', 1)
        if len(qs) == 2:
            upstream_path = upstream_path + '?' + qs[1]
        # GET requests carrying Upgrade: websocket are hijacked into a
        # raw bidirectional socket relay. Everything else is normal HTTP.
        if method == 'GET' and self.headers.get('Upgrade', '').lower() == 'websocket':
            self._proxy_app_websocket(port, upstream_path)
            return True
        self._proxy_app_request(port, upstream_path, method=method)
        return True

    # ttyd's in-pod port. Reserved in AppsManager.INTERNAL_PORTS (users can't
    # pin/expose it via the generic app proxy); this dedicated route is how the
    # mobile app embeds the live terminal, behind the same Bearer/app-session
    # auth as the app proxy.
    TTYD_PORT = 7681

    def _dispatch_terminal_proxy(self, claude_path, method):
        """Match /api/terminal-proxy/... and forward to ttyd (HTTP + WS)."""
        m = re.match(r'^/api/terminal-proxy(/.*)?$', claude_path)
        if not m:
            return False
        upstream_path = m.group(1) or '/'
        qs = self.path.split('?', 1)
        if len(qs) == 2:
            upstream_path = upstream_path + '?' + qs[1]
        if method == 'GET' and self.headers.get('Upgrade', '').lower() == 'websocket':
            self._proxy_app_websocket(self.TTYD_PORT, upstream_path, allow_internal=True)
            return True
        self._proxy_app_request(self.TTYD_PORT, upstream_path, method=method,
                                prefix='/api/terminal-proxy', allow_internal=True)
        return True

    def _proxy_app_websocket(self, port, upstream_path, allow_internal=False):
        """Hijack the underlying TCP socket and relay a WebSocket session
        between the client and the upstream app.

        BaseHTTPRequestHandler's request loop reads the next request line
        from `self.rfile` after `do_GET` returns. Setting
        `self.close_connection = True` stops that loop after we take over
        the socket. We never call `self.send_response()` ourselves — the
        upstream's 101 response is relayed verbatim so the client sees the
        real Sec-WebSocket-Accept handshake.

        allow_internal backs the fixed internal routes (ttyd via
        /api/terminal-proxy): skips the is_proxyable reserved-port gate and
        always strips the route prefix (internal upstreams serve from root).
        """
        if not self.check_app_proxy_auth():
            self.send_response(401)
            self.end_headers()
            return
        ok, reason = (True, '') if allow_internal else AppsManager.is_proxyable(port)
        if not ok:
            self.send_response(403)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write((reason + '\n').encode())
            return

        # Compute the forwarded path the same way the HTTP proxy does.
        prefix = f'/api/app-proxy/{port}'
        pin = {} if allow_internal else (AppsManager.get_pin(port) or {})
        keep_prefix = bool(pin.get('strip_prefix', False))
        if not keep_prefix:
            forwarded_path = upstream_path or '/'
        else:
            forwarded_path = prefix + (upstream_path if upstream_path.startswith('/') else '/' + upstream_path)

        # Open the upstream socket.
        try:
            import socket as _socket
            upstream = _socket.create_connection(('127.0.0.1', port), timeout=5)
        except OSError as e:
            self.send_response(502)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(f'WebSocket upstream unreachable: {e}\n'.encode())
            return

        # Send the upstream the original WebSocket handshake. Use the client's
        # headers minus Host/hop-by-hop; rewrite Origin so an app that whitelists
        # localhost still accepts the connection. Keep Sec-WebSocket-Key intact
        # so the upstream's Sec-WebSocket-Accept is valid for the client.
        try:
            req_lines = [f'GET {forwarded_path} HTTP/1.1']
            req_lines.append(f'Host: 127.0.0.1:{port}')
            for k, v in self.headers.items():
                kl = k.lower()
                if kl == 'host':
                    continue
                if kl == 'origin':
                    # Rewrite to the localhost form the upstream expects.
                    req_lines.append(f'Origin: http://localhost:{port}')
                    continue
                req_lines.append(f'{k}: {v}')
            req_lines.append('X-Forwarded-Prefix: ' + prefix)
            if self.headers.get('Host'):
                req_lines.append('X-Forwarded-Host: ' + self.headers['Host'])
            req_lines.append('')
            req_lines.append('')
            handshake = ('\r\n'.join(req_lines)).encode('iso-8859-1')
            upstream.sendall(handshake)
        except OSError as e:
            upstream.close()
            self.send_response(502)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(f'WebSocket handshake failed: {e}\n'.encode())
            return

        # Read the upstream's response headers and stream the raw bytes
        # back to the client. We just need to slurp up to the blank line;
        # everything after that is opaque WebSocket frames.
        try:
            buf = bytearray()
            upstream.settimeout(10)
            while b'\r\n\r\n' not in buf:
                chunk = upstream.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
            header_end = buf.find(b'\r\n\r\n')
            if header_end < 0:
                upstream.close()
                # Don't try to send any response — the framework hasn't seen
                # anything yet, so we can write a normal HTTP error.
                self.send_response(502)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.end_headers()
                self.wfile.write(b'WebSocket upstream returned no response\n')
                return
            head = bytes(buf[:header_end + 4])
            leftover = bytes(buf[header_end + 4:])
        except OSError as e:
            upstream.close()
            self.send_response(502)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(f'WebSocket upstream read failed: {e}\n'.encode())
            return

        # Take over the socket — don't let the framework write any more
        # headers or another request after we return.
        self.close_connection = True
        client_sock = self.connection
        try:
            # Relay the upstream's handshake response verbatim, then any
            # data that arrived in the same packet after the headers.
            client_sock.sendall(head)
            if leftover:
                client_sock.sendall(leftover)
        except OSError:
            upstream.close()
            return

        upstream.settimeout(None)
        client_sock.settimeout(None)

        # Manual access log so an admin can see the 101 in pod logs.
        try:
            self.log_message('"GET %s HTTP/1.1" 101 -', self.path)
        except Exception:
            pass

        # Bidirectional relay. One thread per direction; SHUT_WR on EOF
        # prevents a deadlock when one peer closes write but keeps reading.
        import socket as _socket

        def pipe(src, dst):
            try:
                while True:
                    chunk = src.recv(65536)
                    if not chunk:
                        break
                    dst.sendall(chunk)
            except (OSError, ConnectionResetError):
                pass
            finally:
                try:
                    dst.shutdown(_socket.SHUT_WR)
                except OSError:
                    pass

        t_up = threading.Thread(target=pipe, args=(client_sock, upstream), daemon=True)
        t_down = threading.Thread(target=pipe, args=(upstream, client_sock), daemon=True)
        t_up.start()
        t_down.start()
        t_up.join()
        t_down.join()
        try:
            upstream.close()
        except Exception:
            pass

    def _proxy_app_request(self, port, upstream_path, method='GET', prefix=None,
                           allow_internal=False):
        """Reverse-proxy a request to http://127.0.0.1:<port><upstream_path>.

        Streams the response body via read1+flush so SSE/chunked responses
        arrive promptly. Strips X-Frame-Options + CSP frame-ancestors from
        the upstream so the response can be embedded in the dashboard
        iframe. Rewrites absolute Location headers back to the proxy path.

        Path handling:
        - default (root-path-aware apps configured with --root-path /
          FORCE_SCRIPT_NAME / --base): strip the /api/app-proxy/<port>
          prefix before forwarding; the upstream's router expects the
          unprefixed path and uses X-Forwarded-Prefix for URL generation.
        - pinned port with strip_prefix=False: pass the full path through
          (the Vite-style case where the dev server only matches its own
          --base prefix).

        `prefix` + `allow_internal` back the fixed internal routes (the
        /api/terminal-proxy → ttyd:7681 path the mobile app embeds): a custom
        prefix keeps the trailing-slash normalization honest, allow_internal
        skips the is_proxyable listener/reserved-port gate (the route is
        pinned server-side to a workspace service, never user input), and the
        HTML/Location rewrites are skipped — ttyd's assets are relative.
        """
        if not self.check_app_proxy_auth():
            self.send_response(401)
            self.end_headers()
            return
        internal_route = prefix is not None
        if not allow_internal:
            ok, reason = AppsManager.is_proxyable(port)
            if not ok:
                self.send_response(403)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.end_headers()
                self.wfile.write((reason + '\n').encode())
                return
        prefix = prefix or f'/api/app-proxy/{port}'

        # Trailing-slash 301 so relative URLs resolve against the prefix root.
        if upstream_path in ('', '/'):
            normalized = self.path.split('?', 1)[0].replace('/oauth', '').replace('/browser', '')
            if normalized == prefix:
                self.send_response(301)
                self.send_header('Location', f'{prefix}/')
                self.end_headers()
                return

        # Apply the prefix-stripping rule based on the pin's flag. Internal
        # routes always strip — their upstreams serve from the root.
        pin = {} if internal_route else (AppsManager.get_pin(port) or {})
        keep_prefix = bool(pin.get('strip_prefix', False))  # default: strip
        if not keep_prefix:
            # Strip the proxy prefix from the path we forward upstream.
            # The query string is already attached so just slice the path.
            if '?' in upstream_path:
                p, q = upstream_path.split('?', 1)
                forwarded_path = (p or '/') + '?' + q
            else:
                forwarded_path = upstream_path or '/'
        else:
            # Pass the full path through. The upstream is configured to
            # only match URLs starting with the proxy prefix.
            forwarded_path = prefix + (upstream_path if upstream_path.startswith('/') else '/' + upstream_path)

        # Read request body (if any).
        body = None
        body_len = int(self.headers.get('Content-Length') or 0)
        if body_len > 0:
            body = self.rfile.read(body_len)

        # Build forwarded headers. Drop hop-by-hop + ours; add X-Forwarded-*.
        fwd_headers = {}
        for k, v in self.headers.items():
            kl = k.lower()
            if kl in self._HOP_BY_HOP_HEADERS:
                continue
            # Ask the upstream for an identity-encoded body. We rewrite
            # absolute asset URLs in HTML responses (see below), which only
            # works on uncompressed bytes; over a localhost hop compression
            # buys nothing anyway.
            if kl == 'accept-encoding':
                continue
            # Rewrite Origin to the localhost form the upstream expects. Dev
            # servers (Metro, Vite, …) often 500/403 a request whose Origin is
            # a foreign host (anti-DNS-rebinding / CORS) — and @font-face fonts
            # and fetch()/XHR are CORS requests that carry Origin, so without
            # this icon fonts 500 and many API calls fail. Mirrors the
            # WebSocket proxy, which already rewrites Origin the same way.
            if kl == 'origin':
                v = f'http://localhost:{port}'
            fwd_headers[k] = v
        fwd_headers['Host'] = f'127.0.0.1:{port}'
        fwd_headers['X-Forwarded-Prefix'] = prefix
        fwd_headers['X-Forwarded-Proto'] = 'https' if self.headers.get('X-Forwarded-Proto') == 'https' else 'http'
        if 'Host' in self.headers:
            fwd_headers['X-Forwarded-Host'] = self.headers['Host']
        if body is not None:
            fwd_headers['Content-Length'] = str(len(body))

        try:
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
            conn.request(method, forwarded_path, body=body, headers=fwd_headers)
            resp = conn.getresponse()
        except (ConnectionRefusedError, OSError) as e:
            self.send_response(502)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(f'Bad gateway: cannot reach 127.0.0.1:{port} ({e})\n'.encode())
            return

        # A 200 text/html body gets its root-absolute asset URLs made relative
        # so a stock build (Vite/CRA: <script src="/assets/x">) loads under the
        # proxy prefix instead of 404ing against the dashboard origin root (see
        # _rewrite_html_asset_urls). Buffered because the rewrite changes the
        # length; assets, JSON and SSE still stream untouched. The forwarded
        # request dropped Accept-Encoding, so this body is identity-encoded
        # and rewritable.
        ctype = resp.getheader('Content-Type', '') or ''
        rewrite_body = (
            method != 'HEAD'
            and resp.status == 200
            and 'text/html' in ctype.lower()
            # Internal routes (ttyd) serve relative assets; the rewriter
            # would re-prefix them onto /api/app-proxy/<port> — wrong route.
            and not internal_route
        )
        rewritten = self._rewrite_proxied_html(resp.read(), port) if rewrite_body else None

        # Forward status + filtered headers.
        self.send_response(resp.status, resp.reason)
        for k, v in resp.getheaders():
            kl = k.lower()
            if kl in self._HOP_BY_HOP_HEADERS:
                continue
            if kl == 'x-frame-options':
                continue
            if kl == 'content-security-policy':
                stripped = self._CSP_FRAME_ANCESTORS_RE.sub('', v).strip().strip(';').strip()
                if not stripped:
                    continue
                v = stripped
                # We inject an inline runtime shim into rewritten HTML (see
                # _rewrite_proxied_html). A restrictive script-src — e.g.
                # "script-src 'self'" with no 'unsafe-inline' — makes the
                # browser silently block that inline <script> from executing,
                # so the app's runtime asset requests stay unproxied and a
                # Vite/Rolldown SPA's CSS preloads 404 ("Unable to preload CSS
                # for /…"). Add 'unsafe-inline' to script-src so the shim runs.
                # Only for responses we actually rewrote.
                if rewritten is not None:
                    parts = [p.strip() for p in v.split(';') if p.strip()]
                    has_script_src = False
                    for i, p in enumerate(parts):
                        if p.lower().startswith('script-src'):
                            has_script_src = True
                            if "'unsafe-inline'" not in p.lower():
                                parts[i] = p + " 'unsafe-inline'"
                    if not has_script_src:
                        parts.append("script-src 'self' 'unsafe-inline'")
                    v = '; '.join(parts)
            if kl == 'location' and not internal_route:
                v = self._rewrite_location_header(v, port)
            self.send_header(k, v)
        if rewritten is not None:
            # Body length changed; the upstream's framing headers were already
            # dropped (hop-by-hop), so declare our own so the client doesn't
            # have to wait on connection close to know the body is complete.
            self.send_header('Content-Length', str(len(rewritten)))
        self.end_headers()

        if method == 'HEAD':
            pass
        elif rewritten is not None:
            try:
                self.wfile.write(rewritten)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            # Stream the body. read1 returns what's available so SSE heartbeats
            # arrive without waiting for a full read() to fill.
            try:
                while True:
                    chunk = resp.read1(65536) if hasattr(resp, 'read1') else resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    try:
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
            except (BrokenPipeError, ConnectionResetError):
                pass
        try:
            conn.close()
        except Exception:
            pass

    # --- Apps API (list + pin CRUD) ---

    def _handle_apps_list(self):
        if not self.check_app_proxy_auth():
            self.send_response(401)
            self.end_headers()
            return
        # Embedded iframes need cookie-based auth; bearer-only deployments
        # can't auth iframe sub-resource requests. Let the SPA show a clear
        # explanation instead of the user staring at mysterious 401s.
        unavailable = None
        if AUTH_MODE != 'oauth2':
            unavailable = ('Applications requires the workspace to run behind an OAuth2 '
                           'proxy so iframe sub-resource requests can authenticate via cookies. '
                           'Current AUTH_MODE is "{}".'.format(AUTH_MODE))
        try:
            apps = AppsManager.list_apps()
        except Exception as e:
            self.send_json({'error': str(e)}, 500)
            return
        self.send_json({
            'apps': apps,
            'unavailable_reason': unavailable,
            'auth_mode': AUTH_MODE,
        })

    def _handle_apps_pin_create(self):
        if not self.check_claude_auth():
            self.send_response(401)
            self.end_headers()
            return
        try:
            body = self.read_json_body(max_bytes=4096) or {}
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        except json.JSONDecodeError:
            self.send_json({'error': 'invalid JSON'}, 400)
            return
        try:
            pin = AppsManager.add_pin(
                port=body.get('port'),
                name=body.get('name'),
                strip_prefix=bool(body.get('strip_prefix', False)),
            )
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        self.send_json({'ok': True, 'pin': {**pin, 'port': AppsManager._validate_port(body.get('port'))}}, 201)

    def _handle_apps_pin_delete(self, port):
        if not self.check_claude_auth():
            self.send_response(401)
            self.end_headers()
            return
        try:
            removed = AppsManager.remove_pin(port)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        self.send_json({'ok': True, 'removed': bool(removed)})

    # --- Additional HTTP verbs for the app proxy ---
    #
    # SimpleHTTPRequestHandler doesn't ship do_PUT / do_HEAD / do_OPTIONS, so
    # they 501 by default. The app proxy needs to forward all common verbs
    # so embedded apps (and their Try-it-out clients) work.

    def do_PUT(self):
        self._consume_bearer_marker()
        if self._readonly_block():
            return
        path = self._strip_route_prefix(self.path)
        if self._dispatch_app_proxy(path, 'PUT'):
            return
        # Messaging provider credentials (issue #329) — set provider + creds.
        if path == '/api/gateway/credentials':
            self.handle_gateway_credentials_put()
            return
        self.send_response(501)
        self.end_headers()

    def do_PATCH(self):
        self._consume_bearer_marker()
        if self._readonly_block():
            return
        path = self._strip_route_prefix(self.path)
        if self._dispatch_app_proxy(path, 'PATCH'):
            return
        self.send_response(501)
        self.end_headers()

    def do_HEAD(self):
        self._consume_bearer_marker()
        path = self._strip_route_prefix(self.path)
        if self._dispatch_app_proxy(path, 'HEAD'):
            return
        if self._dispatch_referer_proxy('HEAD'):
            return
        # Fall back to the parent's static-file HEAD handling.
        return super().do_HEAD()

    def do_OPTIONS(self):
        path = self._strip_route_prefix(self.path)
        if self._dispatch_app_proxy(path, 'OPTIONS'):
            return
        # Permissive default — no CORS preflight wired for any other route.
        self.send_response(204)
        self.end_headers()

    def _rewrite_proxied_html(self, body, port):
        """Rewrite a proxied HTML body so a stock build renders AND can reach
        its backend through the /api/app-proxy/<port> prefix.

        Two parts:

        1. Static src/href URLs (parsed by the browser, not via JS) are made
           relative by dropping the leading slash. We relativize rather than
           prepend a prefix because the client reaches us through an external
           /oauth auth segment that oauth2-proxy strips before the request
           arrives here — so the server can neither see nor reconstruct the URL
           the browser actually used. A relative URL sidesteps that: the
           browser resolves it against the iframe document's real URL
           (…/oauth/api/app-proxy/<port>/ — trailing slash guaranteed by the
           301 above), keeping the /oauth prefix so it authenticates. Skips
           protocol-relative (//cdn) and already-proxied URLs; relative URLs
           are already correct. Also skips Next.js /_next/* build assets:
           Turbopack derives chunk identity from the literal <script src>
           attribute, so relativizing it breaks hydration (see repl below).

        2. A runtime shim (_APP_PROXY_SHIM) is injected into <head> to catch
           requests the app builds in JS at request time — fetch('/runs'),
           XHR, EventSource, WebSocket — which relativizing the HTML can't
           touch. The shim re-prefixes those in the browser, where the full
           client-visible prefix is available.
        """
        def repl(mo):
            url = mo.group(3)
            # Leave protocol-relative (//cdn), already-proxied, AND Next.js
            # build assets (/_next/*) untouched. Next's Turbopack runtime keys
            # every chunk by the *literal* <script src> attribute — it reads
            # getAttribute("src"), strips a fixed base, and uses the remainder
            # as the chunk id to locate and run the page entry. Relativizing
            # that attribute (/_next/… → _next/…) changes the derived id, so
            # the entry chunk is never executed: React never hydrates and the
            # app hangs blank / forever-loading with no console error. Keep
            # /_next/* root-absolute; those escaped requests are recovered by
            # _dispatch_referer_proxy (302 back onto the proxy path, reusing
            # the Referer's /oauth prefix so they re-authenticate).
            if (url.startswith(b'//')
                    or url.startswith(b'/api/app-proxy/')
                    or url.startswith(b'/_next/')):
                return mo.group(0)
            rel = url[1:]  # drop the single leading '/'
            return mo.group(1) + mo.group(2) + (rel or b'./')

        body = self._ABS_ASSET_URL_RE.sub(repl, body)

        # Inject (1) a permissive referrer policy and (2) the runtime shim,
        # right after <head> so they take effect before the app's own scripts.
        # The referrer meta makes the app's *navigations* carry the full iframe
        # URL as Referer — without it, a hard navigation (window.location) that
        # drops the app's base lands at the dashboard root with no Referer and
        # 404s, instead of being redirected back onto the proxy path by
        # _dispatch_referer_proxy.
        head_inject = b'<meta name="referrer" content="unsafe-url">' + self._APP_PROXY_SHIM
        if self._HEAD_OPEN_RE.search(body):
            body = self._HEAD_OPEN_RE.sub(
                lambda mo: mo.group(0) + head_inject, body, count=1)
        else:
            body = head_inject + body
        return body

    @staticmethod
    def _rewrite_location_header(value, port):
        """Map upstream-absolute Locations back to the proxy-prefixed path."""
        prefix = f'/api/app-proxy/{port}'
        for host_form in (f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
            if value.startswith(host_form):
                return prefix + value[len(host_form):]
        # Absolute-path Location (e.g. "/foo") — re-prefix so the iframe
        # navigates through the proxy and not to the dashboard origin root.
        if value.startswith('/') and not value.startswith(prefix + '/') and value != prefix:
            return prefix + value
        return value

    def proxy_vnc_request(self):
        # Proxy requests to the local noVNC server.
        # Gate behind the same auth as the rest of the dashboard — the VNC
        # iframe is loaded from an already-authenticated SPA page, so callers
        # will always carry OAuth2 headers or a Bearer token.
        if not self.check_claude_auth():
            self.send_response(401)
            self.end_headers()
            return

        import urllib.request
        import urllib.parse
        vnc_url = None
        try:
            # Split off path + query; reject anything with control characters
            # before we paste it into a URL. self.path is attacker-controllable.
            raw = self.path[5:]  # strip "/vnc/"
            if any(ord(c) < 0x20 or c in ('\x7f',) for c in raw):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'invalid characters in path')
                return
            if '?' in raw:
                path_part, query_part = raw.split('?', 1)
            else:
                path_part, query_part = raw, ''

            # Normalize and confine: drop empty / "." / ".." segments so the
            # caller cannot climb above /. The destination host is hardcoded
            # to localhost:6081, but a normalized path keeps proxied requests
            # to the shapes noVNC actually serves.
            segments = [
                seg for seg in path_part.split('/')
                if seg and seg not in ('.', '..')
            ]
            safe_path = '/'.join(
                urllib.parse.quote(urllib.parse.unquote(s), safe='') for s in segments
            )
            vnc_url = f"http://localhost:6081/{safe_path}"
            if query_part:
                vnc_url += f"?{query_part}"

            with urllib.request.urlopen(vnc_url, timeout=10) as response:
                content = response.read()
                content_type = response.headers.get('Content-Type', 'text/html')
                self.send_response(200)
                self.send_header('Content-type', content_type)
                self.end_headers()
                self.wfile.write(content)
        except Exception as e:
            safe_url = html.escape(vnc_url) if vnc_url else 'N/A'
            error_html = f'''<!DOCTYPE html>
<html>
<head><title>VNC Proxy Error</title></head>
<body>
    <h1>VNC Proxy Error</h1>
    <p>Error accessing VNC: {html.escape(str(e))}</p>
    <p>Path: {html.escape(self.path)}</p>
    <p>VNC URL: {safe_url}</p>
</body>
</html>'''
            self.send_response(500)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(error_html.encode())
    
    def do_POST(self):
        self._consume_bearer_marker()
        if self._readonly_block():
            return
        try:
            # Handle both /api/* and /browser/api/* and /oauth/browser/api/* paths
            path = self._strip_route_prefix(self.path)
            
            # /api/apps/pins — add a pinned port to the Applications page.
            if path == "/api/apps/pins":
                self._handle_apps_pin_create()
                return
            # /api/app-proxy/<port>/... — forward to a locally-listening
            # web app. Match early so it short-circuits the explicit
            # endpoint list below.
            if self._dispatch_app_proxy(path, 'POST'):
                return
            if path == "/api/launch-chrome":
                self.launch_chrome()
            elif path == "/api/open-localhost":
                self.open_localhost()
            elif path == "/api/test-chrome":
                self.test_chrome()
            # Keep Firefox endpoints for backward compatibility
            elif path == "/api/launch-firefox":
                self.launch_chrome()
            elif path == "/api/test-firefox":
                self.test_chrome()
            elif path == "/api/workspace/update":
                self.handle_workspace_update()
            elif path == "/api/workspace/restart":
                self.handle_workspace_restart()
            # GitHub configuration endpoints
            elif path == "/api/github/ssh/generate":
                self.handle_ssh_generate()
            elif path == "/api/github/config":
                self.handle_git_config_post()
            elif path == "/api/github/auth-mode":
                self.handle_set_auth_mode()
            elif path == "/api/github/cli/login-url":
                self.handle_gh_login_instructions()
            elif path == "/api/github/cli/complete-auth":
                self.handle_gh_check_auth()
            elif path == "/api/github/connect/start":
                self.handle_gh_web_login_start()
            elif path == "/api/github/connect/poll":
                self.handle_gh_web_login_poll()
            elif path == "/api/github/connect/cancel":
                self.handle_gh_web_login_cancel()
            # Claude Task API endpoints
            elif path == "/api/claude/tasks":
                self.handle_claude_create_task()
            elif path == "/api/claude/tasks/terminal":
                self.handle_claude_create_terminal_task()
            elif path == "/api/claude/auth/token/regenerate":
                self.handle_claude_regenerate_token()
            # Hypervisor chat threads
            elif path == "/api/hypervisor/threads":
                self.handle_hypervisor_create_thread()
            # Voice interface (issue #396): server-side speech-to-text
            elif path == "/api/hypervisor/transcribe":
                self.handle_hypervisor_transcribe()
            # Conversation Gateway (issue #306): inbound WhatsApp webhook
            # (provider-signature authed, NOT bearer) + link enrollment (bearer).
            elif path == "/api/gateway/whatsapp/webhook":
                self.handle_gateway_whatsapp_webhook()
            elif path == "/api/gateway/link":
                self.handle_gateway_link_create()
            # Messaging provider credentials (issue #329): test-connection.
            elif path == "/api/gateway/test":
                self.handle_gateway_test()
            # Walkie-Talkie in-app loopback preview (bearer-authed).
            elif path == "/api/gateway/internal/inbound":
                self.handle_gateway_internal_inbound()
            elif path == "/api/gateway/internal/control":
                self.handle_gateway_internal_control()
            # Provider keys (dashboard Settings)
            elif path == "/api/provider-keys":
                self.handle_provider_keys_set()
            # User MCP servers (dashboard Settings, issue #353)
            elif path == "/api/mcp-servers":
                self.handle_mcp_servers_set()
            # Webhook CRUD (dashboard)
            elif path == "/api/webhooks":
                self.handle_webhook_create()
            # Cron CRUD (dashboard)
            elif path == "/api/crons":
                self.handle_cron_create()
            # Desktop launcher (dashboard)
            elif path == "/api/desktop":
                self.handle_desktop_create()
            elif path == "/api/desktop/_reorder":
                self.handle_desktop_reorder()
            # Memory API (dashboard surface; mirrored by MCP)
            elif path == "/api/memory":
                self.handle_memory_upsert()
            elif path == "/api/memory/_consolidate":
                self.handle_memory_consolidate()
            elif path == "/api/memory/_sync_claude":
                self.handle_memory_sync_claude()
            elif path == "/api/memory/_import":
                self.handle_memory_import()
            elif path == "/api/memory/_purge":
                self.handle_memory_purge()
            # Skills API (multi-harness SKILL.md surface)
            elif path == "/api/skills/_scan":
                self.handle_skills_scan()
            # File upload (raw body; X-Dest-Path + X-Filename headers)
            elif path == "/api/files/upload":
                self.handle_file_upload()
            # mkdir under /home/dev (JSON body: {path})
            elif path == "/api/files/mkdir":
                self.handle_file_mkdir()
            # rename/move within /home/dev (JSON body: {from, to})
            elif path == "/api/files/rename":
                self.handle_file_rename()
            else:
                # /api/claude/tasks/{id}/message
                m = re.match(r'^/api/claude/tasks/([A-Za-z0-9_-]+)/message$', path)
                if m:
                    self._claude_task_id = m.group(1)
                    self.handle_claude_followup()
                    return
                # /api/hypervisor/threads/{id}/messages — chat follow-up
                m = re.match(r'^/api/hypervisor/threads/([A-Za-z0-9_-]+)/messages$', path)
                if m:
                    self.handle_hypervisor_send_message(m.group(1))
                    return
                # /api/hypervisor/threads/{id}/stop — halt the running turn
                m = re.match(r'^/api/hypervisor/threads/([A-Za-z0-9_-]+)/stop$', path)
                if m:
                    self.handle_hypervisor_stop(m.group(1))
                    return
                # /api/hypervisor/threads/{id}/restore — undo a soft-delete
                m = re.match(r'^/api/hypervisor/threads/([A-Za-z0-9_-]+)/restore$', path)
                if m:
                    self.handle_hypervisor_restore_thread(m.group(1))
                    return
                # /api/hypervisor/threads/{id}/watchers — arm a cross-turn
                # watcher (#402).
                m = re.match(r'^/api/hypervisor/threads/([A-Za-z0-9_-]+)/watchers$', path)
                if m:
                    self.handle_hypervisor_create_watcher(m.group(1))
                    return
                # /api/hypervisor/threads/{id}/rename — set a custom chat title
                m = re.match(r'^/api/hypervisor/threads/([A-Za-z0-9_-]+)/rename$', path)
                if m:
                    self.handle_hypervisor_rename_thread(m.group(1))
                    return
                # /api/hypervisor/threads/{id}/model — switch the model (#308)
                m = re.match(r'^/api/hypervisor/threads/([A-Za-z0-9_-]+)/model$', path)
                if m:
                    self.handle_hypervisor_set_model(m.group(1))
                    return
                # /api/skills/{name}/sync — cross-harness install (PR2).
                # Stricter name charset than the GET route: only the
                # filesystem-safe [a-z0-9-] set may ever build a write path.
                m = re.match(r'^/api/skills/([a-z0-9-]+)/sync$', path)
                if m:
                    self.handle_skills_sync(m.group(1))
                    return
                # /api/claude/tasks/{id}/rename
                m = re.match(r'^/api/claude/tasks/([A-Za-z0-9_-]+)/rename$', path)
                if m:
                    self._claude_task_id = m.group(1)
                    self.handle_claude_rename_task()
                    return
                # /api/claude/tasks/{id}/redeliver-hook
                m = re.match(r'^/api/claude/tasks/([A-Za-z0-9_-]+)/redeliver-hook$', path)
                if m:
                    self._claude_task_id = m.group(1)
                    self.handle_claude_redeliver_hook()
                    return
                # /api/claude/tasks/{id}/prepare-terminal
                m = re.match(r'^/api/claude/tasks/([A-Za-z0-9_-]+)/prepare-terminal$', path)
                if m:
                    self._claude_task_id = m.group(1)
                    self.handle_claude_prepare_terminal()
                    return
                # /api/claude/tasks/{id}/scroll-mode — toggle tmux copy-mode
                m = re.match(r'^/api/claude/tasks/([A-Za-z0-9_-]+)/scroll-mode$', path)
                if m:
                    self._claude_task_id = m.group(1)
                    self.handle_claude_scroll_mode()
                    return
                # /api/claude/tasks/{id}/key — send one control key to the session
                m = re.match(r'^/api/claude/tasks/([A-Za-z0-9_-]+)/key$', path)
                if m:
                    self._claude_task_id = m.group(1)
                    self.handle_claude_send_key()
                    return
                # Desktop launcher per-item routes
                # PUT-like update (POST + id == "update"); /launch fires
                m = re.match(r'^/api/desktop/([a-z0-9]+)/launch$', path)
                if m:
                    self.handle_desktop_launch(m.group(1))
                    return
                m = re.match(r'^/api/desktop/([a-z0-9]+)$', path)
                if m:
                    self.handle_desktop_update(m.group(1))
                    return
                # /api/webhooks/{id}/test — fire as if from outside (dashboard)
                m = re.match(r'^/api/webhooks/([a-zA-Z0-9_-]+)/test$', path)
                if m:
                    self._webhook_id = m.group(1)
                    self.handle_webhook_test()
                    return
                # /api/webhooks/{id} — inbound receiver, HMAC-authed, NOT bearer.
                # Must come AFTER /test so /test is matched first.
                m = re.match(r'^/api/webhooks/([a-zA-Z0-9_-]+)$', path)
                if m:
                    self._webhook_id = m.group(1)
                    self.handle_webhook_receive()
                    return
                # Cron suspend/resume/run-now/rotate-token
                m = re.match(r'^/api/crons/([a-z0-9-]+)/(suspend|resume|run|rotate-token)$', path)
                if m:
                    self._cron_id = m.group(1)
                    self._cron_action = m.group(2)
                    self.handle_cron_action()
                    return
                # /api/triggers/cron-fire/{id} — receiver called by the
                # CronJob's curl pod. Bearer auth (fire_token), NOT OAuth.
                m = re.match(r'^/api/triggers/cron-fire/([a-z0-9-]+)$', path)
                if m:
                    self._cron_id = m.group(1)
                    self.handle_cron_fire()
                    return
                # Memory: relations endpoint takes a (ns, key) pair.
                m = re.match(r'^/api/memory/([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+)/relations$', path)
                if m:
                    self.handle_memory_link(m.group(1), m.group(2))
                    return
                self.send_response(404)
                self.end_headers()
                self.wfile.write(f'API endpoint not found. Received: {self.path}'.encode())
        except ValueError as e:
            self.send_client_error(str(e), 400)
        except Exception as e:
            self.send_error_response(f'Server error: {str(e)}')
    
    def send_success_response(self, message):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(message.encode())
    
    def send_error_response(self, message):
        error_id = uuid.uuid4().hex[:12]
        import traceback
        traceback.print_exc()
        print(f'[error_id={error_id}] {message}', file=sys.stderr)
        body = json.dumps({'error': 'internal error', 'error_id': error_id})
        self.send_response(500)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(body.encode())
    def send_client_error(self, message, status_code=400):
        body = json.dumps({'error': message})
        self.send_response(status_code)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(body.encode())
    
    def send_livez(self):
        """Liveness probe — proves the HTTP server thread is alive and can
        answer, nothing more. Deliberately does ZERO blocking work: no socket
        connects to sub-services (see send_health_check), no auth, no disk, no
        JSON. On a 2-CPU pod a busy assistant task + many tmux-streaming
        handler threads can starve the GIL enough that a heavier handler can't
        finish inside the 10s liveness timeout for 3 straight probes (~90s),
        and the kubelet then SIGTERMs the container — killing the user's live
        tmux + tasks. A handler this cheap needs the GIL for only microseconds,
        so it returns even under heavy contention. Sub-service status belongs
        to /health (readiness) and the /health/* detail endpoints."""
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(b'ok')

    def send_health_check(self):
        """Overall health check endpoint - always returns 200 to avoid blocking"""
        vscode_status = self.check_service_health('localhost', 8080)
        terminal_status = self.check_service_health('localhost', 7681)
        browser_status = self.check_service_health('localhost', 6081)
        
        health_data = {
            'status': 'healthy' if (terminal_status and browser_status) else 'degraded',
            'services': {
                'vscode': {'status': 'up' if vscode_status else 'down', 'port': 8080},
                'terminal': {'status': 'up' if terminal_status else 'down', 'port': 7681},
                'browser': {'status': 'up' if browser_status else 'down', 'port': 6081}
            },
            'timestamp': time.time()
        }
        
        # Always return 200 to avoid blocking the service
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(json.dumps(health_data).encode())
    
    def send_vscode_health(self):
        """VS Code health check - always returns 200"""
        status = self.check_service_health('localhost', 8080)
        response = {'service': 'vscode', 'status': 'up' if status else 'down', 'port': 8080}
        
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())
    
    def send_terminal_health(self):
        """Terminal health check - always returns 200"""
        status = self.check_service_health('localhost', 7681)
        response = {'service': 'terminal', 'status': 'up' if status else 'down', 'port': 7681}
        
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())
    
    def send_browser_health(self):
        """Browser/VNC health check - always returns 200"""
        vnc_status = self.check_service_health('localhost', 5900)  # x11vnc
        websockify_status = self.check_service_health('localhost', 6081)  # websockify
        
        status = vnc_status and websockify_status
        response = {
            'service': 'browser',
            'status': 'up' if status else 'down',
            'components': {
                'vnc': 'up' if vnc_status else 'down',
                'websockify': 'up' if websockify_status else 'down'
            }
        }
        
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())

    def send_metrics(self):
        """Send system metrics (CPU, memory, disk) as JSON.
        Auth-gated to avoid double-duty as an unauthenticated workload
        side-channel — public-demo callers get through via AUTH_MODE=none."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        metrics = MetricsCollector.get_all_metrics()

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(json.dumps(metrics).encode())

    def send_github_status(self):
        """Send combined GitHub status as JSON.
        Strictly auth-gated (allow_none_mode=False) — the response leaks
        the SSH public-key fingerprint, gh CLI username, and git
        name/email, none of which should ever surface on a public demo."""
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        status = GitHubManager.get_full_status()

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(json.dumps(status).encode())

    def send_git_config(self):
        """Send git config as JSON. Strictly auth-gated (allow_none_mode=False)
        — exposes the operator's git name + email."""
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        config = GitHubManager.get_git_config()

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(json.dumps(config).encode())

    def send_workspace_version(self):
        """Current vs latest workspace version, brokered from the controller.
        Auth-gated (exposes the workspace's image version). Returns
        {available:false} cleanly when self-serve updates aren't wired, so the
        SPA can simply hide the section instead of erroring."""
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if not UpdateManager.enabled():
            self.send_json({'available': False,
                            'reason': 'self-serve updates not configured'})
            return
        status, payload = UpdateManager.get_version()
        if status == 200:
            self.send_json({'available': True, **payload})
        else:
            self.send_json({'available': True, 'error': payload.get('error', 'controller error')},
                           status if status >= 400 else 502)

    def handle_workspace_update(self):
        """Broker a 'restart and pull latest' for THIS workspace to the
        controller. The controller authorizes the action on our own user."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if not UpdateManager.enabled():
            self.send_json({'error': 'self-serve updates not configured'}, 501)
            return
        try:
            n = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(n).decode('utf-8') if 0 < n <= MAX_REQUEST_BODY_BYTES else ''
            data = json.loads(raw) if raw else {}
        except (ValueError, OSError):
            data = {}
        status, payload = UpdateManager.do_update(data.get('version') or None)
        self.send_json(payload, status)

    def handle_workspace_restart(self):
        """Broker a plain restart (current image, no version change) for THIS
        workspace to the controller (#352). Same token-gated self-serve channel
        as update, so the controller authorizes it on our own user only."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if not UpdateManager.enabled():
            self.send_json({'error': 'self-serve restart not configured'}, 501)
            return
        status, payload = UpdateManager.do_restart()
        self.send_json(payload, status)

    def handle_ssh_generate(self):
        """Handle SSH key generation request"""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body) if body else {}

            email = data.get('email', 'user@example.com')
            result = GitHubManager.generate_ssh_key(email)

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def handle_git_config_post(self):
        """Handle git config update request"""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body) if body else {}

            name = data.get('name', '')
            email = data.get('email', '')

            if not name or not email:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Name and email are required'}).encode())
                return

            result = GitHubManager.set_git_config(name, email)

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def handle_set_auth_mode(self):
        """Switch the workspace GitHub auth mode: 'app' (managed installation
        token) or 'personal' (the user's own `gh auth login`). Persists the
        choice and re-points git's credential helper immediately (issue #256).
        Auth-gated; blocked in read-only demo via the do_POST chokepoint."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body) if body else {}
            mode = (data.get('mode') or '').strip()
            status = GitHubManager.set_auth_mode(mode)
            self.send_json(status, 200)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def handle_gh_login_instructions(self):
        """Return instructions for gh CLI authentication"""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        instructions = GitHubManager.start_device_flow()

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(instructions).encode())

    def handle_gh_check_auth(self):
        """Check if gh CLI authentication is complete"""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        status = GitHubManager.get_gh_cli_status()

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(status).encode())

    def handle_gh_web_login_start(self):
        """Start the browser-less 'Connect GitHub' device flow (issue #303) and
        return the one-time code + verification URL for the dashboard to show.
        Auth-gated; blocked in read-only demo via the do_POST chokepoint."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            self.send_json(GitHubManager.start_web_login(), 200)
        except Exception as e:
            self.send_json({'error': str(e)}, 502)

    def handle_gh_web_login_poll(self):
        """Poll the in-flight device flow. On success the workspace is switched
        to 'personal' mode server-side and the response carries connected:true."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            self.send_json(GitHubManager.poll_web_login(), 200)
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def handle_gh_web_login_cancel(self):
        """Abort an in-flight device flow (user closed the dialog)."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        GitHubManager.cancel_web_login()
        self.send_json({'ok': True}, 200)

    def check_service_health(self, host, port):
        """Check if a service is listening on the given port"""
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(2)
                result = s.connect_ex((host, port))
                return result == 0
        except Exception:
            return False
    
    def test_chrome(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            # Test browser installation
            browser_paths = [
                '/usr/local/bin/browser',
                '/usr/bin/lynx',
                '/usr/bin/w3m', 
                '/usr/bin/firefox-esr',
                '/usr/bin/firefox',
                '/usr/bin/chromium-browser',
                '/usr/bin/google-chrome'
            ]
            
            browser_path = None
            for path in browser_paths:
                if os.path.exists(path):
                    browser_path = path
                    break
            
            if not browser_path:
                self.send_error_response('Browser not found. Installation may have failed.')
                return
            
            # Test Xvfb display
            display = os.environ.get('DISPLAY', ':99')
            try:
                result = subprocess.run(['xdpyinfo', '-display', display], 
                                       capture_output=True, text=True, timeout=5)
                if result.returncode != 0:
                    # xdpyinfo failed, but check if Xvfb process is running instead
                    xvfb_check = subprocess.run(['pgrep', 'Xvfb'], capture_output=True)
                    if xvfb_check.returncode != 0:
                        self.send_error_response(f'X11 display {display} not available')
                        return
            except (subprocess.TimeoutExpired, FileNotFoundError):
                # xdpyinfo not available or timed out, check if Xvfb process is running
                xvfb_check = subprocess.run(['pgrep', 'Xvfb'], capture_output=True)
                if xvfb_check.returncode != 0:
                    self.send_error_response(f'X11 display {display} not available (Xvfb not running)')
                    return
            
            self.send_success_response(f'✅ Browser found at: {browser_path}\n✅ X11 display {display} available')
            
        except Exception as e:
            self.send_error_response(f'Test failed: {str(e)}')
    
    def launch_chrome(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            # Try different Chrome/Chromium locations
            browser_commands = [
                ('/usr/local/bin/browser', []),
                ('/usr/bin/firefox-esr', ['--safe-mode']),
                ('/usr/bin/firefox', ['--safe-mode']),
                ('firefox-esr', ['--safe-mode']),
                ('firefox', ['--safe-mode']),
                ('chromium-browser', ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu']),
                ('/usr/bin/chromium-browser', ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu']),
                ('/usr/bin/google-chrome', ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'])
            ]
            
            browser_cmd = None
            browser_args = []
            for cmd, args in browser_commands:
                if os.path.exists(cmd) or subprocess.run(['which', cmd], capture_output=True).returncode == 0:
                    browser_cmd = cmd
                    browser_args = args
                    break
            
            if not browser_cmd:
                self.send_error_response('No Chrome browser found. Download may have failed.')
                return
            
            env = os.environ.copy()
            env['DISPLAY'] = ':99'
            
            # Launch browser in background
            cmd_list = [browser_cmd] + browser_args + ['--new-window']
            process = subprocess.Popen(
                cmd_list, 
                env=env,
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.DEVNULL
            )
            
            # Give it a moment to start
            time.sleep(2)
            
            if process.poll() is None:  # Process is still running
                self.send_success_response(f'✅ Chrome launched successfully (PID: {process.pid})')
            else:
                self.send_error_response('Chrome process exited immediately')
                
        except FileNotFoundError:
            self.send_error_response('Chrome not found. Please install Chrome first.')
        except Exception as e:
            self.send_error_response(f'Error launching Chrome: {str(e)}')
    
    def open_localhost(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            # Accept an optional {"port": <int>, "path": "<suffix>"} JSON
            # body so the dashboard's Preview pane can re-point the in-pod
            # browser without a code change. Path is appended after the
            # port (e.g. localhost:8080/admin or localhost:3000/?dev=1);
            # falls back to "/" when nothing is sent. The historical
            # port-only body still works.
            port = 8080
            url_path = '/'
            try:
                content_length = int(self.headers.get('Content-Length', 0) or 0)
                if content_length:
                    raw = self.rfile.read(content_length).decode('utf-8')
                    body = json.loads(raw) if raw else {}
                    if isinstance(body, dict):
                        if 'port' in body:
                            port = int(body['port'])
                        raw_path = str(body.get('path') or '').strip()
                        if raw_path:
                            # Normalize: ensure leading slash, no scheme,
                            # no host, no embedded newlines. Reject if it
                            # contains characters that don't belong in a
                            # path/query/fragment.
                            if '\n' in raw_path or '\r' in raw_path or ' ' in raw_path:
                                self.send_error_response('path must not contain whitespace or newlines')
                                return
                            if raw_path.lower().startswith(('http://', 'https://')):
                                self.send_error_response('path must be a relative suffix, not a full URL')
                                return
                            if not raw_path.startswith('/'):
                                raw_path = '/' + raw_path
                            url_path = raw_path
            except (ValueError, json.JSONDecodeError):
                self.send_error_response('Invalid JSON body — expected {"port": <int>, "path": "<suffix>"}')
                return
            if not (1 <= port <= 65535):
                self.send_error_response('port must be between 1 and 65535')
                return

            env = os.environ.copy()
            env['DISPLAY'] = ':99'

            url = f'http://localhost:{port}{url_path}'

            # Kill only browsers launched by this handler — pkill -f chrome
            # would also kill any user-spawned dev tool whose name includes
            # the substring (e.g. chrome-devtools-frontend). The marker dir
            # is unique to this handler and appears in every launched
            # browser's argv (chromium via --user-data-dir=, firefox via
            # -profile <dir>) so pkill -f on the literal path matches both.
            kc_user_data_dir = '/tmp/kc-managed-browser'
            os.makedirs(kc_user_data_dir, exist_ok=True)
            subprocess.run(['pkill', '-f', kc_user_data_dir],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            time.sleep(0.3)

            # --app and --start-fullscreen together give a kiosk-like surface:
            # no tabs, no URL bar, no window chrome — just the page. Combined
            # with vnc.html?resize=scale on the dashboard side, the Preview
            # pane ends up showing essentially only the browser content.
            # --user-data-dir is the marker pkill uses above to scope the
            # kill to only browsers we launched.
            chrome_args = [
                '--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu',
                f'--user-data-dir={kc_user_data_dir}',
                '--start-fullscreen', f'--app={url}',
            ]
            # Firefox uses -profile <dir>; we mirror the chromium marker so
            # both can be killed by the single pkill above.
            firefox_args = ['--safe-mode', '-profile', kc_user_data_dir, '--kiosk', url]

            browser_commands = [
                ('chromium-browser', chrome_args),
                ('/usr/bin/chromium-browser', chrome_args),
                ('/usr/bin/google-chrome', chrome_args),
                ('/usr/local/bin/browser', []),
                ('/usr/bin/firefox-esr', firefox_args),
                ('/usr/bin/firefox', firefox_args),
                ('firefox-esr', firefox_args),
                ('firefox', firefox_args),
            ]

            browser_cmd = None
            browser_args = []
            for cmd, args in browser_commands:
                if os.path.exists(cmd) or subprocess.run(['which', cmd], capture_output=True).returncode == 0:
                    browser_cmd = cmd
                    browser_args = args
                    break

            if not browser_cmd:
                self.send_error_response('No Chrome browser found. Download may have failed.')
                return

            # If we fell through to /usr/local/bin/browser (no args defined),
            # append the URL so it still navigates somewhere.
            cmd_list = [browser_cmd] + browser_args
            if browser_cmd == '/usr/local/bin/browser':
                cmd_list.append(url)

            process = subprocess.Popen(
                cmd_list,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            # Give it a moment to start
            time.sleep(1)

            if process.poll() is None:  # Process is still running
                self.send_success_response(f'✅ Chrome opened with {url} (PID: {process.pid})')
            else:
                self.send_error_response('Chrome process exited immediately')

        except FileNotFoundError:
            self.send_error_response('Chrome not found. Please install Chrome first.')
        except Exception as e:
            self.send_error_response(f'Error opening localhost in Chrome: {str(e)}')

class EventBroker:
    """In-process fan-out of dashboard events to connected /api/events SSE
    clients, so the SPA can replace per-route polling with push (issue #93).

    Each subscriber gets a small bounded queue. If a client is too slow we drop
    its oldest event rather than block the publisher — SSE is lossy-tolerant
    here because the SPA reconciles via a normal fetch on (re)connect, so a
    dropped event at worst delays an update until the next poll-fallback tick.
    publish() never raises and never blocks the caller (e.g. the reconcile
    loop or a request thread).
    """

    QUEUE_MAX = 200
    _subscribers = set()
    _lock = threading.Lock()

    @classmethod
    def subscribe(cls):
        q = queue.Queue(maxsize=cls.QUEUE_MAX)
        with cls._lock:
            cls._subscribers.add(q)
        return q

    @classmethod
    def unsubscribe(cls, q):
        with cls._lock:
            cls._subscribers.discard(q)

    @classmethod
    def subscriber_count(cls):
        with cls._lock:
            return len(cls._subscribers)

    @classmethod
    def publish(cls, event_type, data=None):
        """Fan an event out to every subscriber. Never raises, never blocks."""
        event = {'type': event_type, 'data': data or {}, 'ts': time.time()}
        with cls._lock:
            subs = list(cls._subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                # Slow consumer — drop its oldest event to make room.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass
        return event


class TaskReconciler:
    """Single-process background poller that reconciles non-terminal task
    status on an interval, so completion hooks fire and finished_at /
    waiting-for-input update even when no client is reading the task.

    Idempotent; safe to start once. Modeled on memory.sync.ClaudeMemorySyncer.
    See issue #96.
    """

    _started = False
    _thread = None
    _stop_event = threading.Event()
    _last_run_at = 0.0
    _last_reconciled = 0
    _start_lock = threading.Lock()

    @classmethod
    def start(cls, *, interval_seconds=10):
        with cls._start_lock:
            if cls._started:
                return
            cls._started = True

        def _loop():
            while not cls._stop_event.is_set():
                try:
                    cls._last_reconciled = ClaudeTaskManager.reconcile_running()
                    cls._last_run_at = time.time()
                except Exception as e:
                    print(f'[task-reconciler] pass failed: {e}', file=sys.stderr)
                cls._stop_event.wait(interval_seconds)

        t = threading.Thread(target=_loop, name='task-reconciler', daemon=True)
        cls._thread = t
        t.start()

    @classmethod
    def status(cls):
        return {
            'running': cls._started and (cls._thread is not None and cls._thread.is_alive()),
            'last_run_at': cls._last_run_at or None,
            'last_reconciled': cls._last_reconciled,
        }


if __name__ == "__main__":
    # Change to the directory containing our files
    os.chdir('/tmp/browser')

    # Initialize the persistent-memory subsystem (runs migrations, opens DB).
    # Failure here is non-fatal: the rest of the server keeps working, and
    # /api/memory* returns 503 until the import error is fixed.
    if _MEMORY_AVAILABLE:
        try:
            MemoryManager.store()
            print(f'[memory] initialized at /home/dev/.claude-memory/memory.db')
        except Exception as e:
            print(f'[memory] init failed: {e}', file=sys.stderr)
        # Start background sync of Claude Code's native auto-memory files
        # (~/.claude/projects/*/memory/*.md) into the SQLite store so they
        # appear in the dashboard alongside dashboard- and MCP-authored
        # entries. One-way, idempotent, skips unchanged via mtime tag.
        try:
            ClaudeMemorySyncer.start(interval_seconds=60)
            print('[memory] claude-auto-memory syncer started (60s)')
        except Exception as e:
            print(f'[memory] syncer start failed: {e}', file=sys.stderr)

        # Start the Phase-2 embedding worker: drains embeddings_pending into
        # the vec_memories table so search() can fuse keyword + semantic hits.
        # No-ops (returns False) when no provider is configured or the
        # sqlite-vec extension is unavailable — Phase-1 deploys are unaffected.
        try:
            _embed_interval = int(os.environ.get('KC_EMBED_INTERVAL', '30'))
        except (TypeError, ValueError):
            _embed_interval = 30
        try:
            if EmbeddingWorker.start(interval_seconds=_embed_interval):
                print(f'[memory] embedding worker started ({_embed_interval}s)')
            else:
                print('[memory] embedding worker disabled '
                      '(no provider or sqlite-vec unavailable)')
        except Exception as e:
            print(f'[memory] embedding worker start failed: {e}', file=sys.stderr)

        # Optional periodic GC (#107): hard-purge soft-deleted memories older
        # than KC_MEMORY_GC_DAYS and VACUUM, so tombstones don't accumulate
        # unbounded. Off by default (var unset/<=0); manual purge is always
        # available via POST /api/memory/_purge.
        try:
            _gc_days = float(os.environ.get('KC_MEMORY_GC_DAYS', '0') or '0')
        except (TypeError, ValueError):
            _gc_days = 0.0
        if _gc_days > 0:
            try:
                _gc_interval_h = float(os.environ.get('KC_MEMORY_GC_INTERVAL_H', '12') or '12')
            except (TypeError, ValueError):
                _gc_interval_h = 12.0

            def _gc_loop(days, interval_s):
                while True:
                    try:
                        res = MemoryManager.purge_deleted(older_than_days=days)
                        if res.get('purged_memories'):
                            print(f"[memory] gc purged {res['purged_memories']} "
                                  f"reclaimed {res['bytes_reclaimed']}B")
                    except Exception as e:
                        print(f'[memory] gc pass failed: {e}', file=sys.stderr)
                    time.sleep(interval_s)

            t = threading.Thread(
                target=_gc_loop, args=(_gc_days, _gc_interval_h * 3600),
                name='memory-gc', daemon=True)
            t.start()
            print(f'[memory] periodic GC started '
                  f'(>{_gc_days}d every {_gc_interval_h}h)')

    # Multi-harness skills scanner (issue #187): keeps an in-memory snapshot
    # of SKILL.md-style definitions from every supported agent harness and
    # publishes 'skills.changed' on the EventBroker when files change on disk.
    # Independent of the memory subsystem — its own availability flag.
    if _SKILLS_AVAILABLE:
        try:
            _skills_interval = int(os.environ.get('KC_SKILLS_INTERVAL', '30'))
        except (TypeError, ValueError):
            _skills_interval = 30
        try:
            SkillsSyncer.start(interval_seconds=_skills_interval,
                               publish=EventBroker.publish)
            print(f'[skills] multi-harness skills syncer started ({_skills_interval}s)')
        except Exception as e:
            print(f'[skills] syncer start failed: {e}', file=sys.stderr)

    # Hypervisor chat GC (#260): chats are soft-deleted (thread.json stamped
    # with deleted_at) so an accidental delete can be restored from "Recently
    # deleted". Hard-purge tombstones older than KC_HYPERVISOR_GC_DAYS on boot
    # and periodically so the PVC doesn't grow unbounded. Defaults to 30 days;
    # set <=0 to keep tombstones forever (manual purge only).
    if _HYPERVISOR_AVAILABLE:
        try:
            _hv_gc_days = float(os.environ.get('KC_HYPERVISOR_GC_DAYS', '30') or '30')
        except (TypeError, ValueError):
            _hv_gc_days = 30.0
        if _hv_gc_days > 0:
            try:
                _hv_gc_interval_h = float(
                    os.environ.get('KC_HYPERVISOR_GC_INTERVAL_H', '12') or '12')
            except (TypeError, ValueError):
                _hv_gc_interval_h = 12.0

            def _hv_gc_loop(days, interval_s):
                while True:
                    try:
                        res = HypervisorSession.purge_deleted(older_than_days=days)
                        if res.get('purged'):
                            print(f"[hypervisor] gc purged {res['purged']} "
                                  f"deleted chat(s)")
                    except Exception as e:
                        print(f'[hypervisor] gc pass failed: {e}', file=sys.stderr)
                    time.sleep(interval_s)

            t = threading.Thread(
                target=_hv_gc_loop, args=(_hv_gc_days, _hv_gc_interval_h * 3600),
                name='hypervisor-gc', daemon=True)
            t.start()
            print(f'[hypervisor] periodic GC started '
                  f'(>{_hv_gc_days}d every {_hv_gc_interval_h}h)')

    # Conversation Gateway (issue #306): build the gateway + install its
    # turn-complete observer at startup so a turn dispatched over WhatsApp
    # delivers its result even if the first inbound races the runner. Lazy
    # get_gateway() also installs it, but doing it here makes it deterministic.
    if _GATEWAY_AVAILABLE and _HYPERVISOR_AVAILABLE:
        try:
            if get_gateway() is not None:
                print('[gateway] turn-complete observer installed')
        except Exception as e:
            print(f'[gateway] startup init failed: {e}', file=sys.stderr)

    # Cross-turn watchers (issue #402): the server process owns the poll loop,
    # so a watcher armed inside a Hypervisor turn survives the turn (and, via
    # per-thread watchers.json, a server restart). Wire the task-status
    # provider here — hypervisor_session can't import server.
    if _HYPERVISOR_AVAILABLE and hv_watchers is not None:
        def _hv_watch_task_status(task_id):
            # Task ids are [A-Za-z0-9_-]; refuse anything else so a watcher
            # target can never traverse out of TASKS_DIR.
            if not re.fullmatch(r'[A-Za-z0-9_-]+', task_id or ''):
                return None
            task_dir = os.path.join(ClaudeTaskManager.TASKS_DIR, task_id)
            meta_path = os.path.join(task_dir, 'task.json')
            if not os.path.isfile(meta_path):
                return None
            with open(meta_path) as f:
                meta = json.load(f)
            # Same reconcile the task endpoints run: flips a finished tmux
            # session to completed and derives waiting-for-input, so the
            # watcher sees fresh status even when nobody is polling the API.
            ClaudeTaskManager._reconcile_status(meta, task_dir)
            return meta.get('status', 'unknown')

        try:
            hv_watchers.set_task_status_provider(_hv_watch_task_status)
            hv_watchers.start()
            print('[hypervisor] cross-turn watcher loop started')
        except Exception as e:
            print(f'[hypervisor] watcher start failed: {e}', file=sys.stderr)

    # Background task reconciler: flips finished tasks running -> completed and
    # fires their completion hooks even when nothing is reading them, so headless
    # webhook/cron callbacks are timely (issue #96).
    try:
        _reconcile_interval = int(os.environ.get('KC_RECONCILE_INTERVAL', '10'))
    except (TypeError, ValueError):
        _reconcile_interval = 10
    try:
        TaskReconciler.start(interval_seconds=_reconcile_interval)
        print(f'[tasks] background reconciler started ({_reconcile_interval}s)')
    except Exception as e:
        print(f'[tasks] reconciler start failed: {e}', file=sys.stderr)

    print("Starting Browser API Server on port 6080...")
    print("Available endpoints:")
    print("  GET  /           - Browser interface")
    print("  POST /api/launch-chrome - Launch Chrome")
    print("  POST /api/open-localhost - Open localhost:8080 in Chrome")
    print("  POST /api/test-chrome   - Test Chrome installation")
    print("  POST /api/launch-firefox - Launch Chrome (legacy endpoint)")
    print("  POST /api/test-firefox   - Test Chrome (legacy endpoint)")
    print("  --- Claude Task API ---")
    print("  POST /api/claude/tasks              - Create new task")
    print("  POST /api/claude/tasks/terminal     - Create plain-bash terminal task")
    print("  GET  /api/claude/tasks              - List all tasks")
    print("  GET  /api/claude/tasks/{id}         - Get task detail + output")
    print("  GET  /api/claude/tasks/{id}/output  - Get raw output")
    print("  POST /api/claude/tasks/{id}/message - Send follow-up prompt")
    print("  POST /api/claude/tasks/{id}/rename  - Rename a task")
    print("  DELETE /api/claude/tasks/{id}       - Kill a running task")
    print("  GET  /api/claude/auth/token         - Get bearer token (OAuth2 only)")
    print("  POST /api/claude/auth/token/regenerate - Regenerate token (OAuth2 only)")
    print("  GET  /api/claude/assistants         - List enabled assistants")
    print("  GET  /api/hypervisor/config         - Hypervisor chat config")
    print("  *    /api/hypervisor/threads[/{id}]  - Hypervisor chat threads")
    print("  --- Memory API (Phase 1) ---")
    print("  GET    /api/memory                       - List/search memories")
    print("  POST   /api/memory                       - Upsert a memory")
    print("  GET    /api/memory/{ns}/{key}            - Get one memory")
    print("  DELETE /api/memory/{ns}/{key}            - Soft-delete a memory")
    print("  GET    /api/memory/{ns}/{key}/history    - Revisions")
    print("  GET    /api/memory/{ns}/{key}/refs       - Access log")
    print("  GET    /api/memory/{ns}/{key}/neighbors  - Graph walk")
    print("  POST   /api/memory/{ns}/{key}/relations  - Create relation")
    print("  GET    /api/memory/stats                 - Counts + health")
    print("  GET    /api/skills                       - List skills (all harnesses)")
    print("  GET    /api/skills/{name}                - One skill (+divergent variants)")
    print("  GET    /api/skills/stats                 - Counts by system/scope")
    print("  POST   /api/skills/_scan                 - Force rescan")
    print("  POST   /api/skills/{name}/sync           - Install skill into other harnesses")

    with http.server.ThreadingHTTPServer(("", 6080), BrowserHandler) as httpd:
        httpd.serve_forever()