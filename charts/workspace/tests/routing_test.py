"""Tests for the route table that replaced do_GET's if/elif chain (#100).

Three layers:
  * `RouteTable` itself — first match wins, exact vs regex patterns, capture
    groups, raw-vs-normalized path selection, the two query-string
    columns, the `sets=` column, name-based handler lookup.
  * The system domain's table — the ordering hazards that used to be nothing
    but a comment asking the next editor not to move the lines, asserted
    directly against the table with no HTTP request involved.
  * End-to-end over a real server — the same status codes the endpoints
    answered before the split, including the ones that depend on a route
    matching the RAW path (`/oauth/livez` is a 404, and has to stay one).

Run with:
    cd charts/workspace && python3 -m unittest tests.routing_test
"""

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402
from handlers import system  # noqa: E402
from handlers.routing import RouteTable  # noqa: E402
from tests.http_harness import (  # noqa: E402
    DomainEndpointTests, DomainRouteTests, EndpointTestCase)


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


class RouteTableQueryTests(unittest.TestCase):
    """The `query=True` column: which handlers are handed the parsed query
    string, which do_GET used to parse inline and pass positionally."""

    def test_only_routes_with_the_column_see_the_query_string(self):
        # Same request shape either way; the column is the whole difference.
        t = RouteTable()
        t.add('GET', '/with', 'alpha', query=True)
        t.add('GET', '/without', 'beta')
        rec = _Recorder()
        t.dispatch(rec, 'GET', '/with', '/with?scope=user&x=1')
        t.dispatch(rec, 'GET', '/without', '/without?scope=user&x=1')
        self.assertEqual(rec.calls,
                         [('alpha', ({'scope': ['user'], 'x': ['1']},)),
                          ('beta', ())])

    def test_the_query_dict_precedes_the_capture_groups(self):
        t = RouteTable()
        t.add('GET', re.compile(r'^/api/docs/([^/]+)$'), 'alpha', query=True)
        rec = _Recorder()
        t.dispatch(rec, 'GET', '/api/docs/search', '/api/docs/search?q=a')
        self.assertEqual(rec.calls, [('alpha', ({'q': ['a']}, 'search'))])

    def test_a_path_with_no_query_string_yields_an_empty_dict(self):
        # Not None — the handlers call .get() on it unconditionally.
        t = RouteTable()
        t.add('GET', '/api/skills', 'alpha', query=True)
        rec = _Recorder()
        t.dispatch(rec, 'GET', '/api/skills', '/api/skills')
        self.assertEqual(rec.calls, [('alpha', ({},))])


class RouteTableStripQueryTests(unittest.TestCase):
    """The `strip_query=True` column: do_GET routes on a path whose query is
    already gone, every other verb routes on one that still has it."""

    def test_a_plain_route_does_not_match_once_a_query_is_attached(self):
        # The bug the column exists to prevent, spelled out.
        t = RouteTable()
        t.add('DELETE', '/api/files', 'alpha')
        self.assertIsNotNone(t.match('DELETE', '/api/files', '/api/files'))
        self.assertIsNone(t.match('DELETE', '/api/files?path=a.txt',
                                  '/api/files?path=a.txt'))

    def test_the_column_matches_the_route_portion_before_the_question_mark(self):
        t = RouteTable()
        t.add('DELETE', '/api/files', 'alpha', strip_query=True)
        for path in ('/api/files', '/api/files?path=a.txt', '/api/files?'):
            self.assertIsNotNone(t.match('DELETE', path, path), path)
        # Still an exact match on the route portion — not a prefix.
        self.assertIsNone(t.match('DELETE', '/api/filesx?a=1',
                                  '/api/filesx?a=1'))

    def test_the_column_applies_to_regex_patterns_too(self):
        t = RouteTable()
        t.add('DELETE', re.compile(r'^/api/worktrees/([^/?]+)$'), 'alpha',
              strip_query=True)
        _, args = t.match('DELETE', '/api/worktrees/repo?force=1',
                          '/api/worktrees/repo?force=1')
        self.assertEqual(args, ('repo',))

    def test_the_handler_still_sees_the_untouched_request_path(self):
        # Stripping is for MATCHING only; handlers re-parse self.path.
        t = RouteTable()
        t.add('DELETE', '/api/files', 'alpha', strip_query=True, query=True)
        rec = _Recorder()
        t.dispatch(rec, 'DELETE', '/api/files?path=a.txt',
                   '/api/files?path=a.txt')
        self.assertEqual(rec.calls, [('alpha', ({'path': ['a.txt']},))])


