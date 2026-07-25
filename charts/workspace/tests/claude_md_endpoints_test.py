"""Drift guard for the curl commands documented in claude-md.txt (#452).

claude-md.txt is shipped into every workspace as CLAUDE.md and treated as
ground truth by agents. When server.py's routing or auth gates change but
the doc doesn't (e.g. /metrics gained an auth gate while the doc still
showed a bare curl), agents follow the doc, get a 401, and conclude the
endpoint is broken.

This suite extracts every documented `curl localhost:6080/...` command,
boots the real server with AUTH_MODE=oauth2 (the mode where server.py is
the enforcer), and replays each GET exactly as documented — bare if the
doc shows no Authorization header, with the Bearer token if it shows one —
asserting the response is 200. POST/DELETE commands and `{id}`-placeholder
paths are skipped (side effects), as are the OAuth2-session-only auth
endpoints, which a Bearer token deliberately cannot reach.

Run with:
    cd charts/workspace && python3 -m unittest tests.claude_md_endpoints_test
"""

import http.server
import os
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402

CLAUDE_MD = os.path.join(os.path.dirname(HERE), 'claude-md.txt')
TEST_TOKEN = 'claude-md-drift-guard-test-token'

# Documented as reachable only through a browser OAuth2 session — a Bearer
# token is rejected by design (check_oauth_only), so they can't be replayed.
OAUTH_SESSION_ONLY = {
    '/api/claude/auth/token',
    '/api/claude/auth/token/regenerate',
}


def extract_curl_commands(text):
    """All documented curl commands targeting localhost:6080, with
    backslash line-continuations joined."""
    joined = text.replace('\\\n', ' ')
    return [c for c in re.findall(r'curl[^`\n]*', joined) if 'localhost:6080' in c]


def parse_command(cmd):
    """(method, path, documents_auth_header) for one curl command."""
    m = re.search(r'-X\s+(\w+)', cmd)
    method = m.group(1).upper() if m else 'GET'
    p = re.search(r'localhost:6080(/[^\s\'"`]*)', cmd)
    path = p.group(1) if p else '/'
    return method, path, 'Authorization: Bearer' in cmd


class ClaudeMdEndpointTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix='kc-claude-md-')
        cls._saves = {
            'auth_mode': server.AUTH_MODE,
            'tasks_dir': server.ClaudeTaskManager.TASKS_DIR,
            'token_file': server.ClaudeTaskManager.TOKEN_FILE,
            'metrics': server.MetricsCollector.get_all_metrics,
            'gh_status': server.GitHubManager.get_full_status,
        }
        server.AUTH_MODE = 'oauth2'  # server.py enforces auth itself
        server.ClaudeTaskManager.TASKS_DIR = cls.tmpdir
        server.ClaudeTaskManager.TOKEN_FILE = os.path.join(cls.tmpdir, '.api-token')
        with open(server.ClaudeTaskManager.TOKEN_FILE, 'w') as f:
            f.write(TEST_TOKEN)
        # Stub the heavy collectors — this suite tests routing + auth, not
        # /proc parsing or gh/ssh subprocess calls.
        server.MetricsCollector.get_all_metrics = staticmethod(lambda: {'stub': True})
        server.GitHubManager.get_full_status = staticmethod(lambda: {'stub': True})

        import socket
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        cls.port = s.getsockname()[1]
        s.close()
        cls.httpd = http.server.ThreadingHTTPServer(
            ('127.0.0.1', cls.port), server.BrowserHandler,
        )
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.AUTH_MODE = cls._saves['auth_mode']
        server.ClaudeTaskManager.TASKS_DIR = cls._saves['tasks_dir']
        server.ClaudeTaskManager.TOKEN_FILE = cls._saves['token_file']
        server.MetricsCollector.get_all_metrics = cls._saves['metrics']
        server.GitHubManager.get_full_status = cls._saves['gh_status']
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _get(self, path, with_auth):
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}')
        if with_auth:
            req.add_header('Authorization', f'Bearer {TEST_TOKEN}')
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_documented_gets_respond_200_as_written(self):
        with open(CLAUDE_MD) as f:
            commands = extract_curl_commands(f.read())
        self.assertTrue(commands, 'no curl commands found — parser or doc broke')

        checked = 0
        for cmd in commands:
            method, path, has_auth = parse_command(cmd)
            if method != 'GET' or '{' in path or path in OAUTH_SESSION_ONLY:
                continue
            with self.subTest(cmd=cmd.strip()):
                status = self._get(path, with_auth=has_auth)
                self.assertEqual(
                    status, 200,
                    f'documented command got {status}: `{cmd.strip()}` — '
                    'claude-md.txt has drifted from server.py (see #452)',
                )
            checked += 1

        # If the doc or parser changes shape, don't green-wash on zero checks.
        self.assertGreaterEqual(checked, 4, f'only {checked} GET commands checked')


if __name__ == '__main__':
    unittest.main()
