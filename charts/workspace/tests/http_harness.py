"""Boot a real `server.BrowserHandler` for an endpoint test (#100).

A dozen suites under `tests/` hand-roll the same three things: pick a port,
start a ThreadingHTTPServer on BrowserHandler in a daemon thread, and issue
requests that have to yield a status code even when it is an error one
(`urllib` raises on 4xx/5xx, so every suite catches HTTPError itself). The
route-table series adds one such suite per domain, so the shape is lifted
here once rather than copied per domain.

Deliberately only that shape. A suite that needs fixtures — a temp DOCS_DIR,
a patched manager, the auth bypass — still sets those up in its own
`setUpClass`; this base class knows about nothing but the socket.
"""

import http.server
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402


class EndpointTestCase(unittest.TestCase):
    """One server per test class, on a port the kernel picks.

    AUTH_MODE is pinned to oauth2 by default — the mode where server.py is
    its own enforcer, so an auth-gated route answers 401 instead of doing
    real work. A 401 is also what separates "the route matched" from "it fell
    through to the 404", which is most of what a routing test wants to know.
    """

    #: AUTH_MODE for the lifetime of the class; restored on teardown.
    auth_mode = 'oauth2'

    @classmethod
    def setUpClass(cls):
        cls._auth_mode_save = server.AUTH_MODE
        server.AUTH_MODE = cls.auth_mode
        # Port 0 lets the kernel pick a free one, with no window between
        # probing for it and binding it.
        cls.httpd = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0), server.BrowserHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.AUTH_MODE = cls._auth_mode_save

    def request(self, path, method='GET', body=None):
        """Return `(status, body)` whether or not the response was an error."""
        req = urllib.request.Request(
            f'http://127.0.0.1:{self.port}{path}', data=body, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def get(self, path):
        return self.request(path)

    def post(self, path, body=b''):
        return self.request(path, method='POST', body=body)
