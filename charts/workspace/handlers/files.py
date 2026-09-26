"""Workspace file browser: list, read, upload, mkdir, rename, delete (#100).

Everything under `/api/files`, rooted at `/home/dev`. The first migrated
domain that spans three verbs, and the one that brings the query-string
wrinkle into the table.

## The query string

`do_GET` strips the query off before routing, so its five read endpoints match
plain paths even though every one of them takes `?path=<rel>`. Every *other*
verb routes on `_strip_route_prefix(self.path)`, which leaves the query
attached — which is why the chain spelled the delete branch
`path.split('?', 1)[0] == '/api/files'`. That is now the route's
`strip_query=True` column (see handlers/routing.py); the three POST routes
take JSON bodies or headers, so they match plain paths like the reads.

## What stays a guard rather than a route

The traversal confinement (`_resolve_under_home_dev`), the public-demo
confidentiality gate (`_reject_public_read`) and the dotfile/filename checks
are per-handler guards, not dispatch. They move with the handlers unchanged —
`HOME_DEV` and the size limits stay class attributes on the mixin, so the
suites that pin them to a tempdir (`server.BrowserHandler.HOME_DEV = …`) keep
working: the assignment lands on BrowserHandler and shadows the mixin's value
for every `cls.HOME_DEV` read.

`AUTH_MODE` and `PUBLIC_FILE_ROOT` still live in server.py and are reached
through `handlers.server` so they stay late-bound — the gate has to read
whatever the process (or a test) set, not a value captured at import.
"""

import json
import mimetypes
import os
import re
import urllib.parse
import zipfile

import handlers
from handlers.routing import RouteTable


