"""The metrics-only scrape listener and the HTTP telemetry it serves (#869).

Three properties, in order of how expensive they are to get wrong:

1. **The listener serves metrics and nothing else.** The NetworkPolicy opens
   this port, not 6080, to the monitoring namespace. If any dashboard or Task
   API route answered here, a pod there could reach it, and in oauth2 mode
   server.py trusts a forged `X-Auth-Request-User`. `MetricsListenerTests`
   drives a real listener with the real render function.
2. **The scrape credential stops at the listener.** KC_METRICS_SCRAPE_TOKEN
   buys the exposition on that port and nothing on 6080; the Task API token
   is not accepted by the listener. `ApiPortTests`.
3. **HTTP telemetry stays bounded.** Its routes come from the RouteTables plus
   one "other" series and its statuses are classes, so distinct paths, ids and
   probes never add series. `HttpTelemetryCardinalityTests`.

Run with:  python3 -m unittest tests.metrics_listener_test   (from charts/workspace/)
"""
from __future__ import annotations

import contextlib
import http.server
import io
import os
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import server  # noqa: E402
import metrics_listener  # noqa: E402
import observability_http  # noqa: E402
import promparse  # noqa: E402
from handlers.routing import RouteTable  # noqa: E402
from observability_metrics import Registry  # noqa: E402
from prometheus_metrics_test import PREFIX, stubbed  # noqa: E402

SCRAPE = 'derived-scrape-token-for-this-workspace'


@contextlib.contextmanager
def scrape_token(value=SCRAPE):
    with mock.patch.dict(os.environ):
        os.environ.pop(metrics_listener.TOKEN_ENV, None)
        if value is not None:
            os.environ[metrics_listener.TOKEN_ENV] = value
        yield


def request(port, path, token=None, method='GET', headers=None):
    """(status, headers, body) without raising on 4xx/5xx."""
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', method=method)
    if token is not None:
        req.add_header('Authorization', f'Bearer {token}')
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, dict(e.headers), e.read()


class MetricsListenerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = metrics_listener.start(server.PrometheusMetricsCollector.scrape, 0, '127.0.0.1')
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_serves_the_exposition_with_the_scrape_token(self):
        with scrape_token(), stubbed():
            status, headers, body = request(self.port, '/metrics/prometheus', SCRAPE)
        self.assertEqual(status, 200)
        self.assertTrue(headers['Content-Type'].startswith('text/plain; version=0.0.4'))
        parsed = promparse.assert_valid(body.decode('utf-8'))
        # Product families and this process's HTTP telemetry, one document.
        self.assertIn(PREFIX + 'metrics_collector_up', parsed.types)
        self.assertIn('kubecoder_http_requests_total', parsed.types)

    def test_refuses_without_a_matching_scrape_token(self):
        task_api_token = 'task-api-token-is-not-a-scrape-credential'
        with scrape_token(), mock.patch.object(server.ClaudeTaskManager, 'get_or_create_token',
                                               staticmethod(lambda: task_api_token)):
            for token in (None, '', 'wrong', SCRAPE + 'x', task_api_token):
                with self.subTest(token=token):
                    status, headers, body = request(self.port, '/metrics/prometheus', token)
                    self.assertEqual(status, 401)
                    self.assertEqual(headers.get('WWW-Authenticate'), 'Bearer')
                    self.assertNotIn(b'kubecoder_', body)

    def test_an_absent_or_blank_configured_token_never_authenticates(self):
        # Deleting the Secret must close the endpoint, not open it.
        for configured in (None, '', '   '):
            with scrape_token(configured):
                for presented in ('', '   ', 'anything'):
                    status, _, _ = request(self.port, '/metrics/prometheus', presented)
                    self.assertEqual(status, 401, f'{configured!r} vs {presented!r}')

    def test_token_is_read_per_request_so_rotation_takes_effect(self):
        with scrape_token('first'), stubbed():
            self.assertEqual(request(self.port, '/metrics/prometheus', 'first')[0], 200)
        with scrape_token('second'):
            self.assertEqual(request(self.port, '/metrics/prometheus', 'first')[0], 401)

    def test_serves_no_other_path_even_with_the_token_and_a_forged_identity(self):
        # The routes a pod in the monitoring namespace would want if this port
        # led to BrowserHandler. None of them exist here, whatever headers say.
        forged = {'X-Auth-Request-User': 'owner', 'Remote-User': 'owner',
                  'X-Forwarded-User': 'owner', 'X-Auth-Request-Email': 'owner@example.com'}
        paths = ('/', '/metrics', '/health', '/api/claude/tasks', '/api/claude/auth/token',
                 '/api/files/list', '/api/memory', '/oauth/metrics/prometheus',
                 '/metrics/prometheus/', '/metrics/prometheus/../../api/claude/tasks',
                 '/vnc/', '/terminal')
        with scrape_token():
            for path in paths:
                with self.subTest(path=path):
                    status, _, body = request(self.port, path, SCRAPE, headers=forged)
                    self.assertEqual(status, 404)
                    self.assertEqual(body, b'not found\n')

    def test_refuses_every_method_but_get(self):
        with scrape_token():
            for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'):
                with self.subTest(method=method):
                    status, headers, _ = request(self.port, '/metrics/prometheus', SCRAPE, method=method)
                    self.assertEqual(status, 405)
                    self.assertEqual(headers.get('Allow'), 'GET')

    def test_the_handler_is_not_the_dashboard_handler(self):
        # Structural guard: nothing mixed in from BrowserHandler, so no route
        # table and no header-trusting auth can ever be reachable here.
        handler = metrics_listener.handler_for(lambda: '')
        self.assertFalse(issubclass(handler, server.BrowserHandler))
        self.assertFalse(hasattr(handler, 'check_claude_auth'))
        self.assertEqual({n for n in dir(handler) if n.startswith('do_')},
                         {'do_GET', 'do_HEAD', 'do_POST', 'do_PUT', 'do_PATCH', 'do_DELETE', 'do_OPTIONS'})

    def test_render_failure_is_a_503_not_a_traceback(self):
        def broken():
            raise RuntimeError('token=hunter2')
        httpd = metrics_listener.start(broken, 0, '127.0.0.1')
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        with scrape_token(), contextlib.redirect_stderr(io.StringIO()):
            status, _, body = request(httpd.server_address[1], '/metrics/prometheus', SCRAPE)
        self.assertEqual(status, 503)
        self.assertNotIn(b'hunter2', body)


class ConfiguredPortTests(unittest.TestCase):
    def test_off_unless_configured(self):
        self.assertIsNone(metrics_listener.configured_port({}))
        self.assertIsNone(metrics_listener.configured_port({'KC_METRICS_PORT': ' '}))
        self.assertEqual(metrics_listener.configured_port({'KC_METRICS_PORT': '6082'}), 6082)

    def test_a_bad_value_disables_the_listener_instead_of_crashing(self):
        # Read at import time: raising would take the dashboard down with it.
        with contextlib.redirect_stderr(io.StringIO()):
            for raw in ('abc', '0', '70000', '-1'):
                self.assertIsNone(metrics_listener.configured_port({'KC_METRICS_PORT': raw}))


