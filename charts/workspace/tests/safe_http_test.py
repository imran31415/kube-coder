"""The shared SSRF guard (safe_http.py).

Extracted from server.py's completion-hook path so the Board Processor reuses
it rather than growing a second, subtly different implementation. These tests
cover the guard on its own terms; tests/completion_hook_ssrf_test.py continues
to cover it through the hook, and both must stay green.

Run:  python3 -m unittest tests.safe_http_test   (from charts/workspace/)
"""

import io
import os
import sys
import unittest
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import safe_http  # noqa: E402

PUBLIC_IP = '93.184.216.34'

INTERNAL = {
    'loopback': '127.0.0.1',
    'private-10': '10.0.0.1',
    'private-172': '172.16.5.4',
    'private-192': '192.168.1.1',
    'link-local': '169.254.1.1',
    'cloud-metadata': '169.254.169.254',
    'unspecified': '0.0.0.0',
    'multicast': '224.0.0.1',
    'ipv6-loopback': '::1',
    'ipv6-ula': 'fd00::1',
}


def _gai(*addrs):
    def fake(host, port, *a, **k):
        return [(2, 1, 6, '', (addr, port)) for addr in addrs]
    return fake


class _FakeResponse(io.BytesIO):
    def __init__(self, payload=b'{}', status=200, headers=None):
        super().__init__(payload)
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class PublicIPTests(unittest.TestCase):
    def test_internal_addresses_rejected(self):
        for name, ip in INTERNAL.items():
            with self.subTest(kind=name):
                self.assertIsNone(safe_http.public_ip(ip))

    def test_public_addresses_allowed(self):
        for ip in (PUBLIC_IP, '8.8.8.8', '2606:2800:220:1::248'):
            with self.subTest(ip=ip):
                self.assertIsNotNone(safe_http.public_ip(ip))

    def test_ipv4_mapped_ipv6_is_normalized_before_classification(self):
        """`::ffff:10.0.0.1` is an internal address wearing a v6 costume."""
        self.assertIsNone(safe_http.public_ip('::ffff:127.0.0.1'))
        self.assertIsNone(safe_http.public_ip('::ffff:10.0.0.1'))
        self.assertIsNone(safe_http.public_ip('::ffff:169.254.169.254'))
        self.assertIsNotNone(safe_http.public_ip(f'::ffff:{PUBLIC_IP}'))

    def test_garbage_is_not_an_address(self):
        for junk in ('', None, 'not-an-ip', 'example.com', 12345):
            self.assertIsNone(safe_http.public_ip(junk))


class ResolveAndPinTests(unittest.TestCase):
    def test_returns_the_public_address(self):
        with mock.patch.object(safe_http.socket, 'getaddrinfo', _gai(PUBLIC_IP)):
            self.assertEqual(
                safe_http.resolve_and_pin('api.example.com', 443), PUBLIC_IP)

    def test_any_internal_address_rejects_the_whole_name(self):
        """A name that resolves to both must not be reachable via the public
        one — the internal answer could be served at connect time."""
        with mock.patch.object(safe_http.socket, 'getaddrinfo',
                               _gai(PUBLIC_IP, '10.0.0.1')):
            with self.assertRaises(safe_http.SSRFError):
                safe_http.resolve_and_pin('multi.example', 80)

    def test_dns_failure_fails_closed(self):
        def boom(*a, **k):
            raise safe_http.socket.gaierror('nope')
        with mock.patch.object(safe_http.socket, 'getaddrinfo', boom):
            with self.assertRaises(safe_http.SSRFError):
                safe_http.resolve_and_pin('ghost.example', 80)

    def test_empty_result_fails_closed(self):
        with mock.patch.object(safe_http.socket, 'getaddrinfo', lambda *a, **k: []):
            with self.assertRaises(safe_http.SSRFError):
                safe_http.resolve_and_pin('empty.example', 80)

    def test_resolution_happens_exactly_once(self):
        """No second lookup means no rebinding window."""
        gai = mock.Mock(side_effect=_gai(PUBLIC_IP))
        with mock.patch.object(safe_http.socket, 'getaddrinfo', gai):
            safe_http.resolve_and_pin('api.example.com', 443)
        self.assertEqual(gai.call_count, 1)

    def test_allow_internal_relaxes_classification_but_still_pins(self):
        with mock.patch.object(safe_http.socket, 'getaddrinfo', _gai('10.0.0.5')):
            self.assertEqual(
                safe_http.resolve_and_pin('internal.svc', 80, allow_internal=True),
                '10.0.0.5')


