"""Tests for the triggers route table that replaced the elif branches (#100).

Webhooks, crons and page-watches — twenty-one routes across three verbs, and
the domain #733 flagged for the one ordering hazard explicitly commented in
the source. Two layers, mirroring tests/routing_test.py:
  * The table — every route resolves to its own handler, the `{id}/…` family
    is shown safe by construction rather than by order, and the `sets=`
    column is asserted to put the right capture group on the right attribute.
  * End-to-end over a real server — a 401 proves a dashboard route matched,
    and the three endpoints that are NOT dashboard-authed are checked to be
    reachable without a session (they answer on their own terms, not 401).

Handler behaviour is covered by tests/trigger_runs_test.py,
tests/page_watch_api_test.py and tests/server_test.py, all unchanged.

Run with:
    cd charts/workspace && python3 -m unittest tests.triggers_routes_test
"""

import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402
from handlers import triggers  # noqa: E402
from handlers.routing import RouteTable  # noqa: E402
from tests.http_harness import (  # noqa: E402
    DomainEndpointTests, DomainRouteTests, EndpointTestCase, dispatch_to_mock)


class TriggerRouteOrderTests(DomainRouteTests, unittest.TestCase):

    table = triggers.ROUTES
    foreign_paths = (('GET', '/api/webhooks/'), ('GET', '/api/crons/c1/runs/1'),
                     ('GET', '/api/triggers'), ('GET', '/api/webhooksx'))
    oauth_samples = (('GET', '/api/webhooks'), ('POST', '/api/crons/c1/suspend'),
                     ('DELETE', '/api/page-watches/p1'))
    wrong_verb_samples = (('DELETE', '/api/webhooks'),
                          ('GET', '/api/triggers/cron-fire/c1'),
                          ('POST', '/api/crons/c1/runs'))

    def test_the_reads(self):
        for base, kind in (('webhooks', 'webhook'), ('crons', 'cron'),
                           ('page-watches', 'page_watch')):
            self.assertEqual(self.resolve('GET', f'/api/{base}'),
                             f'handle_{kind}_list')
            self.assertEqual(self.resolve('GET', f'/api/{base}/t1'),
                             f'handle_{kind}_get')
            self.assertEqual(self.resolve('GET', f'/api/{base}/t1/runs'),
                             f'handle_{kind}_runs')

    def test_the_creates_and_deletes(self):
        for base, kind in (('webhooks', 'webhook'), ('crons', 'cron'),
                           ('page-watches', 'page_watch')):
            self.assertEqual(self.resolve('POST', f'/api/{base}'),
                             f'handle_{kind}_create')
            self.assertEqual(self.resolve('DELETE', f'/api/{base}/t1'),
                             f'handle_{kind}_delete')

    def test_webhook_test_is_not_read_as_the_inbound_receiver(self):
        # THE hazard of this domain. /test is dashboard-authed; the bare
        # route is the HMAC-authed receiver. They must not swap.
        self.assertEqual(self.resolve('POST', '/api/webhooks/wh1/test'),
                         'handle_webhook_test')
        self.assertEqual(self.resolve('POST', '/api/webhooks/wh1'),
                         'handle_webhook_receive')

    def test_the_specific_routes_are_safe_by_construction_not_by_order(self):
        # Every id charset excludes '/' and every pattern is anchored, so a
        # `{id}/runs` or `{id}/test` path cannot be read as an `{id}` even
        # when the bare route is tried first. Resolve against a reversed copy
        # of the table and require the same answers.
        inverted = RouteTable()
        inverted.routes.extend(reversed(triggers.ROUTES.routes))
        for http_method, path in (
                ('GET', '/api/webhooks/wh1/runs'),
                ('GET', '/api/crons/c1/runs'),
                ('GET', '/api/page-watches/p1/runs'),
                ('POST', '/api/webhooks/wh1/test'),
                ('POST', '/api/crons/c1/suspend'),
                ('POST', '/api/page-watches/p1/check')):
            self.assertEqual(inverted.match(http_method, path, path)[0].handler,
                             self.resolve(http_method, path), path)

    def test_the_cron_actions_are_an_allowlist(self):
        for action in ('suspend', 'resume', 'run', 'rotate-token'):
            self.assertEqual(self.resolve('POST', f'/api/crons/c1/{action}'),
                             'handle_cron_action', action)
        self.assertIsNone(self.resolve('POST', '/api/crons/c1/delete'))

    def test_the_page_watch_actions_are_an_allowlist(self):
        for action in ('suspend', 'resume', 'check'):
            self.assertEqual(
                self.resolve('POST', f'/api/page-watches/p1/{action}'),
                'handle_page_watch_action', action)
        self.assertIsNone(self.resolve('POST', '/api/page-watches/p1/run'))

    def test_the_cronjob_receivers(self):
        self.assertEqual(self.resolve('POST', '/api/triggers/cron-fire/c1'),
                         'handle_cron_fire')
        self.assertEqual(
            self.resolve('POST', '/api/triggers/page-watch-check/p1'),
            'handle_page_watch_check')

    def test_webhook_ids_are_wider_than_cron_and_page_watch_slugs(self):
        # Preserved verbatim: webhook ids allow upper case and underscores.
        self.assertEqual(self.resolve('GET', '/api/webhooks/WH_1'),
                         'handle_webhook_get')
        self.assertIsNone(self.resolve('GET', '/api/crons/C_1'))
        self.assertIsNone(self.resolve('GET', '/api/page-watches/P_1'))


