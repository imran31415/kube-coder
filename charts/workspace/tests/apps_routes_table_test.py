"""Tests for the last three route tables in the series (#100).

`handlers/apps.py`, `handlers/devcontainer.py` and `handlers/workspace.py` —
the routes that were left once every real domain had moved. With these,
BrowserHandler is bootstrap + middleware and nothing else.

Two things here are not pure moves and both are pinned:

  * `handle_mode` — do_GET built `/api/mode`'s JSON inline in its dispatch
    branch. Lifting it gives the table something to name; the response is
    asserted field-for-field so the move cannot have changed it.
  * The browser launchers moved from *after* the app-proxy dispatcher to
    *before* it, because one table cannot straddle a middleware stage. Safe
    only while their paths cannot match the proxy prefix — `hoisted_over_paths`.

What deliberately did NOT move is the proxy itself. `_dispatch_app_proxy` and
friends are middleware, not routes, and the issue's end state is "server.py
reduced to bootstrap + middleware".

Run with:
    cd charts/workspace && python3 -m unittest tests.apps_routes_table_test
"""

import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402
from handlers import apps, devcontainer, workspace  # noqa: E402
from tests.http_harness import (  # noqa: E402
    DomainEndpointTests, DomainRouteTests, EndpointTestCase,
    dispatch_through_adapter,
)


class AppRouteTests(DomainRouteTests, unittest.TestCase):

    table = apps.ROUTES
    foreign_paths = (('GET', '/api/apps/'), ('GET', '/api/appsx'),
                     ('DELETE', '/api/apps/pins/notaport'),
                     ('POST', '/api/launch-safari'))
    oauth_samples = (('GET', '/api/apps'), ('POST', '/api/apps/pins'),
                     ('DELETE', '/api/apps/pins/8080'))
    wrong_verb_samples = (('DELETE', '/api/apps'),
                          ('GET', '/api/apps/pins'),
                          ('GET', '/api/launch-chrome'))
    # The launchers now sit above _dispatch_app_proxy. Nothing here may
    # resolve in this table, or the proxy would have been shadowed.
    hoisted_over_paths = (('POST', '/api/app-proxy/3000/'),
                          ('POST', '/api/app-proxy/8080/foo/bar'),
                          ('GET', '/api/app-proxy/3000/index.html'))
    owned_prefixes = ('/api/apps', '/api/claude/apps', '/api/launch-',
                      '/api/test-', '/api/open-localhost')

    def test_the_routes(self):
        for http_method, path, handler in (
                ('GET', '/api/apps', '_handle_apps_list'),
                ('GET', '/api/claude/apps/session', 'handle_app_session_mint'),
                ('POST', '/api/apps/pins', '_handle_apps_pin_create'),
                ('DELETE', '/api/apps/pins/8080', 'route_apps_pin_delete'),
                ('POST', '/api/open-localhost', 'open_localhost')):
            self.assertEqual(self.resolve(http_method, path), handler, path)

    def test_the_firefox_aliases_point_at_the_chrome_handlers(self):
        # Back-compat, preserved verbatim: two paths, one handler each.
        self.assertEqual(self.resolve('POST', '/api/launch-chrome'),
                         'launch_chrome')
        self.assertEqual(self.resolve('POST', '/api/launch-firefox'),
                         'launch_chrome')
        self.assertEqual(self.resolve('POST', '/api/test-chrome'), 'test_chrome')
        self.assertEqual(self.resolve('POST', '/api/test-firefox'), 'test_chrome')

    def test_the_pin_port_reaches_the_handler_as_an_int(self):
        # The chain did the int() at the dispatch site; the table hands over
        # a string, so an adapter converts it.
        h = dispatch_through_adapter(apps.ROUTES, 'route_apps_pin_delete',
                                     'DELETE', '/api/apps/pins/8080')
        h._handle_apps_pin_delete.assert_called_once_with(8080)

    def test_a_pin_must_be_digits(self):
        self.assertIsNone(self.resolve('DELETE', '/api/apps/pins/http'))
        self.assertIsNone(self.resolve('DELETE', '/api/apps/pins/'))


