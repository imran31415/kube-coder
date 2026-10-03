"""Security scan HTTP API — gates, validation and the promise not to block.

Boots a real ThreadingHTTPServer so the auth gate, the READONLY chokepoint and
route dispatch are all exercised together, following tests/boards_api_test.py.
The backend is a stub, so nothing here spawns a process or reaches a network,
and HOME + KC_SCANS_DIR point at temporary directories so no test touches the
workspace owner's real scanner settings or scan history.

The test this file exists for is `test_create_answers_without_waiting`: a scan
runs for minutes to hours, and an endpoint that waited on one would hold a
request handler thread until it finished.

Run:  python3 -m unittest tests.scans_api_test   (from charts/workspace/)
"""

import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import scan_backends  # noqa: E402
import scans  # noqa: E402
import server  # noqa: E402
import strix_connection  # noqa: E402

SCAN_ID_SHAPE = 'scn_abc123def456'


class StubBackend(scan_backends.ScanBackend):
    """No process, no files, no network — driven by the test."""

    name = 'stub'
    ready = {'ok': True, 'reason': '', 'detail': ''}
    start_delay = 0.0
    state = None
    alive = True

    def preflight(self):
        return type(self).ready

    def resolve_target(self, port):
        return f'http://gateway.test:{port}'

    def start(self, spec):
        if type(self).start_delay:
            time.sleep(type(self).start_delay)
        return {'backend': self.name, 'job': 'j1',
                'scan_dir': spec['scan_dir']}

    def artifacts(self, handle):
        return type(self).state

    def is_alive(self, handle):
        return type(self).alive

    def stop(self, handle):
        type(self).alive = False

    def tail_log(self, handle, limit=4000):
        return ''


class ApiTestCase(unittest.TestCase):
    """One server per class, with every live store redirected."""

    AUTH_OK = True
    READONLY = False
    ENABLED = True

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._env_save = {k: os.environ.get(k)
                         for k in ('KC_STRIX_HOME', 'KC_SCANS_DIR')}
        os.environ['KC_STRIX_HOME'] = cls._tmp.name
        os.environ['KC_SCANS_DIR'] = os.path.join(cls._tmp.name, 'scans')

        cls._auth_save = server.BrowserHandler.check_claude_auth
        server.BrowserHandler.check_claude_auth = \
            lambda self, allow_none_mode=True: cls.AUTH_OK
        cls._ro_save = server.READONLY_MODE
        server.READONLY_MODE = cls.READONLY
        cls._enabled_save = server.SCANS_ENABLED
        server.SCANS_ENABLED = cls.ENABLED

        cls.httpd = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0), server.BrowserHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.BrowserHandler.check_claude_auth = cls._auth_save
        server.READONLY_MODE = cls._ro_save
        server.SCANS_ENABLED = cls._enabled_save
        for key, value in cls._env_save.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls._tmp.cleanup()

    def setUp(self):
        StubBackend.ready = {'ok': True, 'reason': '', 'detail': ''}
        StubBackend.start_delay = 0.0
        StubBackend.state = None
        StubBackend.alive = True
        # Each test starts from an empty workspace: nothing connected and no
        # scan history. Otherwise a test that connects leaves the next one
        # connected — so the not-connected gate can never be exercised — and
        # a listing assertion sees whatever earlier tests left behind.
        strix_connection.clear_connection()
        shutil.rmtree(scans.ScansManager.scans_dir(), ignore_errors=True)
        # Saving a connection kicks off the first-use install. That is a real
        # `pip install` of a large dependency tree, so the suite replaces it —
        # a unit test must never depend on a package index being reachable.
        self.addCleanup(setattr, strix_connection, 'ensure_installed',
                        strix_connection.ensure_installed)
        strix_connection.ensure_installed = lambda runner=None: {
            'state': 'ready', 'version': 'test', 'error': ''}
        # Pollers are not wanted here: these tests assert on one HTTP reply,
        # not on a scan progressing.
        original = scans.ScansManager._spawn_poller
        scans.ScansManager._spawn_poller = classmethod(lambda cls, _id: None)
        self.addCleanup(setattr, scans.ScansManager, '_spawn_poller', original)
        scans.ScansManager.configure(
            backend_factory=StubBackend,
            publish=lambda *a, **kw: None,
            emit_feed=lambda *a, **kw: None,
            targets_provider=lambda: [
                {'port': 3000, 'name': 'shop', 'addr': '0.0.0.0',
                 'reachable': True, 'reason': ''}],
        )

    # -- requests ----------------------------------------------------------

    def request(self, path, method='GET', body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f'http://127.0.0.1:{self.port}{path}', data=data, method=method)
        if data is not None:
            req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, self._decode(r.read())
        except urllib.error.HTTPError as e:
            return e.code, self._decode(e.read())

    @staticmethod
    def _decode(raw):
        try:
            return json.loads(raw.decode())
        except (ValueError, UnicodeDecodeError):
            return raw

    def connect(self, model='prov/model', key='sk-test-key-abcdefghij'):
        status, body = self.request('/api/scans/connection', 'POST',
                                    {'model': model, 'api_key': key})
        self.assertEqual(status, 200, body)
        return body

    def start_scan(self, **body):
        body.setdefault('port', 3000)
        body.setdefault('mode', 'quick')
        return self.request('/api/scans', 'POST', body)


