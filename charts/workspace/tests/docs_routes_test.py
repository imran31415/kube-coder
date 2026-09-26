"""Tests for the docs route table that replaced do_GET's elif branches (#100).

Two layers, mirroring tests/routing_test.py:
  * The table — the ordering hazard `/api/docs/search` vs the page-id
    pattern, and which route reads the query string, asserted directly with
    no HTTP request involved.
  * End-to-end over a real server — a 401 proves the route matched at all,
    and the paths that never matched still 404.

Content behaviour (manifest shape, search ranking, the 404 for an unknown
page id) is covered by tests/docs_api_test.py and is unchanged by the split.

Run with:
    cd charts/workspace && python3 -m unittest tests.docs_routes_test
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402
from handlers import docs  # noqa: E402
from tests.http_harness import EndpointTestCase  # noqa: E402


class _Recorder:
    """Stand-in for a BrowserHandler: records the arguments it was called
    with, which is how the `query=True` column gets asserted."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *args: self.calls.append((name, args))


class DocsRouteOrderTests(unittest.TestCase):

    def _hit(self, path, raw=None):
        return docs.ROUTES.match('GET', path, raw if raw is not None else path)

    def _handler_for(self, path, raw=None):
        hit = self._hit(path, raw)
        return hit[0].handler if hit else None

    def test_search_is_not_read_as_a_page_id(self):
        # The hazard: `search` matches the page-id pattern, so the search
        # route only works while it stays registered first.
        self.assertEqual(self._handler_for('/api/docs/search'),
                         'handle_docs_search')

    def test_manifest_and_page_routes(self):
        self.assertEqual(self._handler_for('/api/docs'), 'handle_docs_manifest')
        self.assertEqual(self._handler_for('/api/docs/tasks-concepts'),
                         'handle_docs_page')

    def test_page_id_is_passed_as_a_capture_group(self):
        _, args = self._hit('/api/docs/tasks-concepts')
        self.assertEqual(args, ('tasks-concepts',))

    def test_paths_outside_the_domain_do_not_match(self):
        for path in ('/api/docs/', '/api/docs/a/b', '/api/docs/bad id',
                     '/api/docsx'):
            self.assertIsNone(self._handler_for(path), path)

    def test_every_route_matches_the_normalized_path(self):
        # The SPA prefixes its calls with /oauth; none of these are raw-only.
        for path in ('/api/docs', '/api/docs/search', '/api/docs/tasks-api'):
            self.assertIsNotNone(
                self._handler_for(path, raw='/oauth' + path), path)

    def test_only_search_reads_the_query_string(self):
        by_handler = {r.handler: r.query for r in docs.ROUTES.routes}
        self.assertEqual(by_handler, {'handle_docs_manifest': False,
                                      'handle_docs_search': True,
                                      'handle_docs_page': False})

    def test_search_is_handed_the_parsed_query(self):
        # do_GET used to parse this inline and pass it positionally.
        rec = _Recorder()
        self.assertTrue(docs.ROUTES.dispatch(
            rec, 'GET', '/api/docs/search', '/api/docs/search?q=needle&limit=5'))
        self.assertEqual(
            rec.calls,
            [('handle_docs_search', ({'q': ['needle'], 'limit': ['5']},))])

    def test_every_route_names_a_real_handler_method(self):
        for route in docs.ROUTES.routes:
            self.assertTrue(hasattr(server.BrowserHandler, route.handler),
                            f'{route} names a method BrowserHandler lacks')


class DocsEndpointBehaviourTests(EndpointTestCase):
    """Status codes for the migrated routes, over a real server."""

    def test_the_whole_domain_is_auth_gated(self):
        # 401 rather than 404 is also the proof the route matched at all.
        for path in ('/api/docs', '/api/docs/search', '/api/docs/search?q=x',
                     '/api/docs/tasks-concepts'):
            self.assertEqual(self.get(path)[0], 401, path)

    def test_reachable_under_the_oauth_prefix(self):
        for path in ('/api/docs', '/api/docs/search?q=x',
                     '/api/docs/tasks-concepts'):
            self.assertEqual(self.get('/oauth' + path)[0], 401, path)

    def test_unmatched_shapes_stay_404(self):
        for path in ('/api/docs/', '/api/docs/a/b', '/api/docsx'):
            self.assertEqual(self.get(path)[0], 404, path)


if __name__ == '__main__':
    unittest.main()
