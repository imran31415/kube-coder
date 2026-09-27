"""Tests for the projects / feed / desktop route tables (#100).

Three small CRUD surfaces lifted in one change, so they share a suite. All
three were contiguous on every verb, so the tables are straight lifts and
these tests are mostly the generic ones plus each domain's own facts.

The one thing that is not a pure move: `GET /api/desktop/{id}` had no handler
method at all — do_GET carried its body inline in the dispatch branch. It is
`handle_desktop_get` now, and the test below pins that it still answers
without an auth gate, exactly as the inline version did.

Handler behaviour is covered by tests/feed_test.py, tests/push_notify_test.py,
tests/projects_test.py and tests/desktop_test.py where those exist; this suite
only asserts the wiring.

Run with:
    cd charts/workspace && python3 -m unittest tests.crud_routes_table_test
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402
from handlers import desktop, feed, projects  # noqa: E402
from handlers.routing import RouteTable  # noqa: E402
from tests.http_harness import (  # noqa: E402
    DomainEndpointTests, DomainRouteTests, EndpointTestCase, args_for,
    dispatch_to_mock,
)


def _order_is_presentational(test, table, samples):
    """Every one of `samples` resolves the same against a reversed table."""
    inverted = RouteTable()
    inverted.routes.extend(reversed(table.routes))
    for http_method, path in samples:
        expected = table.match(http_method, path, path)[0].handler
        test.assertEqual(inverted.match(http_method, path, path)[0].handler,
                         expected, f'{http_method} {path}')


class ProjectRouteTests(DomainRouteTests, unittest.TestCase):

    table = projects.ROUTES
    foreign_paths = (('GET', '/api/projects/'), ('GET', '/api/projectsx'),
                     ('GET', '/api/projects/p1/brief/extra'),
                     ('GET', '/api/projects/Upper'))
    oauth_samples = (('GET', '/api/projects/p1'), ('POST', '/api/projects'),
                     ('PUT', '/api/projects/p1'), ('DELETE', '/api/projects/p1'))
    wrong_verb_samples = (('DELETE', '/api/projects'),
                          ('GET', '/api/projects/_discover'),
                          ('PUT', '/api/projects'))

    def test_the_routes(self):
        for http_method, path, handler in (
                ('GET', '/api/projects', 'handle_project_list'),
                ('GET', '/api/projects/p1', 'handle_project_get'),
                ('GET', '/api/projects/p1/brief', 'handle_project_brief'),
                ('POST', '/api/projects', 'handle_project_create'),
                ('POST', '/api/projects/_discover', 'handle_project_discover'),
                ('PUT', '/api/projects/p1', 'handle_project_update'),
                ('DELETE', '/api/projects/p1', 'handle_project_delete')):
            self.assertEqual(self.resolve(http_method, path), handler, path)

    def test_discover_is_not_a_project_id(self):
        # `_discover` has an underscore, which the slug charset excludes — so
        # it cannot be read as a project even though it sits under the same
        # prefix. On GET there is no such route, so it simply does not match.
        self.assertEqual(self.resolve('POST', '/api/projects/_discover'),
                         'handle_project_discover')
        self.assertIsNone(self.resolve('GET', '/api/projects/_discover'))

    def test_the_id_lands_on_the_attribute_each_handler_reads(self):
        for http_method, path in (('GET', '/api/projects/p1'),
                                  ('GET', '/api/projects/p1/brief'),
                                  ('PUT', '/api/projects/p1'),
                                  ('DELETE', '/api/projects/p1')):
            self.assertEqual(
                dispatch_to_mock(projects.ROUTES, http_method, path)._project_id,
                'p1', path)

    def test_order_is_presentational(self):
        _order_is_presentational(self, projects.ROUTES, (
            ('GET', '/api/projects/p1/brief'), ('GET', '/api/projects/p1')))


class FeedRouteTests(DomainRouteTests, unittest.TestCase):

    table = feed.ROUTES
    foreign_paths = (('GET', '/api/feed/'), ('GET', '/api/feedx'),
                     ('POST', '/api/feed/notanid/read'),
                     ('POST', '/api/push'))
    oauth_samples = (('GET', '/api/feed'), ('POST', '/api/feed'),
                     ('POST', '/api/push/register'))
    wrong_verb_samples = (('DELETE', '/api/feed'),
                          ('GET', '/api/push/register'),
                          ('GET', '/api/feed/fd_1/read'))

    def test_the_routes(self):
        for http_method, path, handler in (
                ('GET', '/api/feed', 'handle_feed_list'),
                ('GET', '/api/feed/unread_count', 'handle_feed_unread_count'),
                ('POST', '/api/feed', 'handle_feed_create'),
                ('POST', '/api/feed/fd_1/read', 'handle_feed_read'),
                ('POST', '/api/feed/fd_1/dismiss', 'handle_feed_dismiss'),
                ('POST', '/api/push/register', 'handle_push_register'),
                ('POST', '/api/push/unregister', 'handle_push_unregister')):
            self.assertEqual(self.resolve(http_method, path), handler, path)

    def test_an_item_id_must_carry_its_prefix(self):
        # The `fd_` prefix is what keeps these from matching anything else
        # under /api/feed/ — including /unread_count.
        self.assertEqual(
            args_for(feed.ROUTES, 'POST', '/api/feed/fd_abc/read'), ('fd_abc',))
        self.assertIsNone(self.resolve('POST', '/api/feed/abc/read'))
        self.assertIsNone(self.resolve('POST', '/api/feed/unread_count/read'))

    def test_order_is_presentational(self):
        _order_is_presentational(self, feed.ROUTES, (
            ('GET', '/api/feed/unread_count'), ('GET', '/api/feed'),
            ('POST', '/api/feed/fd_1/read')))


class DesktopRouteTests(DomainRouteTests, unittest.TestCase):

    table = desktop.ROUTES
    foreign_paths = (('GET', '/api/desktop/'), ('GET', '/api/desktopx'),
                     ('GET', '/api/desktop/UPPER'),
                     ('POST', '/api/desktop/d1/stop'))
    oauth_samples = (('GET', '/api/desktop/d1'), ('POST', '/api/desktop'),
                     ('DELETE', '/api/desktop/d1'))
    wrong_verb_samples = (('PUT', '/api/desktop/d1'),
                          ('GET', '/api/desktop/_reorder'),
                          ('DELETE', '/api/desktop'))

    def test_the_routes(self):
        for http_method, path, handler in (
                ('GET', '/api/desktop', 'handle_desktop_list'),
                ('GET', '/api/desktop/d1', 'handle_desktop_get'),
                ('POST', '/api/desktop', 'handle_desktop_create'),
                ('POST', '/api/desktop/_reorder', 'handle_desktop_reorder'),
                ('POST', '/api/desktop/d1/launch', 'handle_desktop_launch'),
                ('POST', '/api/desktop/d1', 'handle_desktop_update'),
                ('DELETE', '/api/desktop/d1', 'handle_desktop_delete')):
            self.assertEqual(self.resolve(http_method, path), handler, path)

    def test_the_update_is_a_post_not_a_put(self):
        # Preserved, not fixed: the SPA calls it this way. Changing the verb
        # would be an API break dressed up as a refactor.
        self.assertEqual(self.resolve('POST', '/api/desktop/d1'),
                         'handle_desktop_update')
        self.assertIsNone(self.resolve('PUT', '/api/desktop/d1'))

    def test_reorder_is_not_read_as_an_item_id(self):
        # `_reorder` has an underscore; the item charset is [a-z0-9]+.
        self.assertEqual(self.resolve('POST', '/api/desktop/_reorder'),
                         'handle_desktop_reorder')

    def test_order_is_presentational(self):
        _order_is_presentational(self, desktop.ROUTES, (
            ('POST', '/api/desktop/_reorder'), ('POST', '/api/desktop/d1'),
            ('POST', '/api/desktop/d1/launch')))


class CrudEndpointBehaviourTests(DomainEndpointTests, EndpointTestCase):
    """Status codes for all three domains, over a real server."""

    oauth_reachable = (('GET', '/api/projects'), ('GET', '/api/feed'),
                       ('POST', '/api/desktop'))
    unmatched_shapes = (('GET', '/api/projects/'), ('GET', '/api/feedx'),
                        ('POST', '/api/desktop/d1/stop'))

    def test_the_session_authed_routes_are_gated(self):
        # 401 rather than 404 is also the proof the route matched at all.
        for path in ('/api/projects', '/api/projects/p1',
                     '/api/projects/p1/brief', '/api/feed',
                     '/api/feed/unread_count', '/api/desktop'):
            self.assertEqual(self.get(path)[0], 401, path)
        for path in ('/api/projects', '/api/projects/_discover', '/api/feed',
                     '/api/feed/fd_1/read', '/api/push/register',
                     '/api/desktop', '/api/desktop/_reorder',
                     '/api/desktop/d1/launch', '/api/desktop/d1'):
            self.assertEqual(self.post(path)[0], 401, path)
        for http_method, path in (('PUT', '/api/projects/p1'),
                                  ('DELETE', '/api/projects/p1'),
                                  ('DELETE', '/api/desktop/d1')):
            self.assertEqual(self.request(path, method=http_method)[0], 401,
                             f'{http_method} {path}')

    def test_the_desktop_item_read_has_no_auth_gate(self):
        # Captured, not changed: this was an inline dispatch branch with no
        # check_claude_auth call, and lifting it into a named handler did not
        # add one. It answers its own 404 for an unknown item rather than the
        # 401 every neighbouring route gives.
        status, body = self.get('/api/desktop/nosuchitem')
        self.assertEqual(status, 404)
        self.assertEqual(body, b'{"error": "item not found"}')


if __name__ == '__main__':
    unittest.main()
