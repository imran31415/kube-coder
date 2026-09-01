"""Tests for the task interrupt endpoint and tmux Escape command.

Run with:
    cd charts/workspace && python3 -m unittest tests.interrupt_test
"""

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402


def _free_port():
    import socket
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _result(code=0, stderr=''):
    return mock.Mock(returncode=code, stdout='', stderr=stderr)


def _post(url):
    req = urllib.request.Request(url, data=b'{}', method='POST')
    req.add_header('Content-Type', 'application/json')
    return urllib.request.urlopen(req, timeout=5)


class InterruptTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_tasks_dir = server.ClaudeTaskManager.TASKS_DIR
        server.ClaudeTaskManager.TASKS_DIR = self.tmp.name

    def tearDown(self):
        server.ClaudeTaskManager.TASKS_DIR = self.old_tasks_dir
        self.tmp.cleanup()

    def _task(self, task_id='task-1'):
        task_dir = os.path.join(self.tmp.name, task_id)
        os.makedirs(task_dir)
        meta = {'task_id': task_id, 'status': 'running', 'tmux_session': 'kube-coder-task-1'}
        with open(os.path.join(task_dir, 'task.json'), 'w') as f:
            json.dump(meta, f)
        return meta

    def test_interrupt_sends_escape_after_confirming_the_session_is_live(self):
        meta = self._task()
        with mock.patch.object(server.subprocess, 'run', side_effect=[_result(), _result()]) as run:
            task, interrupted, err = server.ClaudeTaskManager.interrupt_task('task-1')

        self.assertIsNone(err)
        self.assertTrue(interrupted)
        self.assertEqual(task, meta)
        # Bounded: an unbounded tmux call parks a request-handler thread for
        # good if the tmux server wedges, and every other tmux call in
        # server.py passes a timeout.
        self.assertEqual(
            run.call_args_list,
            [
                mock.call(['tmux', 'has-session', '-t', 'kube-coder-task-1'],
                          capture_output=True, text=True, timeout=10),
                mock.call(['tmux', 'send-keys', '-t', 'kube-coder-task-1', 'Escape'],
                          capture_output=True, text=True, timeout=10),
            ],
        )

    def test_an_already_finished_session_is_a_no_op_not_an_error(self):
        """Stop is pressed while a turn is ending; the race must be quiet.

        Reporting "Session is no longer running" as a failure means clicking
        Stop microseconds after the CLI settles raises an error toast for a
        turn that did exactly what the user wanted. handle_hypervisor_stop
        treats an idle thread the same way, and says why.
        """
        meta = self._task()
        with mock.patch.object(server.subprocess, 'run', return_value=_result(1)) as run:
            task, interrupted, err = server.ClaudeTaskManager.interrupt_task('task-1')

        self.assertIsNone(err)
        self.assertFalse(interrupted)
        self.assertEqual(task, meta)
        run.assert_called_once_with(
            ['tmux', 'has-session', '-t', 'kube-coder-task-1'],
            capture_output=True, text=True, timeout=10)

    def test_interrupt_returns_not_found_without_running_tmux(self):
        with mock.patch.object(server.subprocess, 'run') as run:
            task, interrupted, err = server.ClaudeTaskManager.interrupt_task('missing')

        self.assertIsNone(task)
        self.assertFalse(interrupted)
        self.assertEqual(err, 'Task not found')
        run.assert_not_called()

    def test_a_torn_task_json_is_transient_not_a_missing_task(self):
        """task.json is written non-atomically, so a read can land mid-write."""
        task_dir = os.path.join(server.ClaudeTaskManager.TASKS_DIR, 'task-1')
        os.makedirs(task_dir, exist_ok=True)
        with open(os.path.join(task_dir, 'task.json'), 'w') as f:
            f.write('{"task_id": "task-1", "sta')   # truncated mid-write

        with mock.patch.object(server.subprocess, 'run') as run:
            task, interrupted, err = server.ClaudeTaskManager.interrupt_task('task-1')

        self.assertIsNone(task)
        self.assertFalse(interrupted)
        self.assertIn('unreadable', err)
        run.assert_not_called()

    def test_a_wedged_tmux_does_not_propagate_an_exception(self):
        self._task()
        with mock.patch.object(server.subprocess, 'run',
                               side_effect=subprocess.TimeoutExpired('tmux', 10)):
            task, interrupted, err = server.ClaudeTaskManager.interrupt_task('task-1')

        # has-session timing out is indistinguishable from "no session".
        self.assertIsNone(err)
        self.assertFalse(interrupted)


class InterruptEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_auth_mode = server.AUTH_MODE
        server.AUTH_MODE = 'basic'
        cls.port = _free_port()
        cls.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', cls.port), server.BrowserHandler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.AUTH_MODE = cls.old_auth_mode

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_tasks_dir = server.ClaudeTaskManager.TASKS_DIR
        server.ClaudeTaskManager.TASKS_DIR = self.tmp.name
        task_dir = os.path.join(self.tmp.name, 'task-1')
        os.makedirs(task_dir)
        self.meta = {'task_id': 'task-1', 'status': 'running', 'tmux_session': 'kube-coder-task-1'}
        with open(os.path.join(task_dir, 'task.json'), 'w') as f:
            json.dump(self.meta, f)

    def tearDown(self):
        server.ClaudeTaskManager.TASKS_DIR = self.old_tasks_dir
        self.tmp.cleanup()

    def test_post_interrupt_returns_task_metadata(self):
        with mock.patch.object(server.subprocess, 'run', side_effect=[_result(), _result()]) as run:
            with _post(f'http://127.0.0.1:{self.port}/api/claude/tasks/task-1/interrupt') as response:
                self.assertEqual(response.status, 200)
                body = json.loads(response.read())

        self.assertEqual(run.call_count, 2)
        self.assertTrue(body.pop('interrupted'))
        self.assertEqual(body, self.meta)

    def test_stopping_an_already_finished_turn_is_a_200_not_a_404(self):
        """The fire-and-forget race, end to end: the button must not toast."""
        with mock.patch.object(server.subprocess, 'run', return_value=_result(1)):
            with _post(f'http://127.0.0.1:{self.port}/api/claude/tasks/task-1/interrupt') as response:
                self.assertEqual(response.status, 200)
                body = json.loads(response.read())
        self.assertFalse(body['interrupted'])

    def test_an_unknown_task_is_still_a_404(self):
        try:
            with _post(f'http://127.0.0.1:{self.port}/api/claude/tasks/nope/interrupt') as response:
                self.fail(f'expected 404, got {response.status}')
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)


if __name__ == '__main__':
    unittest.main()
