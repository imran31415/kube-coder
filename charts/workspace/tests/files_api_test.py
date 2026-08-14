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
import io
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
import zipfile
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

    def _upload(self, dest, filename, body, extract=None):
        """POST raw bytes to /api/files/upload (issue #356 upload surface)."""
        headers = {
            'X-Dest-Path': urllib.parse.quote(dest),
            'X-Filename': urllib.parse.quote(filename),
            'Content-Type': 'application/octet-stream',
        }
        if extract:
            headers['X-Extract'] = extract
        r = urllib.request.Request(self._url('/api/files/upload'),
                                   data=body, method='POST', headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, raw

    @staticmethod
    def _zip_bytes(members):
        buf = io.BytesIO()
        # Deflate, so the zip-bomb test's zeros actually compress.
        with zipfile.ZipFile(buf, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            for name, data in members:
                zf.writestr(name, data)
        return buf.getvalue()


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

    # ── Upload + zip extraction (issue #356) ─────────────────────────────

    def test_upload_writes_file(self):
        status, body = self._upload('up', 'hello.bin', b'abc123')
        self.assertEqual(status, 201)
        self.assertEqual(body['size'], 6)
        with open(os.path.join(self.tmpdir, 'up', 'hello.bin'), 'rb') as f:
            self.assertEqual(f.read(), b'abc123')

    def test_zip_upload_extracts_tree(self):
        data = self._zip_bytes([('proj/a.txt', 'A'), ('proj/sub/', ''), ('proj/sub/b.txt', 'B')])
        status, out = self._upload('unpacked', 'proj.zip', data, extract='zip')
        self.assertEqual(status, 201)
        self.assertEqual(out['extracted'], 2)
        self.assertEqual(out['path'], 'unpacked')
        with open(os.path.join(self.tmpdir, 'unpacked', 'proj', 'sub', 'b.txt')) as f:
            self.assertEqual(f.read(), 'B')
        # Only the unpacked tree remains — the staged archive is deleted.
        self.assertEqual(os.listdir(os.path.join(self.tmpdir, 'unpacked')), ['proj'])

    def test_zip_slip_rejected_before_any_write(self):
        evil = self._zip_bytes([('ok.txt', 'fine'), ('../evil.txt', 'x')])
        status, out = self._upload('slipdir', 'evil.zip', evil, extract='1')
        self.assertEqual(status, 400)
        self.assertIn('unsafe path', out['error'])
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, 'evil.txt')))
        # All-or-nothing: names are vetted BEFORE extraction, so the safe
        # member wasn't written either, and the staged archive is gone.
        self.assertEqual(os.listdir(os.path.join(self.tmpdir, 'slipdir')), [])

    def test_extract_requires_zip_filename(self):
        status, out = self._upload('up', 'notzip.txt', b'abc', extract='zip')
        self.assertEqual(status, 400)
        self.assertIn('.zip', out['error'])

    def test_bad_zip_bytes_rejected(self):
        status, out = self._upload('up', 'garbage.zip', b'this is not a zip', extract='zip')
        self.assertEqual(status, 400)
        self.assertIn('not a valid zip', out['error'])

    def test_zip_bomb_rejected_by_uncompressed_size(self):
        # 300 KB of zeros deflates to a few hundred bytes, so the request body
        # passes the Content-Length cap — only the expansion check catches it.
        data = self._zip_bytes([('zeros.bin', b'\0' * 300_000)])
        save = server.BrowserHandler.MAX_UPLOAD_BYTES
        server.BrowserHandler.MAX_UPLOAD_BYTES = 100_000
        try:
            status, out = self._upload('up', 'bomb.zip', data, extract='zip')
        finally:
            server.BrowserHandler.MAX_UPLOAD_BYTES = save
        self.assertEqual(status, 400)
        self.assertIn('expands to', out['error'])
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, 'up', 'zeros.bin')))

    def test_zip_symlink_members_skipped(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            info = zipfile.ZipInfo('link')
            info.external_attr = 0o120777 << 16  # symlink mode bits
            zf.writestr(info, '/etc/passwd')
            zf.writestr('real.txt', 'ok')
        status, out = self._upload('symdir', 'sym.zip', buf.getvalue(), extract='zip')
        self.assertEqual(status, 201)
        self.assertEqual(out['extracted'], 1)
        self.assertFalse(os.path.lexists(os.path.join(self.tmpdir, 'symdir', 'link')))
        self.assertTrue(os.path.exists(os.path.join(self.tmpdir, 'symdir', 'real.txt')))


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

    def test_upload_blocked(self):
        status, _body = self._upload('up', 'a.txt', b'x')
        self.assertEqual(status, 403)
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, 'up', 'a.txt')))

    def test_zip_extract_blocked(self):
        status, _body = self._upload('up', 'a.zip', self._zip_bytes([('a.txt', 'x')]),
                                     extract='zip')
        self.assertEqual(status, 403)

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


