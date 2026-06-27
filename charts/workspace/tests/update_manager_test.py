"""Unit tests for server.py's UpdateManager — the broker that forwards a
workspace's own version check / update to the workspace-controller's token-gated
self-serve endpoints. No network: urllib.request.urlopen is patched per-test.

Run with:  python3 -m unittest tests.update_manager_test  (from charts/workspace/)
"""

import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402


class _Resp(io.BytesIO):
    """A urlopen()-like object: context manager + .status + readable body."""
    def __init__(self, payload, status=200):
        super().__init__(json.dumps(payload).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


class UpdateManagerTest(unittest.TestCase):
    def setUp(self):
        server.CONTROLLER_SELF_SERVE_URL = 'http://workspace-controller.coder.svc:8081'
        server.CONTROLLER_SELF_SERVE_TOKEN = 'shh'
        self._du = server.CronManager.detect_user
        server.CronManager.detect_user = staticmethod(lambda: 'octo')

    def tearDown(self):
        server.CronManager.detect_user = self._du

    def test_enabled_requires_url_and_token(self):
        self.assertTrue(server.UpdateManager.enabled())
        server.CONTROLLER_SELF_SERVE_TOKEN = ''
        self.assertFalse(server.UpdateManager.enabled())
        server.CONTROLLER_SELF_SERVE_TOKEN = 'shh'
        server.CONTROLLER_SELF_SERVE_URL = ''
        self.assertFalse(server.UpdateManager.enabled())

    def test_get_version_builds_request_and_parses(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured['url'] = req.full_url
            captured['method'] = req.get_method()
            captured['token'] = req.get_header('X-kc-service-token')
            return _Resp({'user': 'octo', 'version': 'v1.3.0',
                          'latestVersion': 'v1.4.0', 'updateAvailable': True})

        with mock.patch.object(server.urllib.request, 'urlopen', fake_urlopen):
            status, payload = server.UpdateManager.get_version()

        self.assertEqual(status, 200)
        self.assertTrue(payload['updateAvailable'])
        self.assertEqual(
            captured['url'],
            'http://workspace-controller.coder.svc:8081/api/self/workspaces/octo/version')
        self.assertEqual(captured['method'], 'GET')
        self.assertEqual(captured['token'], 'shh')

    def test_do_update_posts_version_body(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured['method'] = req.get_method()
            captured['body'] = req.data
            captured['url'] = req.full_url
            return _Resp({'ok': True, 'toVersion': 'v1.4.0'})

        with mock.patch.object(server.urllib.request, 'urlopen', fake_urlopen):
            status, payload = server.UpdateManager.do_update('v1.4.0')

        self.assertEqual(status, 200)
        self.assertEqual(captured['method'], 'POST')
        self.assertEqual(json.loads(captured['body']), {'version': 'v1.4.0'})
        self.assertTrue(captured['url'].endswith('/api/self/workspaces/octo/update'))

    def test_do_update_without_version_sends_empty_body(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured['body'] = req.data
            return _Resp({'ok': True})

        with mock.patch.object(server.urllib.request, 'urlopen', fake_urlopen):
            server.UpdateManager.do_update()
        self.assertEqual(json.loads(captured['body']), {})

    def test_http_error_returns_status_and_payload(self):
        def fake_urlopen(req, timeout=None):
            body = io.BytesIO(json.dumps({'error': 'no workspace ws-octo'}).encode())
            raise urllib.error.HTTPError(req.full_url, 404, 'Not Found', {}, body)

        with mock.patch.object(server.urllib.request, 'urlopen', fake_urlopen):
            status, payload = server.UpdateManager.do_update()
        self.assertEqual(status, 404)
        self.assertEqual(payload['error'], 'no workspace ws-octo')

    def test_network_error_degrades_to_502(self):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError('connection refused')

        with mock.patch.object(server.urllib.request, 'urlopen', fake_urlopen):
            status, payload = server.UpdateManager.get_version()
        self.assertEqual(status, 502)
        self.assertIn('unreachable', payload['error'])


if __name__ == '__main__':
    unittest.main()