class TriggerCaptureAttributeTests(unittest.TestCase):
    """`sets=`: these handlers read their parameters off the request, and the
    chain assigned them at the dispatch site."""

    def test_the_id_lands_on_the_attribute_each_handler_reads(self):
        for http_method, path, attr, value in (
                ('GET', '/api/webhooks/wh1', '_webhook_id', 'wh1'),
                ('GET', '/api/crons/c1/runs', '_cron_id', 'c1'),
                ('DELETE', '/api/page-watches/p1', '_page_watch_id', 'p1'),
                ('POST', '/api/triggers/cron-fire/c2', '_cron_id', 'c2')):
            h = dispatch_to_mock(triggers.ROUTES, http_method, path)
            self.assertEqual(getattr(h, attr), value, path)

    def test_an_action_route_sets_both_id_and_action(self):
        h = dispatch_to_mock(triggers.ROUTES, 'POST', '/api/crons/c1/rotate-token')
        self.assertEqual((h._cron_id, h._cron_action), ('c1', 'rotate-token'))
        h.handle_cron_action.assert_called_once_with()

        h = dispatch_to_mock(triggers.ROUTES, 'POST', '/api/page-watches/p1/check')
        self.assertEqual((h._page_watch_id, h._page_watch_action),
                         ('p1', 'check'))
        h.handle_page_watch_action.assert_called_once_with()



class TriggerEndpointBehaviourTests(DomainEndpointTests, EndpointTestCase):
    """Status codes for the migrated routes, over a real server."""

    oauth_reachable = (('GET', '/api/webhooks'), ('POST', '/api/crons'))
    unmatched_shapes = (('GET', '/api/webhooks/'),
                        ('POST', '/api/crons/c1/delete'),
                        ('DELETE', '/api/triggers/cron-fire/c1'))

    def test_the_dashboard_routes_are_auth_gated(self):
        # 401 rather than 404 is also the proof the route matched at all.
        for base in ('webhooks', 'crons', 'page-watches'):
            for path in (f'/api/{base}', f'/api/{base}/t1',
                         f'/api/{base}/t1/runs'):
                self.assertEqual(self.get(path)[0], 401, path)
            self.assertEqual(self.post(f'/api/{base}')[0], 401, base)
            self.assertEqual(
                self.request(f'/api/{base}/t1', method='DELETE')[0], 401, base)
        self.assertEqual(self.post('/api/webhooks/wh1/test')[0], 401)
        self.assertEqual(self.post('/api/crons/c1/suspend')[0], 401)
        self.assertEqual(self.post('/api/page-watches/p1/check')[0], 401)

    def test_the_three_unsessioned_endpoints_answer_on_their_own_terms(self):
        # The inbound receiver and the two CronJob receivers authenticate
        # themselves (HMAC over the body / a per-trigger fire_token), so a 401
        # from check_claude_auth is exactly what they must NOT return. For an
        # unknown id they answer their own deliberately-uninformative JSON
        # 404, which is what distinguishes "reached the handler" from
        # do_POST's plain-text fall-through 404.
        for path in ('/api/webhooks/wh1',
                     '/api/triggers/cron-fire/c1',
                     '/api/triggers/page-watch-check/p1'):
            status, body = self.post(path)
            self.assertEqual(status, 404, path)
            self.assertEqual(body, b'{"error": "Not found or unauthorized"}',
                             path)

    def test_a_routing_miss_looks_different_from_those(self):
        # The discriminator the test above relies on: do_POST's fall-through
        # answers plain text, not the handlers' JSON.
        status, body = self.post('/api/webhooks/wh1/nope')
        self.assertEqual(status, 404)
        self.assertTrue(body.startswith(b'API endpoint not found'), body)


if __name__ == '__main__':
    unittest.main()