class _ApiServer(unittest.TestCase):
    """BrowserHandler on a loopback port, oauth2 mode (server.py enforces)."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix='kc-metrics-listener-')
        cls._saved = (server.AUTH_MODE, server.ClaudeTaskManager.TASKS_DIR,
                      server.ClaudeTaskManager.TOKEN_FILE)
        server.AUTH_MODE = 'oauth2'
        server.ClaudeTaskManager.TASKS_DIR = cls.tmpdir
        server.ClaudeTaskManager.TOKEN_FILE = os.path.join(cls.tmpdir, '.api-token')
        cls.token = server.ClaudeTaskManager.get_or_create_token()
        cls.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), server.BrowserHandler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        (server.AUTH_MODE, server.ClaudeTaskManager.TASKS_DIR,
         server.ClaudeTaskManager.TOKEN_FILE) = cls._saved
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)


class ApiPortTests(_ApiServer):
    def test_the_scrape_token_opens_nothing_on_the_api_port(self):
        with scrape_token():
            for path in ('/metrics/prometheus', '/metrics', '/api/claude/tasks'):
                with self.subTest(path=path):
                    self.assertEqual(request(self.port, path, SCRAPE)[0], 401)

    def test_the_task_api_token_still_reads_the_exposition_on_the_api_port(self):
        # Unchanged from before #869 for existing clients.
        with stubbed():
            status, _, body = request(self.port, '/metrics/prometheus', self.token)
        self.assertEqual(status, 200)
        parsed = promparse.assert_valid(body.decode('utf-8'))
        self.assertIn('kubecoder_http_request_duration_seconds', parsed.types)


class HttpTelemetryCardinalityTests(_ApiServer):
    """Route labels come from a fixed vocabulary, whatever arrives."""

    LABELS = {'service', 'listener', 'route', 'method', 'status', 'kind', 'operation', 'le'}
    STATUS = {'1xx', '2xx', '3xx', '4xx', '5xx', 'other'}

    def setUp(self):
        self.registry = Registry()
        patcher = mock.patch.object(observability_http, 'REGISTRY', self.registry)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def route_vocabulary():
        names = {observability_http.OTHER_ROUTE} | set(observability_http.EXACT_ROUTES)
        # The shared helper's controller templates (WORKSPACE_ROUTE).
        for prefix in ('/api/', '/api/self/'):
            for suffix in ('', '/metrics', '/start', '/stop', '/restart', '/update',
                           '/version', '/resources'):
                names.add(f'{prefix}workspaces/:user{suffix}')
        for module in list(sys.modules.values()):
            table = getattr(module, 'ROUTES', None)
            if isinstance(table, RouteTable):
                names |= {route.handler for route in table.routes}
        return names

    def hit(self, n):
        for i in range(n):
            ident = uuid.uuid4().hex
            for path in (f'/api/claude/tasks/{ident}', f'/api/memory/ns/{ident}',
                         f'/no/such/{ident}?token={ident}', f'/api/workspaces/{ident}/metrics',
                         f'/assets/{ident}.js'):
                request(self.port, path, self.token)
            request(self.port, f'/api/claude/tasks/{ident}', self.token, method='DELETE')

    def series(self):
        return promparse.assert_valid(self.registry.exposition()).samples

    def test_distinct_paths_and_ids_add_no_series(self):
        self.hit(3)
        before = len(self.series())
        self.hit(40)
        self.assertEqual(len(self.series()), before)

    def test_every_label_comes_from_a_fixed_vocabulary(self):
        self.hit(5)
        routes = self.route_vocabulary()
        samples = self.series()
        self.assertTrue(samples)
        for name, labels in samples:
            labels = dict(labels)
            self.assertLessEqual(set(labels), self.LABELS, name)
            if 'route' in labels:
                self.assertIn(labels['route'], routes)
            if 'status' in labels:
                self.assertIn(labels['status'], self.STATUS)
        text = self.registry.exposition()
        self.assertNotRegex(text, re.compile(r'[0-9a-f]{32}'))
        self.assertNotIn('token=', text)

    def test_many_requests_over_few_shapes_make_few_routes(self):
        # 60 requests in six shapes, every one with a fresh id: at most one
        # route value per shape (several land in "other"), never one per path.
        self.hit(10)
        routes = {dict(l)['route'] for _, l in self.series() if 'route' in dict(l)}
        self.assertLessEqual(len(routes), 6, routes)
        self.assertIn(observability_http.OTHER_ROUTE, routes)

if __name__ == '__main__':
    unittest.main()
