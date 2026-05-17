"""Regression tests for the Phase-1 auth gate added to legacy endpoints.

Pre-fix, the following POST handlers ran without authenticating the
caller (server.py:3379-3398), relying entirely on the ingress OAuth2
proxy. A port-forward or NodePort would have exposed them. This file
asserts every one now returns 401 without auth.

Run with:
    cd charts/workspace && python3 -m unittest tests.auth_matrix_test
"""

import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

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


def _post(url, body=b''):
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Content-Type', 'application/json')
    return urllib.request.urlopen(req, timeout=5)


class LegacyAuthGateTests(unittest.TestCase):
    """Boot a server with no auth bypass and assert each legacy POST
    endpoint refuses unauthenticated callers."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix='kc-auth-')
        # Some handlers touch ClaudeTaskManager paths — point them at the
        # tempdir so a 401 happens BEFORE we trample a real workspace.
        cls._tasks_dir_save = server.ClaudeTaskManager.TASKS_DIR
        server.ClaudeTaskManager.TASKS_DIR = cls.tmpdir
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
        server.ClaudeTaskManager.TASKS_DIR = cls._tasks_dir_save
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _expect_401(self, path, body=b''):
        try:
            with _post(f'http://127.0.0.1:{self.port}{path}', body=body) as r:
                self.fail(f'{path}: expected 401, got {r.status}')
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401, f'{path} responded {e.code} not 401')

    def test_launch_chrome_requires_auth(self):
        self._expect_401('/api/launch-chrome')

    def test_test_chrome_requires_auth(self):
        self._expect_401('/api/test-chrome')

    def test_launch_firefox_requires_auth(self):
        self._expect_401('/api/launch-firefox')

    def test_test_firefox_requires_auth(self):
        self._expect_401('/api/test-firefox')

    def test_open_localhost_requires_auth(self):
        self._expect_401('/api/open-localhost', body=json.dumps({'port': 5173}).encode())

    def test_ssh_generate_requires_auth(self):
        self._expect_401('/api/github/ssh/generate', body=json.dumps({'email': 'x@y'}).encode())

    def test_git_config_post_requires_auth(self):
        self._expect_401('/api/github/config', body=json.dumps({'name': 'x', 'email': 'x@y'}).encode())

    def test_gh_login_url_requires_auth(self):
        self._expect_401('/api/github/cli/login-url')

    def test_gh_complete_auth_requires_auth(self):
        self._expect_401('/api/github/cli/complete-auth')


if __name__ == '__main__':
    unittest.main()
