"""Scroll-mode endpoint: copy-mode toggle + touch scroll directions.

The scroll directions back the mobile scroll overlay (TerminalPane.tsx): touch
emits no wheel events, so the ttyd iframe never scrolls in copy-mode and the
overlay POSTs a direction here, which we translate to a tmux copy-mode motion.

Run with:
    cd charts/workspace && python3 -m unittest tests.scroll_mode_test
"""

import http.server
import json
import os
import sys
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


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST')
    req.add_header('Content-Type', 'application/json')
    return urllib.request.urlopen(req, timeout=5)


class ScrollModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # server.py reads AUTH_MODE at import time. Pin it to 'basic' so these
        # unauthenticated POSTs pass check_claude_auth regardless of the ambient
        # environment — otherwise running inside an oauth2 workspace pod (where
        # AUTH_MODE=oauth2 is exported) makes every request 401.
        cls._orig_auth_mode = server.AUTH_MODE
        server.AUTH_MODE = 'basic'
        cls.port = _free_port()
        cls.httpd = http.server.ThreadingHTTPServer(
            ('127.0.0.1', cls.port), server.BrowserHandler,
        )
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.AUTH_MODE = cls._orig_auth_mode

    def _call(self, body):
        """POST the body and return (status, the tmux argv that was run)."""
        captured = {}

        def fake_run(cmd, *a, **k):
            captured['cmd'] = cmd
            return mock.Mock(returncode=0, stdout='', stderr='')

        with mock.patch('server.subprocess.run', side_effect=fake_run), \
             mock.patch.object(server.ClaudeTaskManager, 'get_task',
                               return_value={'tmux_session': 'sess-T'}):
            with _post(f'http://127.0.0.1:{self.port}/api/claude/tasks/T/scroll-mode',
                       body) as r:
                return r.status, captured.get('cmd')

    def test_enter_uses_copy_mode(self):
        st, cmd = self._call({'action': 'enter'})
        self.assertEqual(st, 200)
        self.assertEqual(cmd, ['tmux', 'copy-mode', '-t', 'sess-T'])

    def test_exit_cancels_copy_mode(self):
        st, cmd = self._call({'action': 'exit'})
        self.assertEqual(st, 200)
        self.assertEqual(cmd, ['tmux', 'send-keys', '-t', 'sess-T', '-X', 'cancel'])

    def test_scroll_up_sends_copy_mode_motion_with_count(self):
        st, cmd = self._call({'action': 'up', 'lines': 3})
        self.assertEqual(st, 200)
        self.assertEqual(
            cmd, ['tmux', 'send-keys', '-t', 'sess-T', '-X', '-N', '3', 'scroll-up'])

    def test_scroll_down_defaults_to_one_line(self):
        st, cmd = self._call({'action': 'down'})
        self.assertEqual(st, 200)
        self.assertEqual(
            cmd, ['tmux', 'send-keys', '-t', 'sess-T', '-X', '-N', '1', 'scroll-down'])

    def test_line_count_is_clamped(self):
        _, cmd = self._call({'action': 'up', 'lines': 9999})
        self.assertEqual(cmd[-2:], ['40', 'scroll-up'])

    def test_invalid_action_rejected(self):
        try:
            with _post(f'http://127.0.0.1:{self.port}/api/claude/tasks/T/scroll-mode',
                       {'action': 'sideways'}) as r:
                self.fail(f'expected 400, got {r.status}')
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)


if __name__ == '__main__':
    unittest.main()
