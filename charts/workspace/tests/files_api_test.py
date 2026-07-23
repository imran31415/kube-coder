"""Tests for the Files-manager endpoints on server.py.

Covers the write/read surface added for issue #92:
  * GET  /api/files/download  — attachment stream, traversal-guarded
  * GET  /api/files/preview   — text/image/binary descriptor, size cap
  * POST /api/files/rename    — move within /home/dev, no overwrite
  * DELETE /api/files         — file / empty-dir delete, guarded

Two suites:
  * FilesApiTests            — auth bypassed, HOME_DEV pinned to a tempdir, so
                               we exercise the happy paths + traversal guard.
  * FilesApiReadonlyTests    — READONLY_MODE on, proving every mutating verb is
                               server-enforced (403), not merely hidden in the UI.

The path-traversal guard (_resolve_under_home_dev) is also unit-tested directly.

Run with:
    cd charts/workspace && python3 -m unittest tests.files_api_test
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
import urllib.parse
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


class _Base(unittest.TestCase):
    """Boots a real ThreadingHTTPServer with HOME_DEV pinned to a tempdir."""

    READONLY = False
    # None => leave server.AUTH_MODE untouched; a string patches it for the suite
    # (so the AUTH_MODE=none public-demo gate can be exercised in isolation).
    AUTH_MODE = None

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = os.path.realpath(tempfile.mkdtemp(prefix='kc-files-'))
        # Seed a small tree.
        with open(os.path.join(cls.tmpdir, 'hello.txt'), 'w') as f:
            f.write('hello world\n')
        os.makedirs(os.path.join(cls.tmpdir, 'sub'))
        with open(os.path.join(cls.tmpdir, 'sub', 'nested.txt'), 'w') as f:
            f.write('nested\n')
        os.makedirs(os.path.join(cls.tmpdir, 'emptydir'))
        with open(os.path.join(cls.tmpdir, 'binary.bin'), 'wb') as f:
            f.write(b'\x00\x01\x02\x03BINARY')
        # Hidden (dot) credential-style files/dirs the public demo must not leak.
        os.makedirs(os.path.join(cls.tmpdir, '.claude-tasks'))
        with open(os.path.join(cls.tmpdir, '.claude-tasks', '.api-token'), 'w') as f:
            f.write('secret-token\n')
        os.makedirs(os.path.join(cls.tmpdir, '.config', 'gh'))
        with open(os.path.join(cls.tmpdir, '.config', 'gh', 'hosts.yml'), 'w') as f:
            f.write('github.com:\n  oauth_token: gho_secret\n')
        os.makedirs(os.path.join(cls.tmpdir, '.ssh'))
        with open(os.path.join(cls.tmpdir, '.ssh', 'id_ed25519'), 'w') as f:
            f.write('PRIVATE KEY\n')
        with open(os.path.join(cls.tmpdir, '.env'), 'w') as f:
            f.write('API_KEY=sk-secret\n')

        cls._home_save = server.BrowserHandler.HOME_DEV
        server.BrowserHandler.HOME_DEV = cls.tmpdir
        cls._auth_save = server.BrowserHandler.check_claude_auth
        server.BrowserHandler.check_claude_auth = lambda self: True
        cls._ro_save = server.READONLY_MODE
        server.READONLY_MODE = cls.READONLY
        cls._authmode_save = server.AUTH_MODE
        if cls.AUTH_MODE is not None:
            server.AUTH_MODE = cls.AUTH_MODE

        cls.port = _free_port()
        cls.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', cls.port), server.BrowserHandler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.BrowserHandler.HOME_DEV = cls._home_save
        server.BrowserHandler.check_claude_auth = cls._auth_save
        server.READONLY_MODE = cls._ro_save
        server.AUTH_MODE = cls._authmode_save
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _url(self, path):
        return f'http://127.0.0.1:{self.port}{path}'

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {'Content-Type': 'application/json'} if data else {}
        r = urllib.request.Request(self._url(path), data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                raw = resp.read()
                ctype = resp.headers.get('Content-Type', '')
                parsed = json.loads(raw) if raw and 'application/json' in ctype else raw
                return resp.status, parsed, resp
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw), e
            except Exception:
                return e.code, raw, e


class FilesApiGuardUnitTests(unittest.TestCase):
    """Directly unit-test the traversal guard classmethod."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='kc-guard-'))
        self._save = server.BrowserHandler.HOME_DEV
        server.BrowserHandler.HOME_DEV = self.tmp

    def tearDown(self):
        server.BrowserHandler.HOME_DEV = self._save
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_relative_stays_inside(self):
        got = server.BrowserHandler._resolve_under_home_dev('a/b')
        self.assertEqual(got, os.path.join(self.tmp, 'a', 'b'))

    def test_leading_slash_is_relative(self):
        got = server.BrowserHandler._resolve_under_home_dev('/screenshots')
        self.assertEqual(got, os.path.join(self.tmp, 'screenshots'))

    def test_dotdot_escape_rejected(self):
        with self.assertRaises(ValueError):
            server.BrowserHandler._resolve_under_home_dev('../etc/passwd')

    def test_absolute_escape_rejected(self):
        # lstrip('/') makes this relative, but the ../ still tries to climb out.
        with self.assertRaises(ValueError):
            server.BrowserHandler._resolve_under_home_dev('../../etc/shadow')

    def test_symlink_escape_rejected(self):
        # A symlink whose target is outside HOME_DEV resolves (realpath) to the
        # target and fails the containment check.
        outside = os.path.realpath(tempfile.mkdtemp(prefix='kc-outside-'))
        try:
            link = os.path.join(self.tmp, 'escape')
            os.symlink(outside, link)
            with self.assertRaises(ValueError):
                server.BrowserHandler._resolve_under_home_dev('escape/secret')
        finally:
            shutil.rmtree(outside, ignore_errors=True)


