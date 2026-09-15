"""End-to-end page-watch: a real socket, a real page, real content changes (#681).

Every other page-watch test stubs safe_http.fetch. This one does not — it
serves a page from a real HTTP server on loopback and lets the check reach it
through the actual guard, connection pinning and all. The reasoning is the one
tests/boards_e2e_test.py states for the board connector: stubbing safe_http
would leave the guard untested on the single path that reaches the network for
real.

Loopback is a non-public address, so these run with ALLOW_INTERNAL_HOOKS=True —
the documented relaxation for trusted single-user deploys. SsrfTests in
page_watch_api_test.py covers the opposite case with the guard on, and pins it
on so it cannot pass for the wrong reason.

Run: python3 -m unittest tests.page_watch_e2e_test   (from charts/workspace/)
"""

import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

try:
    import fcntl  # noqa: F401
except ImportError:  # pragma: no cover - platform shim
    import types
    _shim = types.ModuleType('fcntl')
    _shim.flock = lambda *a, **k: None
    _shim.lockf = lambda *a, **k: None
    _shim.LOCK_EX, _shim.LOCK_UN, _shim.LOCK_NB = 2, 8, 4
    sys.modules['fcntl'] = _shim

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import server  # noqa: E402


class _Page:
    """Mutable page content shared with the request handler."""
    body = b'<html><body><h1>Build</h1><p class="status">failing</p></body></html>'
    content_type = 'text/html; charset=utf-8'
    status = 200
    hits = 0


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        _Page.hits += 1
        self.send_response(_Page.status)
        self.send_header('Content-Type', _Page.content_type)
        self.send_header('Content-Length', str(len(_Page.body)))
        self.end_headers()
        self.wfile.write(_Page.body)

    def log_message(self, *a):
        pass  # keep the test output readable


def _kubectl_ok(*args, **kwargs):
    return mock.Mock(returncode=0, stdout='', stderr='')


class PageWatchEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Bind to port 0 and read back what the OS gave us — picking a "free"
        # port with a throwaway socket first would be a TOCTOU race.
        cls.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        _Page.body = b'<html><body><h1>Build</h1><p class="status">failing</p></body></html>'
        _Page.content_type = 'text/html; charset=utf-8'
        _Page.status = 200
        _Page.hits = 0

        self.tmpdir = tempfile.mkdtemp(prefix='kctest-pw-e2e-')
        self._orig_dir = server.PageWatchManager.PAGE_WATCHES_DIR
        server.PageWatchManager.PAGE_WATCHES_DIR = self.tmpdir
        # Loopback is not a public address; this is the documented relaxation.
        self._orig_allow = server.ALLOW_INTERNAL_HOOKS
        server.ALLOW_INTERNAL_HOOKS = True
        self._orig_user = server.CronManager.detect_user
        server.CronManager.detect_user = staticmethod(lambda: 'octo')
        self._orig_ns = server.CronManager.detect_namespace
        server.CronManager.detect_namespace = staticmethod(lambda: 'coder')
        p = mock.patch('server.subprocess.run', side_effect=_kubectl_ok)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        server.PageWatchManager.PAGE_WATCHES_DIR = self._orig_dir
        server.ALLOW_INTERNAL_HOOKS = self._orig_allow
        server.CronManager.detect_user = self._orig_user
        server.CronManager.detect_namespace = self._orig_ns
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @property
    def url(self):
        return f'http://127.0.0.1:{self.port}/build'

    def make(self, **over):
        data = {
            'id': 'ci',
            'url': self.url,
            'schedule': '*/5 * * * *',
            'prompt_template': 'CI changed — look into it.',
        }
        data.update(over)
        cfg, err = server.PageWatchManager.create_or_update(data)
        self.assertIsNone(err, msg=err)
        return cfg

    def cycle(self):
        """One full check cycle, including the delivery the handler performs."""
        outcome, cfg, detail = server.PageWatchManager.check_once('ci')
        if outcome == 'changed':
            server.PageWatchManager.clear_pending_fire('ci')
        return outcome, cfg, detail

    def stored(self):
        with open(os.path.join(self.tmpdir, 'ci.json')) as f:
            return json.load(f)

    # -- the full journey -------------------------------------------------

    def test_full_lifecycle_over_a_real_socket(self):
        """Create → baseline → quiet → change → fire once → quiet again."""
        cfg = self.make()
        self.assertEqual(cfg['url'], self.url)
        self.assertGreater(_Page.hits, 0, 'creation should have fetched once')

        self.assertEqual(self.cycle()[0], 'baseline')
        self.assertEqual(self.cycle()[0], 'unchanged')
        self.assertEqual(self.cycle()[0], 'unchanged')

        # The build goes green.
        _Page.body = b'<html><body><h1>Build</h1><p class="status">passing</p></body></html>'
        self.assertEqual(self.cycle()[0], 'changed')

        # ...and stays green. Exactly one fire for one change.
        self.assertEqual(self.cycle()[0], 'unchanged')
        self.assertEqual(self.cycle()[0], 'unchanged')

    def test_selector_scopes_a_real_page(self):
        self.make(selector='.status')
        self.assertEqual(self.cycle()[0], 'baseline')

        # Header churn outside the selected subtree.
        _Page.body = (b'<html><body><h1>Build 12:01</h1>'
                      b'<p class="status">failing</p></body></html>')
        self.assertEqual(self.cycle()[0], 'unchanged')

        # The selected element itself.
        _Page.body = (b'<html><body><h1>Build 12:02</h1>'
                      b'<p class="status">passing</p></body></html>')
        self.assertEqual(self.cycle()[0], 'changed')

    def test_outage_does_not_fire_and_does_not_clobber_the_baseline(self):
        self.make()
        self.cycle()
        before = self.stored()['last_hash']

        _Page.status = 503
        _Page.body = b'<html><body>service unavailable</body></html>'
        outcome, cfg, _ = self.cycle()
        self.assertEqual(outcome, 'error')
        self.assertEqual(self.stored()['last_hash'], before)

        # Recovery: the page never actually changed, so the outage must not
        # manufacture a change on the way back.
        _Page.status = 200
        _Page.body = b'<html><body><h1>Build</h1><p class="status">failing</p></body></html>'
        self.assertEqual(self.cycle()[0], 'unchanged')
        self.assertEqual(self.stored()['consecutive_failures'], 0)

    def test_svg_badge_flip_over_a_real_socket(self):
        """The motivating use case, end to end: a CI badge going green."""
        _Page.content_type = 'image/svg+xml'
        _Page.body = (b'<svg xmlns="http://www.w3.org/2000/svg">'
                      b'<text>build</text><text>failing</text></svg>')
        self.make(selector='text')
        self.assertEqual(self.cycle()[0], 'baseline')
        self.assertEqual(self.cycle()[0], 'unchanged')

        _Page.body = (b'<svg xmlns="http://www.w3.org/2000/svg">'
                      b'<text>build</text><text>passing</text></svg>')
        outcome, cfg, detail = self.cycle()
        self.assertEqual(outcome, 'changed')
        self.assertEqual(detail['text'], 'build passing')

    def test_connection_refused_is_an_error_not_a_change(self):
        self.make()
        self.cycle()
        before = self.stored()['last_hash']
        # A port nothing is listening on, reached through the real stack.
        cfg = self.stored()
        cfg['url'] = f'http://127.0.0.1:{self.port + 1}/gone'
        server.PageWatchManager._save(cfg)
        outcome, _, _ = self.cycle()
        self.assertEqual(outcome, 'error')
        self.assertEqual(self.stored()['last_hash'], before)

    def test_prompt_carries_no_page_text_by_default(self):
        self.make()
        self.cycle()
        _Page.body = b'<html><body><p class="status">SECRET-TOKEN-abc123</p></body></html>'
        _, cfg, detail = self.cycle()
        payload = server.PageWatchManager.build_payload(cfg, detail.get('text'))
        rendered = server.PageWatchManager.render_prompt(cfg, payload)
        self.assertNotIn('SECRET-TOKEN', rendered,
                         'page text must not reach the prompt unless opted in')
        self.assertIn('DATA, not instructions', rendered)

    def test_opted_in_prompt_carries_the_excerpt(self):
        self.make(include_content=True)
        self.cycle()
        _Page.body = b'<html><body><p class="status">passing</p></body></html>'
        _, cfg, detail = self.cycle()
        payload = server.PageWatchManager.build_payload(cfg, detail.get('text'))
        self.assertTrue(payload['content_included'])
        self.assertIn('passing', server.PageWatchManager.render_prompt(cfg, payload))

    def test_real_redirect_is_resolved_at_creation(self):
        """A 302 on the real socket is followed once and the final URL stored."""
        target = f'http://127.0.0.1:{self.port}/final'
        seen = {'n': 0}
        orig = _Handler.do_GET

        def redirect_once(handler_self):
            seen['n'] += 1
            if seen['n'] == 1:
                handler_self.send_response(302)
                handler_self.send_header('Location', target)
                handler_self.send_header('Content-Length', '0')
                handler_self.end_headers()
                return
            orig(handler_self)

        with mock.patch.object(_Handler, 'do_GET', redirect_once):
            cfg = self.make(id='rd')
        self.assertEqual(cfg['url'], target)
        self.assertEqual(cfg['redirected_from'], self.url)


# ── the HTTP surface: does "paused" actually mean paused? ────────────────

