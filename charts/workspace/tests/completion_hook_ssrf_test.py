"""SSRF-hardening tests for completion-hook delivery (security review finding 5).

The completion-hook validator used to (a) fail OPEN when DNS resolution failed,
(b) validate the URL with one DNS lookup while urlopen did a second one (TOCTOU
/ DNS-rebinding), and (c) let urllib follow 3xx redirects to an internal target
that was never re-validated. This suite pins those holes shut:

  - direct internal addresses (loopback / RFC1918 / link-local / metadata /
    multicast / reserved / unspecified) are rejected by the validator,
  - IPv4-mapped IPv6 internal addresses are normalized and rejected,
  - DNS-resolution failure now fails CLOSED,
  - delivery resolves ONCE and pins the connection to the validated IP, so a
    rebinding answer on a second lookup can't be reached,
  - all redirects (301/302/303/307/308, incl. HTTPS->HTTP downgrade and long
    chains) are refused rather than followed,
  - ordinary public HTTPS hooks still work,
  - ALLOW_INTERNAL_HOOKS=true still bypasses the guard (documented relaxation).

No network is used: socket.getaddrinfo is monkeypatched, and the pinned
http.client connection classes are swapped for a fake that returns canned
responses while recording which IP it was pinned to.

Run with:    python3 -m unittest tests.completion_hook_ssrf_test
(from charts/workspace/)
"""

import http.client
import os
import sys
import unittest
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402

CTM = server.ClaudeTaskManager

PUBLIC_IP = '93.184.216.34'  # example.com — a real public address

INTERNAL = {
    'loopback': '127.0.0.1',
    'rfc1918_10': '10.0.0.5',
    'rfc1918_192': '192.168.1.1',
    'rfc1918_172': '172.16.0.1',
    'link_local': '169.254.1.1',
    'metadata': '169.254.169.254',
    'multicast': '224.0.0.1',
    'reserved': '240.0.0.1',
    'unspecified': '0.0.0.0',
}


def _gai(*ips):
    """socket.getaddrinfo stand-in returning fixed IPv4 answers."""
    def _inner(host, port=None, *a, **k):
        return [(server.socket.AF_INET, server.socket.SOCK_STREAM,
                 server.socket.IPPROTO_TCP, '', (ip, port or 0)) for ip in ips]
    return _inner


class _FakeResp:
    """Minimal stand-in for http.client.HTTPResponse as urllib.do_open uses it."""
    def __init__(self, status, location=None, body=b''):
        self.status = status
        self.code = status
        self.reason = 'Reason'
        self.msg = 'Reason'
        self._headers = http.client.HTTPMessage()
        if location:
            self._headers['Location'] = location
        self._body = body
        self.headers = self._headers

    def info(self):
        return self._headers

    def read(self, amt=None):
        b, self._body = self._body, b''
        return b if amt is None else b[:amt]

    def getheader(self, name, default=None):
        return self._headers.get(name, default)

    def geturl(self):
        return ''

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    """Stand-in for _PinnedHTTP(S)Connection: records the pinned IP and returns
    a canned response instead of touching the network."""
    instances = []
    response = None

    def __init__(self, host, pinned_ip=None, **kw):
        self.host = host
        self.pinned_ip = pinned_ip
        self.kw = kw
        self.requested = None
        self.sock = None  # do_open checks `if h.sock`
        _FakeConn.instances.append(self)

    def set_debuglevel(self, level):
        pass

    def request(self, method, url, body=None, headers=None, encode_chunked=False):
        self.requested = {'method': method, 'url': url, 'headers': headers}

    def getresponse(self):
        return _FakeConn.response

    def close(self):
        pass


class SSRFTestBase(unittest.TestCase):
    def setUp(self):
        _FakeConn.instances = []
        _FakeConn.response = None
        # Guard tests never run with the relaxation on unless they set it.
        self._orig_allow = server.ALLOW_INTERNAL_HOOKS
        server.ALLOW_INTERNAL_HOOKS = False
        self.addCleanup(self._restore_allow)

    def _restore_allow(self):
        server.ALLOW_INTERNAL_HOOKS = self._orig_allow

    def _patch_gai(self, gai):
        p = mock.patch.object(server.socket, 'getaddrinfo', gai)
        p.start()
        self.addCleanup(p.stop)

    def _patch_conn(self, response, https=False):
        _FakeConn.response = response
        target = '_PinnedHTTPSConnection' if https else '_PinnedHTTPConnection'
        p = mock.patch.object(server, target, _FakeConn)
        p.start()
        self.addCleanup(p.stop)