class FilesApiTests(_Base):
    READONLY = False

    def test_download_streams_attachment(self):
        status, body, resp = self._req('GET', '/api/files/download?path=hello.txt')
        self.assertEqual(status, 200)
        self.assertEqual(body, b'hello world\n')
        cd = resp.headers.get('Content-Disposition', '')
        self.assertIn('attachment', cd)
        self.assertIn('hello.txt', cd)
        # Never render inline on this origin.
        self.assertEqual(resp.headers.get('X-Content-Type-Options'), 'nosniff')
        self.assertEqual(resp.headers.get('Content-Type'), 'application/octet-stream')

    def test_download_missing_404(self):
        status, _body, _ = self._req('GET', '/api/files/download?path=nope.txt')
        self.assertEqual(status, 404)

    def test_download_traversal_rejected(self):
        q = urllib.parse.quote('../../etc/passwd', safe='')
        status, _body, _ = self._req('GET', f'/api/files/download?path={q}')
        self.assertEqual(status, 400)

    def test_download_directory_404(self):
        status, _body, _ = self._req('GET', '/api/files/download?path=sub')
        self.assertEqual(status, 404)

    def test_preview_text(self):
        status, body, _ = self._req('GET', '/api/files/preview?path=hello.txt')
        self.assertEqual(status, 200)
        self.assertEqual(body['kind'], 'text')
        self.assertEqual(body['content'], 'hello world\n')
        self.assertFalse(body['truncated'])

    def test_preview_binary(self):
        status, body, _ = self._req('GET', '/api/files/preview?path=binary.bin')
        self.assertEqual(status, 200)
        self.assertEqual(body['kind'], 'binary')

    def test_preview_truncates_large_text(self):
        big = os.path.join(self.tmpdir, 'big.txt')
        with open(big, 'w') as f:
            f.write('x' * (server.BrowserHandler.PREVIEW_MAX_BYTES + 500))
        try:
            status, body, _ = self._req('GET', '/api/files/preview?path=big.txt')
            self.assertEqual(status, 200)
            self.assertEqual(body['kind'], 'text')
            self.assertTrue(body['truncated'])
            self.assertEqual(len(body['content']), server.BrowserHandler.PREVIEW_MAX_BYTES)
        finally:
            os.remove(big)

    def test_preview_image_descriptor(self):
        # A .png (even with junk bytes) is classified by extension → image.
        img = os.path.join(self.tmpdir, 'pic.png')
        with open(img, 'wb') as f:
            f.write(b'\x89PNG\r\n')
        try:
            status, body, _ = self._req('GET', '/api/files/preview?path=pic.png')
            self.assertEqual(status, 200)
            self.assertEqual(body['kind'], 'image')
            self.assertEqual(body['path'], 'pic.png')
        finally:
            os.remove(img)

    def test_view_pdf_inline_with_ranges(self):
        pdf = os.path.join(self.tmpdir, 'doc.pdf')
        with open(pdf, 'wb') as f:
            f.write(b'%PDF-1.4\n' + b'x' * 100)
        try:
            status, body, resp = self._req('GET', '/api/files/view?path=doc.pdf')
            self.assertEqual(status, 200)
            self.assertEqual(resp.headers.get('Content-Type'), 'application/pdf')
            self.assertEqual(resp.headers.get('Content-Disposition'), 'inline')
            self.assertEqual(resp.headers.get('X-Content-Type-Options'), 'nosniff')
            self.assertEqual(resp.headers.get('Accept-Ranges'), 'bytes')
            # PDFs must NOT get the sandbox CSP — it breaks the browser viewer.
            self.assertIsNone(resp.headers.get('Content-Security-Policy'))
            self.assertTrue(body.startswith(b'%PDF'))
        finally:
            os.remove(pdf)

    def test_view_pdf_range_request(self):
        pdf = os.path.join(self.tmpdir, 'ranged.pdf')
        with open(pdf, 'wb') as f:
            f.write(b'%PDF-1.4\n' + b'abcdefghij')
        try:
            r = urllib.request.Request(self._url('/api/files/view?path=ranged.pdf'),
                                       headers={'Range': 'bytes=0-3'})
            with urllib.request.urlopen(r, timeout=5) as resp:
                self.assertEqual(resp.status, 206)
                self.assertEqual(resp.read(), b'%PDF')
                self.assertIn('bytes 0-3/', resp.headers.get('Content-Range', ''))
        finally:
            os.remove(pdf)

    def test_view_html_is_sandboxed(self):
        html = os.path.join(self.tmpdir, 'page.html')
        with open(html, 'w') as f:
            f.write('<h1>hi</h1><script>alert(1)</script>')
        try:
            status, body, resp = self._req('GET', '/api/files/view?path=page.html')
            self.assertEqual(status, 200)
            self.assertEqual(resp.headers.get('Content-Type'), 'text/html')
            # The XSS defusal: unique origin, scripts blocked.
            self.assertEqual(resp.headers.get('Content-Security-Policy'), 'sandbox')
            self.assertEqual(resp.headers.get('X-Content-Type-Options'), 'nosniff')
            self.assertIn(b'<h1>hi</h1>', body)
        finally:
            os.remove(html)

    def test_view_svg_is_sandboxed(self):
        svg = os.path.join(self.tmpdir, 'pic.svg')
        with open(svg, 'w') as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg"></svg>')
        try:
            status, _body, resp = self._req('GET', '/api/files/view?path=pic.svg')
            self.assertEqual(status, 200)
            self.assertEqual(resp.headers.get('Content-Type'), 'image/svg+xml')
            self.assertEqual(resp.headers.get('Content-Security-Policy'), 'sandbox')
        finally:
            os.remove(svg)

    def test_view_rejects_non_document_type(self):
        # text/plain is rendered client-side via /preview, never served inline here.
        status, _body, _ = self._req('GET', '/api/files/view?path=hello.txt')
        self.assertEqual(status, 415)

    def test_view_missing_404(self):
        status, _body, _ = self._req('GET', '/api/files/view?path=nope.pdf')
        self.assertEqual(status, 404)

    def test_view_traversal_rejected(self):
        q = urllib.parse.quote('../../etc/passwd', safe='')
        status, _body, _ = self._req('GET', f'/api/files/view?path={q}')
        self.assertEqual(status, 400)

    def test_rename_moves_file(self):
        with open(os.path.join(self.tmpdir, 'old.txt'), 'w') as f:
            f.write('x')
        status, body, _ = self._req('POST', '/api/files/rename',
                                    {'from': 'old.txt', 'to': 'renamed.txt'})
        self.assertEqual(status, 200)
        self.assertEqual(body['path'], 'renamed.txt')
        self.assertTrue(os.path.exists(os.path.join(self.tmpdir, 'renamed.txt')))
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, 'old.txt')))

    def test_rename_no_overwrite(self):
        with open(os.path.join(self.tmpdir, 'a.txt'), 'w') as f:
            f.write('a')
        with open(os.path.join(self.tmpdir, 'b.txt'), 'w') as f:
            f.write('b')
        status, _body, _ = self._req('POST', '/api/files/rename', {'from': 'a.txt', 'to': 'b.txt'})
        self.assertEqual(status, 409)

    def test_rename_traversal_rejected(self):
        status, _body, _ = self._req('POST', '/api/files/rename',
                                     {'from': 'hello.txt', 'to': '../escape.txt'})
        self.assertEqual(status, 400)

    def test_delete_file(self):
        p = os.path.join(self.tmpdir, 'trash.txt')
        with open(p, 'w') as f:
            f.write('bye')
        status, body, _ = self._req('DELETE', '/api/files?path=trash.txt')
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])
        self.assertFalse(os.path.exists(p))

    def test_delete_empty_dir(self):
        d = os.path.join(self.tmpdir, 'gone')
        os.makedirs(d)
        status, _body, _ = self._req('DELETE', '/api/files?path=gone')
        self.assertEqual(status, 200)
        self.assertFalse(os.path.exists(d))

    def test_delete_nonempty_dir_rejected(self):
        status, _body, _ = self._req('DELETE', '/api/files?path=sub')
        self.assertEqual(status, 409)
        self.assertTrue(os.path.exists(os.path.join(self.tmpdir, 'sub')))

    def test_delete_root_rejected(self):
        status, _body, _ = self._req('DELETE', '/api/files?path=')
        self.assertEqual(status, 400)

    def test_delete_traversal_rejected(self):
        q = urllib.parse.quote('../../etc/hosts', safe='')
        status, _body, _ = self._req('DELETE', f'/api/files?path={q}')
        self.assertEqual(status, 400)


