"""Tests for AppsManager and the /api/apps + /api/app-proxy endpoints.

Three suites:

- AppsManagerUnit covers /proc/net/tcp[6] parsing, the pin file CRUD,
  and the merged list_apps view, all without touching the HTTP server.
- AppsApiTests boots a ThreadingHTTPServer with check_claude_auth
  bypassed and exercises the JSON endpoints.
- AppsProxyTests stands up a tiny stub upstream on a random loopback
  port and proves the reverse proxy forwards paths + headers + body and
  rewrites Location headers correctly.

Run with:
    cd charts/workspace && python3 -m unittest tests.apps_test
"""

import http.server
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


# A /proc/net/tcp file with one listen on 127.0.0.1:8000 (port 0x1F40)
# and one established TCP connection that must be ignored. The state
# column is the 4th whitespace-separated field; 0A = LISTEN, 01 = ESTAB.
TCP_FIXTURE = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:1F40 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 12345 1 0000000000000000 100 0 0 10 0
   1: 0100007F:9C40 0100007F:E8BE 01 00000000:00000000 00:00000000 00000000  1000        0 12346 1 0000000000000000 100 0 0 10 0
"""

# /proc/net/tcp6 with ::1:5000 (0x1388) listen and a non-loopback listen
# (2606:... port 80) that must be filtered out.
TCP6_FIXTURE = """  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000000000000000000001000000:1388 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 22222 1 0000000000000000 100 0 0 10 0
   1: 06260100000000000000000000000000:0050 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 22223 1 0000000000000000 100 0 0 10 0