class FilesRoutes:
    """BrowserHandler methods backing `ROUTES`, plus their shared guards."""

    # ── File upload + browse (rooted at /home/dev) ─────────────────────
    # We deliberately keep these endpoints scoped to /home/dev with a
    # realpath-based traversal check so a crafted X-Dest-Path can't escape
    # the user's home directory (e.g. via ".." or absolute paths).
    HOME_DEV = '/home/dev'
    MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MiB

    @classmethod
    def _resolve_under_home_dev(cls, rel_path: str) -> str:
        rel = (rel_path or '').strip()
        # Treat leading slashes as relative to /home/dev — users naturally
        # type "/screenshots" or "screenshots" interchangeably.
        rel = rel.lstrip('/')
        abs_path = os.path.realpath(os.path.join(cls.HOME_DEV, rel))
        if abs_path != cls.HOME_DEV and not abs_path.startswith(cls.HOME_DEV + os.sep):
            raise ValueError('path escapes /home/dev')
        return abs_path

    @classmethod
    def _path_has_hidden_segment(cls, abs_path: str) -> bool:
        """True if any path segment of abs_path *below* HOME_DEV starts with a
        dot (e.g. .claude-tasks, .config, .ssh, .git). HOME_DEV itself is never
        counted — only the portion a request can address."""
        try:
            rel = os.path.relpath(abs_path, cls.HOME_DEV)
        except ValueError:
            return True  # different drive / unrelatable → treat as hidden
        if rel in ('', '.'):
            return False
        return any(seg.startswith('.') for seg in rel.split(os.sep)
                   if seg not in ('', '.', '..'))

    def _reject_public_read(self, target: str) -> bool:
        """Public-demo confidentiality gate for direct file reads. In
        AUTH_MODE=none (the unauthenticated public demo) refuse to serve a file
        whose resolved path contains a hidden (dot) segment, and — when the
        operator set PUBLIC_FILE_ROOT — anything outside that subdir. This closes
        finding 3: READONLY_MODE protects integrity, not confidentiality, so the
        credential/config dotfiles that directory listings already hide must not
        be reachable via a direct download/preview/view/raw request. Authed modes
        (oauth2/basic) are untouched, so a logged-in user keeps full access to
        their own dotfiles. Returns True — and sends a 404, matching a
        nonexistent file so existence isn't confirmed — when the read must be
        refused; the caller then stops. Traversal/symlink-escape protection is
        handled earlier by _resolve_under_home_dev and stays intact."""
        if handlers.server.AUTH_MODE != 'none':
            return False
        if handlers.server.PUBLIC_FILE_ROOT:
            try:
                root = self._resolve_under_home_dev(handlers.server.PUBLIC_FILE_ROOT)
            except ValueError:
                root = self.HOME_DEV
            if target != root and not target.startswith(root + os.sep):
                self.send_json({'error': 'not a file'}, 404)
                return True
        if self._path_has_hidden_segment(target):
            self.send_json({'error': 'not a file'}, 404)
            return True
        return False

    @staticmethod
    def _safe_filename(name: str) -> bool:
        if not name or name in ('.', '..'):
            return False
        if '/' in name or '\\' in name or '\x00' in name:
            return False
        if len(name) > 255:
            return False
        return True

    def handle_files_list(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        rel_dir = (params.get('path', [''])[0] or '').strip()
        try:
            target = self._resolve_under_home_dev(rel_dir)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        if not os.path.isdir(target):
            self.send_json({'error': 'not a directory'}, 404)
            return
        entries = []
        try:
            for name in sorted(os.listdir(target), key=str.lower):
                if name.startswith('.'):  # hide dotfiles
                    continue
                full = os.path.join(target, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                entries.append({
                    'name': name,
                    'kind': 'dir' if os.path.isdir(full) else 'file',
                    'size': st.st_size,
                    'mtime': int(st.st_mtime),
                })
        except OSError as e:
            self.send_json({'error': f'list failed: {e}'}, 500)
            return
        # Surface dirs first so the UI can render a sensible tree.
        entries.sort(key=lambda e: (0 if e['kind'] == 'dir' else 1, e['name'].lower()))
        rel = os.path.relpath(target, self.HOME_DEV)
        if rel == '.':
            rel = ''
        self.send_json({'path': rel, 'entries': entries})

    def handle_file_raw(self):
        """GET /api/files/raw?path=<rel> — stream a workspace MEDIA file.

        Auth-gated + traversal-guarded (reuses _resolve_under_home_dev). Powers
        the Hypervisor chat's image/video rendering: the agent saves a file under
        /home/dev and shows it via show_media(path=...), and the client fetches
        the bytes here.

        SECURITY: restricted to image/* and video/* by content type. The
        dashboard is served from this same origin with no CSP, so serving an
        arbitrary /home/dev HTML/JS file here would be stored XSS — refuse
        anything that isn't media, and send nosniff + inline disposition.
        Supports a single Range request so <video> seeking works.
        """
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        qs = urllib.parse.urlparse(self.path).query
        rel = (urllib.parse.parse_qs(qs).get('path', [''])[0] or '').strip()
        try:
            target = self._resolve_under_home_dev(rel)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        if not os.path.isfile(target):
            self.send_json({'error': 'not a file'}, 404)
            return
        if self._reject_public_read(target):
            return
        ctype = mimetypes.guess_type(target)[0] or ''
        if not (ctype.startswith('image/') or ctype.startswith('video/')):
            self.send_json(
                {'error': 'only image/* and video/* files can be served here'}, 415)
            return
        try:
            size = os.path.getsize(target)
            start, end = 0, size - 1
            is_range = False
            rng = self.headers.get('Range', '')
            m = re.match(r'^bytes=(\d*)-(\d*)$', rng.strip()) if rng else None
            if m and size > 0:
                s, e = m.group(1), m.group(2)
                if s:
                    start = int(s)
                    end = int(e) if e else size - 1
                elif e:  # suffix range: last N bytes
                    start = max(0, size - int(e))
                    end = size - 1
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.end_headers()
                    return
                is_range = True
            length = end - start + 1
            with open(target, 'rb') as fh:
                self.send_response(206 if is_range else 200)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(length))
                self.send_header('Accept-Ranges', 'bytes')
                if is_range:
                    self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                self.send_header('Content-Disposition', 'inline')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Cache-Control', 'private, max-age=60')
                self.end_headers()
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away mid-stream; headers already sent
        except OSError as e:
            try:
                self.send_json({'error': f'read failed: {e}'}, 500)
            except Exception:
                pass

    # Zip-extract guardrails (issue #356): a hostile archive must not be able
    # to fill the disk via a compression bomb (total uncompressed size is
    # checked against MAX_UPLOAD_BYTES BEFORE extracting) or exhaust inodes
    # with millions of tiny members.
    MAX_ZIP_MEMBERS = 10000

    def _extract_zip_upload(self, zip_path: str, dest_dir: str) -> int:
        """Extract an uploaded .zip into dest_dir. Returns the number of files
        written. Raises ValueError for unsafe or oversized archives and
        zipfile.BadZipFile for non-zip bytes.

        Zip-slip: member names with an absolute path, a `..` segment, or a
        backslash are rejected outright (the whole upload fails, rather than
        silently skipping — a partial extract would be confusing). Symlink
        members are skipped: zipfile.extract would write them as regular
        files holding the target path, which is never what anyone wants."""
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            if len(infos) > self.MAX_ZIP_MEMBERS:
                raise ValueError(f'zip has too many entries (max {self.MAX_ZIP_MEMBERS})')
            total = sum(i.file_size for i in infos)
            if total > self.MAX_UPLOAD_BYTES:
                raise ValueError(
                    f'zip expands to {total} bytes (max {self.MAX_UPLOAD_BYTES})')
            real_dest = os.path.realpath(dest_dir)
            for info in infos:
                name = info.filename
                if name.startswith('/') or '\\' in name or '..' in name.split('/'):
                    raise ValueError(f'unsafe path in zip: {name!r}')
                # Belt-and-braces: the resolved target must stay under dest.
                target = os.path.realpath(os.path.join(real_dest, name))
                if target != real_dest and not target.startswith(real_dest + os.sep):
                    raise ValueError(f'unsafe path in zip: {name!r}')
            count = 0
            for info in infos:
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    continue  # symlink member
                zf.extract(info, real_dest)
                if not info.is_dir():
                    count += 1
            return count

    def handle_file_upload(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        # Client URL-encodes both headers because HTTP header values are
        # ISO-8859-1; Unicode filenames (smart quotes, emoji, CJK, accented
        # letters) would otherwise trip fetch() in the browser. Decode here
        # before applying the safe-filename / under-home checks so the
        # validation runs on the actual intended path.
        rel_dir = urllib.parse.unquote((self.headers.get('X-Dest-Path') or '').strip())
        filename = urllib.parse.unquote((self.headers.get('X-Filename') or '').strip())
        # X-Extract: zip → body is a .zip archive; unpack it into X-Dest-Path
        # instead of storing the archive itself (issue #356).
        extract = (self.headers.get('X-Extract') or '').strip().lower() in ('1', 'true', 'zip')
        if not self._safe_filename(filename):
            self.send_json({'error': 'invalid X-Filename header'}, 400)
            return
        if extract and not filename.lower().endswith('.zip'):
            self.send_json({'error': 'X-Extract requires a .zip filename'}, 400)
            return
        try:
            dest_dir = self._resolve_under_home_dev(rel_dir)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        try:
            content_length = int(self.headers.get('Content-Length', 0) or 0)
        except ValueError:
            self.send_json({'error': 'invalid Content-Length'}, 400)
            return
        if content_length <= 0:
            self.send_json({'error': 'empty body'}, 400)
            return
        if content_length > self.MAX_UPLOAD_BYTES:
            self.send_json({'error': f'file too large (max {self.MAX_UPLOAD_BYTES} bytes)'}, 413)
            return
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as e:
            self.send_json({'error': f'mkdir failed: {e}'}, 500)
            return
        final_path = os.path.join(dest_dir, filename)
        # Extract mode stages the archive next to the destination (same
        # filesystem) and always removes it — only the unpacked tree remains.
        write_path = final_path + '.uploading' if extract else final_path
        # Stream to disk in 64 KiB chunks so a 200 MiB upload doesn't have to
        # fully buffer in memory before we touch the filesystem.
        try:
            with open(write_path, 'wb') as fh:
                remaining = content_length
                while remaining > 0:
                    chunk = self.rfile.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    fh.write(chunk)
                    remaining -= len(chunk)
        except OSError as e:
            self.send_json({'error': f'write failed: {e}'}, 500)
            return
        if extract:
            try:
                count = self._extract_zip_upload(write_path, dest_dir)
            except zipfile.BadZipFile:
                self.send_json({'error': 'not a valid zip archive'}, 400)
                return
            except ValueError as e:
                self.send_json({'error': str(e)}, 400)
                return
            except OSError as e:
                self.send_json({'error': f'extract failed: {e}'}, 500)
                return
            finally:
                try:
                    os.unlink(write_path)
                except OSError:
                    pass
            rel_out = os.path.relpath(dest_dir, self.HOME_DEV)
            if rel_out == '.':
                rel_out = ''
            self.send_json({
                'ok': True,
                'path': rel_out,
                'absolute_path': dest_dir,
                'extracted': count,
            }, 201)
            return
        try:
            size = os.path.getsize(final_path)
        except OSError:
            size = 0
        # Match perms to existing files in the target dir — workspace pods
        # run as a non-root user, so this is mostly a safety net.
        try:
            os.chmod(final_path, 0o644)
        except OSError:
            pass
        rel_out = os.path.relpath(final_path, self.HOME_DEV)
        self.send_json({
            'ok': True,
            'path': rel_out,
            'absolute_path': final_path,
            'size': size,
        }, 201)

    def handle_file_mkdir(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            content_length = int(self.headers.get('Content-Length', 0) or 0)
            raw = self.rfile.read(content_length).decode('utf-8') if content_length else '{}'
            body = json.loads(raw) if raw else {}
        except (ValueError, json.JSONDecodeError):
            self.send_json({'error': 'invalid JSON body'}, 400)
            return
        rel_dir = (body.get('path') or '').strip()
        if not rel_dir:
            self.send_json({'error': 'path is required'}, 400)
            return
        try:
            target = self._resolve_under_home_dev(rel_dir)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as e:
            self.send_json({'error': f'mkdir failed: {e}'}, 500)
            return
        rel_out = os.path.relpath(target, self.HOME_DEV)
        if rel_out == '.':
            rel_out = ''
        self.send_json({'ok': True, 'path': rel_out}, 201)

    # Text preview is capped so a multi-gigabyte log can't be slurped into
    # memory (and shipped to the browser) by a single click. Larger files fall
    # back to download.
    PREVIEW_MAX_BYTES = 256 * 1024

    def handle_file_download(self):
        """GET /api/files/download?path=<rel> — stream ANY workspace file as an
        attachment (traversal-guarded via _resolve_under_home_dev).

        SECURITY: unlike /api/files/raw (which serves image/* + video/* inline
        for the Hypervisor chat), this can serve arbitrary bytes — including an
        HTML/JS file that would be stored XSS if rendered on this same (CSP-less)
        origin. We defuse that by ALWAYS sending `Content-Disposition:
        attachment` + `X-Content-Type-Options: nosniff` + a neutral
        application/octet-stream type, so the browser downloads rather than
        renders. Directories can't be downloaded."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        qs = urllib.parse.urlparse(self.path).query
        rel = (urllib.parse.parse_qs(qs).get('path', [''])[0] or '').strip()
        try:
            target = self._resolve_under_home_dev(rel)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        if not os.path.isfile(target):
            self.send_json({'error': 'not a file'}, 404)
            return
        if self._reject_public_read(target):
            return
        name = os.path.basename(target)
        # RFC 6266: ASCII fallback (with quotes/backslashes/control chars
        # stripped) plus a UTF-8 filename* so non-ASCII names survive.
        ascii_name = re.sub(r'[^\x20-\x7e]', '_', name).replace('\\', '_').replace('"', '_')
        star = urllib.parse.quote(name, safe='')
        try:
            size = os.path.getsize(target)
            with open(target, 'rb') as fh:
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(size))
                self.send_header(
                    'Content-Disposition',
                    f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{star}",
                )
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Cache-Control', 'private, no-store')
                self.end_headers()
                while True:
                    chunk = fh.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away mid-stream; headers already sent
        except OSError as e:
            try:
                self.send_json({'error': f'read failed: {e}'}, 500)
            except Exception:
                pass

    def handle_file_preview(self):
        """GET /api/files/preview?path=<rel> — JSON preview descriptor for the
        Files pane. Text files (size-capped) return their content with a
        truncation marker; images signal the client to render via
        /api/files/raw; everything else (binary / oversized) signals download."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        qs = urllib.parse.urlparse(self.path).query
        rel = (urllib.parse.parse_qs(qs).get('path', [''])[0] or '').strip()
        try:
            target = self._resolve_under_home_dev(rel)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        if not os.path.isfile(target):
            self.send_json({'error': 'not a file'}, 404)
            return
        if self._reject_public_read(target):
            return
        rel_out = os.path.relpath(target, self.HOME_DEV)
        try:
            size = os.path.getsize(target)
        except OSError as e:
            self.send_json({'error': f'stat failed: {e}'}, 500)
            return
        ctype = mimetypes.guess_type(target)[0] or ''
        if ctype.startswith('image/'):
            self.send_json({'kind': 'image', 'path': rel_out, 'mime': ctype, 'size': size})
            return
        if ctype.startswith('video/'):
            self.send_json({'kind': 'video', 'path': rel_out, 'mime': ctype, 'size': size})
            return
        # Peek only the first PREVIEW_MAX_BYTES: a huge text log still previews
        # (truncated, with a marker), while a huge binary never gets slurped in.
        try:
            with open(target, 'rb') as fh:
                raw = fh.read(self.PREVIEW_MAX_BYTES + 1)
        except OSError as e:
            self.send_json({'error': f'read failed: {e}'}, 500)
            return
        # A NUL byte is the classic "this is binary" tell; also treat anything
        # that isn't valid UTF-8 as binary rather than mangling it.
        if b'\x00' in raw:
            self.send_json({'kind': 'binary', 'path': rel_out, 'mime': ctype, 'size': size,
                            'reason': 'binary'})
            return
        truncated = len(raw) > self.PREVIEW_MAX_BYTES
        body = raw[:self.PREVIEW_MAX_BYTES]
        try:
            text = body.decode('utf-8')
        except UnicodeDecodeError:
            self.send_json({'kind': 'binary', 'path': rel_out, 'mime': ctype, 'size': size,
                            'reason': 'binary'})
            return
        self.send_json({'kind': 'text', 'path': rel_out, 'mime': ctype or 'text/plain',
                        'size': size, 'content': text, 'truncated': truncated})

    # Document types the Hypervisor chat renders in a sandboxed <iframe>/WebView
    # (markdown/text/code are rendered client-side from /api/files/preview, and
    # image/video stream from /api/files/raw, so only these need serving inline).
    VIEW_INLINE_MIME = {
        'application/pdf',
        'text/html', 'application/xhtml+xml',
        'image/svg+xml',
        'text/xml', 'application/xml',
    }

    def handle_file_view(self):
        """GET /api/files/view?path=<rel> — stream a workspace DOCUMENT inline so
        the Hypervisor chat can render it in a sandboxed frame (PDF, HTML, SVG).

        Auth-gated + traversal-guarded (reuses _resolve_under_home_dev), like
        /api/files/raw. Restricted to a small document allowlist (VIEW_INLINE_MIME).

        SECURITY: the dashboard is served from this same origin with no CSP, so
        serving an arbitrary HTML/JS file inline would be stored XSS. We defuse
        that with `Content-Security-Policy: sandbox` (no allow-scripts, no
        allow-same-origin) on every response *except* application/pdf — the
        browser's PDF viewer already isolates any embedded JS, and the sandbox
        directive would break it. Always send nosniff + inline. PDFs support a
        single Range so large-file seeking works.
        """
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        qs = urllib.parse.urlparse(self.path).query
        rel = (urllib.parse.parse_qs(qs).get('path', [''])[0] or '').strip()
        try:
            target = self._resolve_under_home_dev(rel)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        if not os.path.isfile(target):
            self.send_json({'error': 'not a file'}, 404)
            return
        if self._reject_public_read(target):
            return
        ctype = mimetypes.guess_type(target)[0] or ''
        if ctype not in self.VIEW_INLINE_MIME:
            self.send_json(
                {'error': f'{ctype or "this file type"} cannot be viewed inline; '
                          'use /api/files/preview (text) or /api/files/download'}, 415)
            return
        is_pdf = ctype == 'application/pdf'
        try:
            size = os.path.getsize(target)
            start, end = 0, size - 1
            is_range = False
            # Range only matters for the PDF viewer; text/HTML docs are small.
            rng = self.headers.get('Range', '') if is_pdf else ''
            m = re.match(r'^bytes=(\d*)-(\d*)$', rng.strip()) if rng else None
            if m and size > 0:
                s, e = m.group(1), m.group(2)
                if s:
                    start = int(s)
                    end = int(e) if e else size - 1
                elif e:  # suffix range: last N bytes
                    start = max(0, size - int(e))
                    end = size - 1
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.end_headers()
                    return
                is_range = True
            length = end - start + 1
            with open(target, 'rb') as fh:
                self.send_response(206 if is_range else 200)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(length))
                if is_pdf:
                    self.send_header('Accept-Ranges', 'bytes')
                    if is_range:
                        self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                else:
                    # Neutralize any active content in HTML/SVG/XML: unique origin,
                    # no scripts, no access to the dashboard's cookies/DOM.
                    self.send_header('Content-Security-Policy', 'sandbox')
                self.send_header('Content-Disposition', 'inline')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Cache-Control', 'private, max-age=60')
                self.end_headers()
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away mid-stream; headers already sent
        except OSError as e:
            try:
                self.send_json({'error': f'read failed: {e}'}, 500)
            except Exception:
                pass

    def handle_file_delete(self):
        """DELETE /api/files?path=<rel> — remove a file or an EMPTY directory
        (traversal-guarded). Refuses to recurse into a non-empty directory so a
        stray click can't wipe a subtree; refuses to delete /home/dev itself."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        qs = urllib.parse.urlparse(self.path).query
        rel = (urllib.parse.parse_qs(qs).get('path', [''])[0] or '').strip()
        try:
            target = self._resolve_under_home_dev(rel)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        if target == self.HOME_DEV:
            self.send_json({'error': 'refusing to delete /home/dev'}, 400)
            return
        # lexists so a dangling/relative symlink can still be removed (a symlink
        # ESCAPING /home/dev is already rejected above: realpath resolved its
        # target and failed the containment check).
        if not os.path.lexists(target):
            self.send_json({'error': 'not found'}, 404)
            return
        try:
            if os.path.isdir(target) and not os.path.islink(target):
                if os.listdir(target):
                    self.send_json({'error': 'directory not empty'}, 409)
                    return
                os.rmdir(target)
            else:
                os.remove(target)
        except OSError as e:
            self.send_json({'error': f'delete failed: {e}'}, 500)
            return
        self.send_json({'ok': True})

    def handle_file_rename(self):
        """POST /api/files/rename {from,to} — move/rename within /home/dev. Both
        endpoints are traversal-guarded; `to` may name a new path but must not
        already exist (no silent overwrite) and its parent must exist."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            content_length = int(self.headers.get('Content-Length', 0) or 0)
            raw = self.rfile.read(content_length).decode('utf-8') if content_length else '{}'
            body = json.loads(raw) if raw else {}
        except (ValueError, json.JSONDecodeError):
            self.send_json({'error': 'invalid JSON body'}, 400)
            return
        src_rel = (body.get('from') or '').strip()
        dst_rel = (body.get('to') or '').strip()
        if not src_rel or not dst_rel:
            self.send_json({'error': 'both from and to are required'}, 400)
            return
        try:
            src = self._resolve_under_home_dev(src_rel)
            dst = self._resolve_under_home_dev(dst_rel)
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        if src == self.HOME_DEV:
            self.send_json({'error': 'refusing to move /home/dev'}, 400)
            return
        if not os.path.lexists(src):
            self.send_json({'error': 'source not found'}, 404)
            return
        if os.path.lexists(dst):
            self.send_json({'error': 'destination already exists'}, 409)
            return
        if not os.path.isdir(os.path.dirname(dst)):
            self.send_json({'error': 'destination directory does not exist'}, 400)
            return
        try:
            os.rename(src, dst)
        except OSError as e:
            self.send_json({'error': f'rename failed: {e}'}, 500)
            return
        rel_out = os.path.relpath(dst, self.HOME_DEV)
        self.send_json({'ok': True, 'path': rel_out})


#: Consulted by do_GET, do_POST and do_DELETE where each verb's branches used
#: to sit. Registration order is match order; no two patterns here overlap, so
#: the order is the chain's and carries no hazard.
ROUTES = RouteTable()

ROUTES.add('GET', '/api/files/list', 'handle_files_list')
# Stream a workspace media file (Hypervisor image/video rendering).
ROUTES.add('GET', '/api/files/raw', 'handle_file_raw')
# Download any workspace file as an attachment (Files manager).
ROUTES.add('GET', '/api/files/download', 'handle_file_download')
# Inline preview descriptor for the Files pane (text/image/binary).
ROUTES.add('GET', '/api/files/preview', 'handle_file_preview')
# Stream a document inline for the Hypervisor chat's sandboxed viewer
# (PDF/HTML/SVG). Markdown/text render client-side via /preview.
ROUTES.add('GET', '/api/files/view', 'handle_file_view')

# Raw body (X-Dest-Path + X-Filename headers) / JSON bodies — no query string.
ROUTES.add('POST', '/api/files/upload', 'handle_file_upload')
ROUTES.add('POST', '/api/files/mkdir', 'handle_file_mkdir')
ROUTES.add('POST', '/api/files/rename', 'handle_file_rename')

# The one route whose parameter rides the query string on a verb that routes
# on the un-stripped path — see the module docstring.
ROUTES.add('DELETE', '/api/files', 'handle_file_delete', strip_query=True)
