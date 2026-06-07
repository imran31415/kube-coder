#!/usr/bin/env python3
"""
Global constants and environment variable configuration for the kube-coder server.
This module centralizes all configuration to avoid scattering throughout the codebase.
"""

import os
import sys

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


def check_safety_invariants():
    """Validate safety invariants at startup."""
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
    if DEMO_SHOW_ALL:
        print('[server.py] DEMO_SHOW_ALL=true — SPA renders mutation UI (still 403-gated).', file=sys.stderr)
