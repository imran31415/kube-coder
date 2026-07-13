"""Unit tests for charts/workspace/hypervisor_session.py.

Covers the canonical-event normalization that the whole Hypervisor redesign
rests on — the Claude stream-json adapter and the fallback adapter — plus the
session's event append/read and turn bookkeeping. No live CLI is spawned.

Run with:    python3 -m unittest tests.hypervisor_session_test
(from charts/workspace/)
"""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import hypervisor_session as hs  # noqa: E402


class ClaudeAdapterParseTest(unittest.TestCase):
    def setUp(self):
        self.a = hs.ClaudeAdapter()
        self.ctx = {'workdir': '/home/dev', 'preamble': 'PRE'}

    def test_init_captures_session_id_and_emits_nothing(self):
        line = json.dumps({'type': 'system', 'subtype': 'init',
                           'session_id': 'sess-123'})
        out = self.a.parse(self.ctx, line)
        self.assertEqual(out, [])
        self.assertEqual(self.ctx['claude_session_id'], 'sess-123')

    def test_assistant_text_becomes_message(self):
        line = json.dumps({'type': 'assistant', 'message': {'content': [
            {'type': 'text', 'text': 'Everything is healthy.'}]}})
        out = self.a.parse(self.ctx, line)
        self.assertEqual(out, [{'role': 'assistant', 'type': 'message',
                                'text': 'Everything is healthy.'}])

    def test_tool_use_becomes_tool_call(self):
        line = json.dumps({'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 't1', 'name': 'Bash',
             'input': {'command': 'ps aux'}}]}})
        out = self.a.parse(self.ctx, line)
        self.assertEqual(out[0]['type'], 'tool_call')
        self.assertEqual(out[0]['tool']['name'], 'Bash')
        self.assertEqual(out[0]['tool_id'], 't1')

    def test_tool_result_becomes_tool_result(self):
        line = json.dumps({'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 't1',
             'content': [{'type': 'text', 'text': 'ok'}], 'is_error': False}]}})
        out = self.a.parse(self.ctx, line)
        self.assertEqual(out[0]['type'], 'tool_result')
        self.assertEqual(out[0]['tool_use_id'], 't1')
        self.assertIn('ok', out[0]['text'])

    def test_result_error_subtype_emits_error(self):
        line = json.dumps({'type': 'result', 'subtype': 'error_max_turns',
                           'session_id': 'sess-9'})
        out = self.a.parse(self.ctx, line)
        self.assertEqual(out[0]['type'], 'error')
        self.assertEqual(self.ctx['claude_session_id'], 'sess-9')

    def test_garbage_line_is_ignored(self):
        self.assertEqual(self.a.parse(self.ctx, 'not json at all'), [])

    def test_build_resumes_when_session_id_present(self):
        self.ctx['claude_session_id'] = 'sess-abc'
        spec = self.a.build(self.ctx, 'hello', first=False)
        self.assertIn('--resume', spec['argv'])
        self.assertIn('sess-abc', spec['argv'])
        # No preamble injection once resuming.
        self.assertNotIn('--append-system-prompt', spec['argv'])

    def test_build_first_turn_appends_preamble(self):
        spec = self.a.build(self.ctx, 'hello', first=True)
        self.assertIn('--append-system-prompt', spec['argv'])
        self.assertIn('--permission-mode', spec['argv'])
        self.assertIn('bypassPermissions', spec['argv'])

    def test_build_uses_minimal_strict_mcp_config(self):
        spec = self.a.build(self.ctx, 'hello', first=True)
        self.assertIn('--mcp-config', spec['argv'])
        self.assertIn('--strict-mcp-config', spec['argv'])
        cfg = json.loads(spec['argv'][spec['argv'].index('--mcp-config') + 1])
        self.assertEqual(set(cfg['mcpServers']), {'dashboard', 'memory'})

    def test_build_forces_home_and_drops_api_key(self):
        spec = self.a.build(self.ctx, 'hello', first=True)
        self.assertEqual(spec['env']['HOME'], hs.WORKSPACE_HOME)
        self.assertNotIn('ANTHROPIC_API_KEY', spec['env'])


class FallbackAdapterTest(unittest.TestCase):
    def test_strips_ansi_and_emits_message(self):
        a = hs.FallbackAdapter()
        out = a.finalize_buffered({}, 0, '\x1b[32mhello\x1b[0m world')
        self.assertEqual(out, [{'role': 'assistant', 'type': 'message',
                                'text': 'hello world'}])

    def test_nonzero_exit_no_output_is_error(self):
        a = hs.FallbackAdapter()
        out = a.finalize_buffered({'assistant': 'ante'}, 1, '')
        self.assertEqual(out[0]['type'], 'error')


class SessionEventsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = hs.HYPERVISOR_DIR
        hs.HYPERVISOR_DIR = self.tmp

    def tearDown(self):
        hs.HYPERVISOR_DIR = self._orig

    def test_create_append_read_roundtrip(self):
        s = hs.HypervisorSession.create(
            assistant='claude', workdir='/home/dev', cli_cmd='claude',
            preamble='PRE', title='hi there')
        self.assertEqual(s.read_meta()['adapter_kind'], 'claude')
        s._append([{'role': 'user', 'type': 'message', 'text': 'one'}])
        s._append([{'role': 'assistant', 'type': 'message', 'text': 'two'}])
        evs = s.read_events()
        self.assertEqual([e['seq'] for e in evs], [1, 2])
        self.assertEqual(evs[1]['text'], 'two')
        # since-cursor returns only newer events.
        self.assertEqual(len(s.read_events(since_seq=1)), 1)

    def test_assistant_adapter_routing(self):
        cases = {'claude': 'claude', 'ante': 'ante',
                 'opencode-openrouter': 'opencode', 'opencode-deepseek': 'opencode',
                 'librefang': 'fallback', 'kc-harness': 'fallback'}
        for assistant, kind in cases.items():
            s = hs.HypervisorSession.create(
                assistant=assistant, workdir='/home/dev', cli_cmd=assistant,
                preamble='', title='x')
            self.assertEqual(s.read_meta()['adapter_kind'], kind, assistant)


