"""Tests for the gateway route table that replaced the elif branches (#100).

Conversation Gateway: 13 routes across four verbs, contiguous on every verb —
so, as with boards, the table is a straight lift and this suite's job is the
domain's own facts rather than a hoist argument.

The fact worth the most attention here is that three different auth postures
share one table, and the table is responsible for none of them. That is the
point: moving a route into a table must not change who may call it.

Handler behaviour — signature verification, the projection, rate limiting — is
covered by tests/gateway_routes_test.py, tests/gateway_test.py,
tests/gateway_whatsapp_test.py and tests/gateway_preview_test.py, all
unchanged. This suite only asserts the wiring.

Run with:
    cd charts/workspace && python3 -m unittest tests.gateway_routes_table_test
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402
from handlers import gateway  # noqa: E402
from handlers.routing import RouteTable  # noqa: E402
from tests.http_harness import (  # noqa: E402
    DomainEndpointTests, DomainRouteTests, EndpointTestCase, args_for,
)

_HASH = 'a' * 64


class GatewayRouteOrderTests(DomainRouteTests, unittest.TestCase):

    table = gateway.ROUTES
    foreign_paths = (('GET', '/api/gateway'), ('GET', '/api/gateway/'),
                     ('GET', '/api/gateway/link'), ('GET', '/api/gatewayx'),
                     ('GET', '/api/gateway/internal'))
    oauth_samples = (('GET', '/api/gateway/links'),
                     ('POST', '/api/gateway/link'),
                     ('PUT', '/api/gateway/credentials'),
                     ('DELETE', '/api/gateway/credentials'))
    wrong_verb_samples = (('DELETE', '/api/gateway/links'),
                          ('GET', '/api/gateway/test'),
                          ('POST', '/api/gateway/providers'),
                          ('PUT', '/api/gateway/link'))

    def test_the_reads(self):
        for path, handler in (
                ('/api/gateway/whatsapp/webhook',
                 'handle_gateway_whatsapp_verify'),
                ('/api/gateway/links', 'handle_gateway_link_list'),
                ('/api/gateway/providers', 'handle_gateway_providers'),
                ('/api/gateway/credentials', 'handle_gateway_credentials_get'),
                ('/api/gateway/internal/transcript',
                 'handle_gateway_internal_transcript')):
            self.assertEqual(self.resolve('GET', path), handler, path)

    def test_the_writes(self):
        for path, handler in (
                ('/api/gateway/whatsapp/webhook',
                 'handle_gateway_whatsapp_webhook'),
                ('/api/gateway/link', 'handle_gateway_link_create'),
                ('/api/gateway/test', 'handle_gateway_test'),
                ('/api/gateway/internal/inbound',
                 'handle_gateway_internal_inbound'),
                ('/api/gateway/internal/control',
                 'handle_gateway_internal_control')):
            self.assertEqual(self.resolve('POST', path), handler, path)

    def test_the_credential_replace_and_the_deletes(self):
        self.assertEqual(self.resolve('PUT', '/api/gateway/credentials'),
                         'handle_gateway_credentials_put')
        self.assertEqual(self.resolve('DELETE', '/api/gateway/credentials'),
                         'handle_gateway_credentials_delete')
        self.assertEqual(self.resolve('DELETE', f'/api/gateway/link/{_HASH}'),
                         'handle_gateway_link_delete')

    def test_the_same_path_is_two_routes_on_two_verbs(self):
        # /whatsapp/webhook is the Meta verify handshake on GET and the
        # inbound message on POST — different handlers, same path.
        self.assertEqual(self.resolve('GET', '/api/gateway/whatsapp/webhook'),
                         'handle_gateway_whatsapp_verify')
        self.assertEqual(self.resolve('POST', '/api/gateway/whatsapp/webhook'),
                         'handle_gateway_whatsapp_webhook')
        # …as is /credentials across three verbs.
        for http_method, handler in (
                ('GET', 'handle_gateway_credentials_get'),
                ('PUT', 'handle_gateway_credentials_put'),
                ('DELETE', 'handle_gateway_credentials_delete')):
            self.assertEqual(self.resolve(http_method, '/api/gateway/credentials'),
                             handler, http_method)

    def test_the_singular_plural_split_is_preserved(self):
        # Read the collection at /links, create at /link, delete at
        # /link/{hash}. Preserved verbatim; the asymmetry is pre-existing.
        self.assertEqual(self.resolve('GET', '/api/gateway/links'),
                         'handle_gateway_link_list')
        self.assertEqual(self.resolve('POST', '/api/gateway/link'),
                         'handle_gateway_link_create')
        self.assertIsNone(self.resolve('GET', '/api/gateway/link'))
        self.assertIsNone(self.resolve('POST', '/api/gateway/links'))

    def test_a_link_id_must_be_a_sha256_hash(self):
        # The charset IS the constraint — it is what keeps this route from
        # matching anything else under /api/gateway/.
        self.assertEqual(
            args_for(gateway.ROUTES, 'DELETE', f'/api/gateway/link/{_HASH}'),
            (_HASH,))
        for bad in ('short', _HASH.upper(), _HASH + 'a', _HASH[:-1]):
            self.assertIsNone(
                self.resolve('DELETE', f'/api/gateway/link/{bad}'), bad)

    def test_order_is_presentational(self):
        # Nothing in this domain overlaps: every route is an exact path except
        # the hash-constrained delete. Reversed, the table answers the same.
        inverted = RouteTable()
        inverted.routes.extend(reversed(gateway.ROUTES.routes))
        for route in gateway.ROUTES.routes:
            path = (route.pattern if isinstance(route.pattern, str)
                    else f'/api/gateway/link/{_HASH}')
            hit = inverted.match(route.http_method, path, path)
            self.assertEqual(hit[0].handler, route.handler,
                             f'{route.http_method} {path}')


class GatewayEndpointBehaviourTests(DomainEndpointTests, EndpointTestCase):
    """Status codes for the migrated routes, over a real server.

    The split this suite exists to protect: moving a route into a table must
    not change who may call it. Two of these endpoints must NOT answer 401
    without a session, because an external provider can never have one.
    """

    oauth_reachable = (('GET', '/api/gateway/links'),
                       ('POST', '/api/gateway/link'),
                       ('PUT', '/api/gateway/credentials'))
    unmatched_shapes = (('GET', '/api/gateway'),
                        ('GET', '/api/gateway/link'),
                        ('DELETE', '/api/gateway/link/short'))

    def test_the_session_authed_routes_are_gated(self):
        # 401 rather than 404 is also the proof the route matched at all.
        for path in ('/api/gateway/links', '/api/gateway/providers',
                     '/api/gateway/credentials',
                     '/api/gateway/internal/transcript'):
            self.assertEqual(self.get(path)[0], 401, path)
        for path in ('/api/gateway/link', '/api/gateway/test',
                     '/api/gateway/internal/inbound',
                     '/api/gateway/internal/control'):
            self.assertEqual(self.post(path)[0], 401, path)
        for http_method, path in (
                ('PUT', '/api/gateway/credentials'),
                ('DELETE', '/api/gateway/credentials'),
                ('DELETE', f'/api/gateway/link/{_HASH}')):
            self.assertEqual(self.request(path, method=http_method)[0], 401,
                             f'{http_method} {path}')

    def test_the_provider_facing_routes_do_not_demand_a_session(self):
        # THE posture this domain must not lose. Twilio and Meta cannot carry
        # an OAuth session, so these two authenticate on the provider
        # signature the adapter verifies. A 401 here would mean the gateway
        # had silently stopped receiving messages — so the assertion is
        # "anything but 401", not a specific success code (what they answer
        # depends on whether a gateway is configured at all).
        for http_method, path in (('GET', '/api/gateway/whatsapp/webhook'),
                                  ('POST', '/api/gateway/whatsapp/webhook')):
            status = self.request(path, method=http_method)[0]
            self.assertNotEqual(status, 401, f'{http_method} {path}')
            self.assertNotEqual(status, 404, f'{http_method} {path}')


if __name__ == '__main__':
    unittest.main()
