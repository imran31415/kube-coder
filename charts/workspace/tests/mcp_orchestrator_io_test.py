"""Unit tests for mcp_agent_orchestrator.py I/O helpers, tool handlers and
JSON-RPC dispatch — the parts not exercised by mcp_agent_orchestrator_test.py.

Covers:
  * meta read/write round-trip + corruption tolerance
  * output-log reader (tail / full / truncation / missing)
  * exit-code reader, session liveness, live-session counting
  * lineage append (dedupe + missing-parent no-op)
  * model/agent env resolution
  * get_status / get_output / list_subagents / wait_for_agent / kill tools
  * initialize / list-tools / call-tool dispatch incl. error paths and main()

tmux/subprocess are patched per-test; TASKS_DIR is redirected to a tempdir.

Run with:    python3 -m unittest tests.mcp_orchestrator_io_test
(from charts/workspace/)
"""

import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import mcp_agent_orchestrator as orch  # noqa: E402


def _proc(returncode=0, stdout='', stderr=''):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


class TasksDirTestCase(unittest.TestCase):
    """Redirect TASKS_DIR to an isolated tempdir for every test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        p = mock.patch.object(orch, 'TASKS_DIR', self.tmp)
        p.start()
        self.addCleanup(p.stop)

    def make_task(self, task_id, **meta):
        task_dir = os.path.join(self.tmp, task_id)
        os.makedirs(task_dir, exist_ok=True)
        m = {'task_id': task_id, 'status': 'running'}
        m.update(meta)
        with open(os.path.join(task_dir, 'task.json'), 'w') as f:
            json.dump(m, f)
        return task_dir


class MetaIoTests(TasksDirTestCase):
    def test_write_then_read_round_trips(self):
        d = os.path.join(self.tmp, 't')
        os.makedirs(d)
        self.assertTrue(orch._write_meta(d, {'task_id': 't', 'status': 'running'}))
        self.assertEqual(orch._read_meta(d)['status'], 'running')

    def test_read_missing_returns_none(self):
        self.assertIsNone(orch._read_meta(os.path.join(self.tmp, 'nope')))

    def test_read_corrupt_json_returns_none(self):
        d = os.path.join(self.tmp, 'bad')
        os.makedirs(d)
        with open(os.path.join(d, 'task.json'), 'w') as f:
            f.write('{not json')
        self.assertIsNone(orch._read_meta(d))

    def test_write_meta_failure_returns_false(self):
        # Target dir does not exist → open() raises OSError → False.
        self.assertFalse(orch._write_meta('/no/such/dir', {'x': 1}))


class ReadOutputTests(TasksDirTestCase):
    def _log(self, task_id, text):
        d = self.make_task(task_id)
        with open(os.path.join(d, 'output.log'), 'w') as f:
            f.write(text)
        return d

    def test_missing_log(self):
        d = self.make_task('t')
        self.assertEqual(orch._read_output(d), '(no output available)')

    def test_full_read(self):
        d = self._log('t', 'line1\nline2\n')
        self.assertEqual(orch._read_output(d), 'line1\nline2\n')

    def test_tail_returns_last_n_lines(self):
        d = self._log('t', 'a\nb\nc\nd\n')
        self.assertEqual(orch._read_output(d, tail=2), 'c\nd\n')

    def test_truncates_oversized_full_read(self):
        d = self._log('t', 'X' * 5000)
        with mock.patch.object(orch, 'MAX_OUTPUT_BYTES', 100):
            out = orch._read_output(d)
        self.assertTrue(out.startswith('(…output truncated…)'))
        self.assertIn('X', out)


class ExitCodeAndSessionTests(TasksDirTestCase):
    def test_read_exit_code_valid(self):
        d = self.make_task('t')
        with open(orch._exit_code_path(d), 'w') as f:
            f.write('0\n')
        self.assertEqual(orch._read_exit_code(d), 0)

    def test_read_exit_code_missing_or_garbage(self):
        d = self.make_task('t')
        self.assertIsNone(orch._read_exit_code(d))
        with open(orch._exit_code_path(d), 'w') as f:
            f.write('not-an-int')
        self.assertIsNone(orch._read_exit_code(d))

    def test_session_is_alive_true_false(self):
        with mock.patch.object(orch.subprocess, 'run', return_value=_proc(0)):
            self.assertTrue(orch._session_is_alive('claude-x'))
        with mock.patch.object(orch.subprocess, 'run', return_value=_proc(1)):
            self.assertFalse(orch._session_is_alive('claude-x'))

    def test_count_live_agent_sessions_counts_claude_prefix(self):
        out = 'claude-a\nclaude-b\nother\nmysess\n'
        with mock.patch.object(orch.subprocess, 'run', return_value=_proc(0, out)):
            self.assertEqual(orch._count_live_agent_sessions(), 2)

    def test_count_live_agent_sessions_none_when_tmux_fails(self):
        with mock.patch.object(orch.subprocess, 'run', return_value=_proc(1)):
            self.assertEqual(orch._count_live_agent_sessions(), 0)


class AppendSubTaskTests(TasksDirTestCase):
    def test_appends_and_dedupes(self):
        self.make_task('parent')
        orch._append_sub_task_id('parent', 'child1')
        orch._append_sub_task_id('parent', 'child1')  # dup ignored
        orch._append_sub_task_id('parent', 'child2')
        meta = orch._read_meta(os.path.join(self.tmp, 'parent'))
        self.assertEqual(meta['sub_task_ids'], ['child1', 'child2'])

    def test_missing_parent_is_noop(self):
        # Must not raise when the parent task dir doesn't exist.
        orch._append_sub_task_id('ghost', 'child')


class ModelResolutionTests(unittest.TestCase):
    def test_opencode_deepseek_model(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('KC_DEEPSEEK_MODEL', None)
            self.assertEqual(orch._opencode_model('opencode-deepseek'), 'deepseek/deepseek-chat')
            os.environ['KC_DEEPSEEK_MODEL'] = 'deepseek-coder'
            self.assertEqual(orch._opencode_model('opencode-deepseek'), 'deepseek/deepseek-coder')

    def test_opencode_openrouter_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('KC_OPENROUTER_MODEL', None)
            self.assertTrue(orch._opencode_model('opencode-openrouter').startswith('openrouter/'))

    def test_librefang_agent_default_and_override(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('KC_LIBREFANG_AGENT', None)
            self.assertEqual(orch._librefang_agent(), 'coder')
            os.environ['KC_LIBREFANG_AGENT'] = 'custom'
            self.assertEqual(orch._librefang_agent(), 'custom')


class GetStatusToolTests(TasksDirTestCase):
    def test_missing_task_id(self):
        self.assertTrue(orch._tool_get_agent_status({}).get('isError'))

    def test_not_found(self):
        res = orch._tool_get_agent_status({'task_id': 'ghost'})
        self.assertTrue(res.get('isError'))
        self.assertIn('not found', res['content'][0]['text'])

    def test_success_payload(self):
        self.make_task('t', status='completed', assistant='ante', mode='headless', exit_code=0)
        res = orch._tool_get_agent_status({'task_id': 't'})
        self.assertFalse(res.get('isError'))
        payload = json.loads(res['content'][0]['text'])
        self.assertEqual(payload['status'], 'completed')
        self.assertEqual(payload['assistant'], 'ante')


class GetOutputToolTests(TasksDirTestCase):
    def test_missing_and_not_found(self):
        self.assertTrue(orch._tool_get_agent_output({}).get('isError'))
        self.assertTrue(orch._tool_get_agent_output({'task_id': 'ghost'}).get('isError'))

    def test_returns_output(self):
        d = self.make_task('t', status='completed')
        with open(os.path.join(d, 'output.log'), 'w') as f:
            f.write('hello\n')
        res = orch._tool_get_agent_output({'task_id': 't'})
        payload = json.loads(res['content'][0]['text'])
        self.assertEqual(payload['output'], 'hello\n')


class ListSubagentsToolTests(TasksDirTestCase):
    def test_missing_parent(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('KC_TASK_ID', None)
            self.assertTrue(orch._tool_list_subagents({}).get('isError'))

    def test_filters_by_parent(self):
        self.make_task('c1', parent_task_id='P', status='completed')
        self.make_task('c2', parent_task_id='other', status='completed')
        self.make_task('c3', parent_task_id='P', status='completed')
        res = orch._tool_list_subagents({'parent_task_id': 'P'})
        payload = json.loads(res['content'][0]['text'])
        self.assertEqual(payload['count'], 2)
        ids = {t['task_id'] for t in payload['sub_tasks']}
        self.assertEqual(ids, {'c1', 'c3'})


class WaitForAgentToolTests(TasksDirTestCase):
    def test_missing_task_id(self):
        self.assertTrue(orch._tool_wait_for_agent({}).get('isError'))

    def test_returns_immediately_when_already_done(self):
        d = self.make_task('t', status='completed')
        with open(os.path.join(d, 'output.log'), 'w') as f:
            f.write('done\n')
        res = orch._tool_wait_for_agent({'task_id': 't'})
        payload = json.loads(res['content'][0]['text'])
        self.assertEqual(payload['status'], 'completed')

    def test_timeout_path(self):
        self.make_task('t', status='running')
        # timeout=0 → loop body never runs → timeout payload.
        res = orch._tool_wait_for_agent({'task_id': 't', 'timeout': 0.0})
        payload = json.loads(res['content'][0]['text'])
        self.assertEqual(payload['status'], 'timeout')


class KillAgentToolTests(TasksDirTestCase):
    def test_missing_and_not_found(self):
        self.assertTrue(orch._tool_kill_agent({}).get('isError'))
        self.assertTrue(orch._tool_kill_agent({'task_id': 'ghost'}).get('isError'))

    def test_kills_and_marks_meta(self):
        self.make_task('t', status='running', tmux_session='claude-t')
        with mock.patch.object(orch.subprocess, 'run', return_value=_proc(0)) as run:
            res = orch._tool_kill_agent({'task_id': 't'})
        run.assert_called_once()
        self.assertEqual(json.loads(res['content'][0]['text'])['status'], 'killed')
        self.assertEqual(orch._read_meta(os.path.join(self.tmp, 't'))['status'], 'killed')


class DispatchTests(TasksDirTestCase):
    def test_initialize_sends_server_info(self):
        with mock.patch.object(orch, '_send') as send:
            orch._handle_initialize(1, {})
        result = send.call_args[0][0]['result']
        self.assertEqual(result['serverInfo']['name'], 'agent-orchestrator')

    def test_list_tools_returns_schemas(self):
        with mock.patch.object(orch, '_send') as send:
            orch._handle_list_tools(2, {})
        tools = send.call_args[0][0]['result']['tools']
        names = {t['name'] for t in tools}
        self.assertIn('spawn_agent', names)

    def test_call_tool_rejects_non_dict_params(self):
        with mock.patch.object(orch, '_error') as err:
            orch._handle_call_tool(3, 'not-a-dict')
        self.assertEqual(err.call_args[0][1], -32602)

    def test_call_tool_unknown_tool(self):
        with mock.patch.object(orch, '_error') as err:
            orch._handle_call_tool(4, {'name': 'bogus', 'arguments': {}})
        self.assertEqual(err.call_args[0][1], -32601)

    def test_call_tool_dispatches_and_wraps_handler_exception(self):
        boom = mock.Mock(side_effect=RuntimeError('kaboom'))
        with mock.patch.dict(orch.TOOLS, {'x': {'handler': boom, 'schema': {'name': 'x'}}}), \
             mock.patch.object(orch, '_send') as send:
            orch._handle_call_tool(5, {'name': 'x', 'arguments': {}})
        result = send.call_args[0][0]['result']
        self.assertTrue(result['isError'])
        self.assertIn('kaboom', result['content'][0]['text'])

    def test_call_tool_success_passes_through(self):
        ok = mock.Mock(return_value={'content': [{'type': 'text', 'text': 'ok'}]})
        with mock.patch.dict(orch.TOOLS, {'x': {'handler': ok, 'schema': {'name': 'x'}}}), \
             mock.patch.object(orch, '_send') as send:
            orch._handle_call_tool(6, {'name': 'x', 'arguments': {'a': 1}})
        ok.assert_called_once_with({'a': 1})
        self.assertEqual(send.call_args[0][0]['result']['content'][0]['text'], 'ok')


class MainLoopTests(unittest.TestCase):
    def _run_main(self, lines):
        stdin = io.StringIO(''.join(line + '\n' for line in lines))
        with mock.patch.object(sys, 'stdin', stdin), \
             mock.patch.object(orch, '_send') as send, \
             mock.patch.object(orch, '_error') as err:
            rc = orch.main()
        return rc, send, err

    def test_dispatches_known_method(self):
        rc, send, err = self._run_main([json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})])
        self.assertEqual(rc, 0)
        send.assert_called()
        err.assert_not_called()

    def test_unknown_method_errors(self):
        _, _, err = self._run_main([json.dumps({'id': 9, 'method': 'nope'})])
        self.assertEqual(err.call_args[0][1], -32601)

    def test_blank_and_invalid_lines_skipped(self):
        rc, send, err = self._run_main(['', '   ', 'not json', '[1,2,3]'])
        self.assertEqual(rc, 0)
        send.assert_not_called()
        err.assert_not_called()


if __name__ == '__main__':
    unittest.main()