class GateTests(ApiTestCase):

    def test_an_unknown_scan_is_a_404_not_a_500(self):
        status, _ = self.request(f'/api/scans/{SCAN_ID_SHAPE}')
        self.assertEqual(status, 404)

    def test_a_malformed_id_does_not_reach_a_handler_at_all(self):
        status, _ = self.request('/api/scans/../../etc/passwd')
        self.assertIn(status, (400, 404))

    def test_the_targets_list_is_served(self):
        status, body = self.request('/api/scans/targets')
        self.assertEqual(status, 200)
        self.assertEqual(body['targets'][0]['port'], 3000)

    def test_an_empty_workspace_lists_no_scans(self):
        status, body = self.request('/api/scans')
        self.assertEqual(status, 200)
        self.assertEqual(body['scans'], [])

    def test_a_malformed_body_is_a_400(self):
        req = urllib.request.Request(
            f'http://127.0.0.1:{self.port}/api/scans', data=b'{not json',
            method='POST')
        req.add_header('Content-Type', 'application/json')
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail('expected an error')
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)


class UnauthenticatedTests(ApiTestCase):

    AUTH_OK = False

    def test_every_route_refuses_an_unauthenticated_caller(self):
        for method, path in (('GET', '/api/scans'),
                             ('GET', '/api/scans/targets'),
                             ('GET', '/api/scans/connection'),
                             ('GET', f'/api/scans/{SCAN_ID_SHAPE}'),
                             ('POST', '/api/scans'),
                             ('POST', '/api/scans/connection'),
                             ('POST', '/api/scans/connection/test'),
                             ('POST', f'/api/scans/{SCAN_ID_SHAPE}/stop'),
                             ('DELETE', '/api/scans/connection'),
                             ('DELETE', f'/api/scans/{SCAN_ID_SHAPE}')):
            status, _ = self.request(path, method,
                                     {} if method == 'POST' else None)
            self.assertEqual(status, 401, f'{method} {path}')


class DisabledTests(ApiTestCase):

    ENABLED = False

    def test_a_workspace_without_scanning_says_so_rather_than_erroring(self):
        status, body = self.request('/api/scans')
        self.assertEqual(status, 503)
        self.assertEqual(body['code'], 'disabled')
        self.assertIn('switched off', body['error'])

    def test_starting_a_scan_is_refused_too(self):
        status, body = self.start_scan()
        self.assertEqual(status, 503)
        self.assertEqual(body['code'], 'disabled')


class ReadOnlyTests(ApiTestCase):

    READONLY = True

    def test_reads_still_work_on_a_read_only_workspace(self):
        self.assertEqual(self.request('/api/scans')[0], 200)

    def test_no_scan_can_be_started_or_stopped(self):
        """A scan sends real attack traffic and spends real money; the
        read-only demo must not be able to do either."""
        self.assertEqual(self.start_scan()[0], 403)
        self.assertEqual(
            self.request(f'/api/scans/{SCAN_ID_SHAPE}/stop', 'POST', {})[0],
            403)
        self.assertEqual(
            self.request(f'/api/scans/{SCAN_ID_SHAPE}', 'DELETE')[0], 403)

    def test_the_saved_model_settings_cannot_be_changed(self):
        status, _ = self.request('/api/scans/connection', 'POST',
                                 {'model': 'prov/model'})
        self.assertEqual(status, 403)


class ConnectionTests(ApiTestCase):

    def test_an_unconnected_workspace_reports_it_plainly(self):
        status, body = self.request('/api/scans/connection')
        self.assertEqual(status, 200)
        self.assertFalse(body['configured'])
        self.assertIn('install', body)
        self.assertIn('backend', body)

    def test_saving_then_reading_never_returns_the_key(self):
        self.connect(key='sk-super-secret-value')
        status, body = self.request('/api/scans/connection')
        self.assertEqual(status, 200)
        self.assertNotIn('sk-super-secret-value', json.dumps(body))
        self.assertTrue(body['has_key'])
        self.assertEqual(body['model'], 'prov/model')

    def test_the_model_can_be_changed_without_resending_the_key(self):
        self.connect()
        status, body = self.request('/api/scans/connection', 'POST',
                                    {'model': 'other/model'})
        self.assertEqual(status, 200)
        self.assertEqual(body['model'], 'other/model')
        self.assertTrue(body['has_key'])

    def test_an_invalid_model_is_refused(self):
        status, body = self.request('/api/scans/connection', 'POST',
                                    {'model': 'bad\nmodel'})
        self.assertEqual(status, 400)
        self.assertIn('not valid', body['error'])

    def test_clearing_forgets_the_connection(self):
        self.connect()
        status, body = self.request('/api/scans/connection', 'DELETE')
        self.assertEqual(status, 200)
        self.assertFalse(body['configured'])
        self.assertFalse(body['has_key'])


