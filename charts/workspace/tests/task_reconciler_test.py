"""Unit tests for the background task reconciler (issue #96).

Covers ClaudeTaskManager.reconcile_running — the method the TaskReconciler
daemon calls so a finished task's status flips and its completion hook fires
even when no client is reading the task — plus the TaskReconciler lifecycle.

tmux is simulated via subprocess.run stubs; TASKS_DIR is a tempdir;
_fire_completion_hook is mocked so no network I/O happens.

Run with:    python3 -m unittest tests.task_reconciler_test
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


def _tmux_dead(*args, **kwargs):
    """subprocess.run stub: tmux has-session reports the session is gone
    (returncode 1); every other tmux op succeeds."""
    argv = args[0] if args else kwargs.get('args', [])
    if len(argv) >= 2 and argv[0] == 'tmux' and argv[1] == 'has-session':
        return mock.Mock(returncode=1, stdout='', stderr='no session')
    return mock.Mock(returncode=0, stdout='', stderr='')


class ReconcileRunningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kcrecon-')
        self._orig = CTM.TASKS_DIR
        CTM.TASKS_DIR = self.tmp
        self.addCleanup(self._restore)

    def _restore(self):
        CTM.TASKS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _task(self, tid, **meta):
        d = os.path.join(self.tmp, tid)
        os.makedirs(d, exist_ok=True)
        m = {'task_id': tid}
        m.update(meta)
        with open(os.path.join(d, 'task.json'), 'w') as f:
            json.dump(m, f)
        return d

    def _read(self, tid):
        with open(os.path.join(self.tmp, tid, 'task.json')) as f:
            return json.load(f)

    def test_dead_running_task_completes_and_fires_hook(self):
        self._task('t1', status='running', tmux_session='kube-coder-t1',
                   response_url='http://hook.example/cb')
        with mock.patch.object(server.subprocess, 'run', side_effect=_tmux_dead), \
             mock.patch.object(CTM, '_fire_completion_hook') as fire:
            n = CTM.reconcile_running()
        self.assertEqual(n, 1)
        fire.assert_called_once()
        meta = self._read('t1')
        self.assertEqual(meta['status'], 'completed')
        self.assertIn('finished_at', meta)
        self.assertIn('hook_fired_at', meta)

    def test_completes_without_hook_when_no_response_url(self):
        self._task('t1', status='running', tmux_session='s')
        with mock.patch.object(server.subprocess, 'run', side_effect=_tmux_dead), \
             mock.patch.object(CTM, '_fire_completion_hook') as fire:
            CTM.reconcile_running()
        fire.assert_not_called()
        self.assertEqual(self._read('t1')['status'], 'completed')

    def test_terminal_tasks_skipped_without_tmux_call(self):
        self._task('done', status='completed', tmux_session='x')
        self._task('killed', status='killed', tmux_session='y')
        with mock.patch.object(server.subprocess, 'run') as run:
            n = CTM.reconcile_running()
        self.assertEqual(n, 0)
        run.assert_not_called()  # terminal → cheap skip, no subprocess

    def test_live_session_stays_running(self):
        self._task('t1', status='running', tmux_session='kube-coder-t1')
        # has-session returns 0 (alive); capture-pane returns stable output.
        with mock.patch.object(server.subprocess, 'run',
                               return_value=mock.Mock(returncode=0, stdout='x', stderr='')), \
             mock.patch.object(CTM, '_fire_completion_hook') as fire:
            n = CTM.reconcile_running()
        self.assertEqual(n, 1)
        fire.assert_not_called()
        self.assertEqual(self._read('t1')['status'], 'running')

    def test_corrupt_and_missing_meta_skipped(self):
        # A corrupt task dir + a dir with no task.json must not break the pass.
        bad = os.path.join(self.tmp, 'bad'); os.makedirs(bad)
        with open(os.path.join(bad, 'task.json'), 'w') as f:
            f.write('{not json')
        os.makedirs(os.path.join(self.tmp, 'nometa'))
        self._task('good', status='running', tmux_session='s')
        with mock.patch.object(server.subprocess, 'run', side_effect=_tmux_dead), \
             mock.patch.object(CTM, '_fire_completion_hook'):
            n = CTM.reconcile_running()
        self.assertEqual(n, 1)  # only the good running task

    def test_missing_tasks_dir_returns_zero(self):
        CTM.TASKS_DIR = os.path.join(self.tmp, 'gone')
        shutil.rmtree(self.tmp, ignore_errors=True)
        # ensure_tasks_dir recreates it empty → zero tasks, no raise.
        self.assertEqual(CTM.reconcile_running(), 0)

    def test_max_tasks_bound(self):
        for i in range(5):
            self._task(f't{i}', status='running', tmux_session='s')
        with mock.patch.object(server.subprocess, 'run', side_effect=_tmux_dead), \
             mock.patch.object(CTM, '_fire_completion_hook'):
            n = CTM.reconcile_running(max_tasks=2)
        self.assertEqual(n, 2)

    def test_reconcile_failure_does_not_raise(self):
        self._task('t1', status='running', tmux_session='s')
        with mock.patch.object(CTM, '_reconcile_status', side_effect=RuntimeError('boom')):
            n = CTM.reconcile_running()  # must swallow + continue
        self.assertEqual(n, 0)


class TaskReconcilerLifecycleTests(unittest.TestCase):
    def test_start_is_idempotent(self):
        orig = server.TaskReconciler._started
        server.TaskReconciler._started = True
        try:
            # Second start() must be a no-op and must not raise / spawn a thread.
            server.TaskReconciler.start(interval_seconds=10)
        finally:
            server.TaskReconciler._started = orig

    def test_status_shape(self):
        s = server.TaskReconciler.status()
        for k in ('running', 'last_run_at', 'last_reconciled'):
            self.assertIn(k, s)


if __name__ == '__main__':
    unittest.main()