class FilesApiReadonlyTests(_Base):
    READONLY = True

    def test_delete_blocked(self):
        status, body, _ = self._req('DELETE', '/api/files?path=hello.txt')
        self.assertEqual(status, 403)
        self.assertEqual(body.get('code'), 'readonly')
        self.assertTrue(os.path.exists(os.path.join(self.tmpdir, 'hello.txt')))

    def test_rename_blocked(self):
        status, body, _ = self._req('POST', '/api/files/rename',
                                    {'from': 'hello.txt', 'to': 'x.txt'})
        self.assertEqual(status, 403)
        self.assertEqual(body.get('code'), 'readonly')

    def test_download_allowed_in_readonly(self):
        # Reads stay available in the public demo.
        status, _body, _ = self._req('GET', '/api/files/download?path=hello.txt')
        self.assertEqual(status, 200)

    def test_preview_allowed_in_readonly(self):
        status, body, _ = self._req('GET', '/api/files/preview?path=hello.txt')
        self.assertEqual(status, 200)
        self.assertEqual(body['kind'], 'text')

    def test_view_allowed_in_readonly(self):
        pdf = os.path.join(self.tmpdir, 'ro.pdf')
        with open(pdf, 'wb') as f:
            f.write(b'%PDF-1.4\n')
        try:
            status, _body, _ = self._req('GET', '/api/files/view?path=ro.pdf')
            self.assertEqual(status, 200)
        finally:
            os.remove(pdf)


