"""Tests for the route table that replaced do_GET's if/elif chain (#100).

Three layers:
  * `RouteTable` itself — first match wins, exact vs regex patterns, capture
    groups, raw-vs-normalized path selection, name-based handler lookup.
  * The system domain's table — the ordering hazards that used to be nothing
    but a comment asking the next editor not to move the lines, asserted
    directly against the table with no HTTP request involved.
  * End-to-end over a real server — the same status codes the endpoints
    answered before the split, including the ones that depend on a route
    matching the RAW path (`/oauth/livez` is a 404, and has to stay one).

Run with:
    cd charts/workspace && python3 -m unittest tests.routing_test
"""

import http.server
import os
import re
import sys
import threading
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402
from handlers import system  # noqa: E402
from handlers.routing import RouteTable  # noqa: E402


class _Recorder:
    """Stand-in for a BrowserHandler: records which method the table called."""

    def __init__(self):
        self.calls = []

    def alpha(self, *args):
        self.calls.append(('alpha', args))

    def beta(self, *args):
        self.calls.append(('beta', args))


class RouteTableTests(unittest.TestCase):

    def test_exact_pattern_matches_whole_path_only(self):
        t = RouteTable()
        t.add('GET', '/vnc', 'alpha')
        self.assertIsNotNone(t.match('GET', '/vnc', '/vnc'))
        self.assertIsNone(t.match('GET', '/vnc/', '/vnc/'))
        self.assertIsNone(t.match('GET', '/vnc/core.js', '/vnc/core.js'))

    def test_http_method_is_part_of_the_match(self):
        t = RouteTable()
        t.add('GET', '/api/thing', 'alpha')
        self.assertIsNotNone(t.match('GET', '/api/thing', '/api/thing'))
        self.assertIsNone(t.match('DELETE', '/api/thing', '/api/thing'))

    def test_first_registration_wins(self):
        t = RouteTable()
        t.add('GET', '/vnc/', 'alpha')
        t.add('GET', re.compile(r'^/vnc/'), 'beta')
        route, _ = t.match('GET', '/vnc/', '/vnc/')
        self.assertEqual(route.handler, 'alpha')
        route, _ = t.match('GET', '/vnc/core.js', '/vnc/core.js')
        self.assertEqual(route.handler, 'beta')

    def test_registering_the_general_pattern_first_shadows_the_specific_one(self):
        # The failure mode the table exists to make visible: swap the order and
        # the specific route becomes unreachable. Nothing stops you — order is
        # the priority model — but it is now one readable table, not two
        # branches 400 lines apart.
        t = RouteTable()
        t.add('GET', re.compile(r'^/vnc/'), 'beta')
        t.add('GET', '/vnc/', 'alpha')
        route, _ = t.match('GET', '/vnc/', '/vnc/')
        self.assertEqual(route.handler, 'beta')

    def test_regex_groups_are_passed_positionally(self):
        t = RouteTable()
        t.add('GET', re.compile(r'^/api/memory/([^/]+)/([^/]+)$'), 'alpha')
        rec = _Recorder()
        self.assertTrue(t.dispatch(rec, 'GET', '/api/memory/user/name',
                                   '/api/memory/user/name'))
        self.assertEqual(rec.calls, [('alpha', ('user', 'name'))])

    def test_exact_route_calls_handler_with_no_arguments(self):
        t = RouteTable()
        t.add('GET', '/livez', 'alpha')
        rec = _Recorder()
        self.assertTrue(t.dispatch(rec, 'GET', '/livez', '/livez'))
        self.assertEqual(rec.calls, [('alpha', ())])

    def test_raw_path_routes_ignore_the_normalized_path(self):
        t = RouteTable()
        t.add('GET', '/livez', 'alpha', raw_path=True)
        # Normalized says /livez, raw still carries the SPA's /oauth prefix.
        self.assertIsNone(t.match('GET', '/livez', '/oauth/livez'))
        self.assertIsNotNone(t.match('GET', '/oauth/livez', '/livez'))

    def test_normalized_routes_ignore_the_raw_path(self):
        t = RouteTable()
        t.add('GET', '/metrics/prometheus', 'alpha')
        self.assertIsNotNone(
            t.match('GET', '/metrics/prometheus', '/oauth/metrics/prometheus'))

    def test_dispatch_returns_false_when_nothing_matches(self):
        t = RouteTable()
        t.add('GET', '/livez', 'alpha')
        rec = _Recorder()
        self.assertFalse(t.dispatch(rec, 'GET', '/nope', '/nope'))
        self.assertEqual(rec.calls, [])

    def test_handler_is_resolved_on_the_request_at_dispatch_time(self):
        # Routes name a method rather than capturing a function, so overriding
        # or patching it behaves exactly as it did when the chain wrote
        # `self.send_livez()` inline.
        t = RouteTable()
        t.add('GET', '/livez', 'alpha')
        rec = _Recorder()
        rec.alpha = lambda *a: rec.calls.append(('patched', a))
        t.dispatch(rec, 'GET', '/livez', '/livez')
        self.assertEqual(rec.calls, [('patched', ())])

    def test_pattern_must_be_a_path(self):
        t = RouteTable()
        with self.assertRaises(ValueError):
            t.add('GET', 'livez', 'alpha')


