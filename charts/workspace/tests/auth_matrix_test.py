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
        # Pin oauth2 — the mode where server.py is the enforcer. The default
        # AUTH_MODE=basic is edge-auth (the ingress authenticates, server.py
        # trusts it), so check_claude_auth short-circuits there; this suite
        # tests server.py's own gate, which applies under oauth2.
        cls._auth_mode_save = server.AUTH_MODE
        server.AUTH_MODE = 'oauth2'
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
        server.AUTH_MODE = cls._auth_mode_save
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

    def test_gh_connect_start_requires_auth(self):
        self._expect_401('/api/github/connect/start')

    def test_gh_connect_poll_requires_auth(self):
        self._expect_401('/api/github/connect/poll')

    def test_gh_connect_cancel_requires_auth(self):
        self._expect_401('/api/github/connect/cancel')


def _get(url, headers=None):
    req = urllib.request.Request(url, method='GET')
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return urllib.request.urlopen(req, timeout=5)


class BearerMarkerAuthTests(unittest.TestCase):
    """The Bearer-token API ingress routes through a `/bearer-api/` marker so
    server.py never trusts upstream identity headers on that path (they can be
    forged — that ingress is not fronted by oauth2-proxy). Requests reaching the
    pod as bare `/api/*` (via the oauth2 ingress) still trust validated headers
    so the dashboard keeps working."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix='kc-bearer-')
        cls._auth_mode_save = server.AUTH_MODE
        cls._trusted_save = server.TRUSTED_PROXY
        cls._verify_save = server.ClaudeTaskManager.verify_token
        cls._tasks_dir_save = server.ClaudeTaskManager.TASKS_DIR
        server.AUTH_MODE = 'oauth2'
        server.TRUSTED_PROXY = True
        server.ClaudeTaskManager.TASKS_DIR = cls.tmpdir
        server.ClaudeTaskManager.verify_token = staticmethod(lambda t: t == 'good-token')
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
        server.AUTH_MODE = cls._auth_mode_save
        server.TRUSTED_PROXY = cls._trusted_save
        server.ClaudeTaskManager.verify_token = cls._verify_save
        server.ClaudeTaskManager.TASKS_DIR = cls._tasks_dir_save
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _url(self, path):
        return f'http://127.0.0.1:{self.port}{path}'

    def test_validated_header_on_plain_api_authenticates(self):
        # Dashboard path: reaches the pod as bare /api/* (oauth2 ingress rewrites
        # /oauth/api/* -> /api/*) with a proxy-validated identity header.
        with _get(self._url('/api/claude/tasks'),
                  {'X-Auth-Request-User': 'realuser'}) as r:
            self.assertEqual(r.status, 200)

    def test_forged_header_on_bearer_marker_is_rejected(self):
        # The exploit: a client-supplied identity header on the Bearer ingress.
        try:
            with _get(self._url('/bearer-api/api/claude/tasks'),
                      {'X-Auth-Request-User': 'attacker'}) as r:
                self.fail(f'expected 401, got {r.status}')
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)

    def test_bearer_token_on_marker_authenticates(self):
        # Legit programmatic client: Bearer token on the Bearer ingress. Also
        # proves the /bearer-api/ marker is stripped so routing still matches.
        with _get(self._url('/bearer-api/api/claude/tasks'),
                  {'Authorization': 'Bearer good-token'}) as r:
            self.assertEqual(r.status, 200)

    def test_forged_header_plus_bad_token_on_marker_is_rejected(self):
        try:
            with _get(self._url('/bearer-api/api/claude/tasks'),
                      {'X-Auth-Request-User': 'attacker',
                       'Authorization': 'Bearer nope'}) as r:
                self.fail(f'expected 401, got {r.status}')
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)


if __name__ == '__main__':
    unittest.main()