class FilesApiPublicDemoTests(_Base):
    """Finding 3: AUTH_MODE=none + READONLY_MODE=true is the UNAUTHENTICATED
    public demo. Reads stay available, but hidden (dot) path segments —
    credential/config files that directory listings already hide — must NOT be
    downloadable/previewable/viewable. Traversal + non-hidden reads unchanged."""

    READONLY = True
    AUTH_MODE = 'none'

    def test_public_cannot_download_api_token(self):
        q = urllib.parse.quote('.claude-tasks/.api-token', safe='')
        status, _body, _ = self._req('GET', f'/api/files/download?path={q}')
        self.assertEqual(status, 404)

    def test_public_cannot_download_gh_hosts(self):
        q = urllib.parse.quote('.config/gh/hosts.yml', safe='')
        status, _body, _ = self._req('GET', f'/api/files/download?path={q}')
        self.assertEqual(status, 404)

    def test_public_cannot_download_ssh_key(self):
        q = urllib.parse.quote('.ssh/id_ed25519', safe='')
        status, _body, _ = self._req('GET', f'/api/files/download?path={q}')
        self.assertEqual(status, 404)

    def test_public_cannot_download_dotfile_at_root(self):
        status, _body, _ = self._req('GET', '/api/files/download?path=.env')
        self.assertEqual(status, 404)

    def test_public_cannot_preview_hidden(self):
        q = urllib.parse.quote('.claude-tasks/.api-token', safe='')
        status, _body, _ = self._req('GET', f'/api/files/preview?path={q}')
        self.assertEqual(status, 404)

    def test_public_cannot_raw_hidden(self):
        # even a media file under a hidden dir is refused
        img = os.path.join(self.tmpdir, '.config', 'pic.png')
        with open(img, 'wb') as f:
            f.write(b'\x89PNG\r\n')
        q = urllib.parse.quote('.config/pic.png', safe='')
        status, _body, _ = self._req('GET', f'/api/files/raw?path={q}')
        self.assertEqual(status, 404)

    def test_public_cannot_view_hidden(self):
        pdf = os.path.join(self.tmpdir, '.config', 'secret.pdf')
        with open(pdf, 'wb') as f:
            f.write(b'%PDF-1.4\n')
        q = urllib.parse.quote('.config/secret.pdf', safe='')
        status, _body, _ = self._req('GET', f'/api/files/view?path={q}')
        self.assertEqual(status, 404)

    def test_public_can_download_normal_file(self):
        # Public demo fixtures / non-hidden files stay downloadable.
        status, body, _ = self._req('GET', '/api/files/download?path=hello.txt')
        self.assertEqual(status, 200)
        self.assertEqual(body, b'hello world\n')

    def test_public_can_download_nested_normal_file(self):
        status, body, _ = self._req('GET', '/api/files/download?path=sub/nested.txt')
        self.assertEqual(status, 200)
        self.assertEqual(body, b'nested\n')

    def test_public_can_preview_normal_file(self):
        status, body, _ = self._req('GET', '/api/files/preview?path=hello.txt')
        self.assertEqual(status, 200)
        self.assertEqual(body['kind'], 'text')

    def test_public_traversal_still_rejected(self):
        q = urllib.parse.quote('../../etc/passwd', safe='')
        status, _body, _ = self._req('GET', f'/api/files/download?path={q}')
        self.assertEqual(status, 400)

    def test_public_symlink_escape_still_rejected(self):
        outside = os.path.realpath(tempfile.mkdtemp(prefix='kc-outside-'))
        try:
            with open(os.path.join(outside, 'secret'), 'w') as f:
                f.write('x')
            link = os.path.join(self.tmpdir, 'escape')
            os.symlink(outside, link)
            q = urllib.parse.quote('escape/secret', safe='')
            status, _body, _ = self._req('GET', f'/api/files/download?path={q}')
            self.assertEqual(status, 400)
        finally:
            shutil.rmtree(outside, ignore_errors=True)
            link = os.path.join(self.tmpdir, 'escape')
            if os.path.islink(link):
                os.remove(link)