class DevcontainerRouteTests(DomainRouteTests, unittest.TestCase):

    table = devcontainer.ROUTES
    foreign_paths = (('GET', '/api/devcontainer/'),
                     ('GET', '/api/devcontainerx'),
                     ('POST', '/api/devcontainer/run'))
    oauth_samples = (('GET', '/api/devcontainer'),
                     ('POST', '/api/devcontainer/apply'))
    wrong_verb_samples = (('POST', '/api/devcontainer'),
                          ('GET', '/api/devcontainer/apply'),
                          ('DELETE', '/api/devcontainer'))

    def test_the_routes(self):
        for http_method, path, handler in (
                ('GET', '/api/devcontainer', '_handle_devcontainer_get'),
                ('GET', '/api/devcontainer/scan', '_handle_devcontainer_scan'),
                ('POST', '/api/devcontainer/apply',
                 '_handle_devcontainer_apply'),
                ('POST', '/api/devcontainer/reset',
                 '_handle_devcontainer_reset')):
            self.assertEqual(self.resolve(http_method, path), handler, path)

    def test_apply_is_a_post_only(self):
        # The one route in the server that can execute out of a cloned repo.
        # It must not be reachable by a GET.
        self.assertIsNone(self.resolve('GET', '/api/devcontainer/apply'))


class WorkspaceRouteTests(DomainRouteTests, unittest.TestCase):

    table = workspace.ROUTES
    foreign_paths = (('GET', '/api/missioncontrol'),
                     ('GET', '/api/missioncontrol/cards/bogus:1'),
                     ('GET', '/api/missioncontrol/cards/build:'),
                     ('GET', '/api/modex'))
    oauth_samples = (('GET', '/api/mode'), ('GET', '/api/events'),
                     ('GET', '/api/missioncontrol/queue'))
    wrong_verb_samples = (('POST', '/api/mode'), ('POST', '/api/events'),
                          ('DELETE', '/api/subagents'))

    def test_the_routes(self):
        for path, handler in (
                ('/api/mode', 'handle_mode'),
                ('/api/events', 'handle_events_stream'),
                ('/api/workspace/dirs', 'handle_workspace_dirs'),
                ('/api/subagents', 'handle_subagents_list'),
                ('/api/security/instruction-scan', '_handle_instruction_scan'),
                ('/api/missioncontrol/queue', 'handle_missioncontrol_queue'),
                ('/api/missioncontrol/cards/build:b1',
                 'handle_missioncontrol_card')):
            self.assertEqual(self.resolve('GET', path), handler, path)

    def test_a_card_kind_is_an_allowlist(self):
        for kind in ('build', 'chat', 'subagent'):
            self.assertEqual(
                self.resolve('GET', f'/api/missioncontrol/cards/{kind}:x1'),
                'handle_missioncontrol_card', kind)
        self.assertIsNone(
            self.resolve('GET', '/api/missioncontrol/cards/board:x1'))


class ModeResponseTests(unittest.TestCase):
    """`/api/mode` was an inline dict; this pins the move didn't change it."""

    def test_every_flag_is_reported(self):
        h = mock.Mock(spec=server.BrowserHandler)
        workspace.WorkspaceRoutes.handle_mode(h)
        (payload,), _ = h.send_json.call_args
        self.assertEqual(set(payload), {
            'readOnly', 'authed', 'authMode', 'demoShowAll', 'ctoEnabled',
            'devcontainerEnabled', 'boardEnabled', 'scansEnabled'})
        self.assertEqual(payload['readOnly'], server.READONLY_MODE)
        self.assertEqual(payload['authMode'], server.AUTH_MODE)
        self.assertEqual(payload['authed'], server.AUTH_MODE != 'none')
        self.assertEqual(payload['demoShowAll'], server.DEMO_SHOW_ALL)
        self.assertEqual(payload['boardEnabled'], server._BOARDS_AVAILABLE)
        self.assertEqual(payload['scansEnabled'], server.SCANS_ENABLED)

    def test_the_flags_follow_the_server_module(self):
        # Read through handlers.server, so a patched value is visible — which
        # is what the inline version got for free by reading the global.
        h = mock.Mock(spec=server.BrowserHandler)
        with mock.patch.object(server, 'READONLY_MODE', True), \
             mock.patch.object(server, 'AUTH_MODE', 'none'):
            workspace.WorkspaceRoutes.handle_mode(h)
        (payload,), _ = h.send_json.call_args
        self.assertTrue(payload['readOnly'])
        self.assertFalse(payload['authed'])