class CreateTests(ApiTestCase):

    def test_a_scan_cannot_start_before_a_model_is_connected(self):
        status, body = self.start_scan()
        self.assertEqual(status, 409)
        self.assertEqual(body['code'], 'not_connected')

    def test_a_connected_workspace_starts_a_scan_and_gets_an_id(self):
        self.connect()
        status, body = self.start_scan()
        self.assertEqual(status, 202, body)
        self.assertTrue(scans.valid_scan_id(body['scan_id']))
        self.assertEqual(body['status'], 'running')
        self.assertEqual(body['target']['url'], 'http://gateway.test:3000')

    def test_create_answers_without_waiting_for_the_scan(self):
        """The promise this endpoint makes. A scan runs for minutes to hours;
        holding the request open would pin a handler thread for all of it."""
        self.connect()
        StubBackend.start_delay = 0.0
        began = time.time()
        status, _ = self.start_scan()
        elapsed = time.time() - began
        self.assertEqual(status, 202)
        self.assertLess(elapsed, 1.0)

    def test_a_port_with_nothing_on_it_is_refused(self):
        self.connect()
        status, body = self.start_scan(port=9999)
        self.assertEqual(status, 400)
        self.assertIn('9999', body['error'])

    def test_an_unknown_scan_depth_is_refused(self):
        self.connect()
        status, body = self.start_scan(mode='exhaustive')
        self.assertEqual(status, 400)
        self.assertIn('quick', body['error'])

    def test_a_nonsense_budget_is_refused_before_anything_is_spent(self):
        self.connect()
        status, body = self.start_scan(budget_usd='lots')
        self.assertEqual(status, 400)
        self.assertIn('Budget', body['error'])

    def test_an_unavailable_backend_explains_itself(self):
        self.connect()
        StubBackend.ready = {'ok': False, 'reason': 'no_container_runtime',
                             'detail': 'Needs build.mode buildkit.'}
        status, body = self.start_scan()
        self.assertEqual(status, 502)
        self.assertIn('buildkit', body['error'])

    def test_a_started_scan_is_then_readable_and_listed(self):
        self.connect()
        _, created = self.start_scan()
        scan_id = created['scan_id']

        status, detail = self.request(f'/api/scans/{scan_id}')
        self.assertEqual(status, 200)
        self.assertEqual(detail['status'], 'running')
        self.assertIn('summary', detail)
        # The backend's private bookkeeping never leaves the server.
        self.assertNotIn('handle', detail)

        status, listing = self.request('/api/scans')
        self.assertEqual(status, 200)
        self.assertEqual([r['id'] for r in listing['scans']], [scan_id])
        self.assertNotIn('findings', listing['scans'][0])


class LifecycleTests(ApiTestCase):

    def running_scan(self):
        self.connect()
        _, created = self.start_scan()
        return created['scan_id']

    def test_stopping_settles_the_scan(self):
        scan_id = self.running_scan()
        status, body = self.request(f'/api/scans/{scan_id}/stop', 'POST', {})
        self.assertEqual(status, 200)
        self.assertEqual(body['status'], 'stopped')

    def test_stopping_an_unknown_scan_is_a_404(self):
        status, _ = self.request(f'/api/scans/{SCAN_ID_SHAPE}/stop', 'POST', {})
        self.assertEqual(status, 404)

    def test_a_finding_can_be_dismissed_and_reopened(self):
        scan_id = self.running_scan()
        StubBackend.state = {'run': {'status': 'running'},
                             'findings': [{'id': 'v1', 'severity': 'high'}]}
        scans.ScansManager._absorb(scan_id, StubBackend.state, alive=True,
                                   backend=StubBackend(), handle={})

        status, body = self.request(
            f'/api/scans/{scan_id}/findings/v1/disposition', 'POST',
            {'disposition': 'dismissed'})
        self.assertEqual(status, 200)
        self.assertEqual(body['dispositions'], {'v1': 'dismissed'})

        status, body = self.request(
            f'/api/scans/{scan_id}/findings/v1/disposition', 'POST',
            {'disposition': 'open'})
        self.assertEqual(status, 200)
        self.assertEqual(body['dispositions'], {})

    def test_dismissing_a_finding_that_is_not_there_is_refused(self):
        scan_id = self.running_scan()
        status, _ = self.request(
            f'/api/scans/{scan_id}/findings/nope/disposition', 'POST',
            {'disposition': 'dismissed'})
        self.assertEqual(status, 400)

    def test_deleting_removes_the_scan(self):
        scan_id = self.running_scan()
        status, body = self.request(f'/api/scans/{scan_id}', 'DELETE')
        self.assertEqual(status, 200)
        self.assertTrue(body['removed'])
        self.assertEqual(self.request(f'/api/scans/{scan_id}')[0], 404)


class ModeProbeTests(ApiTestCase):

    def test_the_capability_flag_is_published_to_the_clients(self):
        """The SPA hides the whole surface on this, so it has to be present
        on a workspace that does not have scanning as well as one that does."""
        status, body = self.request('/api/mode')
        self.assertEqual(status, 200)
        self.assertIn('scansEnabled', body)
        self.assertTrue(body['scansEnabled'])


if __name__ == '__main__':
    unittest.main()