"""


class AppsManagerUnit(unittest.TestCase):
    """Pure-Python coverage: parsing, pin file CRUD, merge order."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='kc-apps-')
        self._tcp_path = os.path.join(self.tmpdir, 'tcp')
        self._tcp6_path = os.path.join(self.tmpdir, 'tcp6')
        with open(self._tcp_path, 'w') as f:
            f.write(TCP_FIXTURE)
        with open(self._tcp6_path, 'w') as f:
            f.write(TCP6_FIXTURE)

        self._pins_save = server.AppsManager.PINS_PATH
        server.AppsManager.PINS_PATH = os.path.join(self.tmpdir, 'apps.json')

    def tearDown(self):
        server.AppsManager.PINS_PATH = self._pins_save
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_parse_listen_ports_picks_loopback_only(self):
        entries = server.AppsManager.parse_listen_ports(
            tcp_path=self._tcp_path, tcp6_path=self._tcp6_path,
        )
        ports = sorted(e['port'] for e in entries)
        # 8000 (v4 lo), 5000 (v6 ::1). Established v4 socket excluded;
        # public-IPv6 listen excluded.
        self.assertEqual(ports, [5000, 8000])

    def test_parse_listen_ports_missing_files_is_silent(self):
        entries = server.AppsManager.parse_listen_ports(
            tcp_path='/no/such/path', tcp6_path='/no/such/path6',
        )
        self.assertEqual(entries, [])

    def test_decode_helpers(self):
        # 0100007F is little-endian 127.0.0.1
        self.assertEqual(server.AppsManager._decode_ipv4_hex('0100007F'), '127.0.0.1')
        # All zeros = "::"
        ipv6_any = '0' * 32
        self.assertEqual(server.AppsManager._decode_ipv6_hex(ipv6_any), '::')
        # Trailing word 0100007F = ::1
        ipv6_lo = '0' * 24 + '01000000'
        self.assertEqual(server.AppsManager._decode_ipv6_hex(ipv6_lo), '::1')

    def test_is_loopback(self):
        for addr in ('127.0.0.1', '::1', '0.0.0.0', '::',
                     '::ffff:127.0.0.1', '::ffff:127.0.0.5'):
            self.assertTrue(server.AppsManager._is_loopback(addr), addr)
        for addr in ('8.8.8.8', '2606:4700::1111'):
            self.assertFalse(server.AppsManager._is_loopback(addr), addr)

    def test_pin_crud_roundtrip(self):
        pin = server.AppsManager.add_pin(port=3000, name='My App')
        self.assertEqual(pin['name'], 'My App')
        self.assertFalse(pin['strip_prefix'])
        self.assertGreater(pin['created_at'], 0)

        self.assertEqual(server.AppsManager.get_pin(3000)['name'], 'My App')

        # Overwrite (same port) keeps it as a single entry.
        server.AppsManager.add_pin(port=3000, name='Renamed', strip_prefix=True)
        self.assertEqual(server.AppsManager.get_pin(3000)['name'], 'Renamed')
        self.assertTrue(server.AppsManager.get_pin(3000)['strip_prefix'])

        self.assertTrue(server.AppsManager.remove_pin(3000))
        self.assertIsNone(server.AppsManager.get_pin(3000))
        self.assertFalse(server.AppsManager.remove_pin(3000))  # idempotent

    def test_pin_validation_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            server.AppsManager.add_pin(port=0, name='ok')
        with self.assertRaises(ValueError):
            server.AppsManager.add_pin(port=70000, name='ok')
        with self.assertRaises(ValueError):
            server.AppsManager.add_pin(port='not-a-port', name='ok')
        with self.assertRaises(ValueError):
            server.AppsManager.add_pin(port=3000, name='')
        with self.assertRaises(ValueError):
            server.AppsManager.add_pin(port=3000, name='bad<chars>')

    def test_list_apps_orders_pinned_then_discovered(self):
        # Pin one port that is currently listening (3000 not in fixture
        # so won't be listening — exercise the "stopped" branch) and one
        # that is (5000 is in tcp6 fixture). Then patch parse_listen_ports
        # to read from our fixture paths.
        server.AppsManager.add_pin(port=5000, name='B Running')
        server.AppsManager.add_pin(port=3000, name='A Stopped')

        orig = server.AppsManager.parse_listen_ports
        server.AppsManager.parse_listen_ports = staticmethod(
            lambda: orig(tcp_path=self._tcp_path, tcp6_path=self._tcp6_path)
        )
        try:
            rows = server.AppsManager.list_apps()
        finally:
            server.AppsManager.parse_listen_ports = staticmethod(orig)

        # Pinned first, sorted by name. Then the unpinned discovered (8000).
        ports = [r['port'] for r in rows]
        self.assertEqual(ports[:2], [3000, 5000])
        self.assertIn(8000, ports[2:])

        by_port = {r['port']: r for r in rows}
        self.assertEqual(by_port[3000]['status'], 'stopped')
        self.assertEqual(by_port[5000]['status'], 'running')
        self.assertEqual(by_port[8000]['status'], 'running')
        self.assertFalse(by_port[8000]['pinned'])

    def test_list_apps_hides_internal_ports(self):
        # Inject a fake fixture containing port 8080 (in INTERNAL_PORTS).
        fake_tcp = os.path.join(self.tmpdir, 'tcp_internal')
        with open(fake_tcp, 'w') as f:
            f.write(
                "  sl  local_address rem_address st\n"
                "   0: 0100007F:1F90 00000000:0000 0A 0 0 0 0 0 1000 0 99999 1\n"
            )
        orig = server.AppsManager.parse_listen_ports
        server.AppsManager.parse_listen_ports = staticmethod(
            lambda: orig(tcp_path=fake_tcp, tcp6_path='/no/such')
        )
        try:
            rows = server.AppsManager.list_apps()
        finally:
            server.AppsManager.parse_listen_ports = staticmethod(orig)
        # 0x1F90 = 8080 (workspace's VS Code port). Auto-discovery hides it.
        self.assertNotIn(8080, [r['port'] for r in rows])

    def test_is_proxyable_blocks_internal_and_not_listening(self):
        # Listener fixture has 8000 and 5000.
        orig = server.AppsManager.parse_listen_ports
        server.AppsManager.parse_listen_ports = staticmethod(
            lambda: orig(tcp_path=self._tcp_path, tcp6_path=self._tcp6_path)
        )
        try:
            ok, _ = server.AppsManager.is_proxyable(8000)
            self.assertTrue(ok)
            ok, reason = server.AppsManager.is_proxyable(8080)  # internal
            self.assertFalse(ok)
            self.assertIn('reserved', reason)
            ok, reason = server.AppsManager.is_proxyable(9999)  # not listening
            self.assertFalse(ok)
            self.assertIn('not currently listening', reason)
            ok, reason = server.AppsManager.is_proxyable('bogus')
            self.assertFalse(ok)
        finally:
            server.AppsManager.parse_listen_ports = staticmethod(orig)


