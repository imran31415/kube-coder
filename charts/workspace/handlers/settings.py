"""Settings: provider keys, user MCP servers, subscription logins (#100).

Nine routes across three verbs — the credentials half of the dashboard's
Settings page. Contiguous on every verb, so the table is a straight lift.

The GitHub and workspace half of that page is NOT here: those writes went
into handlers/system.py, next to the `/api/github/status`, `/api/github/config`
and `/api/workspace/version` reads that #733 moved there. Splitting a domain
across two modules by verb was the one wart that PR left, and reuniting them
costs nothing now.

## Each of the three has a different failure mode, and each keeps it

* Provider keys degrade to a file-backed store that may simply not be
  configured.
* MCP servers are gated on `_MCP_REGISTRY_AVAILABLE` — `_mcp_registry_gate`
  answers 503 rather than 404 when the registry module is missing, because
  the surface exists, the backing does not.
* The Claude login flow is a four-step state machine (start / code / poll /
  cancel) against a single in-process manager, so its gate refuses when no
  login is in flight.

All three gates stay in the handlers, as before.
"""

import json
import re

import handlers
from handlers.routing import RouteTable


class SettingsRoutes:
    """BrowserHandler methods backing `ROUTES`."""

    def handle_provider_keys_list(self):
        # Secrets endpoint — must not be reachable in the unauth public demo
        # (same posture as send_git_config), so allow_none_mode=False.
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        # Masked view only — never returns the key itself.
        self.send_json({'providers': handlers.server.ProviderKeysManager.public_view()})

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
        ok, err = handlers.server.ProviderKeysManager.set(provider, data.get('key'))
        if not ok:
            self.send_json({'error': err}, 400)
            return
        self.send_json({'ok': True, 'provider': provider})

    def handle_provider_keys_delete(self, provider):
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        handlers.server.ProviderKeysManager.delete(provider)
        self.send_json({'ok': True})

    # --- User MCP servers (issue #353) ---
    # Env values may hold API keys, so gate like provider-keys: never
    # reachable in the unauth public demo (allow_none_mode=False), and the
    # list view is redacted (mcp_registry.public_view — hints, never values).

    def _mcp_registry_gate(self):
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return False
        if not handlers.server._MCP_REGISTRY_AVAILABLE:
            self.send_json({'error': 'MCP registry unavailable'}, 503)
            return False
        return True

    def handle_mcp_servers_list(self):
        if not self._mcp_registry_gate():
            return
        self.send_json({'servers': handlers.server.mcp_registry.public_view()})

    def handle_mcp_servers_set(self):
        if not self._mcp_registry_gate():
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        name = (data.get('name') or '').strip()
        ok, err = handlers.server.mcp_registry.set_server(
            name, data.get('command') or '',
            args=data.get('args'), env=data.get('env'),
            enabled=data.get('enabled', True))
        if not ok:
            self.send_json({'error': err}, 400)
            return
        self.send_json({'ok': True, 'name': name, 'sync': handlers.server.mcp_registry.sync_all()})

    def handle_mcp_servers_delete(self, name):
        if not self._mcp_registry_gate():
            return
        if not handlers.server.mcp_registry.delete_server(name):
            self.send_json({'error': 'MCP server not found'}, 404)
            return
        self.send_json({'ok': True, 'sync': handlers.server.mcp_registry.sync_all()})

    def handle_subscriptions_list(self):
        # Reports subscription-login status only (plan/expiry) — no token
        # material — but still a secrets-adjacent endpoint, so gate it like
        # provider-keys (never reachable in the unauth public demo).
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        self.send_json({
            'subscriptions': handlers.server.SubscriptionStatusManager.public_view(),
            # Whether a spawned Claude session would have a working credential —
            # the first-win gate (#494) reads this rather than re-deriving the
            # oauth/api-key precedence in the SPA.
            'claude_ready': handlers.server.SubscriptionStatusManager.claude_credential_present(),
        })

    def handle_subscriptions_logout(self, provider):
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        ok, err = handlers.server.SubscriptionStatusManager.logout(provider)
        if not ok:
            self.send_json({'error': err or 'logout failed'}, 400)
            return
        self.send_json({'ok': True})

    # --- Browser-less "Connect Claude account" (dashboard Settings) ---
    # Same gate as the other subscription endpoints: secrets-adjacent, so
    # never reachable in the unauth public demo (allow_none_mode=False).

    def _claude_login_gate(self):
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return False
        return True

    def handle_claude_login_start(self):
        """Start the server-side `claude auth login` flow and return the OAuth
        URL for the dashboard to open in the user's own browser."""
        if not self._claude_login_gate():
            return
        try:
            self.send_json(handlers.server.ClaudeWebLoginManager.start(), 200)
        except RuntimeError as e:
            self.send_json({'error': str(e)}, 502)

    def handle_claude_login_code(self):
        """Accept the authorization code the user pasted and feed it to the
        waiting CLI. The code is a one-time secret — never logged or echoed."""
        if not self._claude_login_gate():
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        ok, err = handlers.server.ClaudeWebLoginManager.submit_code(data.get('code'))
        if not ok:
            self.send_json({'error': err}, 400)
            return
        self.send_json({'ok': True})

    def handle_claude_login_poll(self):
        """Poll the in-flight login; on success the response carries
        connected:true plus the refreshed subscription view."""
        if not self._claude_login_gate():
            return
        self.send_json(handlers.server.ClaudeWebLoginManager.poll(), 200)

    def handle_claude_login_cancel(self):
        """Abort an in-flight login (user closed the dialog)."""
        if not self._claude_login_gate():
            return
        handlers.server.ClaudeWebLoginManager.cancel()
        self.send_json({'ok': True})


ROUTES = RouteTable()

#: Provider keys are environment-variable names; MCP server names are slugs;
#: a subscription provider is a bare lower-case word (claude|codex).
_ENV = r'([A-Z_]+)'
_MCP = r'([A-Za-z0-9_-]+)'
_PROVIDER = r'([a-z]+)'

ROUTES.add('GET', '/api/provider-keys', 'handle_provider_keys_list')
ROUTES.add('GET', '/api/mcp-servers', 'handle_mcp_servers_list')
ROUTES.add('GET', '/api/subscriptions', 'handle_subscriptions_list')

ROUTES.add('POST', '/api/provider-keys', 'handle_provider_keys_set')
ROUTES.add('POST', '/api/mcp-servers', 'handle_mcp_servers_set')
# Browser-less "Connect Claude account": a four-step state machine. These are
# fixed paths under /subscriptions/claude/, so the DELETE logout route's
# `([a-z]+)` provider pattern cannot reach them.
ROUTES.add('POST', '/api/subscriptions/claude/login/start',
           'handle_claude_login_start')
ROUTES.add('POST', '/api/subscriptions/claude/login/code',
           'handle_claude_login_code')
ROUTES.add('POST', '/api/subscriptions/claude/login/poll',
           'handle_claude_login_poll')
ROUTES.add('POST', '/api/subscriptions/claude/login/cancel',
           'handle_claude_login_cancel')

ROUTES.add('DELETE', re.compile(rf'^/api/provider-keys/{_ENV}$'),
           'handle_provider_keys_delete')
ROUTES.add('DELETE', re.compile(rf'^/api/mcp-servers/{_MCP}$'),
           'handle_mcp_servers_delete')
# Log out of a subscription CLI (claude|codex).
ROUTES.add('DELETE', re.compile(rf'^/api/subscriptions/{_PROVIDER}$'),
           'handle_subscriptions_logout')
