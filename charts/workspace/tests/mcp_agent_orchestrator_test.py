"""Unit tests for charts/workspace/mcp_agent_orchestrator.py.

Covers the pure logic that's easy to get wrong and has no other safety net:
  * Headless vs interactive command construction per assistant
  * Skip-approval flags on headless invocations
  * Resource guards (depth + concurrency) refusing to spawn
  * Status reconciliation distinguishing a crash from a clean finish
  * Shell quoting + env parsing helpers

Real tmux isn't available here, so subprocess.run is patched per-test and
TASKS_DIR is redirected into a tempdir.

Run with:    python3 -m unittest tests.mcp_agent_orchestrator_test
(from charts/workspace/)
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

# Import mcp_agent_orchestrator.py from the parent directory.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import mcp_agent_orchestrator as orch  # noqa: E402


def _tmux_alive(*args, **kwargs):
    """subprocess.run stub: every tmux op succeeds; sessions are alive."""
    return mock.Mock(returncode=0, stdout='', stderr='')


class AssistantCommandTests(unittest.TestCase):
    def test_headless_claude_has_print_and_skip_flags(self):
        cmd = orch._assistant_command('claude', 'fix the bug', headless=True)
        self.assertIn('claude', cmd)
        self.assertIn('-p', cmd)
        self.assertIn('--dangerously-skip-permissions', cmd)
        self.assertIn("'fix the bug'", cmd)

    def test_headless_ante_has_print_and_yolo(self):
        cmd = orch._assistant_command('ante', 'write tests', headless=True)
        self.assertIn('ante', cmd)
        self.assertIn('-p', cmd)
        self.assertIn('--yolo', cmd)
        self.assertIn("'write tests'", cmd)

    def test_headless_librefang_ensures_daemon_then_messages(self):
        cmd = orch._assistant_command('librefang', 'write tests', headless=True)
        # One-shot mode is `librefang message <agent> <prompt>` …
        self.assertIn('librefang message coder', cmd)
        self.assertIn("'write tests'", cmd)
        # … which needs the daemon: the command bootstraps it when down.
        self.assertIn('librefang status', cmd)
        self.assertIn('librefang start', cmd)

    def test_headless_opencode_uses_run_subcommand(self):
        cmd = orch._assistant_command('opencode-openrouter', 'hi', headless=True)
        self.assertIn('opencode run', cmd)
        self.assertIn('--model', cmd)
        self.assertIn('openrouter/', cmd)

    def test_interactive_claude_is_bare_repl(self):
        cmd = orch._assistant_command('claude', 'ignored', headless=False)
        self.assertEqual(cmd, 'claude')
        self.assertNotIn('-p', cmd)

    def test_interactive_ante_is_bare_repl(self):
        self.assertEqual(orch._assistant_command('ante', 'x', headless=False), 'ante')

    def test_interactive_librefang_is_chat_repl(self):
        self.assertEqual(
            orch._assistant_command('librefang', 'x', headless=False),
            'librefang chat coder',
        )

    def test_librefang_agent_env_override_is_quoted(self):
        os.environ['KC_LIBREFANG_AGENT'] = 'my agent'
        try:
            self.assertEqual(
                orch._assistant_command('librefang', 'x', headless=False),
                "librefang chat 'my agent'",
            )
        finally:
            os.environ.pop('KC_LIBREFANG_AGENT', None)

    def test_kc_harness_never_headless(self):
        # kc-harness has no headless interface — even when headless is
        # requested it must fall back to the interactive command.
        cmd = orch._assistant_command('kc-harness', 'x', headless=True)
        self.assertIn('harness.py', cmd)
        self.assertNotIn('-p', cmd)

    def test_unknown_assistant_defaults_to_claude(self):
        self.assertEqual(orch._assistant_command('bogus', 'x', headless=False), 'claude')


class HelperTests(unittest.TestCase):
    def test_shell_quote_passes_safe_tokens(self):
        self.assertEqual(orch._shell_quote('a/b_c-1.2'), 'a/b_c-1.2')

    def test_shell_quote_escapes_single_quote(self):
        quoted = orch._shell_quote("it's")
        # Must survive round-trip through a shell.
        self.assertEqual(quoted, "'it'\\''s'")

    def test_shell_quote_empty(self):
        self.assertEqual(orch._shell_quote(''), "''")

    def test_int_env_default_and_override(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('KC_TEST_INT', None)
            self.assertEqual(orch._int_env('KC_TEST_INT', 7), 7)
            os.environ['KC_TEST_INT'] = '12'
            self.assertEqual(orch._int_env('KC_TEST_INT', 7), 12)
            os.environ['KC_TEST_INT'] = 'not-a-number'
            self.assertEqual(orch._int_env('KC_TEST_INT', 7), 7)


class SpawnGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._patch = mock.patch.object(orch, 'TASKS_DIR', self.tmp)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        # Default to depth 0 unless a test overrides it.
        os.environ.pop('KC_AGENT_DEPTH', None)

    def test_refuses_beyond_max_depth(self):
        os.environ['KC_AGENT_DEPTH'] = str(orch.MAX_SPAWN_DEPTH)
        self.addCleanup(lambda: os.environ.pop('KC_AGENT_DEPTH', None))
        res = orch._tool_spawn_agent({'prompt': 'go', 'assistant': 'ante'})
        self.assertTrue(res.get('isError'))
        self.assertIn('depth', res['content'][0]['text'])

    def test_refuses_at_max_concurrency(self):
        with mock.patch.object(orch, '_count_live_agent_sessions',
                               return_value=orch.MAX_CONCURRENT_SUBAGENTS):
            res = orch._tool_spawn_agent({'prompt': 'go', 'assistant': 'ante'})
        self.assertTrue(res.get('isError'))
        self.assertIn('already running', res['content'][0]['text'])

    def test_empty_prompt_rejected(self):
        res = orch._tool_spawn_agent({'prompt': '   '})
        self.assertTrue(res.get('isError'))

    def test_headless_spawn_writes_meta_and_no_paste(self):
        with mock.patch.object(orch.subprocess, 'run', side_effect=_tmux_alive), \
             mock.patch.object(orch, '_count_live_agent_sessions', return_value=0), \
             mock.patch.object(orch.threading, 'Thread') as thread_cls:
            res = orch._tool_spawn_agent(
                {'prompt': 'do work', 'assistant': 'ante', 'parent_task_id': 'p1'})
        self.assertFalse(res.get('isError'))
        payload = json.loads(res['content'][0]['text'])
        self.assertEqual(payload['mode'], 'headless')
        # Headless must not start the prompt-paste thread.
        thread_cls.assert_not_called()
        # Meta persisted with lineage + depth.
        meta_path = os.path.join(self.tmp, payload['task_id'], 'task.json')
        with open(meta_path) as f:
            meta = json.load(f)
        self.assertEqual(meta['mode'], 'headless')
        self.assertEqual(meta['parent_task_id'], 'p1')
        self.assertEqual(meta['depth'], 1)

    def test_interactive_spawn_starts_paste_thread(self):
        with mock.patch.object(orch.subprocess, 'run', side_effect=_tmux_alive), \
             mock.patch.object(orch, '_count_live_agent_sessions', return_value=0), \
             mock.patch.object(orch.threading, 'Thread') as thread_cls:
            res = orch._tool_spawn_agent(
                {'prompt': 'do work', 'assistant': 'ante', 'mode': 'interactive'})
        payload = json.loads(res['content'][0]['text'])
        self.assertEqual(payload['mode'], 'interactive')
        thread_cls.assert_called_once()

    def _spawn_workdir(self, args):
        with mock.patch.object(orch.subprocess, 'run', side_effect=_tmux_alive), \
             mock.patch.object(orch, '_count_live_agent_sessions', return_value=0), \
             mock.patch.object(orch.threading, 'Thread'):
            res = orch._tool_spawn_agent({'prompt': 'go', 'assistant': 'ante', **args})
        meta_path = os.path.join(self.tmp, json.loads(res['content'][0]['text'])['task_id'], 'task.json')
        with open(meta_path) as f:
            return json.load(f)

    def test_workdir_defaults_to_spawning_agent_cwd(self):
        # No workdir passed → inherit the orchestrator's cwd (the parent's dir).
        meta = self._spawn_workdir({})
        self.assertEqual(meta['workdir'], os.getcwd())

    def test_explicit_existing_workdir_is_used(self):
        meta = self._spawn_workdir({'workdir': self.tmp})
        self.assertEqual(meta['workdir'], self.tmp)

    def test_nonexistent_workdir_falls_back(self):
        meta = self._spawn_workdir({'workdir': '/no/such/dir/xyz'})
        self.assertEqual(meta['workdir'], '/home/dev')


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.task_dir = os.path.join(self.tmp, 't1')
        os.makedirs(self.task_dir)

    def _write_meta(self, **extra):
        meta = {'task_id': 't1', 'status': 'running', 'tmux_session': 'claude-t1'}
        meta.update(extra)
        with open(os.path.join(self.task_dir, 'task.json'), 'w') as f:
            json.dump(meta, f)
        return meta

    def test_clean_exit_marks_completed(self):
        meta = self._write_meta()
        with open(orch._exit_code_path(self.task_dir), 'w') as f:
            f.write('0\n')
        with mock.patch.object(orch.subprocess, 'run',
                               return_value=mock.Mock(returncode=1, stdout='', stderr='')):
            orch._reconcile_status(meta, self.task_dir)
        self.assertEqual(meta['status'], 'completed')
        self.assertEqual(meta['exit_code'], 0)

    def test_nonzero_exit_marks_error(self):
        meta = self._write_meta()
        with open(orch._exit_code_path(self.task_dir), 'w') as f:
            f.write('1\n')
        with mock.patch.object(orch.subprocess, 'run',
                               return_value=mock.Mock(returncode=1, stdout='', stderr='')):
            orch._reconcile_status(meta, self.task_dir)
        self.assertEqual(meta['status'], 'error')
        self.assertEqual(meta['exit_code'], 1)

    def test_live_session_left_running(self):
        meta = self._write_meta()
        with mock.patch.object(orch.subprocess, 'run', side_effect=_tmux_alive):
            orch._reconcile_status(meta, self.task_dir)
        self.assertEqual(meta['status'], 'running')


if __name__ == '__main__':
    unittest.main()
