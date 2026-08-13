"""Board Processor HTTP API — CRUD, test-fetch, draft and the action route.

Boots a real ThreadingHTTPServer so the auth gate, the READONLY chokepoint and
the route dispatch are all exercised, following tests/devcontainer_api_test.py.
Outbound HTTP is stubbed at safe_http.fetch — which is the ONE seam the engine
can reach the network through, so stubbing it proves there is no second path.

Run:  python3 -m unittest tests.boards_api_test   (from charts/workspace/)
"""

import copy
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

# server.py imports fcntl (Unix-only) at module load — shim for non-Unix dev.
try:
    import fcntl  # noqa: F401
except ImportError:  # pragma: no cover - platform shim
    import types
    _shim = types.ModuleType('fcntl')
    _shim.flock = lambda *a, **k: None
    _shim.lockf = lambda *a, **k: None
    _shim.LOCK_EX = _shim.LOCK_UN = _shim.LOCK_SH = _shim.LOCK_NB = 0
    sys.modules['fcntl'] = _shim

import safe_http  # noqa: E402
import server  # noqa: E402
from tests import board_fixtures as fx  # noqa: E402

BM = server.BoardsManager
BCM = server.BoardCredentialsManager


def J(obj, status=200, headers=None):
    return (status, headers or {}, json.dumps(obj).encode('utf-8'))


class _Base(unittest.TestCase):
    READONLY = False
    AUTH_OK = True

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = os.path.realpath(tempfile.mkdtemp(prefix='kc-boards-'))
        cls._saved_home = BM.HOME_ROOT
        BM.HOME_ROOT = cls.tmpdir
        cls._saved_cred_home = BCM.HOME_ROOT
        BCM.HOME_ROOT = cls.tmpdir

        cls._auth_save = server.BrowserHandler.check_claude_auth
        server.BrowserHandler.check_claude_auth = lambda self: cls.AUTH_OK
        cls._ro_save = server.READONLY_MODE
        server.READONLY_MODE = cls.READONLY

        # Every board's base_url is validated at save time; the guard resolves
        # DNS, which a unit test must not do.
        cls._safe_save = safe_http.is_safe_url
        safe_http.is_safe_url = lambda url, **kw: not (
            '169.254.' in url or 'localhost' in url or '127.0.0.1' in url
            or '10.0.' in url)

        # Bind to port 0 and read back what the OS gave us. Picking a "free"
        # port with a throwaway socket and binding it afterwards is a TOCTOU
        # another test in the suite can win, and it surfaces as a confusing
        # connection error inside an unrelated assertion.
        cls.httpd = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0), server.BrowserHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        BM.HOME_ROOT = cls._saved_home
        BCM.HOME_ROOT = cls._saved_cred_home
        server.BrowserHandler.check_claude_auth = cls._auth_save
        server.READONLY_MODE = cls._ro_save
        safe_http.is_safe_url = cls._safe_save
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        # The HTTP server is per-CLASS, so PVC-equivalent state resets per test.
        shutil.rmtree(BM.boards_dir(), ignore_errors=True)
        # The REAL credential store on a temp HOME_ROOT rather than a mock, so
        # these tests also prove `@board-creds/NAME` resolves end to end.
        try:
            os.remove(BCM.creds_file())
        except OSError:
            pass
        BCM.set('JIRA_API_TOKEN', 'secret-token')
        BCM.set('LINEAR_API_KEY', 'lin-key')

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {'Content-Type': 'application/json'} if data else {}
        r = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}',
                                   data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw

    def _jira(self, board_id='acme-jira'):
        cfg = copy.deepcopy(fx.JIRA)
        cfg['id'] = board_id
        return cfg

    def _create(self, cfg=None):
        return self._req('POST', '/api/boards', cfg or self._jira())

    def _stub_fetch(self, responses):
        """Stub the single seam the engine reaches the network through."""
        calls = []

        def fake(url, *, method='GET', headers=None, body=None, timeout=30,
                 allow_internal=False):
            calls.append({'url': url, 'method': method,
                          'headers': dict(headers or {}), 'body': body})
            if not responses:
                raise AssertionError(f'no stubbed response for {method} {url}')
            return responses.pop(0)

        p = mock.patch.object(safe_http, 'fetch', fake)
        p.start()
        self.addCleanup(p.stop)
        return calls


