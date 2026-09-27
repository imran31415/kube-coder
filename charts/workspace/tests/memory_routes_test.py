"""Tests for the memory route table that replaced the elif branches (#100).

The largest migrated domain — sixteen routes across three verbs — and the one
#733's inventory flagged for its `{ns}/{key}` sub-resource family. Two layers,
mirroring tests/routing_test.py:
  * The table — every route resolves to its own handler, the sub-resource
    family is shown to be safe *by construction* rather than by order, and
    the two dispatch-site conversions the adapters carry are asserted.
  * End-to-end over a real server — a 401 proves the route matched at all.

These endpoints had no HTTP-level suite before this PR; the manager they call
is covered by tests/memory_test.py and friends.

Run with:
    cd charts/workspace && python3 -m unittest tests.memory_routes_test
"""

import functools
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402
from handlers import memory  # noqa: E402
from tests.http_harness import (  # noqa: E402
    DomainEndpointTests, DomainRouteTests, EndpointTestCase, args_for,
)


class MemoryRouteOrderTests(DomainRouteTests, unittest.TestCase):

    table = memory.ROUTES
    foreign_paths = (('GET', '/api/memory/'), ('GET', '/api/memory/a/b/c/d'),
                     ('GET', '/api/memory/a/b/unknown'), ('GET', '/api/memoryx'))
    oauth_samples = (('GET', '/api/memory'), ('POST', '/api/memory/_purge'),
                     ('DELETE', '/api/memory/ns/key'))
    wrong_verb_samples = (('GET', '/api/memory/_purge'),
                          ('DELETE', '/api/memory'),
                          ('POST', '/api/memory/ns/key'),
                          ('GET', '/api/memory/ns/key/relations/1'))
    query_handlers = {'handle_memory_list', 'route_memory_neighbors'}

    def test_the_collection_reads(self):
        self.assertEqual(self.resolve('GET', '/api/memory'),
                         'handle_memory_list')
        self.assertEqual(self.resolve('GET', '/api/memory/stats'),
                         'handle_memory_stats')
        self.assertEqual(self.resolve('GET', '/api/memory/export'),
                         'handle_memory_export')

    def test_the_sub_resource_family(self):
        for suffix, handler in (('history', 'handle_memory_history'),
                                ('refs', 'handle_memory_refs'),
                                ('relations', 'handle_memory_relations'),
                                ('neighbors', 'route_memory_neighbors')):
            path = f'/api/memory/user.prefs/editor/{suffix}'
            self.assertEqual(self.resolve('GET', path), handler, path)
        self.assertEqual(self.resolve('GET', '/api/memory/user.prefs/editor'),
                         'handle_memory_get')

    def test_the_bare_read_cannot_swallow_a_sub_resource(self):
        # The hazard #733 inventoried, and why it is presentational: a key is
        # slash-free and every pattern is anchored, so the bare {ns}/{key}
        # route does not match a three-segment path even when it is tried
        # first. The registration order is documentation, not dispatch.
        from handlers.routing import RouteTable
        inverted = RouteTable()
        for route in reversed(memory.ROUTES.routes):
            inverted.routes.append(route)
        for suffix in ('history', 'refs', 'relations', 'neighbors'):
            path = f'/api/memory/ns/key/{suffix}'
            self.assertEqual(
                inverted.match('GET', path, path)[0].handler,
                self.resolve('GET', path), path)

    def test_stats_and_export_are_one_segment_not_a_ns_key_pair(self):
        for path in ('/api/memory/stats', '/api/memory/export'):
            self.assertEqual(args_for(memory.ROUTES, 'GET', path), (), path)

    def test_the_writes(self):
        for suffix, handler in (('', 'handle_memory_upsert'),
                                ('/_consolidate', 'handle_memory_consolidate'),
                                ('/_sync_claude', 'handle_memory_sync_claude'),
                                ('/_import', 'handle_memory_import'),
                                ('/_purge', 'handle_memory_purge')):
            path = '/api/memory' + suffix
            self.assertEqual(self.resolve('POST', path), handler, path)
        self.assertEqual(
            self.resolve('POST', '/api/memory/ns/key/relations'),
            'handle_memory_link')

    def test_the_deletes(self):
        self.assertEqual(self.resolve('DELETE', '/api/memory/ns/key'),
                         'handle_memory_delete')
        self.assertEqual(
            self.resolve('DELETE', '/api/memory/ns/key/relations/42'),
            'route_memory_unlink')
        # The relation id is digits-only — a name is not an id.
        self.assertIsNone(
            self.resolve('DELETE', '/api/memory/ns/key/relations/abc'))


class MemoryAdapterTests(unittest.TestCase):
    """The two dispatch-site conversions that used to be inline.

    Dispatched through the table so the wiring is part of the assertion. The
    adapter itself is un-mocked — a `spec=` mock would stub it out and the
    conversion under test would never run — while the handler it delegates to
    stays a mock, which is the call being inspected.
    """

    @staticmethod
    def _handler(adapter):
        h = mock.Mock(spec=server.BrowserHandler)
        setattr(h, adapter,
                functools.partial(getattr(server.BrowserHandler, adapter), h))
        return h

    def test_neighbors_adapter_moves_the_query_to_the_end(self):
        h = self._handler('route_memory_neighbors')
        memory.ROUTES.dispatch(h, 'GET', '/api/memory/ns/key/neighbors',
                               '/api/memory/ns/key/neighbors?depth=2')
        h.handle_memory_neighbors.assert_called_once_with(
            'ns', 'key', {'depth': ['2']})

    def test_unlink_adapter_converts_the_relation_id_to_an_int(self):
        # unlink_by_id matches an INTEGER primary key; the string the regex
        # captures would never match a row.
        h = self._handler('route_memory_unlink')
        memory.ROUTES.dispatch(h, 'DELETE', '/api/memory/ns/key/relations/42',
                               '/api/memory/ns/key/relations/42')
        h.handle_memory_unlink.assert_called_once_with('ns', 'key', 42)


class MemoryEndpointBehaviourTests(DomainEndpointTests, EndpointTestCase):
    """Status codes for the migrated routes, over a real server."""

    oauth_reachable = (('GET', '/api/memory/stats'),
                       ('POST', '/api/memory/_purge'))
    unmatched_shapes = (('GET', '/api/memory/a/b/c/d'),
                        ('POST', '/api/memory/ns/key'),
                        ('DELETE', '/api/memory/ns/key/relations/abc'))

    def test_reads_are_auth_gated(self):
        # 401 rather than 404 is also the proof the route matched at all.
        for path in ('/api/memory', '/api/memory?q=needle', '/api/memory/stats',
                     '/api/memory/export', '/api/memory/ns/key',
                     '/api/memory/ns/key/history', '/api/memory/ns/key/refs',
                     '/api/memory/ns/key/relations',
                     '/api/memory/ns/key/neighbors?depth=2'):
            self.assertEqual(self.get(path)[0], 401, path)

    def test_writes_are_auth_gated(self):
        for path in ('/api/memory', '/api/memory/_consolidate',
                     '/api/memory/_sync_claude', '/api/memory/_import',
                     '/api/memory/_purge', '/api/memory/ns/key/relations'):
            self.assertEqual(self.post(path)[0], 401, path)

    def test_deletes_are_auth_gated(self):
        for path in ('/api/memory/ns/key', '/api/memory/ns/key/relations/42'):
            self.assertEqual(self.request(path, method='DELETE')[0], 401, path)


if __name__ == '__main__':
    unittest.main()
