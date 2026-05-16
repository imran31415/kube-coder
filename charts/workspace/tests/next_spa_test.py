"""Integration tests for the /next SPA route added in Phase 0.

Boots a real ThreadingHTTPServer on a free port with DASHBOARD_DIST_DIR
pointed at a temp directory shaped like a Vite build, then hits the route
with urllib. Covers:
  * /next and /next/ serve index.html
  * Hashed /next/assets/* files are served with the immutable cache header
  * Unknown deep-link paths fall back to index.html (SPA history)
  * Path traversal attempts (/next/../foo) are rejected with 403
  * The legacy `/` still serves dashboard.html (no regression)
  * When the dist directory does not exist, /next returns a helpful 404

Run with:
    cd charts/workspace && python3 -m unittest tests.next_spa_test
"""

import http.server
import os
import sys
import tempfile
import threading
import unittest
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


class NextSpaRouteTests(unittest.TestCase):
    """End-to-end tests that hit a real HTTP server."""

    @classmethod
    def setUpClass(cls):
        # Build a fake dist/ that looks like a Vite build output.
        cls.tmpdir = tempfile.mkdtemp(prefix='kc-next-')
        cls.dist_dir = os.path.join(cls.tmpdir, 'dist')
        os.makedirs(os.path.join(cls.dist_dir, 'assets'))
        with open(os.path.join(cls.dist_dir, 'index.html'), 'w') as f:
            f.write('<!doctype html><html><body><div id="app"></div></body></html>')
        with open(os.path.join(cls.dist_dir, 'assets', 'main-abc123.js'), 'w') as f:
            f.write('console.log("hello from kube-coder next");')
        with open(os.path.join(cls.dist_dir, 'assets', 'index-xyz789.css'), 'w') as f:
            f.write(':root { --bg: #0a0a0a; }')

        # Stage a fake dashboard.html in the server cwd so legacy `/` works.
        cls.legacy_cwd = os.path.join(cls.tmpdir, 'legacy_cwd')
        os.makedirs(cls.legacy_cwd)
        with open(os.path.join(cls.legacy_cwd, 'dashboard.html'), 'w') as f:
            f.write('<!doctype html><title>legacy</title>')

        os.environ['DASHBOARD_DIST_DIR'] = cls.dist_dir
        cls._cwd_save = os.getcwd()
        os.chdir(cls.legacy_cwd)

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
        os.chdir(cls._cwd_save)
        os.environ.pop('DASHBOARD_DIST_DIR', None)

    def _get(self, path):
        with urllib.request.urlopen(f'http://127.0.0.1:{self.port}{path}', timeout=5) as r:
            return r.status, dict(r.headers), r.read()

    # --- /next/ → index.html ---

    def test_next_root_serves_index_html(self):
        status, headers, body = self._get('/next')
        self.assertEqual(status, 200)
        self.assertIn('text/html', headers.get('Content-Type', ''))
        self.assertIn(b'<div id="app">', body)

    def test_next_trailing_slash_serves_index_html(self):
        status, _, body = self._get('/next/')
        self.assertEqual(status, 200)
        self.assertIn(b'<div id="app">', body)

    def test_oauth_proxied_path_strips_prefix(self):
        # The OAuth2 ingress prepends /oauth to the path.
        status, _, body = self._get('/oauth/next/')
        self.assertEqual(status, 200)
        self.assertIn(b'<div id="app">', body)

    # --- Assets get the immutable cache header ---

    def test_hashed_js_asset_is_cacheable(self):
        status, headers, body = self._get('/next/assets/main-abc123.js')
        self.assertEqual(status, 200)
        self.assertIn('javascript', headers.get('Content-Type', ''))
        cache = headers.get('Cache-Control', '')
        self.assertIn('immutable', cache)
        self.assertIn('max-age=31536000', cache)
        self.assertIn(b'kube-coder next', body)

    def test_hashed_css_asset_is_cacheable(self):
        status, headers, _ = self._get('/next/assets/index-xyz789.css')
        self.assertEqual(status, 200)
        self.assertIn('css', headers.get('Content-Type', ''))
        self.assertIn('immutable', headers.get('Cache-Control', ''))

    def test_index_html_is_not_cached(self):
        _, headers, _ = self._get('/next/')
        cache = headers.get('Cache-Control', '')
        self.assertIn('no-cache', cache)

    # --- SPA history fallback ---

    def test_unknown_deep_link_falls_back_to_index(self):
        # /tasks/abc123 has no extension and no file on disk → serve index.html.
        status, _, body = self._get('/next/tasks/abc123')
        self.assertEqual(status, 200)
        self.assertIn(b'<div id="app">', body)

    def test_missing_asset_with_extension_returns_404(self):
        try:
            self._get('/next/assets/does-not-exist.js')
            self.fail('expected 404')
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    # --- Security: traversal must be refused ---

    def test_traversal_refused(self):
        # urllib normalizes ../ before sending, so we use a percent-encoded form.
        # The handler also runs realpath; either way the response must not be 200.
        try:
            with urllib.request.urlopen(
                f'http://127.0.0.1:{self.port}/next/%2E%2E/dashboard.html', timeout=5,
            ) as r:
                # If the server happens to serve it, body MUST NOT be legacy.
                self.assertNotIn(b'legacy', r.read())
        except urllib.error.HTTPError as e:
            self.assertIn(e.code, (403, 404))

    # --- After cutover: `/` serves the new SPA, legacy at /dashboard-legacy ---

    def test_root_now_serves_new_spa(self):
        status, _, body = self._get('/')
        self.assertEqual(status, 200)
        self.assertIn(b'<div id="app">', body)

    def test_top_level_spa_routes_serve_index_html(self):
        for route in ['/tasks', '/memory', '/triggers', '/files', '/settings']:
            status, _, body = self._get(route)
            self.assertEqual(status, 200, msg=f'{route} returned {status}')
            self.assertIn(b'<div id="app">', body, msg=f'{route} did not serve SPA index.html')

    def test_dashboard_legacy_serves_old_html(self):
        status, _, body = self._get('/dashboard-legacy')
        self.assertEqual(status, 200)
        self.assertIn(b'legacy', body)


class NextSpaMissingDistTests(unittest.TestCase):
    """When DASHBOARD_DIST_DIR is missing, /next should return a helpful 404."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix='kc-next-empty-')
        os.environ['DASHBOARD_DIST_DIR'] = os.path.join(cls.tmpdir, 'never-built')
        cls._cwd_save = os.getcwd()
        os.chdir(cls.tmpdir)
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
        os.chdir(cls._cwd_save)
        os.environ.pop('DASHBOARD_DIST_DIR', None)

    def test_unbuilt_spa_returns_helpful_404(self):
        try:
            urllib.request.urlopen(f'http://127.0.0.1:{self.port}/next/', timeout=5)
            self.fail('expected 404')
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)
            body = e.read().decode('utf-8', errors='ignore')
            self.assertIn('yarn', body.lower())


if __name__ == '__main__':
    unittest.main()