class CrudTests(_Base):
    def test_create_then_get_then_list_then_delete(self):
        status, body = self._create()
        self.assertEqual(status, 201, body)
        self.assertEqual(body['id'], 'acme-jira')

        status, body = self._req('GET', '/api/boards/acme-jira')
        self.assertEqual(status, 200)
        self.assertEqual(body['vendor'], 'jira')
        self.assertEqual(body['actions_allowed'], ['comment', 'set_status'])

        status, body = self._req('GET', '/api/boards')
        self.assertEqual(status, 200)
        self.assertEqual([b['id'] for b in body['boards']], ['acme-jira'])

        status, _body = self._req('DELETE', '/api/boards/acme-jira')
        self.assertEqual(status, 200)
        status, _body = self._req('GET', '/api/boards/acme-jira')
        self.assertEqual(status, 404)

    def test_duplicate_create_conflicts(self):
        self._create()
        status, body = self._create()
        self.assertEqual(status, 409)
        self.assertIn('already exists', body['error'])

    def test_update_replaces_the_connector(self):
        self._create()
        cfg = self._jira()
        cfg['display_name'] = 'Renamed'
        status, body = self._req('PUT', '/api/boards/acme-jira', cfg)
        self.assertEqual(status, 200, body)
        self.assertEqual(body['display_name'], 'Renamed')

    def test_update_of_a_missing_board_is_404(self):
        status, _body = self._req('PUT', '/api/boards/ghost', self._jira('ghost'))
        self.assertEqual(status, 404)

    def test_invalid_id_rejected(self):
        cfg = self._jira()
        cfg['id'] = 'has spaces/and-slashes'
        status, body = self._req('POST', '/api/boards', cfg)
        self.assertEqual(status, 400)
        self.assertIn('id must be', body['error'])

    def test_schema_errors_are_returned_to_the_caller(self):
        cfg = self._jira()
        cfg['map'].pop('id')
        status, body = self._req('POST', '/api/boards', cfg)
        self.assertEqual(status, 400)
        self.assertIn('map.id is required', body['error'])

    def test_invalid_json_body_is_400(self):
        r = urllib.request.Request(
            f'http://127.0.0.1:{self.port}/api/boards', data=b'{not json',
            method='POST', headers={'Content-Type': 'application/json'})
        try:
            urllib.request.urlopen(r, timeout=20)
            self.fail('expected 400')
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_delete_of_a_missing_board_is_404(self):
        status, _body = self._req('DELETE', '/api/boards/ghost')
        self.assertEqual(status, 404)

    def test_stored_connector_never_contains_a_secret(self):
        self._create()
        with open(os.path.join(BM.boards_dir(), 'acme-jira.json')) as f:
            raw = f.read()
        self.assertNotIn('secret-token', raw)
        self.assertIn('@board-creds/JIRA_API_TOKEN', raw)

    def test_credential_status_reports_resolvability_without_the_value(self):
        self._create()
        _status, body = self._req('GET', '/api/boards/acme-jira')
        self.assertTrue(body['credential']['set'])
        self.assertEqual(body['credential']['ref'], '@board-creds/JIRA_API_TOKEN')
        self.assertNotIn('secret-token', json.dumps(body))

    def test_missing_credential_is_refused_at_save_time(self):
        BCM.delete('JIRA_API_TOKEN')
        status, body = self._create()
        self.assertEqual(status, 400)
        self.assertIn('no stored board credential named JIRA_API_TOKEN',
                      body['error'])


class SSRFTests(_Base):
    def test_private_base_url_is_refused_at_save_time(self):
        cfg = self._jira('internal')
        cfg['base_url'] = 'http://10.0.0.5/jira'
        status, body = self._req('POST', '/api/boards', cfg)
        self.assertEqual(status, 400)
        self.assertIn('non-public address', body['error'])

    def test_metadata_service_base_url_is_refused(self):
        cfg = self._jira('meta')
        cfg['base_url'] = 'http://169.254.169.254/latest'
        status, body = self._req('POST', '/api/boards', cfg)
        self.assertEqual(status, 400)
        self.assertIn('non-public address', body['error'])

    def test_ssrf_at_fetch_time_is_reported_not_crashed(self):
        self._create()
        p = mock.patch.object(
            safe_http, 'fetch',
            side_effect=safe_http.SSRFError('resolves to non-public address'))
        p.start()
        self.addCleanup(p.stop)
        status, body = self._req('POST', '/api/boards/acme-jira/test-fetch')
        self.assertEqual(status, 502)
        self.assertIn('refused for safety', body['error'])


