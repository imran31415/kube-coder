"""Unit tests for hypervisor_session.DeepseekHarnessAdapter (issue #639).

The adapter's job is narrow — build the bridge's argv and map its small
envelope onto canonical events — so these tests are correspondingly narrow.
The ACP protocol itself is tested in tests/acp_bridge_test.py against a real
JSON-RPC subprocess; nothing here spawns anything.

The event lines fed to `parse()` are the exact shapes acp_bridge.py emits.

Run with:    python3 -m unittest tests.deepseek_harness_adapter_test
(from charts/workspace/)
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hypervisor_session as hs  # noqa: E402


class AdapterRoutingTest(unittest.TestCase):
    def test_deepseek_harness_gets_its_own_adapter(self):
        self.assertIsInstance(hs._adapter_for('deepseek-harness'),
                              hs.DeepseekHarnessAdapter)
        self.assertIn('deepseek-harness', hs._ADAPTERS)

    def test_it_is_structured_not_the_prose_fallback(self):
        # The whole point of the ACP route is tool cards; landing on
        # FallbackAdapter would silently give prose-only output instead.
        a = hs._adapter_for('deepseek-harness')
        self.assertIsInstance(a, hs._StructuredCliAdapter)
        self.assertNotIsInstance(a, hs.FallbackAdapter)

    def test_existing_assistants_are_unaffected(self):
        self.assertIsInstance(hs._adapter_for('claude'), hs.ClaudeAdapter)
        self.assertIsInstance(hs._adapter_for('codex'), hs.CodexAdapter)
        self.assertIsInstance(hs._adapter_for('ante'), hs.AnteAdapter)
        self.assertIsInstance(hs._adapter_for('opencode-deepseek'),
                              hs.OpencodeAdapter)
        # An unknown id must still land on the fallback, not on us.
        self.assertIsInstance(hs._adapter_for('something-else'),
                              hs.FallbackAdapter)

    def test_opencode_deepseek_still_routes_to_opencode(self):
        # Both DeepSeek paths coexist by design (#639 non-goal: do not change
        # the existing OpenCode→DeepSeek entry). A prefix mixup here would
        # hijack it.
        self.assertIsNot(hs._adapter_for('opencode-deepseek'),
                         hs._adapter_for('deepseek-harness'))


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.a = hs.DeepseekHarnessAdapter()

    def test_spawns_the_bridge_not_the_dsh_cli(self):
        # `dsh` itself speaks ACP, which the one-way process runner cannot
        # drive; the bridge is the thing that must be launched.
        spec = self.a.build({'workdir': '/home/dev'}, 'hi', first=False)
        self.assertTrue(spec['argv'][1].endswith('acp_bridge.py'))
        self.assertNotIn('dsh', spec['argv'])
        self.assertEqual(spec['argv'][2:4], ['--cwd', '/home/dev'])
        self.assertEqual(spec['cwd'], '/home/dev')

    def test_the_bridge_it_points_at_actually_exists(self):
        # Both files land in /tmp/browser at pod boot, so the relative resolve
        # must hold in a plain checkout too.
        self.assertTrue(os.path.isfile(hs.DeepseekHarnessAdapter._bridge_path()))

    def test_prompt_rides_stdin_not_argv(self):
        # Arbitrary user text, possibly long and full of metacharacters.
        text = 'multi\nline $(whoami) `id`'
        spec = self.a.build({}, text, first=False)
        self.assertEqual(spec['stdin'], text + '\n')
        self.assertNotIn(text, spec['argv'])

    def test_preamble_prepends_on_the_first_turn_only(self):
        ctx = {'workdir': '/home/dev', 'preamble': 'ROLE'}
        self.assertEqual(self.a.build(ctx, 'hi', first=True)['stdin'],
                         'ROLE\n\nhi\n')
        self.assertEqual(self.a.build(ctx, 'again', first=False)['stdin'],
                         'again\n')

    def test_no_session_flag_on_the_first_turn(self):
        self.assertNotIn('--session', self.a.build({}, 'hi', first=True)['argv'])

    def test_captured_session_id_is_resumed_on_later_turns(self):
        ctx = {'dsh_session_id': 'sess-7'}
        argv = self.a.build(ctx, 'again', first=False)['argv']
        self.assertEqual(argv[argv.index('--session') + 1], 'sess-7')

    def test_ctx_model_beats_the_pod_default(self):
        ctx = {'model': 'deepseek-v4-pro'}
        with mock.patch.dict(hs.os.environ, {'KC_DSH_MODEL': 'deepseek-v4-flash'}):
            argv = self.a.build(ctx, 'hi', first=True)['argv']
        self.assertEqual(argv[argv.index('--model') + 1], 'deepseek-v4-pro')

    def test_pod_default_model_is_used_when_the_thread_has_none(self):
        with mock.patch.dict(hs.os.environ, {'KC_DSH_MODEL': 'deepseek-v4-flash'}):
            argv = self.a.build({}, 'hi', first=True)['argv']
        self.assertEqual(argv[argv.index('--model') + 1], 'deepseek-v4-flash')

    def test_no_model_flag_when_nothing_is_configured(self):
        with mock.patch.dict(hs.os.environ, {}, clear=True):
            argv = self.a.build({}, 'hi', first=True)['argv']
        self.assertNotIn('--model', argv)

    def test_effort_maps_onto_the_harness_vocabulary(self):
        # The harness advertises off/low/high/max. `high` is its default and
        # its documented balance point, so canonical medium rounds UP to it.
        # (EFFORT_CAP registration lands with the server-side wiring; patch it
        # here so the mapping is exercised end to end either way.)
        cases = {'low': 'low', 'medium': 'high', 'high': 'high',
                 'xhigh': 'max', 'max': 'max'}
        with mock.patch.dict(hs.EFFORT_CAP, {'deepseek-harness': 'max'}):
            for canonical, native in cases.items():
                with self.subTest(effort=canonical):
                    argv = self.a.build({'effort': canonical}, 'hi', first=True)['argv']
                    self.assertEqual(argv[argv.index('--effort') + 1], native)

    def test_unset_or_unknown_effort_injects_nothing(self):
        with mock.patch.dict(hs.EFFORT_CAP, {'deepseek-harness': 'max'}):
            for effort in ('', None, 'turbo'):
                with self.subTest(effort=effort):
                    argv = self.a.build({'effort': effort}, 'hi', first=True)['argv']
                    self.assertNotIn('--effort', argv)

    def test_never_selects_off(self):
        # A user picking a level is asking for reasoning, not for none.
        self.assertNotIn('off',
                         hs.DeepseekHarnessAdapter._EFFORT_NATIVE.values())

    def test_effort_mapping_covers_every_canonical_level(self):
        self.assertEqual(set(hs.DeepseekHarnessAdapter._EFFORT_NATIVE),
                         set(hs.EFFORT_LEVELS))

    def test_build_resets_per_turn_state(self):
        ctx = {'_emitted': True, '_raw': ['stale']}
        self.a.build(ctx, 'hi', first=True)
        self.assertFalse(ctx['_emitted'])
        self.assertEqual(ctx['_raw'], [])
        # The session id is per-THREAD, not per-turn — it must survive.
        ctx['dsh_session_id'] = 'keep-me'
        self.a.build(ctx, 'hi', first=False)
        self.assertEqual(ctx['dsh_session_id'], 'keep-me')


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.a = hs.DeepseekHarnessAdapter()
        self.ctx = {}
        self.a._reset_turn(self.ctx)

    def feed(self, obj):
        return self.a.parse(self.ctx, json.dumps(obj))

    def test_session_event_captures_the_id_and_renders_nothing(self):
        self.assertEqual(
            self.feed({'type': 'session', 'sessionId': 'abc-123'}), [])
        self.assertEqual(self.ctx['dsh_session_id'], 'abc-123')

    def test_message_becomes_assistant_text(self):
        self.assertEqual(self.feed({'type': 'message', 'text': 'Hello.'}),
                         [{'role': 'assistant', 'type': 'message',
                           'text': 'Hello.'}])
        self.assertTrue(self.ctx['_emitted'])

    def test_thought_is_rendered_rather_than_dropped(self):
        # Dropping it would remove the only visible sign a long turn is alive.
        self.assertEqual(self.feed({'type': 'thought', 'text': 'hmm'}),
                         [{'role': 'assistant', 'type': 'message', 'text': 'hmm'}])

    def test_blank_text_renders_nothing(self):
        self.assertEqual(self.feed({'type': 'message', 'text': '   '}), [])
        self.assertEqual(self.feed({'type': 'message'}), [])
        self.assertFalse(self.ctx['_emitted'])

    def test_tool_call_becomes_a_tool_card(self):
        self.assertEqual(
            self.feed({'type': 'tool_call', 'id': 't1', 'name': 'read_file',
                       'title': 'Read README.md', 'kind': 'read',
                       'input': {'path': 'README.md'}}),
            [{'role': 'assistant', 'type': 'tool_call', 'tool_id': 't1',
              'tool': {'name': 'read_file', 'input': {'path': 'README.md'}}}])

    def test_tool_call_falls_back_to_the_title_when_unnamed(self):
        # `name` is an unstable field in ACP; the title is always present.
        out = self.feed({'type': 'tool_call', 'id': 't2', 'title': 'Run tests'})
        self.assertEqual(out[0]['tool']['name'], 'Run tests')
        self.assertEqual(out[0]['tool']['input'], {})

    def test_non_dict_tool_input_degrades_to_empty(self):
        out = self.feed({'type': 'tool_call', 'id': 't3', 'name': 'x',
                         'input': 'not-a-dict'})
        self.assertEqual(out[0]['tool']['input'], {})

    def test_tool_result_carries_the_error_flag(self):
        self.assertEqual(
            self.feed({'type': 'tool_result', 'id': 't1', 'is_error': False,
                       'text': '# kube-coder'}),
            [{'role': 'system', 'type': 'tool_result', 'tool_use_id': 't1',
              'is_error': False, 'text': '# kube-coder'}])
        self.assertEqual(
            self.feed({'type': 'tool_result', 'id': 't9', 'is_error': True,
                       'text': 'exit 1'})[0]['is_error'], True)

    def test_error_surfaces_as_a_system_error(self):
        # The real shape when DEEPSEEK_API_KEY is missing or invalid.
        msg = ('Internal error: turn failed: Authentication Fails, '
               'Your api key: ****0000 is invalid')
        self.assertEqual(self.feed({'type': 'error', 'text': msg}),
                         [{'role': 'system', 'type': 'error', 'text': msg}])

    def test_error_without_text_still_says_something(self):
        self.assertEqual(self.feed({'type': 'error'}),
                         [{'role': 'system', 'type': 'error',
                           'text': 'deepseek-harness error'}])

    def test_usage_and_done_render_nothing(self):
        self.assertEqual(
            self.feed({'type': 'usage', 'used': 1200, 'size': 128000}), [])
        self.assertEqual(self.feed({'type': 'done', 'stopReason': 'end_turn'}), [])

    def test_a_settled_tool_only_turn_does_not_fall_back_to_raw_stdout(self):
        # A turn that ran tools and said nothing is legitimate. `done` marks it
        # handled so finalize() does not dump the raw stream as a "message".
        self.feed({'type': 'done', 'stopReason': 'end_turn'})
        self.assertTrue(self.ctx['_emitted'])
        self.assertEqual(self.a.finalize(self.ctx, 0), [])

    def test_unknown_event_types_are_ignored_not_fatal(self):
        # Forward compatibility: a bridge that grows a new event kind must not
        # blank the chat on an older adapter.
        self.assertEqual(self.feed({'type': 'plan_update', 'entries': []}), [])
        self.assertEqual(self.feed({}), [])

    def test_blank_and_malformed_lines_are_survived(self):
        self.assertEqual(self.a.parse(self.ctx, ''), [])
        self.assertEqual(self.a.parse(self.ctx, '   \n'), [])
        self.assertEqual(self.a.parse(self.ctx, '{not json'), [])
        self.assertEqual(self.a.parse(self.ctx, '[1, 2, 3]'), [])

    def test_a_full_turn_keeps_its_order(self):
        stream = [
            {'type': 'session', 'sessionId': 's1'},
            {'type': 'message', 'text': 'Let me look.'},
            {'type': 'tool_call', 'id': 't1', 'name': 'read_file',
             'input': {'path': 'README.md'}},
            {'type': 'tool_result', 'id': 't1', 'is_error': False,
             'text': '# kube-coder'},
            {'type': 'message', 'text': 'It is the readme.'},
            {'type': 'done', 'stopReason': 'end_turn'},
        ]
        events = []
        for obj in stream:
            events += self.feed(obj)
        self.assertEqual([(e['role'], e['type']) for e in events],
                         [('assistant', 'message'), ('assistant', 'tool_call'),
                          ('system', 'tool_result'), ('assistant', 'message')])
        self.assertEqual(self.a.finalize(self.ctx, 0), [])


class FinalizeTest(unittest.TestCase):
    def setUp(self):
        self.a = hs.DeepseekHarnessAdapter()

    def test_unrecognized_output_is_surfaced_not_lost(self):
        # A bridge/protocol change must never blank the chat.
        ctx = {}
        self.a.build(ctx, 'hi', first=True)
        self.a.parse(ctx, 'Traceback (most recent call last):')
        self.assertEqual(self.a.finalize(ctx, 1),
                         [{'role': 'assistant', 'type': 'message',
                           'text': 'Traceback (most recent call last):'}])

    def test_silent_nonzero_exit_becomes_an_error(self):
        ctx = {}
        self.a.build(ctx, 'hi', first=True)
        out = self.a.finalize(ctx, 1)
        self.assertEqual(out[0]['type'], 'error')
        self.assertIn('deepseek-harness', out[0]['text'])

    def test_an_error_event_is_not_followed_by_a_duplicate_exit_error(self):
        # The bridge exits 1 after reporting a turn failure; reporting it twice
        # would read as two separate problems.
        ctx = {}
        self.a.build(ctx, 'hi', first=True)
        self.a.parse(ctx, json.dumps({'type': 'error', 'text': 'no api key'}))
        self.assertEqual(self.a.finalize(ctx, 1), [])


class NonObjectJsonLineTest(unittest.TestCase):
    """A valid-JSON line that is not an object crashed every structured
    adapter before #639 — each `_parse_obj` indexes it as a mapping. The guard
    lives in the shared base, so assert it for all of them, not just ours."""

    def test_every_structured_adapter_survives_a_bare_array(self):
        for name in ('ante', 'opencode', 'codex', 'deepseek-harness'):
            with self.subTest(adapter=name):
                a = hs._ADAPTERS[name]
                ctx = {}
                a._reset_turn(ctx)
                self.assertEqual(a.parse(ctx, '[1, 2, 3]'), [])
                self.assertEqual(a.parse(ctx, '"just a string"'), [])
                self.assertEqual(a.parse(ctx, 'null'), [])
                # Still surfaced rather than silently swallowed.
                self.assertEqual(a.finalize(ctx, 0),
                                 [{'role': 'assistant', 'type': 'message',
                                   'text': '[1, 2, 3]\n"just a string"\nnull'}])


if __name__ == '__main__':
    unittest.main()