class UploadSizeParsingTests(unittest.TestCase):
    """The chart hands these bounds over as k8s-style strings (#556)."""

    def test_plain_bytes_and_suffixes(self):
        cases = {
            '1024': 1024,
            '2Gi': 2 * 1024 ** 3,
            '2GiB': 2 * 1024 ** 3,
            '500Mi': 500 * 1024 ** 2,
            '1G': 10 ** 9,
            '1.5Gi': int(1.5 * 1024 ** 3),
            '512B': 512,
        }
        for text, want in cases.items():
            self.assertEqual(server.parse_size_bytes(text, -1), want, text)

    def test_zero_disables_rather_than_defaulting(self):
        # "0" must survive as 0 — that is how an operator turns a guard off.
        self.assertEqual(server.parse_size_bytes('0', 999), 0)

    def test_missing_falls_back_to_default(self):
        self.assertEqual(server.parse_size_bytes(None, 7), 7)
        self.assertEqual(server.parse_size_bytes('  ', 7), 7)

    def test_garbage_falls_back_rather_than_raising(self):
        # A typo'd chart value must not take the dashboard down at import.
        with mock.patch.object(server.sys, 'stderr', io.StringIO()):
            self.assertEqual(server.parse_size_bytes('two gigs', 7), 7)
            self.assertEqual(server.parse_size_bytes('-5Gi', 7), 7)
            self.assertEqual(server.parse_size_bytes('10Xi', 7), 7)