class TestFetchTests(_Base):
    def test_returns_normalized_items_beside_an_honest_complete(self):
        self._create()
        calls = self._stub_fetch([
            J({'issues': [{'id': 46, 'key': 'SUP-5',
                           'fields': {'summary': 'Refund not received',
                                      'status': {'name': 'In Review'},
                                      'priority': {'name': 'P2'}}}]}),
        ])
        status, body = self._req('POST', '/api/boards/acme-jira/test-fetch')
        self.assertEqual(status, 200, body)
        self.assertTrue(body['complete'])
        self.assertEqual(body['pages_fetched'], 1)

        item = body['items'][0]
        self.assertEqual(item['id'], '46')
        self.assertEqual(item['key'], 'SUP-5')
        self.assertEqual(item['status'],
                         {'normalized': 'IN_PROGRESS', 'raw': 'In Review'})
        self.assertEqual(item['priority'], {'normalized': 'HIGH', 'raw': 'P2'})
        self.assertEqual(item['raw']['fields']['summary'], 'Refund not received')
        self.assertEqual(item['url'], 'https://acme.atlassian.net/browse/SUP-5')

        self.assertIn('Authorization', calls[0]['headers'])
        self.assertEqual(calls[0]['headers']['Authorization'], 'Basic secret-token')

    def test_reports_incomplete_when_the_board_may_have_been_truncated(self):
        self._create()
        full = [{'id': i, 'key': f'SUP-{i}', 'fields': {}} for i in range(50)]
        self._stub_fetch([J({'issues': full})])       # full page, no token
        _status, body = self._req('POST', '/api/boards/acme-jira/test-fetch')
        self.assertFalse(body['complete'])
        self.assertEqual(body['truncation_reason'],
                         'full_page_no_pagination_metadata')

    def test_is_capped_to_a_few_pages(self):
        self._create()
        self._stub_fetch([
            J({'issues': [{'id': i, 'key': f'S{i}', 'fields': {}}
                          for i in range(50)], 'nextPageToken': f't{i}'})
            for i in range(5)
        ])
        _status, body = self._req('POST', '/api/boards/acme-jira/test-fetch',
                                  {'max_pages': 99})
        self.assertEqual(body['pages_fetched'], BM.TEST_FETCH_MAX_PAGES)
        self.assertFalse(body['complete'])
        self.assertEqual(body['truncation_reason'], 'max_pages')

    def test_vendor_error_is_surfaced_as_502(self):
        self._create()
        self._stub_fetch([(401, {}, b'{"message":"Bad credentials"}')])
        status, body = self._req('POST', '/api/boards/acme-jira/test-fetch')
        self.assertEqual(status, 200)   # engine reported it, transport was fine
        self.assertFalse(body['complete'])
        self.assertEqual(body['truncation_reason'], 'http_401')

    def test_missing_board_is_404(self):
        status, _body = self._req('POST', '/api/boards/ghost/test-fetch')
        self.assertEqual(status, 404)


