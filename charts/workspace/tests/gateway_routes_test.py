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


if __name__ == '__main__':
    unittest.main()
