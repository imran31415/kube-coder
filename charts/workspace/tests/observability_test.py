"""Telemetry contracts exercised with a real threaded HTTP server, no cluster."""
import concurrent.futures
import contextlib
import http.client
import http.server
import io
import json
import math
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import observability_http as observation
from observability_logging import CONTEXT, event, logger, problem
from observability_metrics import Registry
from prometheus_exposition import Exposition, ExpositionError


class HistogramTest(unittest.TestCase):
    def test_histogram_format_and_collisions(self):
        exp = Exposition().histogram('latency', 'elapsed', [({}, [(1., 2), (math.inf, 3)], 2.5)])
        text = exp.render()
        self.assertIn('latency_bucket{le="+Inf"} 3', text)
        self.assertIn('latency_count 3', text)
        self.assertIn('latency_sum 2.5', text)
        with self.assertRaises(ExpositionError):
            exp.gauge('latency_count', 'collision', [({}, 0)])

    def test_rejects_broken_histograms(self):
        for buckets, total in [([(1., 1)], 1), ([(1., 2), (math.inf, 1)], 1),
                               ([(1., 1), (1., 2), (math.inf, 2)], 1), ([(math.inf, 1)], math.nan)]:
            with self.subTest(buckets=buckets, total=total), self.assertRaises(ExpositionError):
                Exposition().histogram('latency', 'elapsed', [({}, buckets, total)])

    def test_concurrent_counts_and_buckets(self):
        reg = Registry()
        def record(_):
            reg.request('workspace', 'workspace', 'fixed', 'GET', 200, 'request', .1)
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            list(pool.map(record, range(1000)))
        text = reg.exposition()
        self.assertIn('status="2xx"} 1000', text)
        self.assertIn('kubecoder_http_request_duration_seconds_count{listener="workspace",method="GET",route="fixed",service="workspace"} 1000', text)

    def test_operation_submitted_is_not_completed(self):
        reg = Registry()
        with reg.operation('provision_submit') as observation:
            observation['outcome'] = 'submitted'
        with self.assertRaises(ValueError):
            with reg.operation('update'):
                raise ValueError('failed')
        text = reg.exposition()
        self.assertIn('outcome="submitted"} 1', text)
        self.assertIn('outcome="error"} 1', text)


class Handler(observation.ObservedHTTP, http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_GET(self):
        if self.path == '/error':
            raise RuntimeError('credential=do-not-expose')
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream' if self.path == '/stream' else 'text/plain')
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(b'ok')


class HTTPTest(unittest.TestCase):
    def setUp(self):
        self.registry = Registry()
        self.patch = patch.object(observation, 'REGISTRY', self.registry)
        self.patch.start()
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.conn = http.client.HTTPConnection(*self.server.server_address, timeout=2)

    def tearDown(self):
        self.conn.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.patch.stop()

    def get(self, path, method='GET'):
        self.conn.request(method, path)
        response = self.conn.getresponse()
        data = response.read()
        return response, data

    def test_keep_alive_ids_and_no_raw_path_labels(self):
        a, _ = self.get('/random/a?token=secret')
        b, _ = self.get('/random/b')
        self.assertNotEqual(a.getheader('X-Request-ID'), b.getheader('X-Request-ID'))
        self.assertFalse(CONTEXT.get())
        text = self.registry.exposition()
        self.assertNotIn('secret', text)
        self.assertNotIn('/random', text)
        self.assertEqual(observation.route_identity('/oauth/api/workspaces/alice/metrics'), '/api/workspaces/:user/metrics')

    def test_safe_500_and_exception_log(self):
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            res, data = self.get('/error')
        self.assertEqual(res.status, 500)
        body = json.loads(data)
        self.assertIn('error_id', body)
        self.assertEqual(body['request_id'], res.getheader('X-Request-ID'))
        self.assertNotIn('do-not-expose', data.decode())
        logs = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertTrue(any(row.get('error_id') == body['error_id'] for row in logs))
        self.assertNotIn('do-not-expose', stream.getvalue())

    def test_stream_not_in_ordinary_latency(self):
        self.get('/stream')
        self.conn.close()
        self.server.shutdown()
        text = self.registry.exposition()
        self.assertNotIn('kubecoder_http_request_duration_seconds_count', text)
        self.assertIn('kind="stream"', text)

    def test_unknown_method_is_bounded(self):
        res, _ = self.get('/x', 'UNBOUNDED-METHOD')
        self.assertEqual(res.status, 501)
        self.server.shutdown()
        self.assertNotIn('UNBOUNDED-METHOD', self.registry.exposition())

    def test_budget_expires_without_network(self):
        token = observation.DEADLINE.set(time.monotonic() - 1)
        try:
            with self.assertRaises(observation.RequestDeadlineExceeded):
                observation.remaining_timeout(8)
        finally:
            observation.DEADLINE.reset(token)


class LoggingTest(unittest.TestCase):
    def test_json_one_line_and_context_reset(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            token = CONTEXT.set({'request_id': 'one'})
            try:
                logger('test').warning('Bearer secret\nhttps://internal/?key=value')
            finally:
                CONTEXT.reset(token)
            event('test', 'next')
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['request_id'], 'one')
        self.assertNotIn('request_id', rows[1])
        self.assertNotIn('Bearer secret', output.getvalue())


if __name__ == '__main__':
    unittest.main()
