"""Regression tests for the task-API token bootstrap (issue #528).

The token in .claude-tasks/.api-token used to be created lazily, by the
GET /api/claude/auth/token handler alone. But the programmatic dispatch path
(mcp_dashboard.py — how the AI CTO starts a build) reads that file off disk and
sends it as the Bearer, so on a fresh pod where nothing had hit the auth
endpoint the first dispatch sent an empty Bearer and got a 401. These tests pin
both halves of the fix: the server materializes the token at boot, and the
dispatch client mints one rather than sending an empty Bearer.

Run with:    python3 -m unittest tests.task_token_bootstrap_test
(from charts/workspace/)
"""

import http.server
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import mcp_dashboard  # noqa: E402
import server  # noqa: E402

CTM = server.ClaudeTaskManager


def _free_port():
    import socket
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TokenMaterializationTests(unittest.TestCase):
    """ClaudeTaskManager's token file: created on demand, never rotated."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kc-token-')
        self._dir_save = CTM.TASKS_DIR
        self._file_save = CTM.TOKEN_FILE
        CTM.TASKS_DIR = os.path.join(self.tmp, '.claude-tasks')
        CTM.TOKEN_FILE = os.path.join(CTM.TASKS_DIR, '.api-token')

    def tearDown(self):
        CTM.TASKS_DIR = self._dir_save
        CTM.TOKEN_FILE = self._file_save
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_file_fails_verification(self):
        # The root cause of the 401: no file → every Bearer is rejected.
        self.assertFalse(os.path.exists(CTM.TOKEN_FILE))
        self.assertFalse(CTM.verify_token('anything'))

    def test_creates_token_the_dashboard_will_accept(self):
        token = CTM.get_or_create_token()
        self.assertTrue(token)
        self.assertTrue(os.path.exists(CTM.TOKEN_FILE))
        self.assertTrue(CTM.verify_token(token))

    def test_token_file_is_owner_only(self):
        CTM.get_or_create_token()
        mode = stat.S_IMODE(os.stat(CTM.TOKEN_FILE).st_mode)
        self.assertEqual(mode, 0o600)

    def test_existing_token_is_never_rotated(self):
        first = CTM.get_or_create_token()
        self.assertEqual(CTM.get_or_create_token(), first)

    def test_empty_token_file_is_refilled(self):
        os.makedirs(CTM.TASKS_DIR, exist_ok=True)
        with open(CTM.TOKEN_FILE, 'w'):
            pass
        token = CTM.get_or_create_token()
        self.assertTrue(token)
        self.assertTrue(CTM.verify_token(token))

    def test_startup_materializes_the_token_before_serving(self):
        # The boot call lives in `if __name__ == "__main__"`, which a test can't
        # import — assert on the source instead so the ordering can't regress.
        src_path = os.path.join(os.path.dirname(HERE), 'server.py')
        with open(src_path) as f:
            src = f.read()
        main_block = src[src.index('if __name__ == "__main__":'):]
        self.assertIn('ClaudeTaskManager.get_or_create_token()', main_block)
        self.assertLess(
            main_block.index('ClaudeTaskManager.get_or_create_token()'),
            main_block.index('serve_forever()'),
            'the token must be materialized before the server accepts requests',
        )


class FreshPodDispatchAuthTests(unittest.TestCase):
    """End-to-end: a dispatch from mcp_dashboard against a live server whose
    token file does not exist yet must authenticate, not 401."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='kc-dispatch-')
        cls._auth_mode_save = server.AUTH_MODE
        cls._trusted_save = server.TRUSTED_PROXY
        cls._dir_save = CTM.TASKS_DIR
        cls._file_save = CTM.TOKEN_FILE
        cls._mcp_token_save = mcp_dashboard.TOKEN_FILE
        cls._mcp_base_save = mcp_dashboard.BASE_URL
        # oauth2 is the mode where server.py is itself the enforcer (basic/none
        # short-circuit), so the Bearer actually has to check out.
        server.AUTH_MODE = 'oauth2'
        server.TRUSTED_PROXY = True
        CTM.TASKS_DIR = os.path.join(cls.tmp, '.claude-tasks')
        CTM.TOKEN_FILE = os.path.join(CTM.TASKS_DIR, '.api-token')
        mcp_dashboard.TOKEN_FILE = CTM.TOKEN_FILE
        cls.port = _free_port()
        mcp_dashboard.BASE_URL = f'http://127.0.0.1:{cls.port}'
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
        CTM.TASKS_DIR = cls._dir_save
        CTM.TOKEN_FILE = cls._file_save
        mcp_dashboard.TOKEN_FILE = cls._mcp_token_save
        mcp_dashboard.BASE_URL = cls._mcp_base_save
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # Every test starts from a brand-new pod: no token on disk.
        shutil.rmtree(CTM.TASKS_DIR, ignore_errors=True)

    def test_dispatch_authenticates_with_no_token_file_on_disk(self):
        self.assertFalse(os.path.exists(CTM.TOKEN_FILE))
        status, _ = mcp_dashboard._api('GET', '/api/claude/tasks')
        self.assertEqual(status, 200, 'fresh-pod dispatch must not 401')
        self.assertTrue(os.path.exists(CTM.TOKEN_FILE))

    def test_dispatch_authenticates_after_boot_materialization(self):
        # The primary fix: the server minted the token at startup, so the very
        # first dispatch reads a real Bearer off disk.
        boot_token = CTM.get_or_create_token()
        self.assertEqual(mcp_dashboard._token(), boot_token)
        status, _ = mcp_dashboard._api('GET', '/api/claude/tasks')
        self.assertEqual(status, 200)

    def test_empty_bearer_is_still_rejected(self):
        # Guard the guard: the 200s above come from a real token, not from the
        # server having gone permissive.
        with mock.patch.object(mcp_dashboard, '_token', lambda: ''):
            status, _ = mcp_dashboard._api('GET', '/api/claude/tasks')
        self.assertEqual(status, 401)


class DispatchTokenMintingTests(unittest.TestCase):
    """mcp_dashboard._token(): defense in depth for a pod whose token file
    somehow went missing — mint one instead of sending an empty Bearer."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kc-mcp-token-')
        self._save = mcp_dashboard.TOKEN_FILE
        mcp_dashboard.TOKEN_FILE = os.path.join(
            self.tmp, '.claude-tasks', '.api-token')

    def tearDown(self):
        mcp_dashboard.TOKEN_FILE = self._save
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mints_a_token_when_the_file_is_absent(self):
        token = mcp_dashboard._token()
        self.assertTrue(token)
        with open(mcp_dashboard.TOKEN_FILE) as f:
            self.assertEqual(f.read().strip(), token)

    def test_minted_token_is_owner_only(self):
        mcp_dashboard._token()
        mode = stat.S_IMODE(os.stat(mcp_dashboard.TOKEN_FILE).st_mode)
        self.assertEqual(mode, 0o600)

    def test_reads_an_existing_token_without_rewriting_it(self):
        os.makedirs(os.path.dirname(mcp_dashboard.TOKEN_FILE), exist_ok=True)
        with open(mcp_dashboard.TOKEN_FILE, 'w') as f:
            f.write('server-minted-token\n')
        self.assertEqual(mcp_dashboard._token(), 'server-minted-token')
        self.assertEqual(mcp_dashboard._token(), 'server-minted-token')

    def test_unwritable_location_degrades_quietly(self):
        mcp_dashboard.TOKEN_FILE = '/proc/kc-nonexistent/.api-token'
        self.assertEqual(mcp_dashboard._token(), '')


if __name__ == '__main__':
    unittest.main()