class ManagedUploadDirTests(unittest.TestCase):
    """Which destinations the quota governs (#556). A project file the user
    deliberately uploaded is NOT upload storage."""

    HOME = '/home/dev'

    def test_managed_destinations(self):
        for rel in ('uploads', 'uploads/t-1', 'uploads/t-1/deep/er',
                    '.claude-tasks/t-1/attachments',
                    '.claude-tasks/t-1/attachments/img-2026'):
            self.assertTrue(
                server.is_managed_upload_dir(self.HOME, os.path.join(self.HOME, rel)), rel)

    def test_unmanaged_destinations(self):
        for rel in ('', 'myproject', 'myproject/data', 'screenshots',
                    '.claude-tasks', '.claude-tasks/t-1',
                    # task state/transcripts are not upload storage
                    '.claude-tasks/t-1/logs', 'uploads-elsewhere'):
            self.assertFalse(
                server.is_managed_upload_dir(self.HOME, os.path.join(self.HOME, rel)), rel)

    def test_outside_home_is_not_managed(self):
        self.assertFalse(server.is_managed_upload_dir(self.HOME, '/etc'))

    def test_usage_counts_only_managed_dirs(self):
        tmp = os.path.realpath(tempfile.mkdtemp(prefix='kc-quota-'))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        os.makedirs(os.path.join(tmp, 'uploads', 't-1'))
        with open(os.path.join(tmp, 'uploads', 't-1', 'a.bin'), 'wb') as f:
            f.write(b'x' * 100)
        os.makedirs(os.path.join(tmp, '.claude-tasks', 't-2', 'attachments'))
        with open(os.path.join(tmp, '.claude-tasks', 't-2', 'attachments', 'b.png'), 'wb') as f:
            f.write(b'y' * 50)
        # Not counted: a task's own transcript, and an ordinary project file.
        with open(os.path.join(tmp, '.claude-tasks', 't-2', 'task.json'), 'wb') as f:
            f.write(b'z' * 10000)
        os.makedirs(os.path.join(tmp, 'myproject'))
        with open(os.path.join(tmp, 'myproject', 'big.bin'), 'wb') as f:
            f.write(b'w' * 10000)

        usage = server.upload_storage_usage(tmp, 1000)
        self.assertEqual(usage['used_bytes'], 150)
        self.assertEqual(usage['quota_bytes'], 1000)
        self.assertEqual(usage['available_bytes'], 850)
        self.assertEqual(usage['percent'], 15.0)
        self.assertEqual(usage['dir_count'], 2)

    def test_unlimited_quota_reports_no_percentage(self):
        tmp = os.path.realpath(tempfile.mkdtemp(prefix='kc-quota-'))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        usage = server.upload_storage_usage(tmp, 0)
        self.assertEqual(usage['used_bytes'], 0)
        self.assertEqual(usage['percent'], 0)
        self.assertIsNone(usage['available_bytes'])

    def test_symlinked_dir_is_not_followed(self):
        # An `uploads/escape -> /` symlink must not make the walk (or the
        # bill) cover the whole volume.
        tmp = os.path.realpath(tempfile.mkdtemp(prefix='kc-quota-'))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        outside = os.path.join(tmp, 'outside')
        os.makedirs(outside)
        with open(os.path.join(outside, 'huge.bin'), 'wb') as f:
            f.write(b'x' * 5000)
        os.makedirs(os.path.join(tmp, 'uploads'))
        os.symlink(outside, os.path.join(tmp, 'uploads', 'escape'))
        self.assertLess(server.upload_storage_usage(tmp, 0)['used_bytes'], 5000)