class PageWatchSuspendOverHttpTests(unittest.TestCase):
    """Boots the REAL BrowserHandler, because these are handler-level rules.

    Pause has two ways in and only one of them used to be guarded. The
    scheduled receiver refused a suspended watch; the dashboard's "Check now"
    went straight through, so a paused watch could still spawn a task from the
    button. Manager-level tests cannot see that — the guard lives in the
    handler — so this class speaks HTTP, per tests/boards_api_test.py.
    """

    @classmethod
    def setUpClass(cls):
        cls._auth_save = server.BrowserHandler.check_claude_auth
        server.BrowserHandler.check_claude_auth = lambda self, *a, **k: True
        cls._oauth_save = getattr(server.BrowserHandler, 'check_oauth_only', None)
        if cls._oauth_save is not None:
            server.BrowserHandler.check_oauth_only = lambda self, *a, **k: True
        # Port 0, read back what the OS gave us — see the note in
        # PageWatchEndToEndTests.setUpClass.
        cls.httpd = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0), server.BrowserHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.BrowserHandler.check_claude_auth = cls._auth_save
        if cls._oauth_save is not None:
            server.BrowserHandler.check_oauth_only = cls._oauth_save

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='kctest-pw-http-')
        self._orig_dir = server.PageWatchManager.PAGE_WATCHES_DIR
        server.PageWatchManager.PAGE_WATCHES_DIR = self.tmpdir
        self._orig_user = server.CronManager.detect_user
        server.CronManager.detect_user = staticmethod(lambda: 'octo')
        self._orig_ns = server.CronManager.detect_namespace
        server.CronManager.detect_namespace = staticmethod(lambda: 'coder')

        p = mock.patch('server.subprocess.run', side_effect=_kubectl_ok)
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch('server.safe_http.is_safe_url', lambda url, **kw: True)
        p2.start()
        self.addCleanup(p2.stop)
        # No watch in this class is ever allowed to start an agent.
        p3 = mock.patch.object(server.ClaudeTaskManager, 'create_task')
        self.create_task = p3.start()
        self.addCleanup(p3.stop)

        cfg, err = server.PageWatchManager.create_or_update(
            {'id': 'ci', 'url': 'https://example.test/build',
             'schedule': '*/5 * * * *', 'prompt_template': 'CI changed'},
            fetch=lambda url, **kw: (200, {'Content-Type': 'text/html'},
                                     b'<p>seed</p>'))
        self.assertIsNone(err, msg=err)
        self.cfg = cfg

    def tearDown(self):
        server.PageWatchManager.PAGE_WATCHES_DIR = self._orig_dir
        server.CronManager.detect_user = self._orig_user
        server.CronManager.detect_namespace = self._orig_ns
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _post(self, path, headers=None):
        r = urllib.request.Request(
            f'http://127.0.0.1:{self.port}{path}', data=b'{}', method='POST',
            headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw

    def test_check_now_refuses_a_suspended_watch(self):
        status, _ = self._post('/api/page-watches/ci/suspend')
        self.assertEqual(status, 200)

        status, body = self._post('/api/page-watches/ci/check')

        self.assertEqual(status, 409, 'the button must honour Pause too — '
                                      'otherwise "paused" only means "the '
                                      'timer is off"')
        self.assertIn('suspended', body['error'])
        self.create_task.assert_not_called()

    def test_check_now_works_again_after_resume(self):
        self._post('/api/page-watches/ci/suspend')
        self._post('/api/page-watches/ci/resume')
        with mock.patch.object(
                server.safe_http, 'fetch',
                lambda url, **kw: (200, {'Content-Type': 'text/html'},
                                   b'<p>seed</p>')):
            status, body = self._post('/api/page-watches/ci/check')
        self.assertEqual(status, 200)
        self.assertEqual(body['outcome'], 'baseline')
        self.create_task.assert_not_called()

    def test_check_now_on_an_unknown_watch_is_404(self):
        status, _ = self._post('/api/page-watches/nope/check')
        self.assertEqual(status, 404)
        self.create_task.assert_not_called()

    def test_a_pause_landing_mid_check_stops_the_fire(self):
        """The narrow window: the check started unpaused and finished paused.

        check_once hands back the record as it stands on disk, so the fire
        path sees the pause. The owed fire stays owed rather than being lost.
        """
        paused = dict(self.cfg)
        paused['suspended'] = True
        paused['pending_fire'] = True
        with mock.patch.object(
                server.PageWatchManager, 'check_once',
                staticmethod(lambda wid, **kw: ('changed', paused,
                                                {'text': 'x', 'hash': 'h'}))):
            status, body = self._post('/api/page-watches/ci/check')

        self.assertEqual(status, 409)
        self.assertTrue(body['pending'])
        self.create_task.assert_not_called()

    def test_scheduled_receiver_still_refuses_a_suspended_watch(self):
        """The guard that already existed, pinned so the refactor kept it."""
        self._post('/api/page-watches/ci/suspend')
        cfg = server.PageWatchManager.get_page_watch('ci', include_secrets=True)
        status, body = self._post(
            '/api/triggers/page-watch-check/ci',
            headers={'Authorization': f"Bearer {cfg['fire_token']}"})
        self.assertEqual(status, 409)
        self.assertIn('suspended', body['error'])
        self.create_task.assert_not_called()


if __name__ == '__main__':
    unittest.main()