class FilesApiAuthedHiddenAccessTests(_Base):
    """The none-mode public gate must NOT touch authenticated modes: an
    oauth2/basic user keeps full access to their own dotfiles."""

    READONLY = False
    AUTH_MODE = 'oauth2'

    def test_authed_can_download_hidden(self):
        q = urllib.parse.quote('.claude-tasks/.api-token', safe='')
        status, body, _ = self._req('GET', f'/api/files/download?path={q}')
        self.assertEqual(status, 200)
        self.assertEqual(body, b'secret-token\n')

    def test_authed_can_preview_hidden(self):
        q = urllib.parse.quote('.env', safe='')
        status, body, _ = self._req('GET', f'/api/files/preview?path={q}')
        self.assertEqual(status, 200)
        self.assertEqual(body['kind'], 'text')


class FilesApiPublicRootTests(_Base):
    """Optional PUBLIC_FILE_ROOT opt-in confines public reads to a subdir."""

    READONLY = True
    AUTH_MODE = 'none'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        os.makedirs(os.path.join(cls.tmpdir, 'demo'))
        with open(os.path.join(cls.tmpdir, 'demo', 'sample.txt'), 'w') as f:
            f.write('sample\n')
        cls._pfr_save = server.PUBLIC_FILE_ROOT
        server.PUBLIC_FILE_ROOT = 'demo'

    @classmethod
    def tearDownClass(cls):
        server.PUBLIC_FILE_ROOT = cls._pfr_save
        super().tearDownClass()

    def test_inside_root_allowed(self):
        status, body, _ = self._req('GET', '/api/files/download?path=demo/sample.txt')
        self.assertEqual(status, 200)
        self.assertEqual(body, b'sample\n')

    def test_outside_root_rejected(self):
        status, _body, _ = self._req('GET', '/api/files/download?path=hello.txt')
        self.assertEqual(status, 404)


