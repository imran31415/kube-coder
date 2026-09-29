"""Tests for the security-scan route table (#726).

Eleven routes across three verbs. The thing worth asserting by hand is the
claim the module makes about itself: that its id pattern is tight enough that
this table has NO ordering hazard, unlike most domains in the package. If a
later change loosens the pattern, `targets` and `connection` start resolving as
scan ids and the failure is silent — a request for the target list reading as
"fetch the scan called targets", which 404s rather than erroring.

Run with:
    cd charts/workspace && python3 -m unittest tests.scans_routes_table_test
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import scans  # noqa: E402
import server  # noqa: E402
from handlers import scans as scan_handlers  # noqa: E402
from tests.http_harness import (  # noqa: E402
    DomainRouteTests, args_for,
)

#: A syntactically valid scan id, as `scans.new_scan_id` makes them.
SCAN = 'scn_abc123def456'


class ScanRouteOrderTests(DomainRouteTests, unittest.TestCase):

    table = scan_handlers.ROUTES
    foreign_paths = (
        ('GET', '/api/scans/'),
        ('GET', '/api/scansx'),
        ('GET', f'/api/scans/{SCAN}/extra'),
        # Ids that do not match the shape are not scans, so they do not
        # resolve — rather than reaching a handler that must re-check them.
        ('GET', '/api/scans/not-a-scan'),
        ('GET', '/api/scans/scn_SHOUTING1234'),
        ('GET', '/api/scans/scn_tooshort'),
        ('GET', '/api/scans/../etc/passwd'),
        ('POST', f'/api/scans/{SCAN}/destroy'),
    )
    oauth_samples = (
        ('GET', '/api/scans'),
        ('GET', f'/api/scans/{SCAN}'),
        ('POST', '/api/scans'),
        ('POST', f'/api/scans/{SCAN}/stop'),
        ('DELETE', f'/api/scans/{SCAN}'),
    )
    wrong_verb_samples = (
        ('DELETE', '/api/scans'),
        ('PUT', f'/api/scans/{SCAN}'),
        ('GET', f'/api/scans/{SCAN}/stop'),
        ('POST', '/api/scans/targets'),
    )

    def test_the_reads_resolve(self):
        for path, handler in (
                ('/api/scans', 'handle_scans_list'),
                ('/api/scans/targets', 'handle_scan_targets'),
                ('/api/scans/connection', 'handle_scan_connection_get'),
                (f'/api/scans/{SCAN}', 'handle_scan_get')):
            self.assertEqual(self.resolve('GET', path), handler, path)

    def test_the_writes_resolve(self):
        for path, handler in (
                ('/api/scans', 'handle_scan_create'),
                ('/api/scans/connection', 'handle_scan_connection_set'),
                ('/api/scans/connection/test', 'handle_scan_connection_test'),
                (f'/api/scans/{SCAN}/stop', 'handle_scan_stop'),
                (f'/api/scans/{SCAN}/findings/v1/disposition',
                 'handle_scan_disposition')):
            self.assertEqual(self.resolve('POST', path), handler, path)

    def test_the_deletes_resolve(self):
        self.assertEqual(self.resolve('DELETE', '/api/scans/connection'),
                         'handle_scan_connection_clear')
        self.assertEqual(self.resolve('DELETE', f'/api/scans/{SCAN}'),
                         'handle_scan_delete')

    def test_the_sub_resource_names_cannot_be_read_as_scan_ids(self):
        """The claim the module makes instead of an ordering comment.

        `targets` and `connection` sit at the same position as a scan id. In
        every other domain here that is a hazard managed by registration
        order; here the id pattern simply cannot match a word, so the table is
        order-independent. Asserted because a later loosening of the pattern
        would break it silently.
        """
        for word in ('targets', 'connection'):
            self.assertFalse(scans.valid_scan_id(word))
            self.assertEqual(self.resolve('GET', f'/api/scans/{word}'),
                             {'targets': 'handle_scan_targets',
                              'connection': 'handle_scan_connection_get'}[word])

    def test_reordering_the_table_would_not_change_any_resolution(self):
        """The order-independence claim, checked directly: with the specific
        routes moved to the END, everything still resolves the same way."""
        from handlers.routing import RouteTable
        shuffled = RouteTable()
        originals = list(self.table.routes)
        for route in reversed(originals):
            shuffled.routes.append(route)
        for method, path in (('GET', '/api/scans/targets'),
                             ('GET', '/api/scans/connection'),
                             ('GET', f'/api/scans/{SCAN}'),
                             ('POST', '/api/scans/connection/test')):
            hit = shuffled.match(method, path, path)
            self.assertIsNotNone(hit, path)
            self.assertEqual(hit[0].handler, self.resolve(method, path), path)

    def test_the_scan_id_group_reaches_the_handler(self):
        self.assertEqual(args_for(self.table, 'GET', f'/api/scans/{SCAN}'),
                         (SCAN,))

    def test_the_disposition_route_passes_both_ids(self):
        self.assertEqual(
            args_for(self.table, 'POST',
                     f'/api/scans/{SCAN}/findings/vuln-1/disposition'),
            (SCAN, 'vuln-1'))

    def test_a_finding_id_may_carry_the_punctuation_scanners_use(self):
        """Finding ids come from the scanner, not from us — they are seen in
        colon- and dot-separated forms."""
        for fid in ('v1', 'vuln-1', 'CVE-2024-1234', 'a.b:c_d'):
            self.assertIsNotNone(
                self.resolve('POST',
                             f'/api/scans/{SCAN}/findings/{fid}/disposition'),
                fid)

    def test_every_route_is_registered_against_this_handler_class(self):
        for route in self.table.routes:
            self.assertTrue(hasattr(scan_handlers.ScanRoutes, route.handler),
                            route.handler)

    def test_the_mixin_is_composed_into_the_request_handler(self):
        self.assertTrue(issubclass(server.BrowserHandler,
                                   scan_handlers.ScanRoutes))


if __name__ == '__main__':
    unittest.main()