class ValidatorTests(SSRFTestBase):
    def test_direct_internal_addresses_rejected(self):
        for name, ip in INTERNAL.items():
            with mock.patch.object(server.socket, 'getaddrinfo', _gai(ip)):
                self.assertFalse(
                    CTM._is_safe_response_url('http://host.example/hook'),
                    f'{name} ({ip}) must be rejected')

    def test_metadata_service_rejected(self):
        self._patch_gai(_gai('169.254.169.254'))
        self.assertFalse(CTM._is_safe_response_url('http://metadata.example/x'))

    def test_public_address_allowed(self):
        self._patch_gai(_gai(PUBLIC_IP))
        self.assertTrue(CTM._is_safe_response_url('https://api.example.com/hook'))

    def test_mixed_public_and_internal_rejected(self):
        # If ANY resolved address is internal, reject the whole name.
        self._patch_gai(_gai(PUBLIC_IP, '10.0.0.1'))
        self.assertFalse(CTM._is_safe_response_url('http://multi.example/x'))

    def test_dns_failure_fails_closed(self):
        def boom(*a, **k):
            raise server.socket.gaierror('nope')
        self._patch_gai(boom)
        self.assertFalse(CTM._is_safe_response_url('http://ghost.example/x'))

    def test_non_http_scheme_rejected(self):
        self.assertFalse(CTM._is_safe_response_url('file:///etc/passwd'))
        self.assertFalse(CTM._is_safe_response_url('gopher://x/'))

    def test_ipv4_mapped_ipv6_internal_rejected(self):
        # classifier-level
        self.assertIsNone(server._hook_public_ip('::ffff:127.0.0.1'))
        self.assertIsNone(server._hook_public_ip('::ffff:10.0.0.1'))
        self.assertIsNone(server._hook_public_ip('::ffff:169.254.169.254'))
        # end-to-end through the resolver
        self._patch_gai(_gai('::ffff:10.0.0.1'))
        self.assertFalse(CTM._is_safe_response_url('http://mapped.example/x'))

    def test_ipv4_mapped_ipv6_public_allowed(self):
        self.assertIsNotNone(server._hook_public_ip(f'::ffff:{PUBLIC_IP}'))


class ResolveAndPinTests(SSRFTestBase):
    def test_pins_public_ip(self):
        self._patch_gai(_gai(PUBLIC_IP))
        self.assertEqual(CTM._resolve_and_pin('api.example.com', 443), PUBLIC_IP)

    def test_internal_raises(self):
        self._patch_gai(_gai('10.0.0.9'))
        with self.assertRaises(server._HookSSRFError):
            CTM._resolve_and_pin('internal.example', 80)

    def test_dns_failure_raises(self):
        def boom(*a, **k):
            raise server.socket.gaierror('nope')
        self._patch_gai(boom)
        with self.assertRaises(server._HookSSRFError):
            CTM._resolve_and_pin('ghost.example', 80)

    def test_single_resolution_no_rebinding(self):
        """Public answer on the 1st lookup, internal on any later lookup.
        Delivery must resolve ONCE and connect to the pinned public IP."""
        calls = {'n': 0}

        def rebind(host, port=None, *a, **k):
            calls['n'] += 1
            ip = PUBLIC_IP if calls['n'] == 1 else '10.0.0.5'
            return [(server.socket.AF_INET, server.socket.SOCK_STREAM,
                     server.socket.IPPROTO_TCP, '', (ip, port or 0))]

        self._patch_gai(rebind)
        self._patch_conn(_FakeResp(200))
        req = urllib.request.Request('http://rebind.example/hook',
                                     data=b'{}', method='POST')
        resp = CTM._hook_urlopen(req, timeout=5)
        self.assertEqual(resp.status, 200)
        self.assertEqual(calls['n'], 1, 'must resolve exactly once')
        self.assertEqual(_FakeConn.instances[0].pinned_ip, PUBLIC_IP)


