"""Route-handler tests for the Conversation Gateway endpoints in server.py
(issue #306): the link enrollment CRUD (POST /api/gateway/link,
GET /api/gateway/links, DELETE /api/gateway/link/<id>) and the inbound webhook
(POST /api/gateway/whatsapp/webhook) + Meta GET verify handshake.

Same style as hypervisor_routes_test.py: exercise server.py's handler methods
directly against a mock.Mock(spec=BrowserHandler) with a REAL ConversationGateway
(temp registry, EchoAdapter) swapped into the module singletons — so the auth
gate, the provider-signature-not-bearer posture, and the dispatch wiring are all
covered without a live HTTP server.

Run:  python3 -m unittest tests.gateway_routes_test   (from charts/workspace/)
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

# server.py imports fcntl (Unix-only) at module load — shim for non-Unix dev.
try:
    import fcntl  # noqa: F401
except ImportError:  # pragma: no cover - platform shim
    import types
    _shim = types.ModuleType('fcntl')
    _shim.flock = lambda *a, **k: None
    _shim.LOCK_EX = _shim.LOCK_UN = _shim.LOCK_SH = _shim.LOCK_NB = 0
    sys.modules['fcntl'] = _shim

import gateway as gw  # noqa: E402
import server  # noqa: E402
from adapters import whatsapp as wa  # noqa: E402


class GatewayRouteTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.reg = gw.IdentityRegistry(base_dir=self.tmp)
        self.adapter = gw.EchoAdapter()
        self.gateway = gw.ConversationGateway(
            registry=self.reg,
            client_factory=lambda b: _NoopClient(),
            token_verifier=lambda t: t == 'tok')
        # Swap our test gateway into the module singletons so the handlers'
        # get_gateway()/get_gateway_adapter() return them without building the
        # real (pod-pathed) instance.
        self._orig = (server._GATEWAY, server._GATEWAY_ADAPTER,
                      server._GATEWAY_AVAILABLE, server._HYPERVISOR_AVAILABLE)
        server._GATEWAY = self.gateway
        server._GATEWAY_ADAPTER = self.adapter
        server._GATEWAY_AVAILABLE = True
        server._HYPERVISOR_AVAILABLE = True

    def tearDown(self):
        (server._GATEWAY, server._GATEWAY_ADAPTER,
         server._GATEWAY_AVAILABLE, server._HYPERVISOR_AVAILABLE) = self._orig

    def _handler(self, authed=True):
        h = mock.Mock(spec=server.BrowserHandler)
        h.check_claude_auth.return_value = authed
        self.responses = []
        h.send_json.side_effect = lambda obj, status=200: self.responses.append((obj, status))
        return h

    def last(self):
        self.assertTrue(self.responses, 'handler sent no response')
        return self.responses[-1]


class _NoopClient:
    """A HypervisorClient stand-in that never runs a real turn — the route tests
    only care about the dispatch decision, not the agent output."""
    def create_thread(self):
        return 'thread-test'
    def send(self, tid, text):
        return True
    def status(self, tid):
        return 'idle'
    def last_seq(self, tid):
        return 0
    def get_events(self, tid, since=0):
        return []
    def stop(self, tid):
        return True
    def exists(self, tid):
        return True


class LinkCreateTest(GatewayRouteTestBase):
    def test_unauthorized_is_401(self):
        h = self._handler(authed=False)
        server.BrowserHandler.handle_gateway_link_create(h)
        self.assertEqual(self.last()[1], 401)

    def test_mints_pairing_code(self):
        h = self._handler()
        h.read_json_body.return_value = {'workspace': 'default'}
        h.headers = {'Host': 'ws.example.com'}
        h._current_bearer_token.return_value = 'tok'
        server.BrowserHandler.handle_gateway_link_create(h)
        obj, status = self.last()
        self.assertEqual(status, 201)
        self.assertRegex(obj['code'], r'^\d{6}$')
        self.assertEqual(obj['expires_in'], 600)

    def test_no_token_is_409(self):
        h = self._handler()
        h.read_json_body.return_value = {}
        h.headers = {'Host': 'ws.example.com'}
        h._current_bearer_token.return_value = ''
        server.BrowserHandler.handle_gateway_link_create(h)
        self.assertEqual(self.last()[1], 409)


class LinkListDeleteTest(GatewayRouteTestBase):
    def test_list_redacts(self):
        self.reg.bind('whatsapp:+1', 'whatsapp', workspace='w',
                     workspace_host='h', token='SECRET')
        h = self._handler()
        server.BrowserHandler.handle_gateway_link_list(h)
        obj, status = self.last()
        self.assertEqual(status, 200)
        self.assertEqual(len(obj['links']), 1)
        self.assertNotIn('SECRET', repr(obj))

    def test_list_unauthorized_is_401(self):
        h = self._handler(authed=False)
        server.BrowserHandler.handle_gateway_link_list(h)
        self.assertEqual(self.last()[1], 401)

    def test_delete_revokes(self):
        rec = self.reg.bind('whatsapp:+1', 'whatsapp', workspace='w',
                           workspace_host='h', token='tok')
        h = self._handler()
        server.BrowserHandler.handle_gateway_link_delete(h, rec['id'])
        obj, status = self.last()
        self.assertEqual(status, 200)
        self.assertTrue(obj['ok'])
        self.assertFalse(self.reg.is_linked('whatsapp:+1'))

    def test_delete_missing_is_404(self):
        h = self._handler()
        server.BrowserHandler.handle_gateway_link_delete(h, 'f' * 64)
        self.assertEqual(self.last()[1], 404)


class WebhookReceiveTest(GatewayRouteTestBase):
    def _raw(self, frm, text, **form):
        f = {'from': frm, 'text': text}
        f.update(form)
        return gw.RawRequest(form=f)

    def test_unknown_sender_gets_200_not_linked(self):
        h = self._handler()
        h._gateway_raw_request.return_value = self._raw('echo:+9', 'hi')
        server.BrowserHandler.handle_gateway_whatsapp_webhook(h)
        obj, status = self.last()
        # A fast 200 so the provider stops retrying; the reply is the polite
        # "not linked" notice.
        self.assertEqual(status, 200)
        self.assertEqual(obj['status'], 'not_linked')

    def test_linked_sender_dispatches(self):
        self.reg.bind('echo:+1', 'echo', workspace='w', workspace_host='h',
                     token='tok')
        h = self._handler()
        h._gateway_raw_request.return_value = self._raw('echo:+1', 'do a thing')
        server.BrowserHandler.handle_gateway_whatsapp_webhook(h)
        obj, status = self.last()
        self.assertEqual(status, 200)
        self.assertEqual(obj['status'], 'dispatched')

    def test_payload_too_large_is_413(self):
        h = self._handler()
        h._gateway_raw_request.return_value = None
        server.BrowserHandler.handle_gateway_whatsapp_webhook(h)
        self.assertEqual(self.last()[1], 413)

    def test_unavailable_gateway_is_503(self):
        server._GATEWAY = None
        server._GATEWAY_AVAILABLE = False
        h = self._handler()
        server.BrowserHandler.handle_gateway_whatsapp_webhook(h)
        self.assertEqual(self.last()[1], 503)


class VerifyHandshakeTest(GatewayRouteTestBase):
    def test_echo_adapter_has_no_handshake_403(self):
        # EchoAdapter.handshake() returns None → 403 (no send_response mock needed
        # beyond capturing the status code).
        h = self._handler()
        h._gateway_raw_request.return_value = gw.RawRequest(method='GET')
        statuses = []
        h.send_response.side_effect = lambda code: statuses.append(code)
        h.end_headers.side_effect = lambda: None
        server.BrowserHandler.handle_gateway_whatsapp_verify(h)
        self.assertIn(403, statuses)


class GatewayCredentialsRouteTest(GatewayRouteTestBase):
    """The messaging config endpoints (issue #329): catalog, redacted
    get/put/delete, test-connection — and the hot-swap of the live adapter on
    save/clear."""

    def setUp(self):
        super().setUp()
        self.tmp2 = tempfile.mkdtemp()
        self._orig_creds = server.GatewayCredentialsManager.CREDS_FILE
        server.GatewayCredentialsManager.CREDS_FILE = os.path.join(
            self.tmp2, 'gateway-credentials.json')
        # env fallback must be deterministic for the delete test.
        self._saved_env = {k: os.environ.pop(k, None) for k in (
            'KC_WHATSAPP_PROVIDER', 'KC_TWILIO_ACCOUNT_SID', 'KC_TWILIO_AUTH_TOKEN',
            'KC_TWILIO_FROM')}

    def tearDown(self):
        server.GatewayCredentialsManager.CREDS_FILE = self._orig_creds
        for k, v in self._saved_env.items():
            if v is not None:
                os.environ[k] = v
        super().tearDown()

    # -- catalog --
    def test_providers_catalog(self):
        h = self._handler()
        server.BrowserHandler.handle_gateway_providers(h)
        obj, status = self.last()
        self.assertEqual(status, 200)
        ids = [p['id'] for p in obj['providers']]
        self.assertIn('twilio', ids)
        self.assertIn('meta', ids)
        self.assertTrue(obj['providers'][0]['credential_fields'])

    def test_providers_unauthorized_is_401(self):
        h = self._handler(authed=False)
        server.BrowserHandler.handle_gateway_providers(h)
        self.assertEqual(self.last()[1], 401)

    # -- get/put/delete --
    def test_get_empty_is_unconfigured(self):
        h = self._handler()
        server.BrowserHandler.handle_gateway_credentials_get(h)
        obj, status = self.last()
        self.assertEqual(status, 200)
        self.assertFalse(obj['credentials']['configured'])

    def test_credentials_unauthorized_is_401(self):
        h = self._handler(authed=False)
        server.BrowserHandler.handle_gateway_credentials_get(h)
        self.assertEqual(self.last()[1], 401)

    def test_put_sets_hot_swaps_and_redacts(self):
        h = self._handler()
        h.read_json_body.return_value = {
            'provider_id': 'twilio',
            'creds': {'account_sid': 'AC1', 'auth_token': 'super-secret-9999'},
            'sender_number': 'whatsapp:+14155238886'}
        server.BrowserHandler.handle_gateway_credentials_put(h)
        obj, status = self.last()
        self.assertEqual(status, 200)
        self.assertTrue(obj['ok'])
        # Response is redacted — the raw secret never round-trips back.
        self.assertNotIn('super-secret-9999', repr(obj))
        # Store persisted the creds.
        raw = server.GatewayCredentialsManager.get_raw()
        self.assertEqual(raw['creds']['auth_token'], 'super-secret-9999')
        # Live adapter hot-swapped to a Twilio provider carrying the new creds.
        self.assertIsInstance(server._GATEWAY_ADAPTER.provider, wa.TwilioProvider)
        self.assertEqual(server._GATEWAY_ADAPTER.provider.account_sid, 'AC1')

    def test_put_unknown_provider_is_400(self):
        h = self._handler()
        h.read_json_body.return_value = {'provider_id': 'nope', 'creds': {}}
        server.BrowserHandler.handle_gateway_credentials_put(h)
        self.assertEqual(self.last()[1], 400)

    def test_put_bad_json_is_400(self):
        h = self._handler()
        h.read_json_body.side_effect = ValueError('bad')
        server.BrowserHandler.handle_gateway_credentials_put(h)
        self.assertEqual(self.last()[1], 400)

    def test_get_after_put_hides_secret(self):
        server.GatewayCredentialsManager.set(
            'twilio', {'account_sid': 'AC1', 'auth_token': 'super-secret-9999'})
        h = self._handler()
        server.BrowserHandler.handle_gateway_credentials_get(h)
        obj, status = self.last()
        self.assertTrue(obj['credentials']['configured'])
        self.assertNotIn('super-secret-9999', repr(obj))

    def test_delete_clears_and_falls_back_to_env(self):
        server.GatewayCredentialsManager.set(
            'twilio', {'account_sid': 'AC1', 'auth_token': 'super-secret-9999'})
        h = self._handler()
        server.BrowserHandler.handle_gateway_credentials_delete(h)
        obj, status = self.last()
        self.assertEqual(status, 200)
        self.assertTrue(obj['ok'])
        self.assertIsNone(server.GatewayCredentialsManager.get_raw())
        # Adapter rebuilt to the env fallback (no env set → empty Twilio).
        self.assertIsInstance(server._GATEWAY_ADAPTER.provider, wa.TwilioProvider)
        self.assertEqual(server._GATEWAY_ADAPTER.provider.account_sid, '')

    # -- test-connection --
    def test_test_empty_store_is_400(self):
        h = self._handler()
        server.BrowserHandler.handle_gateway_test(h)
        obj, status = self.last()
        self.assertEqual(status, 400)
        self.assertFalse(obj['ok'])

    def test_test_passes_with_valid_creds(self):
        server.GatewayCredentialsManager.set(
            'twilio', {'account_sid': 'AC1', 'auth_token': 'super-secret-9999'})
        h = self._handler()
        with mock.patch.object(wa.TwilioProvider, 'validate',
                               return_value=(True, 'HTTP 200')):
            server.BrowserHandler.handle_gateway_test(h)
        obj, status = self.last()
        self.assertEqual(status, 200)
        self.assertTrue(obj['ok'])


if __name__ == '__main__':
    unittest.main()
