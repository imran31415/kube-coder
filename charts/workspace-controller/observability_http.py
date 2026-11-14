"""HTTP observation without duplicating BaseHTTPRequestHandler's parser."""
import contextvars
import http.server
import json
import logging
import re
import sys
import time
import uuid
from urllib.parse import urlsplit

from observability_logging import CONTEXT, event, problem
from observability_metrics import REGISTRY

DEADLINE = contextvars.ContextVar('request_deadline', default=None)
METHODS = frozenset(('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'))
EXACT_ROUTES = frozenset(('/', '/health', '/healthz', '/livez', '/metrics', '/metrics/prometheus',
                          '/api/workspaces', '/api/insights', '/api/capacity', '/api/capacity/summary',
                          '/api/spend', '/api/events', '/api/admin/token', '/api/provision/config', '/api/provision'))
WORKSPACE_ROUTE = re.compile(r'^/api/(self/)?workspaces/[^/]+(?:/(metrics|start|stop|restart|update|version|resources))?$')


#: Every path no route template claims. One series, however many distinct
#: paths, ids or probes arrive.
OTHER_ROUTE = 'other'


def route_identity(path):
    path = urlsplit(path).path
    if path.startswith('/oauth/'):
        path = path[6:]
    if path in EXACT_ROUTES:
        return path
    match = WORKSPACE_ROUTE.fullmatch(path)
    if match:
        return '/api/' + (match[1] or '') + 'workspaces/:user' + ('/' + match[2] if match[2] else '')
    return OTHER_ROUTE


class RequestDeadlineExceeded(RuntimeError):
    """Unlike socket TimeoutError, this must reach the safe JSON error boundary."""


def without_deadline(fn, *args, **kwargs):
    """Run `fn` outside the calling request's budget, keeping its log context.

    For work whose result is shared by every caller (a process-wide cache
    refresh): whichever request happens to miss the cache must not cut that
    refresh short with its own leftover time, or one late request fails the
    refresh for all of them and reports it as a timeout."""
    def run():
        DEADLINE.set(None)
        return fn(*args, **kwargs)
    return contextvars.copy_context().run(run)


def remaining_timeout(default):
    deadline = DEADLINE.get()
    if deadline is None:
        return default
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RequestDeadlineExceeded('request time budget used up before this upstream call')
    return min(default, remaining)


class ObservedHTTP:
    telemetry_service = 'workspace'

    def handle_one_request(self):
        self._obs_status = None
        self._obs_started = None
        self._obs_headers = False
        self._obs_kind = 'request'
        self._obs_active = False
        self._obs_route = OTHER_ROUTE
        self._obs_aborted = False
        self._obs_request_id = uuid.uuid4().hex
        token = CONTEXT.set({'request_id': self._obs_request_id})
        deadline_token = DEADLINE.set(None)
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self._obs_aborted = True
            self.close_connection = True
        except Exception as exc:
            code = 'metrics_timeout' if isinstance(exc, RequestDeadlineExceeded) else 'internal_error'
            detail = problem(self.telemetry_service, code, exc_info=sys.exc_info())
            if not self._obs_headers:
                body = json.dumps(detail).encode()
                try:
                    self.send_response(500)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    self._obs_aborted = True
            self.close_connection = True
        finally:
            try:
                if self._obs_started is not None:
                    elapsed = time.monotonic() - self._obs_started
                    method = getattr(self, 'command', None)
                    method = method if method in METHODS else 'other'
                    listener = self._obs_listener()
                    if self._obs_active:
                        REGISTRY.active(self.telemetry_service, listener, self._obs_kind, -1)
                    REGISTRY.request(self.telemetry_service, listener, self._obs_route, method,
                                     self._obs_status, self._obs_kind, elapsed, self._obs_aborted)
                    event(self.telemetry_service, 'request_completed',
                          route=self._obs_route, method=method, status=self._obs_status,
                          duration_ms=round(elapsed * 1000, 2), aborted=self._obs_aborted)
            except Exception:
                # Telemetry must never replace a business response or recurse
                # through request observation when the registry itself fails.
                event(self.telemetry_service, 'telemetry_failed', logging.ERROR, exc_info=sys.exc_info())
            finally:
                DEADLINE.reset(deadline_token)
                CONTEXT.reset(token)

    def _obs_listener(self):
        if self.telemetry_service == 'workspace':
            return 'workspace'
        return 'self-serve' if getattr(self.server, 'restricted', False) else 'admin'

    def parse_request(self):
        self._obs_started = time.monotonic()
        valid = super().parse_request()
        if valid:
            self._obs_route = route_identity(self.path)
            if self.telemetry_service == 'controller' and self.command == 'GET':
                if self._obs_route in ('/api/capacity', '/api/spend', '/api/insights', '/api/workspaces/:user/metrics'):
                    DEADLINE.set(time.monotonic() + 30)
                elif self._obs_route == '/api/capacity/summary':
                    DEADLINE.set(time.monotonic() + 15)
            REGISTRY.active(self.telemetry_service, self._obs_listener(), self._obs_kind, 1)
            self._obs_active = True
        return valid

    def observe_kind(self, kind):
        if kind not in ('request', 'stream', 'proxy'):
            raise ValueError('unknown request kind')
        if getattr(self, '_obs_active', False) and kind != self._obs_kind:
            REGISTRY.active(self.telemetry_service, self._obs_listener(), self._obs_kind, -1)
            REGISTRY.active(self.telemetry_service, self._obs_listener(), kind, 1)
        self._obs_kind = kind

    def send_response_only(self, code, message=None):
        if code >= 200 or code == 101:
            self._obs_status = code if 100 <= code <= 599 else 500
        if code == 101:
            self.observe_kind('proxy')
        super().send_response_only(code, message)

    def send_header(self, keyword, value):
        if keyword.lower() == 'content-type' and 'text/event-stream' in str(value):
            self.observe_kind('stream')
        super().send_header(keyword, value)

    def end_headers(self):
        if getattr(self, '_obs_request_id', None):
            self.send_header('X-Request-ID', self._obs_request_id)
        super().end_headers()
        self._obs_headers = True

    def log_message(self, fmt, *args):
        # BaseHTTPServer access logs include raw paths/query strings. The
        # completion event replaces them with bounded route identities.
        pass


class MetricsHandler(http.server.BaseHTTPRequestHandler):
    """Separate listener: never shares the proxy-trusting admin handler."""
    def do_GET(self):
        if self.path != '/metrics':
            self.send_error(404)
            return
        try:
            body = REGISTRY.exposition().encode()
        except Exception:
            problem('controller', exc_info=sys.exc_info())
            self.send_error(503, 'Metrics unavailable')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; version=0.0.4; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt, *args):
        pass
