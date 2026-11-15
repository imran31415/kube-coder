"""The metrics-only listener a Prometheus scrape talks to (#869).

Why a separate port instead of `/metrics/prometheus` on 6080: the NetworkPolicy
carve-out that lets Prometheus in is port-scoped, and 6080 is the whole
dashboard and Task API. In oauth2 mode server.py trusts `X-Auth-Request-User` /
`Remote-User` from whatever reaches it directly, so opening 6080 to a pod in the
monitoring namespace would let that pod forge a header and drive the workspace.
This listener has no such trust: it serves one path, to one credential, and
nothing else, so it is the only port the carve-out opens.

The credential is KC_METRICS_SCRAPE_TOKEN, this workspace's own derivation of
the controller's scrape master (HMAC-SHA256(master, "kc-metrics-scrape/<user>"),
seeded by scripts/ensure-workspace-namespace.sh into a Secret the PodMonitor
references). It is read from the environment on every request, so a rotated
Secret takes effect on restart with no stale copy still accepted, and an unset
or blank value authenticates nothing. The Claude Task API token is deliberately
not accepted here: a scraper should never be handed that one.
"""
import http.server
import logging
import os
import secrets
import sys
import threading

from observability_logging import event, problem

PATH = '/metrics/prometheus'
TOKEN_ENV = 'KC_METRICS_SCRAPE_TOKEN'
PORT_ENV = 'KC_METRICS_PORT'
CONTENT_TYPE = 'text/plain; version=0.0.4; charset=utf-8'


def scrape_token_matches(authorization):
    expected = os.environ.get(TOKEN_ENV, '').strip()
    if not expected or not (authorization or '').startswith('Bearer '):
        return False
    return secrets.compare_digest(authorization[7:].strip().encode(), expected.encode())


def configured_port(environ=os.environ):
    """The port from KC_METRICS_PORT, or None when the listener is off.

    A bad value turns the listener off with an error line rather than
    raising: it is read at import, and must not take the dashboard down."""
    raw = (environ.get(PORT_ENV) or '').strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        port = 0
    if not 0 < port < 65536:
        event('workspace', 'metrics_listener_misconfigured', logging.ERROR, value=raw[:20])
        return None
    return port


def handler_for(render):
    """A handler class that serves `render()` at PATH and nothing else.

    `render` returns the exposition text. It is passed in rather than imported
    so this module never pulls in server.py and its route handlers.
    """
    class MetricsScrapeHandler(http.server.BaseHTTPRequestHandler):
        server_version = 'kube-coder-metrics'
        sys_version = ''

        def _send(self, status, body, content_type='text/plain; charset=utf-8', extra=()):
            body = body.encode('utf-8') if isinstance(body, str) else body
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            for name, value in extra:
                self.send_header(name, value)
            self.end_headers()
            if self.command != 'HEAD':
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def do_GET(self):
            if self.path.split('?', 1)[0] != PATH:
                self._send(404, 'not found\n')
                return
            if not scrape_token_matches(self.headers.get('Authorization')):
                self._send(401, 'unauthorized\n', extra=(('WWW-Authenticate', 'Bearer'),))
                return
            try:
                body = render()
            except Exception:
                problem('workspace', exc_info=sys.exc_info())
                self._send(503, 'metrics unavailable\n')
                return
            self._send(200, body, CONTENT_TYPE)

        def _refuse(self):
            self._send(405, 'method not allowed\n', extra=(('Allow', 'GET'),))

        do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _refuse

        def log_message(self, fmt, *args):
            # Access lines would carry raw paths; a scrape every interval adds
            # nothing an operator needs.
            pass

    return MetricsScrapeHandler


def start(render, port, host=''):
    """Serve on a daemon thread; returns the server so tests can stop it."""
    httpd = http.server.ThreadingHTTPServer((host, port), handler_for(render))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name='metrics-listener', daemon=True).start()
    event('workspace', 'metrics_listener_started', port=httpd.server_address[1])
    return httpd