class RouteTableSetsTests(unittest.TestCase):
    """The `sets=` column: capture groups stashed on the request for the
    handlers that read their parameters off it rather than as arguments."""

    def test_a_named_group_lands_on_the_request(self):
        t = RouteTable()
        t.add('GET', re.compile(r'^/api/webhooks/([^/]+)$'), 'alpha',
              sets='_webhook_id')
        rec = _Recorder()
        t.dispatch(rec, 'GET', '/api/webhooks/wh1', '/api/webhooks/wh1')
        self.assertEqual(rec._webhook_id, 'wh1')
        self.assertEqual(rec.calls, [('alpha', ())])

    def test_two_names_consume_two_groups_in_order(self):
        t = RouteTable()
        t.add('POST', re.compile(r'^/api/crons/([^/]+)/(suspend|resume)$'),
              'alpha', sets=('_cron_id', '_cron_action'))
        rec = _Recorder()
        t.dispatch(rec, 'POST', '/api/crons/c1/resume', '/api/crons/c1/resume')
        self.assertEqual((rec._cron_id, rec._cron_action), ('c1', 'resume'))
        self.assertEqual(rec.calls, [('alpha', ())])

    def test_groups_past_the_named_ones_are_still_positional(self):
        t = RouteTable()
        t.add('GET', re.compile(r'^/a/([^/]+)/([^/]+)$'), 'alpha', sets='_id')
        rec = _Recorder()
        t.dispatch(rec, 'GET', '/a/one/two', '/a/one/two')
        self.assertEqual(rec._id, 'one')
        self.assertEqual(rec.calls, [('alpha', ('two',))])

    def test_a_route_without_the_column_sets_nothing(self):
        t = RouteTable()
        t.add('GET', re.compile(r'^/api/webhooks/([^/]+)$'), 'alpha')
        rec = _Recorder()
        t.dispatch(rec, 'GET', '/api/webhooks/wh1', '/api/webhooks/wh1')
        self.assertFalse(hasattr(rec, '_webhook_id'))
        self.assertEqual(rec.calls, [('alpha', ('wh1',))])


class SystemRouteOrderTests(DomainRouteTests, unittest.TestCase):
    """The hazards the old chain carried as comments, as assertions."""

    table = system.ROUTES
    foreign_paths = (('GET', '/health/'), ('GET', '/livezx'),
                     ('GET', '/metrics/'), ('GET', '/api/github'),
                     ('GET', '/api/workspace'))
    # Only these four match the normalized path; the probes, /metrics and the
    # VNC routes are raw-only, which the test below asserts instead.
    oauth_samples = (('GET', '/metrics/prometheus'),
                     ('GET', '/api/github/status'),
                     ('GET', '/api/github/config'),
                     ('GET', '/api/workspace/version'))
    wrong_verb_samples = (('POST', '/livez'), ('POST', '/metrics'),
                          ('DELETE', '/api/github/config'),
                          ('POST', '/vnc'))

    def _handler_for(self, path, raw=None):
        return self.resolve('GET', path, raw)

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


class SystemEndpointBehaviourTests(DomainEndpointTests, EndpointTestCase):
    """Status codes for the migrated routes, over a real server.

    EndpointTestCase pins AUTH_MODE to oauth2 — the mode where server.py is
    its own enforcer — so the auth-gated members of this domain answer 401
    rather than doing real work. A 401 also proves the route matched at all,
    which is what separates "reached the handler" from "fell through to the
    404".
    """

    oauth_reachable = (('GET', '/metrics/prometheus'),
                       ('GET', '/api/github/status'),
                       ('GET', '/api/github/config'),
                       ('GET', '/api/workspace/version'))
    # Pre-existing behaviour, captured rather than changed: the raw-only
    # routes have always been compared against self.path, so the prefixed
    # form falls through to the SPA history check and 404s (their prefixes
    # are in NON_SPA_PREFIXES). The kubelet and in-pod curls use the bare
    # form. Same for a query string on one of them.
    unmatched_shapes = (('GET', '/oauth/livez'), ('GET', '/oauth/health'),
                        ('GET', '/oauth/metrics'), ('GET', '/oauth/vnc'),
                        ('GET', '/livez?probe=1'))

    def test_probes_answer_without_authentication(self):
        self.assertEqual(self.get('/livez'), (200, b'ok'))
        status, body = self.get('/health')
        self.assertEqual(status, 200)
        self.assertIn(b'"services"', body)
        for path, service in (('/health/vscode', b'vscode'),
                              ('/health/terminal', b'terminal'),
                              ('/health/browser', b'browser')):
            status, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertIn(service, body)

    def test_the_rest_of_the_domain_is_auth_gated(self):
        # 401 rather than 404 is also the proof the route matched at all.
        for path in ('/metrics', '/metrics/prometheus', '/api/github/status',
                     '/api/github/config', '/api/workspace/version',
                     '/vnc', '/vnc/', '/vnc-proxy', '/vnc/core.js'):
            self.assertEqual(self.get(path)[0], 401, path)


if __name__ == '__main__':
    unittest.main()
