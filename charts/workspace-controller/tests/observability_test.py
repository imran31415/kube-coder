"""Exercise real controller listeners without Kubernetes or Prometheus."""
import contextlib
import http.client
import http.server
import io
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import controller
from observability_metrics import Registry
from observability_http import DEADLINE, RequestDeadlineExceeded
from observability_logging import CONTEXT
import observability_logging


class ControllerHTTPTest(unittest.TestCase):
    def serve(self, handler):
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
        self.addCleanup(connection.close)
        return connection

    def test_metrics_listener_has_no_admin_routes(self):
        conn = self.serve(controller.MetricsHandler)
        conn.request('GET', '/api/admin/token', headers={'X-Forwarded-User': 'admin'})
        response = conn.getresponse()
        self.assertEqual(response.status, 404)
        response.read()
        conn.request('GET', '/metrics')
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn(b'kubecoder_http_requests_total', response.read())

    def test_partial_metrics_response_is_safe_and_correlated(self):
        conn = self.serve(controller.Handler)
        with patch.object(controller, 'DEV_TOKEN', 'test'), \
             patch.object(controller, 'cluster_capacity', return_value={'metricsError': 'metrics_timeout'}):
            conn.request('GET', '/api/capacity', headers={'Authorization': 'Bearer test'})
            response = conn.getresponse()
            payload = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(payload['problem']['code'], 'metrics_timeout')
        self.assertEqual(payload['problem']['request_id'], response.getheader('X-Request-ID'))
        self.assertTrue(payload['problem']['error_id'])

    def test_operations_record_partial_and_submission_separately(self):
        registry = Registry()
        with patch.object(controller, 'REGISTRY', registry):
            @controller.observed_operation('update')
            def update():
                return {'persistError': 'failed'}

            @controller.observed_operation('provision_submit')
            def submit():
                return {'job': 'example'}

            update()
            submit()
        output = registry.exposition()
        self.assertIn('operation="update",outcome="partial"} 1', output)
        self.assertIn('operation="provision_submit",outcome="submitted"} 1', output)

    def test_exhausted_budget_returns_json_instead_of_closing_without_response(self):
        conn = self.serve(controller.Handler)
        with patch.object(controller, 'DEV_TOKEN', 'test'), \
             patch.object(controller, 'cluster_capacity', side_effect=RequestDeadlineExceeded()):
            conn.request('GET', '/api/capacity', headers={'Authorization': 'Bearer test'})
            response = conn.getresponse()
            payload = json.loads(response.read())
        self.assertEqual(response.status, 500)
        self.assertEqual(payload['code'], 'metrics_timeout')
        self.assertEqual(payload['request_id'], response.getheader('X-Request-ID'))

    def test_parallel_namespace_reads_inherit_budget_and_correlation(self):
        deadline = time.monotonic() + 10
        token = DEADLINE.set(deadline)
        context = CONTEXT.set({'request_id': 'parallel'})
        def read(*args, **kwargs):
            self.assertEqual(DEADLINE.get(), deadline)
            self.assertEqual(CONTEXT.get()['request_id'], 'parallel')
            return {'items': []}
        try:
            with patch.object(controller, 'discover_workspace_namespaces', return_value=['ws-a', 'ws-b']), \
                 patch.object(controller, '_kubectl_json', side_effect=read):
                self.assertEqual(controller._collect('pods'), [])
        finally:
            CONTEXT.reset(context)
            DEADLINE.reset(token)

    def _logs(self, stream):
        return [json.loads(line) for line in stream.getvalue().splitlines() if line.startswith('{')]

    def test_operator_facing_5xx_keeps_its_message_and_logs_it(self):
        # The console shows `error` for a failed provision/scale; a generic
        # "could not be completed" would hide e.g. a GitHub 404 for a typo'd
        # login. The same reason is logged (redacted) under the error_id.
        conn = self.serve(controller.Handler)
        stream = io.StringIO()
        with patch.object(controller, 'DEV_TOKEN', 'test'), \
             patch.object(controller, 'cached_list_workspaces',
                          side_effect=controller.KubectlError('kubectl get failed', 'forbidden: token=abc')), \
             contextlib.redirect_stderr(stream):
            conn.request('GET', '/api/workspaces', headers={'Authorization': 'Bearer test'})
            response = conn.getresponse()
            payload = json.loads(response.read())
        self.assertEqual(response.status, 502)
        self.assertEqual(payload['error'], 'kubectl get failed')
        self.assertEqual(payload['problem']['code'], 'internal_error')
        logged = [r for r in self._logs(stream) if r.get('error_id') == payload['problem']['error_id']]
        self.assertEqual(logged[0]['detail'], 'kubectl get failed')
        # The legacy line keeps kubectl's stderr, with the credential redacted.
        self.assertIn('forbidden: token=[redacted]', stream.getvalue())
        self.assertNotIn('abc', stream.getvalue())

    def test_expected_monitoring_states_are_not_logged(self):
        # Prometheus unset is the chart default and every open tab polls; a
        # warning per poll would log forever.
        conn = self.serve(controller.Handler)
        stream = io.StringIO()
        with patch.object(controller, 'DEV_TOKEN', 'test'), \
             patch.object(controller, 'cluster_capacity', return_value={'metricsError': 'metrics_disabled'}), \
             contextlib.redirect_stderr(stream):
            for _ in range(3):
                conn.request('GET', '/api/capacity', headers={'Authorization': 'Bearer test'})
                response = conn.getresponse()
                payload = json.loads(response.read())
        self.assertEqual(payload['problem']['code'], 'metrics_disabled')
        self.assertIsNone(payload['problem']['error_id'])
        self.assertEqual(payload['problem']['request_id'], response.getheader('X-Request-ID'))
        events = [r['event'] for r in self._logs(stream)]
        self.assertNotIn('metrics_disabled', events)
        self.assertEqual(events.count('request_completed'), 3)

    def test_prometheus_failure_reason_is_logged_at_most_once_a_minute(self):
        stream = io.StringIO()
        def unreachable(*args, **kwargs):
            raise urllib.error.URLError('connection refused')
        with patch.object(controller, 'PROMETHEUS_URL', 'http://user:pw@prom:9090'), \
             patch.object(controller.urllib.request, 'urlopen', side_effect=unreachable), \
             patch.dict(observability_logging._limits, clear=True), \
             contextlib.redirect_stderr(stream):
            for _ in range(5):
                with self.assertRaises(controller.PromError) as ctx:
                    controller._prom_get('/api/v1/query', {'query': 'up'})
                self.assertEqual(str(ctx.exception), 'metrics_unavailable')
        rows = [r for r in self._logs(stream) if r['event'] == 'prometheus_query_failed']
        self.assertEqual(len(rows), 1)
        self.assertIn('connection refused', rows[0]['detail'])
        self.assertNotIn('pw', stream.getvalue())

    def test_shared_cache_refresh_ignores_the_callers_budget(self):
        # One request's leftover time must not abort (and mislabel) the
        # process-wide refresh every other request is waiting on.
        seen = []
        def refresh():
            seen.append(DEADLINE.get())
            return [{'user': 'a'}]
        token = DEADLINE.set(time.monotonic() + 0.01)
        try:
            with patch.object(controller, 'list_workspaces', side_effect=refresh), \
                 patch.dict(controller._list_cache, {'ts': 0.0, 'data': None}):
                self.assertEqual(controller.cached_list_workspaces(), [{'user': 'a'}])
            self.assertIsNotNone(DEADLINE.get())
        finally:
            DEADLINE.reset(token)
        self.assertEqual(seen, [None])

    def test_budget_timeout_says_which_limit_ran_out(self):
        token = DEADLINE.set(time.monotonic() + 2)
        try:
            with patch.object(controller.subprocess, 'run',
                              side_effect=controller.subprocess.TimeoutExpired('kubectl', 2)), \
                 patch.object(controller.time, 'sleep'):
                with self.assertRaises(controller.KubectlError) as ctx:
                    controller._kubectl_json(['get', 'pods'])
        finally:
            DEADLINE.reset(token)
        self.assertIn('time budget', str(ctx.exception))
        self.assertNotIn(f'timed out after {controller.KUBECTL_TIMEOUT}s', str(ctx.exception))
        with patch.object(controller.subprocess, 'run',
                          side_effect=controller.subprocess.TimeoutExpired('kubectl', 1)), \
             patch.object(controller.time, 'sleep'):
            with self.assertRaises(controller.KubectlError) as ctx:
                controller._kubectl_json(['get', 'pods'])
        self.assertEqual(str(ctx.exception), f'kubectl get timed out after {controller.KUBECTL_TIMEOUT}s')


if __name__ == '__main__':
    unittest.main()
