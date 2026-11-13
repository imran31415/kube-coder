"""Exercise real controller listeners without Kubernetes or Prometheus."""
import http.client
import http.server
import json
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import controller
from observability_metrics import Registry
from observability_http import DEADLINE, RequestDeadlineExceeded
from observability_logging import CONTEXT


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


if __name__ == '__main__':
    unittest.main()