class HandlerIsMiddlewareOnlyTests(unittest.TestCase):
    """The acceptance criterion of #100, as a test.

    Every route handler has moved into handlers/; what is left on
    BrowserHandler is dispatch, auth, the readonly gate, prefix-stripping,
    body reading, response helpers, the SPA shell and the app proxy.
    """

    ALLOWED = {
        # verb dispatch
        'do_GET', 'do_POST', 'do_PUT', 'do_PATCH', 'do_DELETE', 'do_HEAD',
        'do_OPTIONS', 'end_headers',
        # auth + gating
        'check_auth', 'check_claude_auth', 'check_oauth_only',
        'check_app_proxy_auth', '_app_session_cookie_value',
        '_consume_bearer_marker', '_readonly_block', '_memory_actor',
        # request/response plumbing
        '_strip_route_prefix', 'read_json_body', 'send_json',
        'send_success_response', 'send_error_response', 'send_client_error',
        # the SPA shell
        'serve_next_spa', '_is_spa_history_path',
        # the app proxy — middleware, not routes
        '_dispatch_app_proxy', '_dispatch_referer_proxy',
        '_dispatch_terminal_proxy', '_proxy_app_request',
        '_proxy_app_websocket', '_rewrite_proxied_html',
        '_rewrite_location_header',
    }

    def test_browserhandler_defines_no_route_handlers(self):
        import ast
        path = os.path.join(os.path.dirname(HERE), 'server.py')
        with open(path) as fh:
            tree = ast.parse(fh.read())
        cls = next(n for n in tree.body
                   if isinstance(n, ast.ClassDef) and n.name == 'BrowserHandler')
        defined = {m.name for m in cls.body if isinstance(m, ast.FunctionDef)}
        self.assertEqual(defined - self.ALLOWED, set(),
                         'a route handler is back in server.py')

    def test_every_domain_table_is_reachable_from_the_handler(self):
        # Each mixin contributes its routes; none is registered but unwired.
        from handlers import (boards, docs, feed, files, gateway, hypervisor,
                              memory, projects, settings, skills, system,
                              tasks, triggers)
        tables = (apps, boards, devcontainer, docs, feed, files, gateway,
                  hypervisor, memory, projects, settings, skills, system,
                  tasks, triggers, workspace)
        total = 0
        for mod in tables:
            for route in mod.ROUTES.routes:
                self.assertTrue(hasattr(server.BrowserHandler, route.handler),
                                f'{mod.__name__}: {route}')
                total += 1
        self.assertGreater(total, 180, 'suspiciously few routes registered')


class FinalEndpointBehaviourTests(DomainEndpointTests, EndpointTestCase):
    """Status codes for the last three domains, over a real server."""

    oauth_reachable = (('GET', '/api/apps'), ('GET', '/api/subagents'),
                       ('POST', '/api/devcontainer/apply'))
    unmatched_shapes = (('GET', '/api/apps/'), ('GET', '/api/modex'),
                        ('POST', '/api/devcontainer/run'))

    def test_mode_answers_without_authentication(self):
        # The one route in these three that must NOT be gated: the public
        # read-only demo fetches it with no auth proxy in front.
        status, body = self.get('/api/mode')
        self.assertEqual(status, 200)
        self.assertIn(b'"authMode"', body)

    def test_everything_else_is_auth_gated(self):
        for path in ('/api/apps', '/api/claude/apps/session', '/api/events',
                     '/api/subagents', '/api/workspace/dirs',
                     '/api/security/instruction-scan',
                     '/api/missioncontrol/queue',
                     '/api/missioncontrol/cards/build:b1',
                     '/api/devcontainer', '/api/devcontainer/scan'):
            self.assertEqual(self.get(path)[0], 401, path)
        for path in ('/api/apps/pins', '/api/launch-chrome',
                     '/api/test-chrome', '/api/open-localhost',
                     '/api/devcontainer/apply', '/api/devcontainer/reset'):
            self.assertEqual(self.post(path)[0], 401, path)
        self.assertEqual(
            self.request('/api/apps/pins/8080', method='DELETE')[0], 401)


if __name__ == '__main__':
    unittest.main()
