"""Tests for the task interrupt endpoint and tmux Escape command.

Run with:
    cd charts/workspace && python3 -m unittest tests.interrupt_test
"""

import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
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
            task, err = server.ClaudeTaskManager.interrupt_task('task-1')

        self.assertIsNone(err)
        self.assertEqual(task, meta)
        self.assertEqual(
            run.call_args_list,
            [
                mock.call(['tmux', 'has-session', '-t', 'kube-coder-task-1'], capture_output=True, text=True),
                mock.call(['tmux', 'send-keys', '-t', 'kube-coder-task-1', 'Escape'], capture_output=True, text=True),
            ],
        )

    def test_interrupt_does_not_send_escape_when_the_session_is_gone(self):
        self._task()
        with mock.patch.object(server.subprocess, 'run', return_value=_result(1)) as run:
            task, err = server.ClaudeTaskManager.interrupt_task('task-1')

        self.assertIsNone(task)
        self.assertEqual(err, 'Session is no longer running')
        run.assert_called_once_with(
            ['tmux', 'has-session', '-t', 'kube-coder-task-1'], capture_output=True, text=True)

    def test_interrupt_returns_not_found_without_running_tmux(self):
        with mock.patch.object(server.subprocess, 'run') as run:
            task, err = server.ClaudeTaskManager.interrupt_task('missing')

        self.assertIsNone(task)
        self.assertEqual(err, 'Task not found')
        run.assert_not_called()


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
                self.assertEqual(json.loads(response.read()), self.meta)

        self.assertEqual(run.call_count, 2)


if __name__ == '__main__':
    unittest.main()