class AnteAdapterTest(unittest.TestCase):
    def setUp(self):
        self.a = hs.AnteAdapter()

    def test_agentmessage_becomes_assistant_text(self):
        ctx = {'workdir': '/home/dev'}
        self.a._reset_turn(ctx)
        # SessionStart captures the resumable id, emits nothing.
        self.assertEqual(self.a.parse(ctx, json.dumps(
            {'event': {'SessionStart': {'session_id': 'ses_1'}}})), [])
        self.assertEqual(ctx['ante_session_id'], 'ses_1')
        # AgentMessage inner is the bare text string.
        out = self.a.parse(ctx, json.dumps({'event': {'AgentMessage': 'PONG'}}))
        self.assertEqual(out, [{'role': 'assistant', 'type': 'message', 'text': 'PONG'}])

    def test_noise_ignored_and_resume_flag(self):
        ctx = {'ante_session_id': 'ses_9', 'workdir': '/home/dev'}
        for noise in ('TurnStart', 'UsageUpdate', 'ExtensionRefreshed'):
            self.assertEqual(self.a.parse(ctx, json.dumps({'event': {noise: {}}})), [])
        spec = self.a.build(ctx, 'hi', first=False)
        self.assertIn('-r', spec['argv'])
        self.assertIn('ses_9', spec['argv'])
        self.assertIn('--output-format', spec['argv'])

    def test_raw_fallback_when_no_structured_events(self):
        ctx = {}
        self.a.build(ctx, 'hi', first=True)       # resets per-turn state
        self.a.parse(ctx, 'plain non-json line')  # accumulates raw
        out = self.a.finalize(ctx, 0)
        self.assertEqual(out, [{'role': 'assistant', 'type': 'message',
                                'text': 'plain non-json line'}])


class OpencodeAdapterTest(unittest.TestCase):
    def setUp(self):
        self.a = hs.OpencodeAdapter()

    def test_text_event_part(self):
        ctx = {}
        self.a._reset_turn(ctx)
        out = self.a.parse(ctx, json.dumps(
            {'type': 'text', 'sessionID': 'ses_x', 'part': {'text': 'PONG'}}))
        self.assertEqual(out, [{'role': 'assistant', 'type': 'message', 'text': 'PONG'}])
        self.assertEqual(ctx['opencode_session_id'], 'ses_x')

    def test_step_events_are_noise(self):
        ctx = {}
        self.a._reset_turn(ctx)
        self.assertEqual(self.a.parse(ctx, json.dumps({'type': 'step_start', 'part': {}})), [])
        self.assertEqual(self.a.parse(ctx, json.dumps({'type': 'step_finish', 'part': {}})), [])

    def test_error_event_surfaces_provider_message(self):
        ctx = {}
        self.a._reset_turn(ctx)
        out = self.a.parse(ctx, json.dumps(
            {'type': 'error', 'error': {'name': 'APIError',
                                        'data': {'message': 'Key limit exceeded'}}}))
        self.assertEqual(out, [{'role': 'system', 'type': 'error', 'text': 'Key limit exceeded'}])

    def test_build_model_from_cli_cmd_and_resume(self):
        ctx = {'cli_cmd': "opencode --model 'openrouter/anthropic/claude-sonnet-4'"}
        spec = self.a.build(ctx, 'hi', first=True)
        self.assertEqual(spec['argv'][:4], ['opencode', 'run', 'hi', '--format'])
        self.assertIn('openrouter/anthropic/claude-sonnet-4', spec['argv'])
        ctx['opencode_session_id'] = 'ses_x'
        self.assertIn('-s', self.a.build(ctx, 'again', first=False)['argv'])


class StopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = hs.HYPERVISOR_DIR
        hs.HYPERVISOR_DIR = self.tmp
        self.s = hs.HypervisorSession.create(
            assistant='claude', workdir='/home/dev', cli_cmd='claude',
            preamble='', title='x')

    def tearDown(self):
        hs.HYPERVISOR_DIR = self._orig
        with hs._RUNLOCK:
            hs._RUNNING.pop(self.s.id, None)
            hs._PROCS.pop(self.s.id, None)
            hs._STOPPING.discard(self.s.id)

    def test_stop_on_idle_thread_is_noop(self):
        self.assertFalse(self.s.stop())
        self.assertFalse(self.s._stop_requested())

    def test_stop_kills_running_process_group(self):
        import subprocess
        proc = subprocess.Popen(['sleep', '30'], start_new_session=True)
        with hs._RUNLOCK:
            hs._RUNNING[self.s.id] = True
            hs._PROCS[self.s.id] = proc
        try:
            self.assertTrue(self.s.stop())
            self.assertTrue(self.s._stop_requested())
            self.assertIsNotNone(proc.poll())  # actually terminated
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_stop_flag_set_before_process_registered(self):
        # Turn is marked running but its Popen hasn't landed yet — stop() should
        # still flag it so the runner skips finalize once the process appears.
        with hs._RUNLOCK:
            hs._RUNNING[self.s.id] = True
        self.assertTrue(self.s.stop())
        self.assertTrue(self.s._stop_requested())


if __name__ == '__main__':
    unittest.main()
