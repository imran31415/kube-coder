"""Integration tests for the SPA routing in server.py.

Boots a real ThreadingHTTPServer on a free port with DASHBOARD_DIST_DIR
pointed at a temp directory shaped like a Vite build, then hits the route
with urllib. Covers:
  * /next and /next/ serve index.html (kept for back-compat)
  * Hashed /next/assets/* files are served with the immutable cache header
  * Unknown deep-link paths fall back to index.html (SPA history)
  * Path traversal attempts (/next/../foo) are rejected with 403
  * Every route in the SPA's own table (parsed from router.ts) serves
    index.html, on a bare path, a nested deep link and an /oauth prefix
  * The fallback stops at the server's namespaces: missing assets, unknown
    /api/* paths and /metrics/prometheus do not get the SPA shell
  * When the dist directory does not exist, /next returns a helpful 404

Run with:
    cd charts/workspace && python3 -m unittest tests.next_spa_test
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


def _spa_routes():
    """Read the SPA's own route table out of web/src/store/router.ts.

    Derived, never restated: the reason /cto, /board, /feed and /skills
    404'd on refresh (#665) is that server.py kept a hand-copied list of
    these paths, and a test that also hardcoded five of them could not
    notice the drift. Parsing the real table means a route added to the SPA
    is covered here the moment it lands.
    """
    router_ts = os.path.join(
        os.path.dirname(HERE), 'web', 'src', 'store', 'router.ts')
    with open(router_ts) as fh:
        src = fh.read()
    block = re.search(r'export const ROUTES: RouteDef\[\] = \[(.*?)\n\];',
                      src, re.S)
    assert block, 'ROUTES table not found in router.ts'
    routes = re.findall(r"path:\s*'([^']+)'", block.group(1))
    assert len(routes) >= 10, f'suspiciously few SPA routes parsed: {routes}'
    return routes


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

        # server.py only uses os.getcwd() for static files like favicon — the
        # SPA itself comes from DASHBOARD_DIST_DIR, so just point cwd at the
        # tmpdir (no legacy dashboard.html needed; that file was removed).
        os.environ['DASHBOARD_DIST_DIR'] = cls.dist_dir
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

    # --- Liveness endpoint ---

    def test_livez_returns_ok_without_auth_or_subservice_checks(self):
        # The k8s liveness probe hits /livez. It must answer 200 with a tiny
        # body and never block on sub-service socket connects (unlike /health),
        # so a busy/GIL-starved server still passes liveness and the kubelet
        # doesn't SIGTERM the pod (killing the user's tmux + tasks).
        status, headers, body = self._get('/livez')
        self.assertEqual(status, 200)
        self.assertEqual(body, b'ok')
        self.assertIn('no-store', headers.get('Cache-Control', ''))

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
                f'http://127.0.0.1:{self.port}/next/%2E%2E/secret', timeout=5,
            ) as r:
                # Whatever it serves, it must not have escaped the dist dir.
                self.assertNotIn(b'secret', r.read())
        except urllib.error.HTTPError as e:
            self.assertIn(e.code, (403, 404))

    # --- Root + top-level SPA routes all serve index.html ---

    def test_root_serves_spa(self):
        status, _, body = self._get('/')
        self.assertEqual(status, 200)
        self.assertIn(b'<div id="app">', body)

    def test_every_spa_route_serves_index_html(self):
        # Every route the app declares must survive a refresh / new-tab open.
        for route in _spa_routes():
            status, headers, body = self._get(route)
            self.assertEqual(status, 200, msg=f'{route} returned {status}')
            self.assertIn(b'<div id="app">', body,
                          msg=f'{route} did not serve SPA index.html')
            self.assertIn('no-cache', headers.get('Cache-Control', ''),
                          msg=f'{route} was served cacheable')

    def test_spa_routes_survive_a_nested_deep_link(self):
        # /tasks/<id>, /board/<id>/review … the client router owns the rest
        # of the path, so the whole subtree has to reach index.html.
        for route in _spa_routes():
            status, _, body = self._get(f'{route}/deep/link')
            self.assertEqual(status, 200, msg=f'{route}/deep/link -> {status}')
            self.assertIn(b'<div id="app">', body)

    def test_oauth_prefixed_spa_routes_serve_index_html(self):
        # In oauth2 mode the SPA keeps the ingress prefix in pushState URLs,
        # so a refresh asks the server for /oauth/<route>.
        for route in _spa_routes():
            status, _, body = self._get(f'/oauth{route}')
            self.assertEqual(status, 200, msg=f'/oauth{route} -> {status}')
            self.assertIn(b'<div id="app">', body)

    # --- …but the fallback must not swallow the server's own namespaces ---

    def test_missing_static_file_still_404s(self):
        # A path with an extension is an asset request. Answering it with
        # HTML would turn a missing bundle into a confusing parse error.
        for path in ['/nope.js', '/favicon-missing.ico', '/assets/gone.css']:
            with self.assertRaises(urllib.error.HTTPError, msg=path) as ctx:
                self._get(path)
            self.assertEqual(ctx.exception.code, 404, msg=path)

    def test_unknown_api_path_does_not_return_the_spa(self):
        # Clients check status codes; a typo'd endpoint must not look like a
        # successful HTML response.
        try:
            status, _, body = self._get('/api/definitely-not-a-route')
        except urllib.error.HTTPError as e:
            self.assertGreaterEqual(e.code, 400)
        else:
            self.assertNotIn(b'<div id="app">', body,
                             msg=f'unknown /api path served the SPA ({status})')

    def test_metrics_prometheus_is_not_swallowed_by_the_fallback(self):
        # Extension-less and unauthenticated — exactly the shape the history
        # fallback matches. It must stay a server route (401, not HTML).
        try:
            status, _, body = self._get('/metrics/prometheus')
        except urllib.error.HTTPError as e:
            self.assertIn(e.code, (401, 403))
        else:
            self.assertNotIn(b'<div id="app">', body,
                             msg=f'/metrics/prometheus served the SPA ({status})')


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
