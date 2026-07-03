"""Tests for the app-proxy session bootstrap (mobile WebView embedding).

A native WebView can only attach an Authorization header to its first
request, so /api/claude/apps/session mints a short-lived HMAC cookie that
check_app_proxy_auth() accepts on the apps surface only. These tests cover
the mint/verify round-trip, expiry, tampering, invalidation on token
rotation, the cookie parse, and the open-redirect guard on `next`.

Run with:
    cd charts/workspace && python3 -m unittest tests.app_session_test
"""

import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402

M = server.ClaudeTaskManager


class AppSessionUnit(unittest.TestCase):
    def setUp(self):
        # Point the token file at a temp path so mint/verify key off a
        # known, isolated Bearer token.
        self._token_file_save = M.TOKEN_FILE
        fd, self._token_path = tempfile.mkstemp(prefix='kc-token-')
        os.close(fd)
        with open(self._token_path, 'w') as f:
            f.write('test-bearer-token')
        M.TOKEN_FILE = self._token_path

    def tearDown(self):
        M.TOKEN_FILE = self._token_file_save
        os.unlink(self._token_path)

    def test_mint_verify_round_trip(self):
        self.assertTrue(M.verify_app_session(M.mint_app_session()))

    def test_expired_session_rejected(self):
        expired = int(time.time()) - 10
        value = f'{expired}.{M._app_session_sig(expired)}'
        self.assertFalse(M.verify_app_session(value))

    def test_tampered_signature_rejected(self):
        value = M.mint_app_session()
        expiry, sig = value.split('.', 1)
        bad = sig[:-1] + ('0' if sig[-1] != '0' else '1')
        self.assertFalse(M.verify_app_session(f'{expiry}.{bad}'))

    def test_tampered_expiry_rejected(self):
        # Extending the expiry without re-signing must fail.
        value = M.mint_app_session()
        expiry, sig = value.split('.', 1)
        self.assertFalse(M.verify_app_session(f'{int(expiry) + 9999}.{sig}'))

    def test_garbage_values_rejected(self):
        for v in ('', 'nodot', '123.', '.sig', 'notanint.deadbeef', None):
            self.assertFalse(M.verify_app_session(v), repr(v))

    def test_token_rotation_invalidates_sessions(self):
        # Sessions are keyed off the stored Bearer token, so rotating it
        # (what regenerate_token does) must invalidate outstanding cookies.
        value = M.mint_app_session()
        self.assertTrue(M.verify_app_session(value))
        with open(self._token_path, 'w') as f:
            f.write('rotated-token')
        self.assertFalse(M.verify_app_session(value))


class _FakeHeaders(dict):
    def get(self, k, default=''):
        return super().get(k, default)


class CookieParseUnit(unittest.TestCase):
    def _value(self, cookie_header):
        fake = type('H', (), {
            'APP_SESSION_COOKIE': server.BrowserHandler.APP_SESSION_COOKIE,
            'headers': _FakeHeaders(Cookie=cookie_header),
        })()
        return server.BrowserHandler._app_session_cookie_value(fake)

    def test_finds_cookie_among_others(self):
        self.assertEqual(self._value('a=1; kc_app_session=exp.sig; b=2'), 'exp.sig')

    def test_missing_cookie_is_empty(self):
        self.assertEqual(self._value('a=1; b=2'), '')
        self.assertEqual(self._value(''), '')

    def test_name_must_match_exactly(self):
        self.assertEqual(self._value('xkc_app_session=nope'), '')


class NextPathGuardUnit(unittest.TestCase):
    """The mint endpoint must only ever redirect into the app proxy."""

    RE = server.BrowserHandler._APP_SESSION_NEXT_RE

    def test_accepts_app_proxy_paths(self):
        for p in ('/api/app-proxy/3000/', '/api/app-proxy/8080',
                  '/api/app-proxy/3000/deep/path'):
            self.assertIsNotNone(self.RE.match(p), p)

    def test_accepts_terminal_proxy_paths(self):
        for p in ('/api/terminal-proxy', '/api/terminal-proxy/', '/api/terminal-proxy/?t=1'):
            self.assertIsNotNone(self.RE.match(p), p)

    def test_rejects_everything_else(self):
        for p in ('/', '/api/claude/tasks', '//evil.com/', 'https://evil.com',
                  '/api/app-proxy/notaport/', '', '/api/app-proxyx/1/',
                  '/api/terminal-proxyx/', '/api/terminal-proxy-evil/'):
            self.assertIsNone(self.RE.match(p), p)


if __name__ == '__main__':
    unittest.main()
