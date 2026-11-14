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

import errno
import http.server
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402
import upload_storage  # noqa: E402


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
        # Hermetic: an unrelated upload test must not depend on the free space
        # of the machine running it. UploadStorageTests pins its own bounds.
        cls._min_free_patch = mock.patch.object(
            server.BrowserHandler, 'UPLOAD_MIN_FREE_BYTES', 0)
        cls._min_free_patch.start()
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
        cls._min_free_patch.stop()
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
        # The destination this upload created goes with it (#556).
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, 'slipdir')))

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


class UploadStorageTests(_Base):
    """The aggregate bounds on /api/files/upload (issue #556)."""

    QUOTA = 64 * 1024

    def setUp(self):
        for name in ('uploads', 'project', '.claude-tasks'):
            shutil.rmtree(os.path.join(self.tmpdir, name), ignore_errors=True)
        os.makedirs(os.path.join(self.tmpdir, '.claude-tasks'))
        for attr, value in (('UPLOAD_QUOTA_BYTES', self.QUOTA),
                            ('UPLOAD_MIN_FREE_BYTES', 0)):
            patcher = mock.patch.object(server.BrowserHandler, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _home_tree(self):
        """Every path under the managed roots, so a test can pin 'nothing left'."""
        found = []
        for name in ('uploads', '.claude-tasks', 'project'):
            for root, dirs, files in os.walk(os.path.join(self.tmpdir, name)):
                found.extend(os.path.join(root, n) for n in dirs + files)
        return found

    def _used(self):
        return server.MetricsCollector.get_upload_usage()['used_bytes']

    def test_under_cap_is_stored(self):
        status, out = self._upload('uploads/t1', 'a.bin', b'x' * 1024)
        self.assertEqual(status, 201, out)
        self.assertEqual(out['size'], 1024)
        with open(os.path.join(self.tmpdir, 'uploads', 't1', 'a.bin'), 'rb') as f:
            self.assertEqual(f.read(), b'x' * 1024)

    def test_exactly_at_cap_is_allowed(self):
        status, out = self._upload('uploads/t1', 'a.bin', b'x' * self.QUOTA)
        self.assertEqual(status, 201, out)
        self.assertEqual(self._used(), self.QUOTA)

    def test_over_cap_is_a_507_saying_what_is_full_and_what_to_change(self):
        self._upload('uploads/t1', 'a.bin', b'x' * self.QUOTA)
        status, out = self._upload('uploads/t2', 'b.bin', b'y')
        self.assertEqual(status, 507)
        self.assertEqual(out['code'], 'upload_quota_exceeded')
        self.assertEqual(out['used_bytes'], self.QUOTA)
        self.assertEqual(out['quota_bytes'], self.QUOTA)
        self.assertIn('files.uploadQuota', out['error'])
        self.assertIn('64.0 KiB', out['error'])

    def test_a_refusal_leaves_no_directory_or_staging_file(self):
        self._upload('uploads/t1', 'a.bin', b'x' * self.QUOTA)
        before = self._home_tree()
        status, _ = self._upload('uploads/brand/new', 'b.bin', b'y')
        self.assertEqual(status, 507)
        self.assertEqual(self._home_tree(), before)

    def test_a_refused_body_is_read_so_the_response_is_not_a_reset(self):
        # Larger than the socket buffers: a server that answers without reading
        # the body would reset the connection and lose the 507 (nginx → 502).
        big = b'x' * (8 * 1024 * 1024)
        status, out = self._upload('uploads/t1', 'big.bin', big)
        self.assertEqual(status, 507)
        self.assertEqual(out['code'], 'upload_quota_exceeded')

    def test_attachments_share_the_cap_with_session_uploads(self):
        self._upload('.claude-tasks/t1/attachments', 'a.png', b'x' * (self.QUOTA - 10))
        status, out = self._upload('uploads/t2', 'b.bin', b'y' * 11)
        self.assertEqual(status, 507, out)
        status, out = self._upload('uploads/t2', 'b.bin', b'y' * 10)
        self.assertEqual(status, 201, out)

    def test_task_state_is_not_charged_as_upload_storage(self):
        task_dir = os.path.join(self.tmpdir, '.claude-tasks', 't1')
        os.makedirs(task_dir)
        with open(os.path.join(task_dir, 'output.log'), 'wb') as f:
            f.write(b'x' * self.QUOTA)
        self.assertEqual(self._used(), 0)

    def test_project_uploads_are_not_charged_against_the_quota(self):
        self._upload('uploads/t1', 'a.bin', b'x' * self.QUOTA)
        status, out = self._upload('project', 'p.bin', b'y' * 1024)
        self.assertEqual(status, 201, out)

    def test_zero_disables_the_quota(self):
        with mock.patch.object(server.BrowserHandler, 'UPLOAD_QUOTA_BYTES', 0):
            status, out = self._upload('uploads/t1', 'a.bin', b'x' * (self.QUOTA * 2))
        self.assertEqual(status, 201, out)

    def test_a_zip_is_charged_its_expanded_size(self):
        # Deflated zeros: the archive is tiny, so only the expansion can trip this.
        data = self._zip_bytes([('zeros.bin', b'\0' * (self.QUOTA + 1))])
        self.assertLess(len(data), self.QUOTA)
        status, out = self._upload('uploads/t1/unz', 'z.zip', data, extract='zip')
        self.assertEqual(status, 507, out)
        self.assertEqual(out['code'], 'upload_quota_exceeded')
        self.assertIn('expands to', out['error'])
        self.assertEqual(self._home_tree(), [])

    def test_a_zip_that_fits_is_not_charged_for_its_archive_too(self):
        # Expansion alone exactly fills the cap: the staged archive is deleted
        # afterwards, so charging it as well would refuse an upload that fits.
        data = self._zip_bytes([('zeros.bin', b'\0' * self.QUOTA)])
        status, out = self._upload('uploads/t1/unz', 'z.zip', data, extract='zip')
        self.assertEqual(status, 201, out)
        self.assertEqual(self._used(), self.QUOTA)

    def test_a_failure_during_extraction_leaves_nothing(self):
        real = zipfile.ZipFile.extract
        calls = []

        def fail_second(zf, member, path=None, pwd=None):
            calls.append(member)
            if len(calls) == 2:
                raise OSError(errno.ENOSPC, 'No space left on device')
            return real(zf, member, path, pwd)

        data = self._zip_bytes([('a.txt', 'A'), ('b.txt', 'B')])
        with mock.patch.object(zipfile.ZipFile, 'extract', fail_second):
            status, out = self._upload('uploads/t1/unz', 'z.zip', data, extract='zip')
        self.assertEqual(status, 507, out)
        self.assertEqual(out['code'], 'insufficient_disk_space')
        self.assertEqual(self._home_tree(), [])

    def test_the_free_space_floor_applies_outside_the_managed_dirs(self):
        with mock.patch.object(server.BrowserHandler, 'UPLOAD_MIN_FREE_BYTES', 1000), \
                mock.patch.object(upload_storage, '_free_bytes', lambda home: 1500):
            status, out = self._upload('project', 'fits.bin', b'x' * 500)
            self.assertEqual(status, 201, out)
            status, out = self._upload('project', 'no.bin', b'x' * 501)
        self.assertEqual(status, 507, out)
        self.assertEqual(out['code'], 'insufficient_disk_space')
        self.assertIn('files.minFreeSpace', out['error'])
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, 'project', 'no.bin')))

    def test_the_tighter_bound_is_the_one_reported(self):
        with mock.patch.object(server.BrowserHandler, 'UPLOAD_MIN_FREE_BYTES', 1000), \
                mock.patch.object(upload_storage, '_free_bytes', lambda home: 1010):
            status, out = self._upload('uploads/t1', 'a.bin', b'x' * 100)
        self.assertEqual(status, 507, out)
        self.assertEqual(out['code'], 'insufficient_disk_space')

    def test_a_real_enospc_is_reported_as_a_full_volume(self):
        with mock.patch('handlers.files.open', create=True,
                        side_effect=OSError(errno.ENOSPC, 'No space left on device')):
            status, out = self._upload('uploads/t1', 'a.bin', b'x')
        self.assertEqual(status, 507, out)
        self.assertEqual(out['code'], 'insufficient_disk_space')
        self.assertEqual(self._home_tree(), [])

    def test_racing_uploads_cannot_overshoot_the_cap(self):
        size = self.QUOTA // 4
        results = []
        barrier = threading.Barrier(12)

        def worker(i):
            barrier.wait()
            results.append(self._upload('uploads/race', f'{i}.bin', b'x' * size)[0])

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [201] * 4 + [507] * 8)
        self.assertEqual(self._used(), self.QUOTA)
        self.assertFalse(upload_storage._live)

    def test_an_in_flight_reservation_counts_against_the_cap(self):
        dest = os.path.join(self.tmpdir, 'uploads', 'slow')
        held = upload_storage.reserve(self.tmpdir, dest, self.QUOTA - 10,
                                      self.QUOTA, 0)
        try:
            status, out = self._upload('uploads/t1', 'a.bin', b'x' * 11)
            self.assertEqual(status, 507, out)
            status, out = self._upload('uploads/t1', 'a.bin', b'x' * 10)
            self.assertEqual(status, 201, out)
        finally:
            held.release()
        status, out = self._upload('uploads/t2', 'b.bin', b'x' * (self.QUOTA - 10))
        self.assertEqual(status, 201, out)

    def test_an_interrupted_upload_is_discarded(self):
        request = ('POST /api/files/upload HTTP/1.0\r\n'
                   'X-Dest-Path: uploads%2Fcut\r\n'
                   'X-Filename: a.bin\r\n'
                   'Content-Length: 4096\r\n\r\n').encode() + b'partial'
        # Half-close so the server reads EOF mid-body, then wait for its reply:
        # the reservation is released before the response is sent. Polling
        # instead raced the handler thread, which could reserve only after the
        # poll had already seen nothing live.
        with socket.create_connection(('127.0.0.1', self.port), timeout=5) as sock:
            sock.sendall(request)
            sock.shutdown(socket.SHUT_WR)
            reply = b''
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                reply += chunk
        self.assertIn(b' 400 ', reply.split(b'\r\n', 1)[0])
        self.assertEqual(self._home_tree(), [])
        self.assertFalse(upload_storage._live)

    def test_metrics_report_usage_and_alert_before_the_wall(self):
        self._upload('uploads/t1', 'a.bin', b'x' * (self.QUOTA * 85 // 100))
        usage = server.MetricsCollector.get_upload_usage()
        self.assertEqual(usage['quota_bytes'], self.QUOTA)
        self.assertEqual(usage['percent'], 85.0)
        alerts = server.MetricsCollector.get_alerts({}, {}, {}, usage)
        self.assertEqual([(a['type'], a['resource']) for a in alerts],
                         [('warning', 'uploads')])

    def test_no_alert_without_a_cap(self):
        usage = upload_storage.usage(self.tmpdir, 0)
        self.assertEqual(usage['percent'], 0)
        self.assertIsNone(usage['available_bytes'])
        self.assertEqual(server.MetricsCollector.get_alerts({}, {}, {}, usage), [])


class UploadStorageUnitTests(unittest.TestCase):
    """upload_storage's pure helpers and ledger, without HTTP in the way."""

    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp(prefix='kc-uploads-'))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.uploads = os.path.join(self.home, 'uploads', 't1')
        os.makedirs(self.uploads)

    def _write(self, name, size):
        path = os.path.join(self.uploads, name)
        with open(path, 'wb') as f:
            f.write(b'x' * size)
        return path

    def test_parse_size(self):
        cases = {'2Gi': 2 * 1024 ** 3, '500Mi': 500 * 1024 ** 2, '1M': 10 ** 6,
                 '1073741824': 1073741824, '0': 0, 0: 0, '1.5Gi': int(1.5 * 1024 ** 3),
                 '2GiB': 2 * 1024 ** 3, '512B': 512}
        for raw, expected in cases.items():
            self.assertEqual(upload_storage.parse_size_bytes(raw, 7), expected, raw)

    def test_parse_size_falls_back_on_blank_and_garbage(self):
        for raw in (None, '', '   ', 'lots', '-1Gi', '1Xi'):
            self.assertEqual(upload_storage.parse_size_bytes(raw, 7), 7, raw)

    def test_managed_dirs_are_recognised_before_they_exist(self):
        managed = upload_storage.is_managed_upload_dir
        self.assertTrue(managed('/h', '/h/uploads/new'))
        self.assertTrue(managed('/h', '/h/.claude-tasks/t/attachments'))
        self.assertTrue(managed('/h', '/h/.claude-tasks/t/attachments/z/y'))
        self.assertFalse(managed('/h', '/h/project'))
        self.assertFalse(managed('/h', '/h/.claude-tasks/t'))
        self.assertFalse(managed('/h', '/h'))

    def test_symlinks_are_not_followed_while_sizing(self):
        target = os.path.join(self.home, 'huge.bin')
        with open(target, 'wb') as f:
            f.write(b'x' * 100000)
        link = os.path.join(self.uploads, 'link')
        os.symlink(target, link)
        self.assertEqual(upload_storage.usage(self.home, 0)['used_bytes'],
                         os.lstat(link).st_size)

    def test_live_staging_is_charged_once(self):
        res = upload_storage.reserve(self.home, self.uploads, 100, 1000, 0)
        self.addCleanup(res.release)
        with open(res.staging_path(self.uploads), 'wb') as f:
            f.write(b'x' * 100)
        self.assertEqual(upload_storage.usage(self.home, 1000)['used_bytes'], 0)

    def test_commit_moves_the_charge_and_release_forgets_it(self):
        res = upload_storage.reserve(self.home, self.uploads, 100, 1000, 0)
        staged = res.staging_path(self.uploads)
        with open(staged, 'wb') as f:
            f.write(b'x' * 100)
        final = os.path.join(self.uploads, 'done.bin')
        res.commit(lambda: os.replace(staged, final))
        self.assertEqual(upload_storage.usage(self.home, 1000)['used_bytes'], 100)
        self.assertEqual((res.quota_bytes, res.disk_bytes), (0, 0))
        res.release()
        res.release()  # idempotent
        self.assertNotIn(res, upload_storage._live)

    def test_release_removes_a_staged_tree(self):
        res = upload_storage.reserve(self.home, self.uploads, 10, 1000, 0)
        staged = res.staging_path(self.uploads)
        os.makedirs(os.path.join(staged, 'sub'))
        res.release()
        self.assertFalse(os.path.exists(staged))

    def test_an_orphan_is_charged_until_swept_on_first_admission(self):
        orphan = self._write('.kc-upload-' + 'a' * 32, 300)
        orphan_dir = os.path.join(self.uploads, '.kc-upload-' + 'b' * 32)
        os.makedirs(orphan_dir)
        keep = self._write('.kc-upload-mine.txt', 5)
        self.assertEqual(upload_storage.usage(self.home, 0)['used_bytes'], 305)
        res = upload_storage.reserve(self.home, self.uploads, 1, 1000, 0)
        self.addCleanup(res.release)
        self.assertFalse(os.path.exists(orphan))
        self.assertFalse(os.path.exists(orphan_dir))
        self.assertTrue(os.path.exists(keep))

    def test_refusal_does_not_hold_a_reservation(self):
        self._write('full.bin', 1000)
        with self.assertRaises(upload_storage.UploadCapacityError):
            upload_storage.reserve(self.home, self.uploads, 1, 1000, 0)
        self.assertFalse(upload_storage._live)


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


if __name__ == '__main__':
    unittest.main()