class FilesApiUploadQuotaTests(_Base):
    """The at-cap and under-cap upload paths (#556).

    The bound REJECTS; nothing is evicted, so an at-cap upload must leave the
    existing files exactly where the agent that was told about them expects."""

    def setUp(self):
        self._quota = server.UPLOAD_QUOTA_BYTES
        self._minfree = server.UPLOAD_MIN_FREE_BYTES
        # Off unless a test opts in, so the free-space floor of the machine
        # actually running the suite can't sway the quota cases.
        server.UPLOAD_MIN_FREE_BYTES = 0
        server.UPLOAD_QUOTA_BYTES = 1000
        for rel in ('uploads', '.claude-tasks/t-1/attachments'):
            shutil.rmtree(os.path.join(self.tmpdir, rel), ignore_errors=True)

    def tearDown(self):
        server.UPLOAD_QUOTA_BYTES = self._quota
        server.UPLOAD_MIN_FREE_BYTES = self._minfree

    def test_under_cap_upload_is_stored(self):
        status, body = self._upload('uploads/t-1', 'small.bin', b'a' * 400)
        self.assertEqual(status, 201)
        self.assertTrue(body['ok'])
        self.assertEqual(
            os.path.getsize(os.path.join(self.tmpdir, 'uploads', 't-1', 'small.bin')), 400)

    def test_at_cap_upload_is_refused_with_an_actionable_error(self):
        self.assertEqual(self._upload('uploads/t-1', 'first.bin', b'a' * 800)[0], 201)
        status, body = self._upload('uploads/t-1', 'second.bin', b'b' * 400)
        self.assertEqual(status, 507)
        self.assertEqual(body['code'], 'upload_quota_exceeded')
        self.assertEqual(body['used_bytes'], 800)
        self.assertEqual(body['quota_bytes'], 1000)
        # Actionable: says what is full, how full, and what to do about it.
        self.assertIn('upload storage is full', body['error'])
        self.assertIn('files.uploadQuota', body['error'])
        # Rejected, never evicted — and no half-written file left behind.
        self.assertTrue(os.path.exists(os.path.join(self.tmpdir, 'uploads', 't-1', 'first.bin')))
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, 'uploads', 't-1', 'second.bin')))

    def test_attachments_count_towards_the_same_cap(self):
        self.assertEqual(
            self._upload('.claude-tasks/t-1/attachments', 'shot.png', b'a' * 900)[0], 201)
        status, body = self._upload('uploads/t-2', 'more.bin', b'b' * 200)
        self.assertEqual(status, 507)
        self.assertEqual(body['used_bytes'], 900)

    def test_refused_upload_does_not_create_the_destination_dir(self):
        status, _ = self._upload('uploads/brand-new', 'big.bin', b'a' * 1200)
        self.assertEqual(status, 507)
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, 'uploads', 'brand-new')))

    def test_project_uploads_are_not_charged_to_the_quota(self):
        # A file the user deliberately put in a project dir is a project file.
        status, _ = self._upload('myproject', 'dataset.bin', b'a' * 5000)
        self.assertEqual(status, 201)
        self.assertEqual(self._upload('uploads/t-1', 'small.bin', b'b' * 900)[0], 201)

    def test_disabled_quota_lets_everything_through(self):
        server.UPLOAD_QUOTA_BYTES = 0
        self.assertEqual(self._upload('uploads/t-1', 'huge.bin', b'a' * 5000)[0], 201)

    def test_zip_expansion_is_charged_against_the_remaining_cap(self):
        # The compressed body fits; what it expands to does not. Checking only
        # Content-Length would sail straight through the cap.
        body = self._zip_bytes([('big.txt', 'x' * 4000)])
        self.assertLess(len(body), 1000)
        status, out = self._upload('uploads/t-1', 'bundle.zip', body, extract='zip')
        self.assertEqual(status, 507)
        self.assertEqual(out['code'], 'upload_quota_exceeded')
        self.assertIn('expands to', out['error'])
        # The staged archive is always cleaned up.
        self.assertFalse(os.path.exists(
            os.path.join(self.tmpdir, 'uploads', 't-1', 'bundle.zip.uploading')))
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, 'uploads', 't-1', 'big.txt')))

    def test_zip_that_fits_still_extracts(self):
        body = self._zip_bytes([('ok.txt', 'x' * 300)])
        status, out = self._upload('uploads/t-1', 'bundle.zip', body, extract='zip')
        self.assertEqual(status, 201)
        self.assertEqual(out['extracted'], 1)

    def test_free_space_floor_refuses_uploads_anywhere(self):
        # Applies wherever the file lands — including outside the managed dirs,
        # which is the case that actually fills a PVC.
        server.UPLOAD_QUOTA_BYTES = 0
        server.UPLOAD_MIN_FREE_BYTES = 1024 ** 3
        fake = os.statvfs(self.tmpdir)

        class _Stat:
            f_bavail = 1000
            f_frsize = 1024
            f_blocks = fake.f_blocks
        with mock.patch.object(server.os, 'statvfs', return_value=_Stat()):
            status, body = self._upload('myproject', 'dataset.bin', b'a' * 400)
        self.assertEqual(status, 507)
        self.assertEqual(body['code'], 'insufficient_disk_space')
        self.assertIn('files.minFreeSpace', body['error'])
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, 'myproject', 'dataset.bin')))

    def test_metrics_expose_current_usage(self):
        self.assertEqual(self._upload('uploads/t-1', 'a.bin', b'a' * 900)[0], 201)
        usage = server.MetricsCollector.get_upload_usage()
        self.assertEqual(usage['used_bytes'], 900)
        self.assertEqual(usage['quota_bytes'], 1000)
        self.assertEqual(usage['percent'], 90.0)
        alerts = server.MetricsCollector.get_alerts(
            {'usage_percent': 0}, {'percent': 0}, {'percent': 0}, usage)
        uploads = [a for a in alerts if a['resource'] == 'uploads']
        self.assertEqual(uploads[0]['type'], 'critical')
        self.assertIn('Upload storage at 90.0%', uploads[0]['message'])


if __name__ == '__main__':
    unittest.main()
