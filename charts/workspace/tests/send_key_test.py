"""`POST /api/claude/tasks/{id}/key` — the one way a key reaches a live session.

Both clients go through here: the mobile key bar (Esc / Shift-Tab / arrows, for
phones with no physical keyboard) and the web composer's Stop button, which is
just `escape`. An interrupt is not a separate endpoint — a second one would be
a second set of failure modes for the same tmux call.

The scenarios below are the ones that were wrong when this was two endpoints:
a finished session reported as an error, and unbounded tmux calls.

Run with:
    cd charts/workspace && python3 -m unittest tests.send_key_test
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


def _tmux(has_session=0, send_keys=0, send_stderr='', raise_on=None):
    """A fake `subprocess.run` that dispatches on the tmux subcommand.

    Deliberately not a positional `side_effect` list: `get_task` shells out to
    `tmux capture-pane` on its way past, so an ordered list gets consumed by a
    call the test never meant to stub and the results land on the wrong ones.
    Keying on argv makes these tests independent of how many other tmux calls
    the handler's collaborators make.
    """
    def run(cmd, *a, **kw):
        sub = 'has-session' if 'has-session' in cmd else (
            'send-keys' if 'send-keys' in cmd else 'other')
        # `get_task` makes its own unbounded has-session/capture-pane calls on
        # the way past. The handler's calls are the bounded ones, so the
        # timeout is what tells them apart — which keeps these tests aimed at
        # the code under test rather than at its collaborators.
        if raise_on and sub == raise_on and kw.get('timeout') is not None:
            raise subprocess.TimeoutExpired('tmux', 10)
        if sub == 'has-session':
            return _result(has_session)
        if sub == 'send-keys':
            return _result(send_keys, send_stderr)
        return _result()
    return run


def _tmux_calls(run, sub):
    """Only the calls for one tmux subcommand, in order."""
    return [c for c in run.call_args_list if sub in c.args[0]]


class SendKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_auth_mode = server.AUTH_MODE
        server.AUTH_MODE = 'basic'
        cls.port = _free_port()
        cls.httpd = http.server.ThreadingHTTPServer(
            ('127.0.0.1', cls.port), server.BrowserHandler)
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
        self.meta = {'task_id': 'task-1', 'status': 'running',
                     'tmux_session': 'kube-coder-task-1'}
        with open(os.path.join(task_dir, 'task.json'), 'w') as f:
            json.dump(self.meta, f)

    def tearDown(self):
        server.ClaudeTaskManager.TASKS_DIR = self.old_tasks_dir
        self.tmp.cleanup()

    def _post(self, task_id='task-1', key='escape'):
        req = urllib.request.Request(
            f'http://127.0.0.1:{self.port}/api/claude/tasks/{task_id}/key',
            data=json.dumps({'key': key}).encode(), method='POST')
        req.add_header('Content-Type', 'application/json')
        return urllib.request.urlopen(req, timeout=5)

    def test_escape_reaches_a_live_session(self):
        with mock.patch.object(server.subprocess, 'run',
                               side_effect=_tmux()) as run:
            with self._post() as response:
                self.assertEqual(response.status, 200)
                body = json.loads(response.read())

        self.assertTrue(body['delivered'])
        self.assertEqual(body['key'], 'escape')
        self.assertEqual(_tmux_calls(run, 'send-keys')[0].args[0],
                         ['tmux', 'send-keys', '-t', 'kube-coder-task-1', 'Escape'])
        # Bounded, like every other tmux call in server.py: unbounded, a wedged
        # tmux server parks this request-handler thread permanently. (Only the
        # handler's own calls are asserted — `get_task` runs its own, and
        # tightening those is a separate change.)
        bounded = [c for c in run.call_args_list if c.kwargs.get('timeout') is not None]
        self.assertEqual(len(bounded), 2, 'both handler tmux calls must be bounded')
        for call in bounded:
            self.assertEqual(call.kwargs['timeout'], 10)

    def test_an_already_finished_session_is_a_200_not_an_error(self):
        """The fire-and-forget race, which is the common case for Stop.

        Stop is pressed while a turn is ending, so a click landing microseconds
        after the CLI settles must not raise a failure toast for a turn that
        did exactly what the user wanted. `delivered: False` says what
        happened without calling it a failure.
        """
        with mock.patch.object(server.subprocess, 'run',
                               side_effect=_tmux(has_session=1)) as run:
            with self._post() as response:
                self.assertEqual(response.status, 200)
                body = json.loads(response.read())

        self.assertFalse(body['delivered'])
        # It must not try to send into a session it just found was gone.
        self.assertEqual(_tmux_calls(run, 'send-keys'), [])

    def test_a_wedged_tmux_does_not_hang_or_500(self):
        with mock.patch.object(server.subprocess, 'run',
                               side_effect=_tmux(raise_on='has-session')):
            with self._post() as response:
                self.assertEqual(response.status, 200)
                self.assertFalse(json.loads(response.read())['delivered'])

    def test_a_live_session_refusing_the_key_is_a_502(self):
        """The session exists but tmux rejected the key — a real, upstream
        fault, and distinct from 'there was nothing to send it to'."""
        with mock.patch.object(server.subprocess, 'run',
                               side_effect=_tmux(send_keys=1, send_stderr='no such pane')):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self._post()
        self.assertEqual(caught.exception.code, 502)

    def test_an_unknown_task_is_a_404(self):
        with mock.patch.object(server.subprocess, 'run') as run:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self._post(task_id='nope')
        self.assertEqual(caught.exception.code, 404)
        run.assert_not_called()

    def test_an_unsupported_key_is_refused_before_tmux(self):
        """The keymap is a whitelist so the body can never become a tmux
        command; nothing may reach the session unvalidated."""
        with mock.patch.object(server.subprocess, 'run') as run:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self._post(key='rm -rf /')
        self.assertEqual(caught.exception.code, 400)
        run.assert_not_called()

    def test_the_mobile_key_bar_still_works(self):
        """Escape is what Stop needs, but this endpoint is shared — the other
        keys the mobile bar sends must keep mapping to their tmux names."""
        for key, tmux_key in (('shift-tab', 'BTab'), ('ctrl-c', 'C-c'),
                              ('up', 'Up'), ('enter', 'Enter')):
            with mock.patch.object(server.subprocess, 'run',
                                   side_effect=_tmux()) as run:
                with self._post(key=key) as response:
                    self.assertEqual(response.status, 200)
            self.assertEqual(_tmux_calls(run, 'send-keys')[0].args[0][-1], tmux_key)


if __name__ == '__main__':
    unittest.main()
