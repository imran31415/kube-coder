"""Tests for the settings route table, and system's new write side (#100).

Two things landed together:

  * `handlers/settings.py` — provider keys, user MCP servers and the
    subscription logins. Nine paths, twelve routes, three verbs.
  * The GitHub and workspace POST routes joined `handlers/system.py`, next to
    the `/api/github/*` and `/api/workspace/version` reads #733 put there.
    That PR split one domain across two modules by verb purely because its
    contiguous GET block ended where it did; this reunites them, so the
    system table's write side is asserted here too.

Handler behaviour is covered by tests/server_test.py's ProviderKeysManager
and SubscriptionStatusManager suites, tests/mcp_registry_test.py and
tests/github_app_token_test.py — all unchanged.

Run with:
    cd charts/workspace && python3 -m unittest tests.settings_routes_table_test
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402
from handlers import settings, system  # noqa: E402
from handlers.routing import RouteTable  # noqa: E402
from tests.http_harness import (  # noqa: E402
    DomainEndpointTests, DomainRouteTests, EndpointTestCase, args_for,
)


class SettingsRouteTests(DomainRouteTests, unittest.TestCase):

    table = settings.ROUTES
    foreign_paths = (('GET', '/api/provider-keys/'),
                     ('GET', '/api/mcp-serversx'),
                     ('DELETE', '/api/provider-keys/lowercase'),
                     ('POST', '/api/subscriptions/claude/login/bogus'))
    oauth_samples = (('GET', '/api/provider-keys'),
                     ('POST', '/api/mcp-servers'),
                     ('DELETE', '/api/subscriptions/claude'))
    wrong_verb_samples = (('PUT', '/api/provider-keys'),
                          ('GET', '/api/subscriptions/claude/login/start'),
                          ('DELETE', '/api/subscriptions'))

    def test_the_routes(self):
        for http_method, path, handler in (
                ('GET', '/api/provider-keys', 'handle_provider_keys_list'),
                ('GET', '/api/mcp-servers', 'handle_mcp_servers_list'),
                ('GET', '/api/subscriptions', 'handle_subscriptions_list'),
                ('POST', '/api/provider-keys', 'handle_provider_keys_set'),
                ('POST', '/api/mcp-servers', 'handle_mcp_servers_set'),
                ('DELETE', '/api/provider-keys/MY_KEY',
                 'handle_provider_keys_delete'),
                ('DELETE', '/api/mcp-servers/some-server',
                 'handle_mcp_servers_delete'),
                ('DELETE', '/api/subscriptions/claude',
                 'handle_subscriptions_logout')):
            self.assertEqual(self.resolve(http_method, path), handler, path)

    def test_the_claude_login_state_machine(self):
        for step in ('start', 'code', 'poll', 'cancel'):
            self.assertEqual(
                self.resolve('POST', f'/api/subscriptions/claude/login/{step}'),
                f'handle_claude_login_{step}', step)

    def test_the_login_steps_are_not_reachable_as_a_logout(self):
        # The logout route is DELETE /api/subscriptions/([a-z]+) — one
        # segment, so it cannot reach the four-segment login paths even
        # though they share the prefix.
        self.assertIsNone(
            self.resolve('DELETE', '/api/subscriptions/claude/login/start'))
        self.assertEqual(self.resolve('DELETE', '/api/subscriptions/codex'),
                         'handle_subscriptions_logout')

    def test_a_provider_key_name_is_an_env_var_name(self):
        self.assertEqual(
            args_for(settings.ROUTES, 'DELETE', '/api/provider-keys/OPENAI_KEY'),
            ('OPENAI_KEY',))
        for bad in ('lower', 'Mixed_Case', 'WITH-DASH', 'WITH9DIGIT'):
            self.assertIsNone(
                self.resolve('DELETE', f'/api/provider-keys/{bad}'), bad)

    def test_an_mcp_server_name_is_wider_than_a_key_name(self):
        for name in ('my-server', 'My_Server', 'srv9'):
            self.assertEqual(
                self.resolve('DELETE', f'/api/mcp-servers/{name}'),
                'handle_mcp_servers_delete', name)

    def test_order_is_presentational(self):
        inverted = RouteTable()
        inverted.routes.extend(reversed(settings.ROUTES.routes))
        for http_method, path in (
                ('POST', '/api/subscriptions/claude/login/start'),
                ('DELETE', '/api/subscriptions/claude'),
                ('DELETE', '/api/provider-keys/MY_KEY')):
            self.assertEqual(inverted.match(http_method, path, path)[0].handler,
                             self.resolve(http_method, path), path)


class SystemWriteRouteTests(unittest.TestCase):
    """The GitHub + workspace writes that joined the system table."""

    def _resolve(self, path):
        hit = system.ROUTES.match('POST', path, path)
        return hit[0].handler if hit else None

    def test_the_writes(self):
        for path, handler in (
                ('/api/workspace/update', 'handle_workspace_update'),
                ('/api/workspace/restart', 'handle_workspace_restart'),
                ('/api/github/ssh/generate', 'handle_ssh_generate'),
                ('/api/github/config', 'handle_git_config_post'),
                ('/api/github/auth-mode', 'handle_set_auth_mode'),
                ('/api/github/cli/login-url', 'handle_gh_login_instructions'),
                ('/api/github/cli/complete-auth', 'handle_gh_check_auth'),
                ('/api/github/connect/start', 'handle_gh_web_login_start'),
                ('/api/github/connect/poll', 'handle_gh_web_login_poll'),
                ('/api/github/connect/cancel', 'handle_gh_web_login_cancel')):
            self.assertEqual(self._resolve(path), handler, path)

    def test_github_config_is_a_read_and_a_write_on_one_path(self):
        # The reunion this change is for: same path, two verbs, one module.
        self.assertEqual(
            system.ROUTES.match('GET', '/api/github/config',
                                '/api/github/config')[0].handler,
            'send_git_config')
        self.assertEqual(self._resolve('/api/github/config'),
                         'handle_git_config_post')

    def test_the_write_routes_are_normalized_path(self):
        # Unlike the probes and VNC routes in this same table, these carry no
        # raw_path flag — the SPA prefixes them with /oauth.
        for path in ('/api/workspace/update', '/api/github/config'):
            self.assertIsNotNone(
                system.ROUTES.match('POST', path, '/oauth' + path), path)

    def test_every_write_route_names_a_real_handler_method(self):
        for route in system.ROUTES.routes:
            self.assertTrue(hasattr(server.BrowserHandler, route.handler),
                            f'{route} names a method BrowserHandler lacks')


class SettingsEndpointBehaviourTests(DomainEndpointTests, EndpointTestCase):
    """Status codes for both tables' new routes, over a real server."""

    oauth_reachable = (('GET', '/api/provider-keys'),
                       ('POST', '/api/mcp-servers'),
                       ('POST', '/api/github/config'))
    unmatched_shapes = (('GET', '/api/provider-keys/'),
                        ('DELETE', '/api/provider-keys/lower'),
                        ('POST', '/api/subscriptions/claude/login/bogus'))

    def test_the_settings_routes_are_auth_gated(self):
        # 401 rather than 404 is also the proof the route matched at all.
        for path in ('/api/provider-keys', '/api/mcp-servers',
                     '/api/subscriptions'):
            self.assertEqual(self.get(path)[0], 401, path)
        for path in ('/api/provider-keys', '/api/mcp-servers',
                     '/api/subscriptions/claude/login/start',
                     '/api/subscriptions/claude/login/code',
                     '/api/subscriptions/claude/login/poll',
                     '/api/subscriptions/claude/login/cancel'):
            self.assertEqual(self.post(path)[0], 401, path)
        for path in ('/api/provider-keys/MY_KEY', '/api/mcp-servers/srv',
                     '/api/subscriptions/claude'):
            self.assertEqual(self.request(path, method='DELETE')[0], 401, path)

    def test_the_github_and_workspace_writes_are_auth_gated(self):
        for path in ('/api/workspace/update', '/api/workspace/restart',
                     '/api/github/ssh/generate', '/api/github/config',
                     '/api/github/auth-mode', '/api/github/cli/login-url',
                     '/api/github/cli/complete-auth',
                     '/api/github/connect/start', '/api/github/connect/poll',
                     '/api/github/connect/cancel'):
            self.assertEqual(self.post(path)[0], 401, path)


if __name__ == '__main__':
    unittest.main()
