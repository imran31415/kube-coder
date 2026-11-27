"""Unit tests for the concurrent-task ceiling (issue #98).

Covers ClaudeTaskManager.count_tasks / count_live_tasks / at_capacity /
_capacity_rejection and that create_task / create_terminal_task refuse to spawn
(returning a `rejected` meta, leaving no task dir) once the busy-task ceiling is
reached. Only BUSY Builds count; one parked on `waiting-for-input` does not
(#928).

Run with:    python3 -m unittest tests.task_capacity_test
(from charts/workspace/)
"""

import json
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


def _tmux(out, returncode=0):
    return mock.patch.object(server.subprocess, 'run',
                             return_value=mock.Mock(returncode=returncode,
                                                    stdout=out, stderr=''))


class CountLiveTasksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kccount-')
        p = mock.patch.object(CTM, 'TASKS_DIR', self.tmp)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _task(self, task_id, status):
        os.makedirs(os.path.join(self.tmp, task_id))
        with open(os.path.join(self.tmp, task_id, 'task.json'), 'w') as f:
            json.dump({'task_id': task_id, 'status': status}, f)

    def test_counts_only_kube_coder_sessions(self):
        out = 'kube-coder-a\nkube-coder-b\nclaude-agent1\nmysess\n'
        with _tmux(out):
            self.assertEqual(CTM.count_live_tasks(), 2)

    def test_zero_when_tmux_unavailable(self):
        with _tmux('', returncode=1):
            self.assertEqual(CTM.count_live_tasks(), 0)

    def test_zero_when_tmux_binary_missing(self):
        with mock.patch.object(server.subprocess, 'run',
                               side_effect=FileNotFoundError('tmux')):
            self.assertEqual(CTM.count_tasks(), {'busy': 0, 'idle': 0})

    def test_a_waiting_build_does_not_hold_a_slot(self):
        # The #928 report: five sessions open, all five finished. None of them
        # is doing work, so none of them may stand between the user and a new
        # Build.
        self._task('run1', 'running')
        self._task('idle1', 'waiting-for-input')
        self._task('idle2', 'waiting-for-input')
        self._task('done1', 'completed')   # session on its way out
        out = 'kube-coder-run1\nkube-coder-idle1\nkube-coder-idle2\nkube-coder-done1\n'
        with _tmux(out):
            self.assertEqual(CTM.count_tasks(), {'busy': 1, 'idle': 3})
            self.assertEqual(CTM.count_live_tasks(), 1)

    def test_an_unknown_session_counts_as_busy(self):
        # No task.json, an unreadable one, or a name that is not a task id:
        # the cap errs towards "it may be working".
        os.makedirs(os.path.join(self.tmp, 'garbled'))
        with open(os.path.join(self.tmp, 'garbled', 'task.json'), 'w') as f:
            f.write('{not json')
        out = 'kube-coder-missing\nkube-coder-garbled\nkube-coder-../x\n'
        with _tmux(out):
            self.assertEqual(CTM.count_tasks(), {'busy': 3, 'idle': 0})


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
            with mock.patch.object(CTM, 'count_tasks',
                                   return_value={'busy': 4, 'idle': 5}):
                r = CTM._capacity_rejection()
        self.assertEqual(r['status'], 'rejected')
        self.assertIsNone(r['task_id'])
        self.assertEqual((r['busy'], r['idle'], r['max_tasks']), (4, 5, 4))
        self.assertIn('4/4 busy', r['error'])
        self.assertIn('5 idle', r['error'])

    def test_rejection_body_reports_the_split(self):
        with mock.patch.object(CTM, 'MAX_TASKS', 2), \
             mock.patch.object(CTM, 'count_tasks',
                               return_value={'busy': 2, 'idle': 7}):
            body = CTM.rejection_body(CTM._capacity_rejection())
        self.assertEqual(body['busy'], 2)
        self.assertEqual(body['idle'], 7)
        self.assertEqual(body['max_tasks'], 2)
        self.assertNotIn('status', body)
        # Any other refusal keeps its old one-key body.
        self.assertEqual(CTM.rejection_body({'status': 'rejected', 'error': 'x'}),
                         {'error': 'x'})


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
