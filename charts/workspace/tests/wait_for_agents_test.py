"""Unit tests for the parallel wait_for_agents fan-in barrier (issue #109).

Covers the new orchestrator tool: waits on a list of task_ids, returns when
all are terminal (or on timeout) with per-task results, and treats unknown ids
as terminal rather than fatal.

TASKS_DIR is redirected to a tempdir; tmux is stubbed; time.sleep is patched.

Run with:    python3 -m unittest tests.wait_for_agents_test
(from charts/workspace/)
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import mcp_agent_orchestrator as orch  # noqa: E402


def _tmux_dead(*args, **kwargs):
    argv = args[0] if args else kwargs.get('args', [])
    if len(argv) >= 2 and argv[0] == 'tmux' and argv[1] == 'has-session':
        return mock.Mock(returncode=1, stdout='', stderr='')
    return mock.Mock(returncode=0, stdout='', stderr='')


class WaitForAgentsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kcwfa-')
        p = mock.patch.object(orch, 'TASKS_DIR', self.tmp)
        p.start()
        self.addCleanup(p.stop)
        s = mock.patch.object(orch.time, 'sleep')
        s.start()
        self.addCleanup(s.stop)

    def _task(self, tid, status='completed', **extra):
        d = os.path.join(self.tmp, tid)
        os.makedirs(d, exist_ok=True)
        meta = {'task_id': tid, 'status': status}
        meta.update(extra)
        with open(os.path.join(d, 'task.json'), 'w') as f:
            json.dump(meta, f)

    def _payload(self, res):
        return json.loads(res['content'][0]['text'])

    def test_requires_non_empty_list(self):
        self.assertTrue(orch._tool_wait_for_agents({}).get('isError'))
        self.assertTrue(orch._tool_wait_for_agents({'task_ids': []}).get('isError'))
        self.assertTrue(orch._tool_wait_for_agents({'task_ids': 'x'}).get('isError'))

    def test_all_terminal_returns_immediately(self):
        self._task('a', status='completed', exit_code=0)
        self._task('b', status='error', exit_code=1)
        with mock.patch.object(orch.subprocess, 'run', side_effect=_tmux_dead):
            res = orch._tool_wait_for_agents({'task_ids': ['a', 'b']})
        p = self._payload(res)
        self.assertTrue(p['all_complete'])
        self.assertEqual(p['count'], 2)
        by_id = {r['task_id']: r for r in p['results']}
        self.assertEqual(by_id['a']['status'], 'completed')
        self.assertEqual(by_id['b']['exit_code'], 1)
        self.assertIn('output', by_id['a'])

    def test_unknown_id_is_not_fatal(self):
        self._task('a', status='completed')
        with mock.patch.object(orch.subprocess, 'run', side_effect=_tmux_dead):
            res = orch._tool_wait_for_agents({'task_ids': ['a', 'ghost']})
        p = self._payload(res)
        self.assertTrue(p['all_complete'])
        by_id = {r['task_id']: r for r in p['results']}
        self.assertEqual(by_id['ghost']['status'], 'not-found')

    def test_timeout_reports_pending(self):
        # A running task whose tmux session is alive stays running.
        self._task('a', status='running', tmux_session='claude-a')
        self._task('b', status='completed')
        with mock.patch.object(
                orch.subprocess, 'run',
                return_value=mock.Mock(returncode=0, stdout='still working', stderr='')):
            res = orch._tool_wait_for_agents({'task_ids': ['a', 'b'], 'timeout': 0})
        p = self._payload(res)
        self.assertFalse(p['all_complete'])
        self.assertEqual(p['timed_out'], ['a'])

    def test_dead_running_task_reconciles_to_complete(self):
        # Running per meta, but tmux session is gone → reconciles to completed.
        self._task('a', status='running', tmux_session='claude-a')
        with mock.patch.object(orch.subprocess, 'run', side_effect=_tmux_dead):
            res = orch._tool_wait_for_agents({'task_ids': ['a']})
        p = self._payload(res)
        self.assertTrue(p['all_complete'])
        self.assertEqual(p['results'][0]['status'], 'completed')

    def test_registered_in_tools(self):
        self.assertIn('wait_for_agents', orch.TOOLS)
        self.assertEqual(orch.TOOLS['wait_for_agents']['schema']['name'], 'wait_for_agents')


if __name__ == '__main__':
    unittest.main()
