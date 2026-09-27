"""Tests for the boards route table that replaced the elif branches (#100).

The Board Processor: 28 routes across four verbs — the largest domain in the
series, the only one that puts anything in `do_PUT`'s table, and the one whose
source comments carried the most ordering claims. Every branch was already
contiguous on every verb, so this suite's job is less about a hoist than about
separating the ordering constraints that are load-bearing from the ones that
only look it.

Handler behaviour is covered by tests/boards_api_test.py,
tests/boards_review_api_test.py, tests/boards_runs_api_test.py,
tests/boards_phase7_test.py and tests/boards_credentials_test.py — all
unchanged.

Run with:
    cd charts/workspace && python3 -m unittest tests.boards_routes_table_test
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402
from handlers import boards  # noqa: E402
from handlers.routing import RouteTable  # noqa: E402
from tests.http_harness import (  # noqa: E402
    DomainEndpointTests, DomainRouteTests, EndpointTestCase, args_for,
    dispatch_through_adapter, dispatch_to_mock,
)


class BoardRouteOrderTests(DomainRouteTests, unittest.TestCase):

    table = boards.ROUTES
    foreign_paths = (('GET', '/api/boards/'), ('GET', '/api/boardsx'),
                     ('GET', '/api/boards/b1/items/i1/extra'),
                     ('GET', '/api/boards/b1/runs/notarun'),
                     ('POST', '/api/boards/b1/staged/i1/destroy'))
    oauth_samples = (('GET', '/api/boards/b1'), ('POST', '/api/boards'),
                     ('PUT', '/api/boards/b1'), ('DELETE', '/api/boards/b1'))
    wrong_verb_samples = (('DELETE', '/api/boards'),
                          ('GET', '/api/boards/b1/test-fetch'),
                          ('POST', '/api/boards/b1/metrics'),
                          ('PUT', '/api/boards/b1/runs'))

    # --- the reads ---------------------------------------------------------

    def test_the_collection_and_detail_reads(self):
        for path, handler in (
                ('/api/boards', 'handle_boards_list'),
                ('/api/boards/b1', 'handle_board_get'),
                ('/api/boards/b1/items', 'handle_board_items'),
                ('/api/boards/b1/review', 'handle_board_review_list'),
                ('/api/boards/b1/metrics', 'handle_board_metrics'),
                ('/api/boards/b1/standing', 'handle_board_standing'),
                ('/api/boards/b1/strategies', 'handle_board_strategies_list'),
                ('/api/boards/b1/runs', 'handle_board_runs_list'),
                ('/api/boards/b1/runs/run-abc', 'handle_board_run_get')):
            self.assertEqual(self.resolve('GET', path), handler, path)

    # --- the hazard that is real -------------------------------------------

    def test_the_reserved_ids_are_not_read_as_boards(self):
        # THE hazard of this domain: `credentials` and `templates` are legal
        # spellings of the board-id charset, so their exact routes only work
        # while they stay ahead of /api/boards/{id}.
        self.assertEqual(self.resolve('GET', '/api/boards/credentials'),
                         'handle_board_credentials_list')
        self.assertEqual(self.resolve('GET', '/api/boards/templates'),
                         'handle_board_templates')
        # …and the bare-id route would happily have taken them.
        self.assertEqual(self.resolve('GET', '/api/boards/anythingelse'),
                         'handle_board_get')

    def test_inverting_the_table_really_does_break_the_reserved_ids(self):
        # The counterpart to the test above: this ordering is load-bearing,
        # not decoration. Reversed, both reserved ids become board ids.
        inverted = RouteTable()
        inverted.routes.extend(reversed(boards.ROUTES.routes))
        for path in ('/api/boards/credentials', '/api/boards/templates'):
            self.assertEqual(inverted.match('GET', path, path)[0].handler,
                             'handle_board_get', path)

    def test_the_reserved_ids_are_reserved_in_the_schema(self):
        # The routing only holds because the schema refuses to create a board
        # with either name. Derived from the real constant, never restated.
        from boards import schema
        for name in ('credentials', 'templates'):
            self.assertIn(name, schema.RESERVED_BOARD_IDS)
        # The other reserved ids exist because a sub-resource segment shares
        # the id charset too — `draft` has a POST route, and `metrics`,
        # `runs` and `strategies` are sub-resource names. On GET they all
        # resolve as board ids, which is harmless only because no board can
        # be created with those names.
        for name in ('draft', 'metrics', 'runs', 'strategies'):
            self.assertIn(name, schema.RESERVED_BOARD_IDS)
            self.assertEqual(self.resolve('GET', f'/api/boards/{name}'),
                             'handle_board_get', name)

    # --- the constraints that only look load-bearing -----------------------

    def test_everything_else_is_safe_by_construction_not_by_order(self):
        # Every other "must come first" in the old chain compares patterns
        # that differ in segment count and are all `$`-anchored, so they
        # cannot collide. Reversed, they still resolve the same way.
        inverted = RouteTable()
        inverted.routes.extend(reversed(boards.ROUTES.routes))
        for http_method, path in (
                ('GET', '/api/boards/b1/runs/run-abc'),
                ('GET', '/api/boards/b1/strategies'),
                ('POST', '/api/boards/b1/strategies/preview'),
                ('POST', '/api/boards/b1/runs/run-abc/stop'),
                ('POST', '/api/boards/b1/items/i1/actions'),
                ('PUT', '/api/boards/credentials/MY_TOKEN'),
                ('DELETE', '/api/boards/credentials/MY_TOKEN'),
                ('DELETE', '/api/boards/b1/strategies/some-name')):
            self.assertEqual(inverted.match(http_method, path, path)[0].handler,
                             self.resolve(http_method, path),
                             f'{http_method} {path}')

    def test_strategy_preview_is_not_shadowed_by_the_save_route(self):
        # The source comment claimed `preview` "would otherwise be read as a
        # strategy named preview". That is true of the DELETE route, whose
        # pattern is `/strategies/(.+)$` — but not of the POST save route,
        # which is `/strategies$` and cannot match an extra segment. Order
        # preserved as found; the claim is corrected here.
        self.assertEqual(
            self.resolve('POST', '/api/boards/b1/strategies/preview'),
            'handle_board_strategy_preview')
        self.assertEqual(self.resolve('POST', '/api/boards/b1/strategies'),
                         'handle_board_strategy_save')
        self.assertEqual(
            self.resolve('DELETE', '/api/boards/b1/strategies/preview'),
            'route_board_strategy_delete')

    def test_a_template_named_like_a_sub_resource_still_wins(self):
        # Pre-existing ambiguity, captured rather than changed: the template
        # route is registered before {id}/strategies, so this is a template
        # lookup, not board `templates`' strategy list. Harmless because
        # `templates` is a reserved board id.
        self.assertEqual(self.resolve('GET', '/api/boards/templates/strategies'),
                         'handle_board_template_get')

    # --- the writes --------------------------------------------------------

    def test_the_writes(self):
        for path, handler in (
                ('/api/boards', 'handle_board_create'),
                ('/api/boards/draft', 'handle_board_draft'),
                ('/api/boards/templates/t1/fill', 'handle_board_template_fill'),
                ('/api/boards/b1/test-fetch', 'handle_board_test_fetch'),
                ('/api/boards/b1/strategies', 'handle_board_strategy_save'),
                ('/api/boards/b1/runs', 'handle_board_run_create'),
                ('/api/boards/b1/runs/run-abc/stop', 'handle_board_run_stop')):
            self.assertEqual(self.resolve('POST', path), handler, path)

    def test_the_replaces_and_deletes(self):
        for http_method, path, handler in (
                ('PUT', '/api/boards/credentials/MY_TOKEN',
                 'handle_board_credential_put'),
                ('PUT', '/api/boards/b1', 'handle_board_update'),
                ('DELETE', '/api/boards/credentials/MY_TOKEN',
                 'handle_board_credential_delete'),
                ('DELETE', '/api/boards/b1/strategies/s1',
                 'route_board_strategy_delete'),
                ('DELETE', '/api/boards/b1', 'handle_board_delete')):
            self.assertEqual(self.resolve(http_method, path), handler, path)

    def test_a_credential_name_must_be_screaming_snake(self):
        # Preserved verbatim: the charset is what keeps a credential route
        # from matching a lower-case board id.
        for http_method in ('PUT', 'DELETE'):
            self.assertIsNone(
                self.resolve(http_method, '/api/boards/credentials/lower'),
                http_method)
            self.assertIsNone(
                self.resolve(http_method, '/api/boards/credentials/AB'),
                http_method)

    def test_a_run_id_must_carry_its_prefix(self):
        self.assertEqual(self.resolve('GET', '/api/boards/b1/runs/run-a1'),
                         'handle_board_run_get')
        self.assertIsNone(self.resolve('GET', '/api/boards/b1/runs/a1'))

    def test_the_review_decisions_are_an_allowlist(self):
        for decision in ('approve', 'reject', 'send-back', 'edit'):
            self.assertEqual(
                self.resolve('POST', f'/api/boards/b1/staged/i1/{decision}'),
                'route_board_review_decide', decision)
        self.assertIsNone(
            self.resolve('POST', '/api/boards/b1/staged/i1/delete'))


class BoardAdapterTests(unittest.TestCase):
    """The three dispatch-site transformations the chain did inline."""

    def test_the_decide_adapter_decodes_the_item_and_renames_send_back(self):
        h = dispatch_through_adapter(
            boards.ROUTES, 'route_board_review_decide', 'POST',
            '/api/boards/b1/staged/PROJ%2F42/send-back')
        self.assertEqual((h._board_id, h._board_item_id), ('b1', 'PROJ/42'))
        # The path spells it `send-back`; the handler takes `send_back`.
        h.handle_board_review_decide.assert_called_once_with('send_back')

    def test_the_decide_adapter_passes_the_other_decisions_through(self):
        h = dispatch_through_adapter(
            boards.ROUTES, 'route_board_review_decide', 'POST',
            '/api/boards/b1/staged/i1/approve')
        h.handle_board_review_decide.assert_called_once_with('approve')

    def test_the_disposition_and_action_adapters_decode_the_item(self):
        for suffix, adapter, method in (
                ('disposition', 'route_board_disposition',
                 'handle_board_disposition'),
                ('actions', 'route_board_action', 'handle_board_action')):
            h = dispatch_through_adapter(
                boards.ROUTES, adapter, 'POST',
                f'/api/boards/b1/items/a%20b/{suffix}')
            self.assertEqual((h._board_id, h._board_item_id), ('b1', 'a b'))
            getattr(h, method).assert_called_once_with()

    def test_the_strategy_delete_adapter_decodes_the_name(self):
        h = dispatch_through_adapter(
            boards.ROUTES, 'route_board_strategy_delete', 'DELETE',
            '/api/boards/b1/strategies/my%20strategy')
        self.assertEqual(h._board_id, 'b1')
        h.handle_board_strategy_delete.assert_called_once_with('my strategy')

    def test_the_ids_that_need_no_adapter_ride_the_sets_column(self):
        h = dispatch_to_mock(boards.ROUTES, 'GET',
                             '/api/boards/b1/runs/run-xyz')
        self.assertEqual((h._board_id, h._board_run_id), ('b1', 'run-xyz'))
        self.assertEqual(
            args_for(boards.ROUTES, 'GET', '/api/boards/templates/t1'), ('t1',))


class BoardEndpointBehaviourTests(DomainEndpointTests, EndpointTestCase):
    """Status codes for the migrated routes, over a real server."""

    oauth_reachable = (('GET', '/api/boards'), ('POST', '/api/boards'),
                       ('PUT', '/api/boards/b1'), ('DELETE', '/api/boards/b1'))
    unmatched_shapes = (('GET', '/api/boards/'),
                        ('GET', '/api/boards/b1/runs/notarun'),
                        ('POST', '/api/boards/b1/staged/i1/destroy'))

    def test_the_reads_are_auth_gated(self):
        # 401 rather than 404 is also the proof the route matched at all.
        for path in ('/api/boards', '/api/boards/credentials',
                     '/api/boards/templates', '/api/boards/templates/t1',
                     '/api/boards/b1', '/api/boards/b1/items',
                     '/api/boards/b1/review', '/api/boards/b1/metrics',
                     '/api/boards/b1/standing', '/api/boards/b1/strategies',
                     '/api/boards/b1/runs', '/api/boards/b1/runs/run-a1'):
            self.assertEqual(self.get(path)[0], 401, path)

    def test_the_writes_are_auth_gated(self):
        for path in ('/api/boards', '/api/boards/draft',
                     '/api/boards/templates/t1/fill',
                     '/api/boards/b1/test-fetch',
                     '/api/boards/b1/strategies',
                     '/api/boards/b1/strategies/preview',
                     '/api/boards/b1/staged/i1/approve',
                     '/api/boards/b1/items/i1/disposition',
                     '/api/boards/b1/items/i1/actions',
                     '/api/boards/b1/runs',
                     '/api/boards/b1/runs/run-a1/stop'):
            self.assertEqual(self.post(path)[0], 401, path)

    def test_the_replaces_and_deletes_are_auth_gated(self):
        for http_method, path in (
                ('PUT', '/api/boards/credentials/MY_TOKEN'),
                ('PUT', '/api/boards/b1'),
                ('DELETE', '/api/boards/credentials/MY_TOKEN'),
                ('DELETE', '/api/boards/b1/strategies/s1'),
                ('DELETE', '/api/boards/b1')):
            self.assertEqual(self.request(path, method=http_method)[0], 401,
                             f'{http_method} {path}')

    def test_put_falls_through_to_501_not_404(self):
        # do_PUT's default, which is a 501 rather than the 404 the other verbs
        # give. This domain introduced the verb's first table dispatch, so the
        # untouched fall-through is worth pinning — including for a path that
        # IS under /api/boards but matches no route.
        self.assertEqual(self.request('/api/nothing', method='PUT')[0], 501)
        self.assertEqual(
            self.request('/api/boards/credentials/lower', method='PUT')[0], 501)


if __name__ == '__main__':
    unittest.main()