class SystemRouteOrderTests(unittest.TestCase):
    """The hazards the old chain carried as comments, as assertions."""

    def _handler_for(self, path, raw=None):
        hit = system.ROUTES.match('GET', path, raw if raw is not None else path)
        return hit[0].handler if hit else None

    def test_bare_vnc_is_the_viewer_and_a_subpath_is_the_proxy(self):
        self.assertEqual(self._handler_for('/vnc'), 'send_vnc_viewer')
        self.assertEqual(self._handler_for('/vnc/'), 'send_vnc_viewer')
        self.assertEqual(self._handler_for('/vnc/core.js'), 'proxy_vnc_request')
        self.assertEqual(self._handler_for('/vnc/app/ui.js'), 'proxy_vnc_request')

    def test_vnc_proxy_is_not_swallowed_by_the_vnc_prefix(self):
        self.assertEqual(self._handler_for('/vnc-proxy'), 'redirect_to_vnc')
        self.assertEqual(self._handler_for('/vnc-proxy/'), 'redirect_to_vnc')

    def test_probes_and_metrics_match_the_raw_path_only(self):
        for path in ('/livez', '/health', '/health/vscode', '/health/terminal',
                     '/health/browser', '/metrics'):
            self.assertIsNotNone(self._handler_for(path), path)
            # Same normalized path, but the request arrived under /oauth.
            self.assertIsNone(self._handler_for(path, raw='/oauth' + path), path)

    def test_spa_prefixed_reads_match_the_normalized_path(self):
        for path in ('/metrics/prometheus', '/api/github/status',
                     '/api/github/config', '/api/workspace/version'):
            self.assertIsNotNone(
                self._handler_for(path, raw='/oauth' + path), path)

    def test_every_route_names_a_real_handler_method(self):
        for route in system.ROUTES.routes:
            self.assertTrue(hasattr(server.BrowserHandler, route.handler),
                            f'{route} names a method BrowserHandler lacks')


class SystemEndpointBehaviourTests(unittest.TestCase):
    """Status codes for the migrated routes, over a real server.

    AUTH_MODE is pinned to oauth2 — the mode where server.py is its own
    enforcer — so the auth-gated members of this domain answer 401 rather than
    doing real work. A 401 also proves the route matched at all, which is what
    separates "reached the handler" from "fell through to the 404".
    """

    @classmethod
    def setUpClass(cls):
        cls._auth_mode_save = server.AUTH_MODE
        server.AUTH_MODE = 'oauth2'
        # Port 0 lets the kernel pick a free one, with no window between
        # probing for it and binding it.
        cls.httpd = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0), server.BrowserHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.AUTH_MODE = cls._auth_mode_save

    def _get(self, path):
        """Return (status, body) whether or not the response was an error."""
        try:
            with urllib.request.urlopen(
                    f'http://127.0.0.1:{self.port}{path}', timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_probes_answer_without_authentication(self):
        self.assertEqual(self._get('/livez'), (200, b'ok'))
        status, body = self._get('/health')
        self.assertEqual(status, 200)
        self.assertIn(b'"services"', body)
        for path, service in (('/health/vscode', b'vscode'),
                              ('/health/terminal', b'terminal'),
                              ('/health/browser', b'browser')):
            status, body = self._get(path)
            self.assertEqual(status, 200, path)
            self.assertIn(service, body)

    def test_the_rest_of_the_domain_is_auth_gated(self):
        # 401 rather than 404 is also the proof the route matched at all.
        for path in ('/metrics', '/metrics/prometheus', '/api/github/status',
                     '/api/github/config', '/api/workspace/version',
                     '/vnc', '/vnc/', '/vnc-proxy', '/vnc/core.js'):
            self.assertEqual(self._get(path)[0], 401, path)

    def test_normalized_routes_are_reachable_under_the_oauth_prefix(self):
        # The SPA and the oauth2 ingress prefix every call with /oauth.
        for path in ('/metrics/prometheus', '/api/github/status',
                     '/api/github/config', '/api/workspace/version'):
            self.assertEqual(self._get('/oauth' + path)[0], 401, path)

    def test_raw_only_routes_stay_404_under_the_oauth_prefix(self):
        # Pre-existing behaviour, captured rather than changed: these have
        # always been compared against self.path, so the prefixed form falls
        # through to the SPA history check and 404s (the prefixes are in
        # NON_SPA_PREFIXES). The kubelet and in-pod curls use the bare form.
        for path in ('/oauth/livez', '/oauth/health', '/oauth/metrics',
                     '/oauth/vnc', '/livez?probe=1'):
            self.assertEqual(self._get(path)[0], 404, path)


if __name__ == '__main__':
    unittest.main()