class PublicDemoPredicateTests(unittest.TestCase):
    """Unit-test the startup-warning predicate + hidden-segment helper without
    booting a server or capturing stdout."""

    def setUp(self):
        self._ro = server.READONLY_MODE
        self._am = server.AUTH_MODE
        self._ack = server.PUBLIC_DEMO_ACK
        self._pfr = server.PUBLIC_FILE_ROOT

    def tearDown(self):
        server.READONLY_MODE = self._ro
        server.AUTH_MODE = self._am
        server.PUBLIC_DEMO_ACK = self._ack
        server.PUBLIC_FILE_ROOT = self._pfr

    def test_warning_fires_in_unacked_public_mode(self):
        server.AUTH_MODE = 'none'
        server.READONLY_MODE = True
        server.PUBLIC_DEMO_ACK = False
        server.PUBLIC_FILE_ROOT = ''
        self.assertTrue(server._public_mode_active())
        self.assertTrue(server._public_demo_needs_ack())

    def test_ack_silences_warning(self):
        server.AUTH_MODE = 'none'
        server.READONLY_MODE = True
        server.PUBLIC_DEMO_ACK = True
        server.PUBLIC_FILE_ROOT = ''
        self.assertFalse(server._public_demo_needs_ack())

    def test_public_file_root_silences_warning(self):
        server.AUTH_MODE = 'none'
        server.READONLY_MODE = True
        server.PUBLIC_DEMO_ACK = False
        server.PUBLIC_FILE_ROOT = 'demo'
        self.assertFalse(server._public_demo_needs_ack())

    def test_authed_mode_is_not_public(self):
        server.AUTH_MODE = 'oauth2'
        server.READONLY_MODE = True
        self.assertFalse(server._public_mode_active())
        self.assertFalse(server._public_demo_needs_ack())

    def test_hidden_segment_helper(self):
        save = server.BrowserHandler.HOME_DEV
        server.BrowserHandler.HOME_DEV = '/home/dev'
        try:
            H = server.BrowserHandler
            self.assertTrue(H._path_has_hidden_segment('/home/dev/.ssh/id_ed25519'))
            self.assertTrue(H._path_has_hidden_segment('/home/dev/.claude-tasks/.api-token'))
            self.assertTrue(H._path_has_hidden_segment('/home/dev/a/.git/config'))
            self.assertFalse(H._path_has_hidden_segment('/home/dev/sub/nested.txt'))
            self.assertFalse(H._path_has_hidden_segment('/home/dev'))
        finally:
            server.BrowserHandler.HOME_DEV = save


if __name__ == '__main__':
    unittest.main()