# --- HTTP endpoint tests (auth bypassed) -------------------------------

class AppsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix='kc-apps-api-')
        cls._pins_save = server.AppsManager.PINS_PATH
        server.AppsManager.PINS_PATH = os.path.join(cls.tmpdir, 'apps.json')
        cls._auth_save = server.BrowserHandler.check_claude_auth
        server.BrowserHandler.check_claude_auth = lambda self: True
        cls._readonly_save = server.READONLY_MODE
        server.READONLY_MODE = False

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
        server.AppsManager.PINS_PATH = cls._pins_save
        server.BrowserHandler.check_claude_auth = cls._auth_save
        server.READONLY_MODE = cls._readonly_save
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _req(self, method, path, body=None):
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(
            f'http://127.0.0.1:{self.port}{path}',
            data=data, headers=headers, method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                raw = r.read()
                return r.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, raw

    def test_apps_list_endpoint_returns_envelope(self):
        status, body = self._req('GET', '/api/apps')
        self.assertEqual(status, 200)
        self.assertIn('apps', body)
        self.assertIn('unavailable_reason', body)
        self.assertIn('auth_mode', body)
        self.assertIsInstance(body['apps'], list)

    def test_pin_create_and_delete_roundtrip(self):
        # Add a pin via the HTTP API and confirm it appears in the list.
        status, body = self._req('POST', '/api/apps/pins',
                                 {'port': 4321, 'name': 'API Test App'})
        self.assertEqual(status, 201)
        self.assertTrue(body.get('ok'))

        status, body = self._req('GET', '/api/apps')
        self.assertEqual(status, 200)
        ports = [a['port'] for a in body['apps']]
        self.assertIn(4321, ports)

        status, body = self._req('DELETE', '/api/apps/pins/4321')
        self.assertEqual(status, 200)
        self.assertTrue(body['removed'])

        status, body = self._req('GET', '/api/apps')
        self.assertNotIn(4321, [a['port'] for a in body['apps']])

    def test_pin_create_rejects_invalid_name(self):
        status, body = self._req('POST', '/api/apps/pins',
                                 {'port': 4321, 'name': 'has\nnewline'})
        self.assertEqual(status, 400)
        self.assertIn('error', body)

    def test_pin_create_rejects_invalid_port(self):
        status, body = self._req('POST', '/api/apps/pins',
                                 {'port': 0, 'name': 'ok'})
        self.assertEqual(status, 400)


# --- Proxy tests -------------------------------------------------------

class _StubUpstreamHandler(http.server.BaseHTTPRequestHandler):
    """Records every request and echoes path/method/body back as JSON."""

    received = []  # class-level so the test can inspect after the request

    def _send(self, status, payload, extra_headers=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        # Send something the proxy should strip: X-Frame-Options.
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).received.append(('GET', self.path, dict(self.headers), b''))
        # Special path: emit a Location header so we can verify rewriting.
        if self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', f'http://127.0.0.1:{self.server.server_port}/elsewhere')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        self._send(200, {'method': 'GET', 'path': self.path})

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(n) if n else b''
        type(self).received.append(('POST', self.path, dict(self.headers), body))
        self._send(200, {'method': 'POST', 'path': self.path, 'body': body.decode()})

    def log_message(self, fmt, *args):  # silence stderr
        return


class AppsProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix='kc-apps-proxy-')
        cls._pins_save = server.AppsManager.PINS_PATH
        server.AppsManager.PINS_PATH = os.path.join(cls.tmpdir, 'apps.json')
        cls._auth_save = server.BrowserHandler.check_claude_auth
        server.BrowserHandler.check_claude_auth = lambda self: True
        cls._readonly_save = server.READONLY_MODE
        server.READONLY_MODE = False

        # Spin up the stub upstream.
        cls.upstream_port = _free_port()
        cls.upstream = http.server.ThreadingHTTPServer(
            ('127.0.0.1', cls.upstream_port), _StubUpstreamHandler,
        )
        cls.upstream_thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.upstream_thread.start()

        # Make is_proxyable report the upstream as listening without
        # touching /proc/net/tcp on the test host.
        cls._parse_save = server.AppsManager.parse_listen_ports
        server.AppsManager.parse_listen_ports = staticmethod(
            lambda: [{'port': cls.upstream_port, 'addr': '127.0.0.1', 'inode': 0}]
        )

        # Boot the dashboard server pointing at the same upstream.
        cls.port = _free_port()
        cls.httpd = http.server.ThreadingHTTPServer(
            ('127.0.0.1', cls.port), server.BrowserHandler,
        )
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        # Give both servers a moment to start accepting.
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close()
        cls.upstream.shutdown(); cls.upstream.server_close()
        server.AppsManager.PINS_PATH = cls._pins_save
        server.AppsManager.parse_listen_ports = cls._parse_save
        server.BrowserHandler.check_claude_auth = cls._auth_save
        server.READONLY_MODE = cls._readonly_save
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        _StubUpstreamHandler.received.clear()

    def _proxy_url(self, suffix=''):
        return f'http://127.0.0.1:{self.port}/api/app-proxy/{self.upstream_port}{suffix}'

    def test_get_forwards_path_and_strips_prefix(self):
        # Default behaviour: prefix stripped, upstream sees /hello/world?x=1
        with urllib.request.urlopen(self._proxy_url('/hello/world?x=1'), timeout=5) as r:
            self.assertEqual(r.status, 200)
            body = json.loads(r.read())
            # Frame-blocking header must be filtered by the proxy.
            self.assertNotIn('x-frame-options', {k.lower() for k in r.headers.keys()})
        self.assertEqual(body['method'], 'GET')
        self.assertEqual(body['path'], '/hello/world?x=1')
        # Upstream saw the forwarded path AND a proxy-prefix header.
        m, p, hdrs, _ = _StubUpstreamHandler.received[-1]
        self.assertEqual(m, 'GET')
        self.assertEqual(p, '/hello/world?x=1')
        self.assertEqual(hdrs.get('X-Forwarded-Prefix'), f'/api/app-proxy/{self.upstream_port}')

    def test_post_body_round_trips(self):
        req = urllib.request.Request(
            self._proxy_url('/echo'), data=b'{"hi":"there"}',
            headers={'Content-Type': 'application/json'}, method='POST',
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            body = json.loads(r.read())
        self.assertEqual(body['method'], 'POST')
        self.assertEqual(body['path'], '/echo')
        self.assertEqual(body['body'], '{"hi":"there"}')

    def test_strip_prefix_pin_keeps_proxy_prefix(self):
        # With strip_prefix=True the upstream sees the full /api/app-proxy/<port>
        # prefix preserved (Vite-style apps configured with that as --base).
        server.AppsManager.add_pin(port=self.upstream_port, name='Vite', strip_prefix=True)
        try:
            with urllib.request.urlopen(self._proxy_url('/main.js'), timeout=5) as r:
                body = json.loads(r.read())
            self.assertEqual(body['path'], f'/api/app-proxy/{self.upstream_port}/main.js')
        finally:
            server.AppsManager.remove_pin(self.upstream_port)

    def test_root_path_redirects_to_trailing_slash(self):
        # Use a no-redirect opener so urllib doesn't auto-follow the 301.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(NoRedirect)
        try:
            opener.open(self._proxy_url(''), timeout=5)
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 301)
            self.assertEqual(e.headers.get('Location'),
                             f'/api/app-proxy/{self.upstream_port}/')
        else:
            self.fail('Expected 301 redirect for /api/app-proxy/<port>')

    def test_absolute_location_header_is_rewritten(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            opener.open(self._proxy_url('/redirect'), timeout=5)
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 302)
            loc = e.headers.get('Location')
            self.assertEqual(loc, f'/api/app-proxy/{self.upstream_port}/elsewhere')
        else:
            self.fail('Expected 302 from stub upstream')

    def test_proxy_rejects_internal_port(self):
        # 8080 is in INTERNAL_PORTS. Returns 403 with a reason.
        try:
            urllib.request.urlopen(
                f'http://127.0.0.1:{self.port}/api/app-proxy/8080/', timeout=5,
            )
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)
            self.assertIn(b'reserved', e.read())
        else:
            self.fail('Expected 403 for internal port')


if __name__ == '__main__':
    unittest.main()