class DeliveryRedirectTests(SSRFTestBase):
    def _run(self, status, location, https=False):
        self._patch_gai(_gai(PUBLIC_IP))
        self._patch_conn(_FakeResp(status, location=location), https=https)
        scheme = 'https' if https else 'http'
        req = urllib.request.Request(f'{scheme}://public.example/hook',
                                     data=b'{}', method='POST')
        with self.assertRaises(urllib.error.HTTPError) as cm:
            CTM._hook_urlopen(req, timeout=5)
        return cm.exception

    def test_all_redirect_codes_rejected_to_internal(self):
        for code in (301, 302, 303, 307, 308):
            for ip in ('127.0.0.1', '10.0.0.1', '169.254.169.254'):
                _FakeConn.instances = []
                err = self._run(code, f'http://{ip}/steal')
                self.assertEqual(err.code, code)
                # Exactly one outbound connection, pinned to the validated
                # PUBLIC ip — the redirect to the internal target was NOT
                # followed.
                self.assertEqual(len(_FakeConn.instances), 1)
                self.assertEqual(_FakeConn.instances[0].pinned_ip, PUBLIC_IP)

    def test_https_to_http_downgrade_rejected(self):
        err = self._run(301, 'http://10.0.0.1/steal', https=True)
        self.assertEqual(err.code, 301)
        self.assertEqual(len(_FakeConn.instances), 1)

    def test_redirect_chain_never_followed(self):
        # A redirect that points at another URL which would itself redirect can
        # never form a chain: we reject on the first hop, so only ONE
        # connection is ever made.
        err = self._run(302, 'http://public2.example/next')
        self.assertEqual(err.code, 302)
        self.assertEqual(len(_FakeConn.instances), 1)


class DeliverySuccessTests(SSRFTestBase):
    def test_ordinary_public_https_ok(self):
        self._patch_gai(_gai(PUBLIC_IP))
        self._patch_conn(_FakeResp(200), https=True)
        req = urllib.request.Request('https://api.example.com/hook',
                                     data=b'{"a":1}', method='POST')
        resp = CTM._hook_urlopen(req, timeout=5)
        self.assertEqual(resp.status, 200)
        self.assertEqual(_FakeConn.instances[0].pinned_ip, PUBLIC_IP)
        # original hostname preserved for Host header / SNI
        self.assertEqual(_FakeConn.instances[0].host, 'api.example.com')

    def test_internal_delivery_raises_ssrf(self):
        self._patch_gai(_gai('10.0.0.1'))
        req = urllib.request.Request('http://internal.example/hook',
                                     data=b'{}', method='POST')
        with self.assertRaises(server._HookSSRFError):
            CTM._hook_urlopen(req, timeout=5)


class NoRedirectHandlerTests(unittest.TestCase):
    def test_redirect_request_returns_none(self):
        h = server._NoRedirectHandler()
        for code in (301, 302, 303, 307, 308):
            self.assertIsNone(
                h.redirect_request(None, None, code, 'msg',
                                   http.client.HTTPMessage(),
                                   'http://10.0.0.1/'))


class AllowInternalHooksTests(SSRFTestBase):
    def test_validator_bypassed(self):
        server.ALLOW_INTERNAL_HOOKS = True
        # No DNS lookup at all — internal URL is accepted outright.
        self.assertTrue(CTM._is_safe_response_url('http://10.0.0.1/hook'))
        self.assertTrue(CTM._is_safe_response_url('http://127.0.0.1/hook'))

    def test_resolve_and_pin_allows_internal(self):
        server.ALLOW_INTERNAL_HOOKS = True
        self._patch_gai(_gai('10.0.0.5'))
        # Still a single lookup + pin, but classification skipped.
        self.assertEqual(CTM._resolve_and_pin('internal.svc', 80), '10.0.0.5')


if __name__ == '__main__':
    unittest.main()