class DraftTests(_Base):
    def test_invalid_draft_returns_every_error(self):
        cfg = copy.deepcopy(fx.LINEAR)
        cfg.pop('display_name')
        cfg['map'].pop('id')
        status, body = self._req('POST', '/api/boards/draft',
                                 {'connector': cfg, 'probe': False})
        self.assertEqual(status, 200)
        self.assertFalse(body['valid'])
        self.assertGreaterEqual(len(body['errors']), 2)

    def test_valid_draft_without_probe_does_no_network(self):
        calls = self._stub_fetch([])
        status, body = self._req('POST', '/api/boards/draft',
                                 {'connector': copy.deepcopy(fx.LINEAR),
                                  'probe': False})
        self.assertEqual(status, 200)
        self.assertTrue(body['valid'])
        self.assertEqual(calls, [])

    def test_probe_runs_the_real_engine_and_persists_nothing(self):
        self._stub_fetch([
            J({'data': {'issues': {
                'nodes': [{'id': 'lin-1', 'identifier': 'ENG-1',
                           'title': 'Crash', 'state': {'name': 'Todo'}}],
                'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}),
        ])
        status, body = self._req('POST', '/api/boards/draft',
                                 {'connector': copy.deepcopy(fx.LINEAR)})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['valid'])
        self.assertTrue(body['probed'])
        self.assertTrue(body['complete'])
        self.assertEqual(body['items'][0]['status'],
                         {'normalized': 'OPEN', 'raw': 'Todo'})
        # Nothing was written to the PVC.
        status, listing = self._req('GET', '/api/boards')
        self.assertEqual(listing['boards'], [])


class ActionTests(_Base):
    def test_declared_action_runs_and_reports_vendor_success(self):
        self._create()
        self._stub_fetch([
            # the handler re-fetches the item rather than trusting the caller
            J({'issues': [{'id': 46, 'key': 'SUP-5', 'fields': {}}]}),
            J({'transitions': [{'id': '31', 'to': {'name': 'Done'}}]}),
            J({}, status=204),
        ])
        status, body = self._req(
            'POST', '/api/boards/acme-jira/items/46/actions',
            {'action': 'set_status', 'params': {'status': 'Done'}})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['ok'])

    def test_undeclared_action_is_refused_by_the_allowlist(self):
        self._create()
        self._stub_fetch([J({'issues': [{'id': 46, 'key': 'SUP-5', 'fields': {}}]})])
        status, body = self._req(
            'POST', '/api/boards/acme-jira/items/46/actions',
            {'action': 'delete_project', 'params': {}})
        self.assertEqual(status, 400)
        self.assertIn('not declared by this connector', body['error'])

    def test_caller_supplied_item_ref_cannot_redirect_the_write(self):
        """An agent must not be able to name item 46 and hand us a ref pointing
        at a different ticket."""
        self._create()
        calls = self._stub_fetch([
            J({'issues': [{'id': 46, 'key': 'SUP-5', 'fields': {}}]}),
            J({'comments': []}),
            J({'id': 1}),
        ])
        status, body = self._req(
            'POST', '/api/boards/acme-jira/items/46/actions',
            {'action': 'comment', 'params': {'body': 'hi'},
             'item': {'id': '46', 'ref': {'issue_key': 'VICTIM-1'}}})
        self.assertEqual(status, 200, body)
        self.assertTrue(all('VICTIM-1' not in c['url'] for c in calls),
                        [c['url'] for c in calls])
        self.assertTrue(any('SUP-5' in c['url'] for c in calls))

    def test_unknown_item_is_404(self):
        self._create()
        self._stub_fetch([J({'issues': [{'id': 46, 'key': 'SUP-5', 'fields': {}}]})])
        status, body = self._req(
            'POST', '/api/boards/acme-jira/items/999/actions',
            {'action': 'comment', 'params': {'body': 'x'}})
        self.assertEqual(status, 404)
        self.assertIn('not found on this board', body['error'])

    def test_missing_required_param_is_400(self):
        self._create()
        self._stub_fetch([J({'issues': [{'id': 46, 'key': 'SUP-5', 'fields': {}}]})])
        status, body = self._req(
            'POST', '/api/boards/acme-jira/items/46/actions',
            {'action': 'comment', 'params': {}})
        self.assertEqual(status, 400)
        self.assertIn('missing required parameter', body['error'])

    def _budgeted_board(self, per_item=1):
        cfg = self._jira()
        cfg['limits'] = {'per_item_writes': per_item,
                         'per_item_writes_window_seconds': 600,
                         'global': {'max_events': 99, 'window_seconds': 60}}
        self._req('POST', '/api/boards', cfg)
        return cfg

    def test_per_item_write_budget_returns_429(self):
        self._budgeted_board(per_item=1)
        cfg = self._jira()
        cfg['limits'] = {'per_item_writes': 1,
                         'global': {'max_events': 99, 'window_seconds': 60}}
        cfg['actions']['comment']['writes'] = 5
        self._req('PUT', '/api/boards/acme-jira', cfg)
        self._stub_fetch([J({'issues': [{'id': 46, 'key': 'SUP-5', 'fields': {}}]})])
        status, body = self._req(
            'POST', '/api/boards/acme-jira/items/46/actions',
            {'action': 'comment', 'params': {'body': 'x'}})
        self.assertEqual(status, 429)
        self.assertIn('rate limited', body['error'])

    def test_the_write_budget_spans_REQUESTS_not_just_one_limiter(self):
        """The budget is durable, so a second request cannot spend it again.

        Held only in the limiter object, this cap would reset on every request
        — a board declaring "one write per ticket" would allow one per HTTP
        call, which is no cap at all. The same persistence is what makes a pod
        restart mid-run safe.
        """
        self._budgeted_board(per_item=1)
        self._stub_fetch([
            J({'issues': [{'id': 46, 'key': 'SUP-5', 'fields': {}}]}),
            J({'comments': []}),                 # idempotency probe: nothing yet
            J({'id': '1'}, status=201),          # the comment lands
            J({'issues': [{'id': 46, 'key': 'SUP-5', 'fields': {}}]}),
        ])
        first, _b = self._req('POST', '/api/boards/acme-jira/items/46/actions',
                              {'action': 'comment', 'params': {'body': 'x'}})
        self.assertEqual(first, 200)

        second, body = self._req('POST',
                                 '/api/boards/acme-jira/items/46/actions',
                                 {'action': 'comment', 'params': {'body': 'y'}})
        self.assertEqual(second, 429, body)
        self.assertIn('per-item', body['error'])

    def test_a_spent_budget_on_one_item_leaves_another_item_free(self):
        self._budgeted_board(per_item=1)
        issues = [{'id': 46, 'key': 'SUP-5', 'fields': {}},
                  {'id': 47, 'key': 'SUP-6', 'fields': {}}]
        self._stub_fetch([
            J({'issues': issues}), J({'comments': []}), J({'id': '1'}, status=201),
            J({'issues': issues}), J({'comments': []}), J({'id': '2'}, status=201),
        ])
        for item_id in ('46', '47'):
            status, body = self._req(
                'POST', f'/api/boards/acme-jira/items/{item_id}/actions',
                {'action': 'comment', 'params': {'body': 'x'}})
            self.assertEqual(status, 200, body)

    def test_the_persisted_budget_is_not_mistaken_for_a_board(self):
        """It lives under .claude-boards/writes/, and list_boards() enumerates
        every *.json directly in .claude-boards/ as a connector."""
        self._budgeted_board(per_item=1)
        self._stub_fetch([
            J({'issues': [{'id': 46, 'key': 'SUP-5', 'fields': {}}]}),
            J({'comments': []}), J({'id': '1'}, status=201),
        ])
        self._req('POST', '/api/boards/acme-jira/items/46/actions',
                  {'action': 'comment', 'params': {'body': 'x'}})
        _s, body = self._req('GET', '/api/boards')
        self.assertEqual([b['id'] for b in body['boards']], ['acme-jira'])

    def test_vendor_failure_is_502_with_ok_false(self):
        self._create()
        self._stub_fetch([
            J({'issues': [{'id': 46, 'key': 'SUP-5', 'fields': {}}]}),
            J({'transitions': [{'id': '31', 'to': {'name': 'Done'}}]}),
            (403, {}, b'{"errorMessages":["no permission"]}'),
        ])
        status, body = self._req(
            'POST', '/api/boards/acme-jira/items/46/actions',
            {'action': 'set_status', 'params': {'status': 'Done'}})
        self.assertEqual(status, 502)
        self.assertFalse(body['ok'])


class AuthTests(_Base):
    AUTH_OK = False

    def test_every_board_route_requires_auth(self):
        for method, path, body in (
            ('GET', '/api/boards', None),
            ('GET', '/api/boards/acme-jira', None),
            ('GET', '/api/boards/acme-jira/items', None),
            ('GET', '/api/boards/credentials', None),
            ('PUT', '/api/boards/credentials/JIRA_API_TOKEN', {'secret': 's'}),
            ('DELETE', '/api/boards/credentials/JIRA_API_TOKEN', None),
            ('POST', '/api/boards', {'id': 'x'}),
            ('POST', '/api/boards/draft', {'connector': {}}),
            ('POST', '/api/boards/acme-jira/test-fetch', None),
            ('POST', '/api/boards/acme-jira/items/1/actions', {'action': 'c'}),
            ('PUT', '/api/boards/acme-jira', {'id': 'x'}),
            ('DELETE', '/api/boards/acme-jira', None),
        ):
            with self.subTest(route=f'{method} {path}'):
                status, _body = self._req(method, path, body)
                self.assertEqual(status, 401)


class ReadonlyTests(_Base):
    READONLY = True

    def test_reads_still_work(self):
        status, _body = self._req('GET', '/api/boards')
        self.assertEqual(status, 200)

    def test_every_mutation_is_403(self):
        for method, path, body in (
            ('POST', '/api/boards', {'id': 'x'}),
            ('POST', '/api/boards/draft', {'connector': {}}),
            ('POST', '/api/boards/acme-jira/test-fetch', None),
            ('POST', '/api/boards/acme-jira/items/1/actions', {'action': 'c'}),
            ('PUT', '/api/boards/acme-jira', {'id': 'x'}),
            ('PUT', '/api/boards/credentials/JIRA_API_TOKEN', {'secret': 's'}),
            ('DELETE', '/api/boards/acme-jira', None),
            ('DELETE', '/api/boards/credentials/JIRA_API_TOKEN', None),
        ):
            with self.subTest(route=f'{method} {path}'):
                status, body = self._req(method, path, body)
                self.assertEqual(status, 403)
                self.assertEqual(body.get('code'), 'readonly')


if __name__ == '__main__':
    unittest.main()
