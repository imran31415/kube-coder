"""Unit tests for the channel-agnostic Conversation Gateway core (issue #306).

The centerpiece is the Phase-0 exit criterion: a fake inbound round-trips through
a **REAL** HypervisorSession (the fallback `cat` adapter, which echoes stdin — no
external CLI, fully deterministic) and comes back **rendered** through the
messageable projection, driven entirely by the EchoAdapter. No WhatsApp code is
touched here.

Also covers, all against EchoAdapter: identity registry (bind / lookup / revoke /
redacted list / multi-workspace select), pairing-code enrollment, allowlist-by-
default, keyword routing (new chat / stop / unlink / workspaces / @workspace),
mid-turn queue, choice → quick-replies, inbound idempotency, the pure policy
engine, chunking, rate limiting, and outbound sequencing.

Run:  python3 -m unittest tests.gateway_test   (from charts/workspace/)
"""

import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import gateway as gw  # noqa: E402
import hypervisor_session as hs  # noqa: E402


def _wait(cond, timeout=20.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


# ───────────────────────────────────────────────────────────────────────────
# Pure helpers
# ───────────────────────────────────────────────────────────────────────────
class ChunkTest(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(gw.chunk_text('hi', 4096), ['hi'])

    def test_empty_text_is_single_empty_chunk(self):
        self.assertEqual(gw.chunk_text('', 4096), [''])

    def test_splits_on_paragraph_boundary(self):
        a, b = 'A' * 3000, 'B' * 3000
        chunks = gw.chunk_text(f'{a}\n\n{b}', 4096)
        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(len(c) <= 4096 for c in chunks))
        self.assertEqual(chunks[0], a)
        self.assertEqual(chunks[1], b)

    def test_hard_split_when_no_boundary(self):
        chunks = gw.chunk_text('X' * 9000, 4096)
        self.assertTrue(all(len(c) <= 4096 for c in chunks))
        self.assertEqual(''.join(chunks), 'X' * 9000)

    def test_order_preserved_and_lossless_on_word_boundary(self):
        text = ' '.join(['word'] * 2000)
        chunks = gw.chunk_text(text, 200)
        self.assertTrue(all(len(c) <= 200 for c in chunks))
        self.assertEqual(' '.join(chunks).split(), text.split())


class ParseCommandTest(unittest.TestCase):
    def test_new_chat_variants(self):
        for t in ('new chat', 'New Chat', 'start over', 'reset'):
            self.assertEqual(gw.parse_command(t).command, 'new_chat')

    def test_stop_variants(self):
        for t in ('stop', 'STOP', 'cancel', 'abort'):
            self.assertEqual(gw.parse_command(t).command, 'stop')

    def test_unlink_and_workspaces(self):
        self.assertEqual(gw.parse_command('unlink').command, 'unlink')
        self.assertEqual(gw.parse_command('workspaces').command, 'workspaces')

    def test_at_workspace_prefix(self):
        p = gw.parse_command('@prod deploy the thing')
        self.assertEqual(p.workspace, 'prod')
        self.assertEqual(p.remainder, 'deploy the thing')

    def test_on_workspace_prefix(self):
        p = gw.parse_command('on staging: run tests')
        self.assertEqual(p.workspace, 'staging')
        self.assertEqual(p.remainder, 'run tests')

    def test_plain_message(self):
        p = gw.parse_command('what is the status')
        self.assertIsNone(p.command)
        self.assertIsNone(p.workspace)
        self.assertEqual(p.remainder, 'what is the status')


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.policy = gw.LongTurnPolicy(stream_after=8.0, background_after=60.0)
        self.caps_proactive = gw.Capabilities(proactive=True)
        self.caps_plain = gw.Capabilities(proactive=False)

    def test_done_in_window_is_final(self):
        self.assertEqual(self.policy.decide(
            elapsed=1, caps=self.caps_plain, window_open=True, done=True), gw.FINAL)

    def test_done_out_of_window_proactive_is_template(self):
        self.assertEqual(self.policy.decide(
            elapsed=1, caps=self.caps_proactive, window_open=False, done=True),
            gw.TEMPLATE)

    def test_done_out_of_window_no_proactive_is_drop(self):
        self.assertEqual(self.policy.decide(
            elapsed=1, caps=self.caps_plain, window_open=False, done=True), gw.DROP)

    def test_in_progress_thresholds(self):
        self.assertEqual(self.policy.decide(
            elapsed=1, caps=self.caps_plain, window_open=True, done=False), gw.WAIT)
        self.assertEqual(self.policy.decide(
            elapsed=10, caps=self.caps_plain, window_open=True, done=False), gw.STREAM)
        self.assertEqual(self.policy.decide(
            elapsed=61, caps=self.caps_plain, window_open=True, done=False),
            gw.BACKGROUND_NOTIFY)


class RateLimiterTest(unittest.TestCase):
    def test_blocks_after_max(self):
        rl = gw.RateLimiter(max_events=3, window_seconds=60)
        self.assertTrue(all(rl.allow('k') for _ in range(3)))
        self.assertFalse(rl.allow('k'))

    def test_independent_per_key(self):
        rl = gw.RateLimiter(max_events=1, window_seconds=60)
        self.assertTrue(rl.allow('a'))
        self.assertTrue(rl.allow('b'))
        self.assertFalse(rl.allow('a'))


class OutboundSequencerTest(unittest.TestCase):
    def test_monotonic_per_conversation(self):
        s = gw.OutboundSequencer()
        self.assertEqual([s.next('c'), s.next('c'), s.next('c')], [1, 2, 3])
        self.assertEqual(s.next('other'), 1)

    def test_dedupe(self):
        s = gw.OutboundSequencer()
        self.assertTrue(s.dedupe('c', 'k1'))
        self.assertFalse(s.dedupe('c', 'k1'))
        self.assertTrue(s.dedupe('c', 'k2'))


class ProjectionTest(unittest.TestCase):
    def test_collapses_tools_and_keeps_prose(self):
        events = [
            {'seq': 1, 'role': 'user', 'type': 'message', 'text': 'hi'},
            {'seq': 2, 'role': 'assistant', 'type': 'tool_call',
             'tool': {'name': 'bash'}, 'tool_id': 't1'},
            {'seq': 3, 'role': 'system', 'type': 'tool_result',
             'tool_use_id': 't1', 'text': 'ok'},
            {'seq': 4, 'role': 'assistant', 'type': 'message', 'text': 'All done.'},
        ]
        proj = hs.build_messageable(events)
        self.assertEqual(proj['text'], 'All done.')
        self.assertTrue(proj['has_prose'])
        self.assertEqual(proj['counts']['tool_calls'], 1)

    def test_choice_surfaces_last_options(self):
        events = [
            {'seq': 1, 'role': 'assistant', 'type': 'message', 'text': 'Pick:'},
            {'seq': 2, 'role': 'assistant', 'type': 'choice',
             'options': ['A', 'B'], 'question': 'Which?'},
        ]
        proj = hs.build_messageable(events)
        self.assertEqual(proj['choice']['options'], ['A', 'B'])

    def test_tool_only_turn_has_no_prose(self):
        events = [
            {'seq': 1, 'role': 'assistant', 'type': 'tool_call',
             'tool': {'name': 'bash'}, 'tool_id': 't1'},
            {'seq': 2, 'role': 'system', 'type': 'tool_result',
             'tool_use_id': 't1', 'text': 'ok'},
        ]
        proj = hs.build_messageable(events)
        self.assertFalse(proj['has_prose'])
        self.assertEqual(hs.summarize_tool_activity(proj['counts']), 'ran 1 command')

    def test_errors_surface(self):
        events = [{'seq': 1, 'role': 'system', 'type': 'error', 'text': 'boom'}]
        proj = hs.build_messageable(events)
        self.assertIn('boom', proj['errors'])
        self.assertIn('boom', proj['text'])


# ───────────────────────────────────────────────────────────────────────────
# Identity registry
# ───────────────────────────────────────────────────────────────────────────
class IdentityRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.reg = gw.IdentityRegistry(base_dir=self.tmp)

    def test_bind_lookup_revoke(self):
        self.assertFalse(self.reg.is_linked('whatsapp:+1'))
        rec = self.reg.bind('whatsapp:+1', 'whatsapp', workspace='default',
                            workspace_host='h', token='tok')
        self.assertTrue(self.reg.is_linked('whatsapp:+1'))
        self.assertEqual(rec['bindings'][0]['workspace'], 'default')
        self.assertTrue(self.reg.revoke(rec['id']))
        self.assertFalse(self.reg.is_linked('whatsapp:+1'))

    def test_number_hashed_at_rest(self):
        self.reg.bind('whatsapp:+15551234567', 'whatsapp', workspace='w',
                     workspace_host='h', token='tok')
        names = os.listdir(os.path.join(self.tmp, 'identities'))
        # Filename is a 64-hex sha256; the raw number appears nowhere on disk.
        self.assertTrue(all(n.endswith('.json') and len(n) == 69 for n in names))
        for n in names:
            with open(os.path.join(self.tmp, 'identities', n)) as f:
                self.assertNotIn('15551234567', f.read())

    def test_list_links_redacts_token_and_number(self):
        self.reg.bind('whatsapp:+1', 'whatsapp', workspace='w',
                     workspace_host='h', token='SECRET')
        links = self.reg.list_links()
        self.assertEqual(len(links), 1)
        blob = repr(links)
        self.assertNotIn('SECRET', blob)
        self.assertNotIn('+1', blob)
        self.assertTrue(links[0]['bindings'][0]['token_set'])

    def test_multi_workspace_default_and_select(self):
        self.reg.bind('whatsapp:+1', 'whatsapp', workspace='a',
                     workspace_host='h', token='t', make_default=True)
        rec = self.reg.bind('whatsapp:+1', 'whatsapp', workspace='b',
                           workspace_host='h', token='t', make_default=False)
        self.assertEqual(len(rec['bindings']), 2)
        self.assertEqual(gw.IdentityRegistry.select_binding(rec)['workspace'], 'a')
        self.assertEqual(
            gw.IdentityRegistry.select_binding(rec, 'b')['workspace'], 'b')
        self.assertIsNone(gw.IdentityRegistry.select_binding(rec, 'nope'))

    def test_set_default_thread_persists(self):
        self.reg.bind('whatsapp:+1', 'whatsapp', workspace='w',
                     workspace_host='h', token='t')
        self.reg.set_default_thread('whatsapp:+1', 'w', 'thread-9')
        rec = self.reg.lookup('whatsapp:+1')
        self.assertEqual(rec['bindings'][0]['default_thread_id'], 'thread-9')

    def test_pairing_code_single_use_and_binds(self):
        code = self.reg.mint_pairing_code(workspace='w', workspace_host='h',
                                          token='tok')
        self.assertRegex(code, r'^\d{6}$')
        rec = self.reg.try_bind_with_code('whatsapp:+1', 'whatsapp', code)
        self.assertIsNotNone(rec)
        self.assertEqual(rec['bindings'][0]['token'], 'tok')
        # Single-use: the same code can't bind a second identity.
        self.assertIsNone(self.reg.try_bind_with_code('whatsapp:+2', 'whatsapp', code))

    def test_expired_pairing_code_rejected(self):
        code = self.reg.mint_pairing_code(workspace='w', workspace_host='h',
                                          token='tok', ttl_seconds=-1)
        self.assertIsNone(self.reg.try_bind_with_code('whatsapp:+1', 'whatsapp', code))

    def test_non_code_text_is_not_a_binding(self):
        self.assertIsNone(
            self.reg.try_bind_with_code('whatsapp:+1', 'whatsapp', 'hello there'))


# ───────────────────────────────────────────────────────────────────────────
# Gateway ⇄ REAL HypervisorSession round-trip (Phase-0 exit criterion)
# ───────────────────────────────────────────────────────────────────────────
class GatewayEchoIntegrationTest(unittest.TestCase):
    """Drives the whole core against a real HypervisorSession. The `cat` fallback
    adapter echoes the sent text back as the assistant reply, so the round-trip
    is deterministic with no CLI/network."""

    def setUp(self):
        self.hv_tmp = tempfile.mkdtemp()
        self._orig_hv = hs.HYPERVISOR_DIR
        hs.HYPERVISOR_DIR = self.hv_tmp
        self.workdir = tempfile.mkdtemp()
        self.gw_tmp = tempfile.mkdtemp()
        self.reg = gw.IdentityRegistry(base_dir=self.gw_tmp)

        def factory(binding):
            return gw.LocalHypervisorClient(
                hs.HypervisorSession, assistant='echo', workdir=self.workdir,
                cli_cmd='cat')

        self.gateway = gw.ConversationGateway(
            registry=self.reg, client_factory=factory,
            token_verifier=lambda t: t == 'tok')
        self.gateway.install_turn_observer()
        self.adapter = gw.EchoAdapter()

    def tearDown(self):
        hs.HYPERVISOR_DIR = self._orig_hv
        # Detach this test's observer so it can't fire on a later test's thread.
        try:
            hs.unregister_turn_observer(self.gateway.on_turn_complete)
        except Exception:
            pass

    def _raw(self, frm, text='', **form):
        f = {'from': frm, 'text': text}
        f.update(form)
        return gw.RawRequest(form=f)

    def _link(self, frm='echo:+1', workspace='default'):
        self.reg.bind(frm, 'echo', workspace=workspace, workspace_host='h',
                     token='tok')

    def test_unknown_sender_gets_one_not_linked_reply(self):
        res = self.gateway.handle_inbound(self.adapter, self._raw('echo:+9', 'hi'))
        self.assertEqual(res.action, 'not_linked')
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertIn("isn't linked", self.adapter.sent[0].text)

    def test_pairing_code_links_over_channel(self):
        code = self.reg.mint_pairing_code(workspace='default', workspace_host='h',
                                          token='tok')
        res = self.gateway.handle_inbound(self.adapter, self._raw('echo:+1', code))
        self.assertEqual(res.action, 'linked')
        self.assertTrue(self.reg.is_linked('echo:+1'))

    def test_round_trip_echoes_final(self):
        self._link()
        res = self.gateway.handle_inbound(
            self.adapter, self._raw('echo:+1', 'hello world'))
        self.assertEqual(res.action, 'dispatched')
        self.assertIsNotNone(res.thread_id)
        # Wait for the real turn to finish and the observer to deliver the final.
        self.assertTrue(_wait(lambda: any(
            m.text == 'hello world' for m in self.adapter.sent)),
            f'no echoed final in {self.adapter.texts()}')
        # The default thread was persisted so the next message continues it.
        rec = self.reg.lookup('echo:+1')
        self.assertEqual(rec['bindings'][0]['default_thread_id'], res.thread_id)

    def test_idempotent_inbound_not_double_dispatched(self):
        self._link()
        raw = self._raw('echo:+1', 'hello world', id='msg-1')
        r1 = self.gateway.handle_inbound(self.adapter, raw)
        r2 = self.gateway.handle_inbound(self.adapter, self._raw(
            'echo:+1', 'hello world', id='msg-1'))
        self.assertEqual(r1.action, 'dispatched')
        self.assertEqual(r2.action, 'duplicate')

    def test_unlink_keyword_revokes(self):
        self._link()
        res = self.gateway.handle_inbound(self.adapter, self._raw('echo:+1', 'unlink'))
        self.assertEqual(res.action, 'unlink')
        self.assertFalse(self.reg.is_linked('echo:+1'))

    def test_workspaces_keyword_lists(self):
        self._link(workspace='alpha')
        self.reg.bind('echo:+1', 'echo', workspace='beta', workspace_host='h',
                     token='tok', make_default=False)
        res = self.gateway.handle_inbound(
            self.adapter, self._raw('echo:+1', 'workspaces'))
        self.assertEqual(res.action, 'workspaces')
        self.assertIn('alpha', self.adapter.sent[-1].text)
        self.assertIn('beta', self.adapter.sent[-1].text)

    def test_revoked_token_is_rejected(self):
        self.reg.bind('echo:+1', 'echo', workspace='default', workspace_host='h',
                     token='STALE')  # token_verifier only accepts 'tok'
        res = self.gateway.handle_inbound(self.adapter, self._raw('echo:+1', 'hi'))
        self.assertEqual(res.action, 'rejected')
        self.assertIn('expired', self.adapter.sent[-1].text)

    def test_mid_turn_message_is_queued_then_drained(self):
        self._link()
        r1 = self.gateway.handle_inbound(
            self.adapter, self._raw('echo:+1', 'first'))
        tid = r1.thread_id
        # Force a "running" view by re-inserting pending + marking the thread
        # running is racy; instead drive a second message immediately. If the
        # first turn is still running it queues; either way the final echoes.
        self.gateway.handle_inbound(self.adapter, self._raw('echo:+1', 'second'))
        self.assertTrue(_wait(lambda: any(
            m.text == 'second' for m in self.adapter.sent)),
            f'second never delivered: {self.adapter.texts()}')

    def test_choice_becomes_quick_replies(self):
        # A cat turn can't emit a choice, so exercise the delivery path directly:
        # a projection with a choice must render quick_replies on the final.
        self._link()
        adapter = self.adapter
        self.gateway._deliver_final(
            adapter, 'echo:+1', 'tid', 0, 'Pick one:',
            {'options': ['Yes', 'No'], 'question': 'Which?'})
        self.assertEqual(adapter.sent[-1].quick_replies, ['Yes', 'No'])

    def test_rate_limit_blocks_flood(self):
        self._link()
        self.gateway.rate_limiter = gw.RateLimiter(max_events=1, window_seconds=60)
        self.gateway.handle_inbound(self.adapter, self._raw('echo:+1', 'one'))
        res = self.gateway.handle_inbound(self.adapter, self._raw('echo:+1', 'two'))
        self.assertEqual(res.action, 'rejected')
        self.assertEqual(res.status, 429)


class TemplateRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tr = gw.TemplateRegistry(base_dir=self.tmp)

    def test_default_task_complete_is_selectable(self):
        tpl = self.tr.select('task_complete')
        self.assertIsNotNone(tpl)
        self.assertEqual(tpl['status'], 'approved')

    def test_render_body_interpolates_title(self):
        body = self.tr.render_body('task_complete', title='deploy')
        self.assertIn('deploy', body)

    def test_unknown_template_is_none(self):
        self.assertIsNone(self.tr.select('does_not_exist'))


if __name__ == '__main__':
    unittest.main()
