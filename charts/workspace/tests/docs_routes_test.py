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
from tests.http_harness import (  # noqa: E402
    DomainEndpointTests, DomainRouteTests, EndpointTestCase, args_for,
)


class _Recorder:
    """Stand-in for a BrowserHandler: records the arguments it was called
    with, which is how the `query=True` column gets asserted."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *args: self.calls.append((name, args))


class DocsRouteOrderTests(DomainRouteTests, unittest.TestCase):

    table = docs.ROUTES
    foreign_paths = (('GET', '/api/docs/'), ('GET', '/api/docs/a/b'),
                     ('GET', '/api/docs/bad id'), ('GET', '/api/docsx'))
    oauth_samples = (('GET', '/api/docs'), ('GET', '/api/docs/search'),
                     ('GET', '/api/docs/tasks-api'))
    wrong_verb_samples = (('POST', '/api/docs'), ('POST', '/api/docs/search'),
                          ('DELETE', '/api/docs/tasks-api'))
    query_handlers = {'handle_docs_search'}

    def test_search_is_not_read_as_a_page_id(self):
        # The hazard: `search` matches the page-id pattern, so the search
        # route only works while it stays registered first.
        self.assertEqual(self.resolve('GET', '/api/docs/search'),
                         'handle_docs_search')

    def test_manifest_and_page_routes(self):
        self.assertEqual(self.resolve('GET', '/api/docs'),
                         'handle_docs_manifest')
        self.assertEqual(self.resolve('GET', '/api/docs/tasks-concepts'),
                         'handle_docs_page')

    def test_page_id_is_passed_as_a_capture_group(self):
        self.assertEqual(args_for(docs.ROUTES, 'GET', '/api/docs/tasks-concepts'),
                         ('tasks-concepts',))

    def test_search_is_handed_the_parsed_query(self):
        # do_GET used to parse this inline and pass it positionally.
        rec = _Recorder()
        self.assertTrue(docs.ROUTES.dispatch(
            rec, 'GET', '/api/docs/search', '/api/docs/search?q=needle&limit=5'))
        self.assertEqual(
            rec.calls,
            [('handle_docs_search', ({'q': ['needle'], 'limit': ['5']},))])


class DocsEndpointBehaviourTests(DomainEndpointTests, EndpointTestCase):
    """Status codes for the migrated routes, over a real server."""

    oauth_reachable = (('GET', '/api/docs'), ('GET', '/api/docs/search?q=x'),
                       ('GET', '/api/docs/tasks-concepts'))
    unmatched_shapes = (('GET', '/api/docs/'), ('GET', '/api/docs/a/b'),
                        ('GET', '/api/docsx'))

    def test_the_whole_domain_is_auth_gated(self):
        # 401 rather than 404 is also the proof the route matched at all.
        for path in ('/api/docs', '/api/docs/search', '/api/docs/search?q=x',
                     '/api/docs/tasks-concepts'):
            self.assertEqual(self.get(path)[0], 401, path)


if __name__ == '__main__':
    unittest.main()