class IsSafeUrlTests(unittest.TestCase):
    def test_non_http_schemes_rejected(self):
        for url in ('file:///etc/passwd', 'gopher://x/1', 'ftp://h/f', ''):
            self.assertFalse(safe_http.is_safe_url(url))

    def test_internal_host_rejected(self):
        with mock.patch.object(safe_http.socket, 'getaddrinfo', _gai('127.0.0.1')):
            self.assertFalse(safe_http.is_safe_url('http://localhost/x'))

    def test_public_host_allowed(self):
        with mock.patch.object(safe_http.socket, 'getaddrinfo', _gai(PUBLIC_IP)):
            self.assertTrue(safe_http.is_safe_url('https://api.example.com/x'))

    def test_never_raises(self):
        def boom(*a, **k):
            raise safe_http.socket.gaierror('nope')
        with mock.patch.object(safe_http.socket, 'getaddrinfo', boom):
            self.assertFalse(safe_http.is_safe_url('https://ghost.example/x'))


class OpenPinnedTests(unittest.TestCase):
    def test_unsupported_scheme_refused(self):
        req = urllib.request.Request('file:///etc/passwd')
        with self.assertRaises(safe_http.SSRFError):
            safe_http.open_pinned(req)

    def test_internal_target_refused(self):
        req = urllib.request.Request('http://internal.example/x')
        with mock.patch.object(safe_http.socket, 'getaddrinfo', _gai('10.0.0.1')):
            with self.assertRaises(safe_http.SSRFError):
                safe_http.open_pinned(req)

    def test_ambient_proxy_is_not_honoured(self):
        """A proxy would route around the pinned IP and reopen the hole."""
        captured = {}

        def fake_build_opener(*handlers):
            captured['handlers'] = handlers
            opener = mock.Mock()
            opener.open.return_value = _FakeResponse()
            return opener

        req = urllib.request.Request('https://api.example.com/x')
        with mock.patch.object(safe_http.socket, 'getaddrinfo', _gai(PUBLIC_IP)), \
             mock.patch.dict(os.environ, {'HTTP_PROXY': 'http://evil:3128',
                                          'HTTPS_PROXY': 'http://evil:3128'}), \
             mock.patch.object(safe_http.urllib.request, 'build_opener',
                               fake_build_opener):
            safe_http.open_pinned(req)

        proxy_handlers = [h for h in captured['handlers']
                          if isinstance(h, urllib.request.ProxyHandler)]
        self.assertEqual(len(proxy_handlers), 1)
        self.assertEqual(proxy_handlers[0].proxies, {},
                         'ProxyHandler must be empty so HTTP(S)_PROXY is ignored')

    def test_no_redirect_handler_refuses_every_3xx(self):
        handler = safe_http.NoRedirectHandler()
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                self.assertIsNone(handler.redirect_request(
                    None, None, code, 'moved', {}, 'http://127.0.0.1/'))


class FetchTests(unittest.TestCase):
    """`fetch` is the board-side entry point — unlike the hook, it needs the
    body back."""

    def _patch_open(self, response):
        return mock.patch.object(safe_http, 'open_pinned', return_value=response)

    def test_returns_status_headers_and_body(self):
        resp = _FakeResponse(b'{"ok":true}', 200, {'Link': '<x>; rel="next"'})
        with self._patch_open(resp):
            status, headers, body = safe_http.fetch('https://api.example.com/x')
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"ok":true}')
        self.assertEqual(headers['Link'], '<x>; rel="next"')

    def test_body_is_capped(self):
        with self._patch_open(_FakeResponse(b'x' * 5000)):
            _status, _headers, body = safe_http.fetch(
                'https://api.example.com/x', max_bytes=100)
        self.assertEqual(len(body), 100)

    def test_http_error_is_returned_with_its_payload_not_raised(self):
        """Vendor rate-limit and permission hints live in the error body."""
        err = urllib.error.HTTPError(
            'https://api.example.com/x', 429, 'Too Many', {'Retry-After': '30'},
            io.BytesIO(b'{"message":"rate limited"}'))
        with mock.patch.object(safe_http, 'open_pinned', side_effect=err):
            status, headers, body = safe_http.fetch('https://api.example.com/x')
        self.assertEqual(status, 429)
        self.assertIn(b'rate limited', body)
        self.assertEqual(headers.get('Retry-After'), '30')

    def test_ssrf_error_propagates(self):
        with mock.patch.object(safe_http, 'open_pinned',
                               side_effect=safe_http.SSRFError('nope')):
            with self.assertRaises(safe_http.SSRFError):
                safe_http.fetch('http://169.254.169.254/latest/meta-data/')

    def test_json_body_is_sent_as_given(self):
        captured = {}

        def fake_open(req, **kw):
            captured['data'] = req.data
            captured['method'] = req.get_method()
            captured['headers'] = req.headers
            return _FakeResponse()

        with mock.patch.object(safe_http, 'open_pinned', fake_open):
            safe_http.fetch('https://api.example.com/g', method='POST',
                            headers={'Content-Type': 'application/json'},
                            body=b'{"query":"{ x }"}')
        self.assertEqual(captured['method'], 'POST')
        self.assertEqual(captured['data'], b'{"query":"{ x }"}')


if __name__ == '__main__':
    unittest.main()
