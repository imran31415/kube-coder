"""Unit tests for the Walkie-Talkie in-app loopback preview (issue #306 follow-up).

Covers the LoopbackAdapter (inbound parse, outbound wire rendering), the
PreviewTranscript, GatewayPreview's window probe, an end-to-end drive through the
SAME gateway core + a REAL HypervisorSession (the `cat` echo) — including the
pairing-code exchange and the simulated out-of-window TEMPLATE path — and the
server route handlers.

Run:  python3 -m unittest tests.gateway_preview_test   (from charts/workspace/)
"""

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

try:
    import fcntl  # noqa: F401
except ImportError:  # pragma: no cover - platform shim
    import types
    _shim = types.ModuleType('fcntl')
    _shim.flock = lambda *a, **k: None
    _shim.LOCK_EX = _shim.LOCK_UN = _shim.LOCK_SH = _shim.LOCK_NB = 0
    sys.modules['fcntl'] = _shim

import gateway as gw  # noqa: E402
import hypervisor_session as hs  # noqa: E402
from adapters.internal import LoopbackAdapter  # noqa: E402
import server  # noqa: E402


def _wait(cond, timeout=20.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


class PreviewTranscriptTest(unittest.TestCase):
    def test_seq_since_and_cursor(self):
        t = gw.PreviewTranscript()
        t.add('in', 'a')
        t.add('out', 'b')
        self.assertEqual(t.cursor(), 2)
        self.assertEqual([m['text'] for m in t.since(0)], ['a', 'b'])
        self.assertEqual([m['text'] for m in t.since(1)], ['b'])

    def test_clear_keeps_seq_monotonic(self):
        t = gw.PreviewTranscript()
        t.add('in', 'a')
        t.clear()
        item = t.add('in', 'b')
        self.assertGreater(item['seq'], 1)


class GatewayPreviewProbeTest(unittest.TestCase):
    def test_probe_only_forces_internal_identity_when_simulating(self):
        p = gw.GatewayPreview()
        self.assertIsNone(p.window_probe(gw.INTERNAL_IDENTITY))
        self.assertIsNone(p.window_probe('whatsapp:+1'))
        p.simulate_out_of_window = True
        self.assertFalse(p.window_probe(gw.INTERNAL_IDENTITY))
        # A real WhatsApp identity is never affected by the preview toggle.
        self.assertIsNone(p.window_probe('whatsapp:+1'))


class LoopbackAdapterTest(unittest.TestCase):
    def setUp(self):
        self.t = gw.PreviewTranscript()
        self.a = LoopbackAdapter(self.t)

    def test_advertises_whatsapp_capabilities(self):
        self.assertTrue(self.a.capabilities.buttons)
        self.assertEqual(self.a.capabilities.max_buttons, 3)
        self.assertTrue(self.a.capabilities.proactive)
        self.assertEqual(self.a.capabilities.max_text_len, 4096)

    def test_inbound_parse(self):
        msg = self.a.inbound(gw.RawRequest(form={'from': 'internal:local',
                                                 'text': 'hi'}))
        self.assertEqual(msg.text, 'hi')
        self.assertEqual(msg.provider_msg_id, '')  # never deduped

    def test_outbound_records_wire_payload(self):
        self.a.outbound(gw.OutboundMessage(
            channel_identity='internal:local', text='Pick:',
            quick_replies=['Yes', 'No']))
        item = self.t.since(0)[-1]
        self.assertEqual(item['direction'], 'out')
        payloads = item['wire']['payloads']
        self.assertEqual(payloads[-1]['type'], 'interactive')
        self.assertEqual(payloads[-1]['interactive']['type'], 'button')

    def test_outbound_publishes_event(self):
        events = []
        a = LoopbackAdapter(self.t, publish=lambda t, d: events.append((t, d)))
        a.outbound(gw.OutboundMessage(channel_identity='internal:local', text='x'))
        self.assertEqual(events[0][0], 'gateway.preview')

    def test_template_wire(self):
        self.a.outbound(gw.OutboundMessage(
            channel_identity='internal:local', text='done', template='task_complete'))
        item = self.t.since(0)[-1]
        self.assertEqual(item['kind'], 'template')
        self.assertEqual(item['wire']['payloads'][0]['type'], 'template')


class LoopbackIntegrationTest(unittest.TestCase):
    """Drive the preview through the real gateway core + a real HypervisorSession
    (cat echo). Mirrors gateway_test's integration harness."""

    def setUp(self):
        self.hv_tmp = tempfile.mkdtemp()
        self._orig_hv = hs.HYPERVISOR_DIR
        hs.HYPERVISOR_DIR = self.hv_tmp
        self.workdir = tempfile.mkdtemp()
        self.reg = gw.IdentityRegistry(base_dir=tempfile.mkdtemp())
        self.preview = gw.GatewayPreview()
        self.gateway = gw.ConversationGateway(
            registry=self.reg,
            client_factory=lambda b: gw.LocalHypervisorClient(
                hs.HypervisorSession, assistant='echo', workdir=self.workdir,
                cli_cmd='cat'),
            token_verifier=lambda t: t == 'tok',
            window_probe=self.preview.window_probe)
        self.gateway.install_turn_observer()
        self.loop = LoopbackAdapter(self.preview.transcript,
                                    identity=gw.INTERNAL_IDENTITY)

    def tearDown(self):
        hs.HYPERVISOR_DIR = self._orig_hv
        try:
            hs.unregister_turn_observer(self.gateway.on_turn_complete)
        except Exception:
            pass

    def _inbound(self, text):
        return self.gateway.handle_inbound(
            self.loop, gw.RawRequest(form={'from': gw.INTERNAL_IDENTITY,
                                           'text': text}))

    def _link(self):
        code = self.reg.mint_pairing_code(
            workspace='default', workspace_host='h', token='tok')
        self._inbound(code)

    def test_link_exchange_then_round_trip(self):
        self._link()
        self.assertTrue(self.reg.is_linked(gw.INTERNAL_IDENTITY))
        # The "✅ Linked" reply landed in the transcript.
        self.assertTrue(any('Linked' in m['text']
                            for m in self.preview.transcript.since(0)))
        res = self._inbound('hello world')
        self.assertEqual(res.action, 'dispatched')
        self.assertTrue(_wait(lambda: any(
            m['direction'] == 'out' and m['text'] == 'hello world'
            for m in self.preview.transcript.since(0))),
            'echoed final never delivered to the loopback transcript')

    def test_out_of_window_simulation_uses_template(self):
        self._link()
        self.preview.simulate_out_of_window = True
        self._inbound('do a big thing')
        # The completed turn takes the TEMPLATE path (kind='template'), not a
        # free-form echo of the result.
        self.assertTrue(_wait(lambda: any(
            m['kind'] == 'template'
            for m in self.preview.transcript.since(0))),
            'no out-of-window template was delivered')

    def test_unknown_before_link_gets_not_linked(self):
        res = self._inbound('hi there')
        self.assertEqual(res.action, 'not_linked')


class PreviewRouteTest(unittest.TestCase):
    def setUp(self):
        self.reg = gw.IdentityRegistry(base_dir=tempfile.mkdtemp())
        self.preview = gw.GatewayPreview()
        self.gateway = gw.ConversationGateway(
            registry=self.reg, client_factory=lambda b: _NoopClient(),
            token_verifier=lambda t: True, window_probe=self.preview.window_probe)
        self.loop = LoopbackAdapter(self.preview.transcript,
                                    identity=gw.INTERNAL_IDENTITY)
        self._orig = (server._GATEWAY, server._GATEWAY_PREVIEW,
                      server._GATEWAY_LOOPBACK, server._GATEWAY_AVAILABLE,
                      server._HYPERVISOR_AVAILABLE)
        server._GATEWAY = self.gateway
        server._GATEWAY_PREVIEW = self.preview
        server._GATEWAY_LOOPBACK = self.loop
        server._GATEWAY_AVAILABLE = True
        server._HYPERVISOR_AVAILABLE = True

    def tearDown(self):
        (server._GATEWAY, server._GATEWAY_PREVIEW, server._GATEWAY_LOOPBACK,
         server._GATEWAY_AVAILABLE, server._HYPERVISOR_AVAILABLE) = self._orig

    def _handler(self, authed=True):
        h = mock.Mock(spec=server.BrowserHandler)
        h.check_claude_auth.return_value = authed
        h.headers = {'Host': 'ws.example.com'}
        h._current_bearer_token.return_value = 'tok'
        h._gw_preview_bundle.side_effect = (
            lambda: server.BrowserHandler._gw_preview_bundle(h))
        h._gw_internal_status.side_effect = (
            lambda gw_: server.BrowserHandler._gw_internal_status(h, gw_))
        self.responses = []
        h.send_json.side_effect = lambda o, s=200: self.responses.append((o, s))
        return h

    def last(self):
        return self.responses[-1]

    def test_transcript_unauthorized(self):
        h = self._handler(authed=False)
        server.BrowserHandler.handle_gateway_internal_transcript(h)
        self.assertEqual(self.last()[1], 401)

    def test_link_control_binds_and_shows_exchange(self):
        h = self._handler()
        h.read_json_body.return_value = {'action': 'link'}
        server.BrowserHandler.handle_gateway_internal_control(h)
        obj, status = self.last()
        self.assertTrue(obj['linked'])
        self.assertTrue(self.reg.is_linked(gw.INTERNAL_IDENTITY))

    def test_inbound_then_transcript(self):
        # Link first.
        self.reg.bind(gw.INTERNAL_IDENTITY, 'internal', workspace='default',
                     workspace_host='h', token='tok')
        h = self._handler()
        h.read_json_body.return_value = {'text': 'ping'}
        server.BrowserHandler.handle_gateway_internal_inbound(h)
        self.assertEqual(self.last()[0]['ok'], True)
        # Transcript reflects the user bubble + (noop client → dispatched) ack.
        h2 = self._handler()
        h2.path = '/api/gateway/internal/transcript?since=0'
        server.BrowserHandler.handle_gateway_internal_transcript(h2)
        obj = self.last()[0]
        self.assertTrue(obj['available'])
        self.assertTrue(obj['linked'])
        self.assertTrue(any(m['text'] == 'ping' for m in obj['messages']))

    def test_simulate_toggle(self):
        h = self._handler()
        h.read_json_body.return_value = {'action': 'simulate', 'on': True}
        server.BrowserHandler.handle_gateway_internal_control(h)
        self.assertTrue(self.preview.simulate_out_of_window)

    def test_reset_clears(self):
        self.reg.bind(gw.INTERNAL_IDENTITY, 'internal', workspace='default',
                     workspace_host='h', token='tok')
        self.preview.transcript.add('in', 'x')
        h = self._handler()
        h.read_json_body.return_value = {'action': 'reset'}
        server.BrowserHandler.handle_gateway_internal_control(h)
        self.assertFalse(self.reg.is_linked(gw.INTERNAL_IDENTITY))
        self.assertEqual(self.preview.transcript.since(0), [])


class _NoopClient:
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


if __name__ == '__main__':
    unittest.main()
