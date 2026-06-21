"""Unit tests for the concurrent-task ceiling (issue #98).

Covers ClaudeTaskManager.count_live_tasks / at_capacity / _capacity_rejection
and that create_task / create_terminal_task refuse to spawn (returning a
`rejected` meta, leaving no task dir) once the live-task ceiling is reached.

Run with:    python3 -m unittest tests.task_capacity_test
(from charts/workspace/)
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402

CTM = server.ClaudeTaskManager


class CountLiveTasksTests(unittest.TestCase):
    def test_counts_only_kube_coder_sessions(self):
        out = 'kube-coder-a\nkube-coder-b\nclaude-agent1\nmysess\n'
        with mock.patch.object(server.subprocess, 'run',
                               return_value=mock.Mock(returncode=0, stdout=out, stderr='')):
            self.assertEqual(CTM.count_live_tasks(), 2)

    def test_zero_when_tmux_unavailable(self):
        with mock.patch.object(server.subprocess, 'run',
                               return_value=mock.Mock(returncode=1, stdout='', stderr='')):
            self.assertEqual(CTM.count_live_tasks(), 0)


class AtCapacityTests(unittest.TestCase):
    def test_at_capacity_boundary(self):
        with mock.patch.object(CTM, 'MAX_TASKS', 3):
            with mock.patch.object(CTM, 'count_live_tasks', return_value=2):
                at_cap, live, cap = CTM.at_capacity()
                self.assertFalse(at_cap)
                self.assertEqual((live, cap), (2, 3))
            with mock.patch.object(CTM, 'count_live_tasks', return_value=3):
                self.assertTrue(CTM.at_capacity()[0])
            with mock.patch.object(CTM, 'count_live_tasks', return_value=5):
                self.assertTrue(CTM.at_capacity()[0])

    def test_rejection_payload_shape(self):
        with mock.patch.object(CTM, 'MAX_TASKS', 4):
            with mock.patch.object(CTM, 'count_live_tasks', return_value=4):
                r = CTM._capacity_rejection()
        self.assertEqual(r['status'], 'rejected')
        self.assertIsNone(r['task_id'])
        self.assertIn('4/4', r['error'])


class CreateTaskCapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kccap-')
        self._orig = CTM.TASKS_DIR
        CTM.TASKS_DIR = self.tmp
        self.addCleanup(self._restore)

    def _restore(self):
        CTM.TASKS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _dirs(self):
        return [d for d in os.listdir(self.tmp) if not d.startswith('.')]

    def test_create_task_rejected_at_capacity_leaves_no_dir(self):
        with mock.patch.object(CTM, 'MAX_TASKS', 2), \
             mock.patch.object(CTM, 'count_live_tasks', return_value=2):
            task = CTM.create_task('do something')
        self.assertEqual(task['status'], 'rejected')
        self.assertIsNone(task['task_id'])
        self.assertEqual(self._dirs(), [])  # no tmux session, no task dir

    def test_create_terminal_task_rejected_at_capacity(self):
        with mock.patch.object(CTM, 'MAX_TASKS', 1), \
             mock.patch.object(CTM, 'count_live_tasks', return_value=9):
            task = CTM.create_terminal_task()
        self.assertEqual(task['status'], 'rejected')
        self.assertEqual(self._dirs(), [])

    def test_under_capacity_does_not_short_circuit(self):
        # Below the cap, create_task proceeds past the guard (it then tries to
        # spawn tmux, which we stub to fail — proving the guard let it through
        # rather than returning a 'rejected' meta).
        with mock.patch.object(CTM, 'MAX_TASKS', 12), \
             mock.patch.object(CTM, 'count_live_tasks', return_value=0), \
             mock.patch.object(server.subprocess, 'run',
                               return_value=mock.Mock(returncode=1, stdout='', stderr='boom')):
            task = CTM.create_task('hi')
        self.assertNotEqual(task.get('status'), 'rejected')


if __name__ == '__main__':
    unittest.main()
