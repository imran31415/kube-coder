"""Request lifecycle and approval boundaries, using real JsonRecord locks on Linux."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from tests.boards_runs_api_test import RM, _Base, server  # also supplies the Windows-only fcntl shim
import approval_auth
from boards.handoff import HandoffError, HandoffManager, TTL, digest, options
from boards.store import real_flock
from handlers import board_handoff


class Requests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = time.time()
        self.target = dict(board_id='billing', board_name='Billing',
                           repository='https://github.com/acme/api', config_digest='cfg', project_digest='project')
        self.runs = {}
        self.executed = []
        self.finished = threading.Event()
        self.authority = mock.Mock(return_value='human')
        self.manager = HandoffManager(self.tmp.name, resolve=lambda data: copy.deepcopy(self.target),
            execute=self.execute, get_run=self.runs.get, publish=mock.Mock(), authority=self.authority,
            clock=lambda: self.now)
        self.body = dict(board_id='billing', repository='https://github.com/acme/api',
                         options=dict(workdir='/home/dev/api', isolate=True, select={'limit': 3}))

    def execute(self, record):
        self.executed.append(record)
        self.runs[record['run_id']] = dict(id=record['run_id'], board_id='billing',
            origin_request_id=record['id'], status='running', items={})
        self.finished.set()

    def prepare(self):
        return self.manager.prepare(copy.deepcopy(self.body), thread_id='thread-1')

    def decide(self, record, decision='approve', decision_id='decision-1234567890'):
        return self.manager.decide(record['id'], dict(decision=decision,
            revision=record['revision'], decision_id=decision_id), human={'Cookie': 'test'})

    def settle(self, record):
        for _ in range(100):
            result = self.manager.get(record['id'])
            if result['state'] != 'starting':
                return result
            self.finished.wait(.01)
            time.sleep(.001)
        self.fail('startup did not settle')

    def test_prepare_has_no_external_effect_and_restores_thread(self):
        record = self.prepare()
        self.assertEqual(record['state'], 'pending')
        self.assertEqual([], self.executed)
        self.authority.assert_not_called()
        self.assertEqual(self.manager.list('thread-1')[0]['id'], record['id'])
        self.assertEqual(self.manager.list('other'), [])

    def test_reject_never_dispatches(self):
        record = self.prepare()
        self.assertEqual(self.decide(record, 'reject')['state'], 'rejected')
        self.assertEqual(self.executed, [])

    def test_approve_persists_run_identity_before_dispatch_and_replays(self):
        record = self.prepare()
        first = self.decide(record)
        self.assertTrue(self.finished.wait(2))
        done = self.settle(record)
        self.assertEqual(done['state'], 'started')
        replay = self.decide(record)
        self.assertEqual(first['run_id'], replay['run_id'])
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(self.executed[0]['state'], 'starting')

    def test_same_decision_id_different_content_conflicts(self):
        record = self.prepare()
        self.decide(record, 'reject')
        with self.assertRaises(HandoffError) as caught:
            self.decide(record, 'approve')
        self.assertEqual(caught.exception.code, 'already_decided')

    def test_expired_request_cannot_approve(self):
        record = self.prepare()
        self.now += TTL + 1
        with self.assertRaises(HandoffError):
            self.decide(record)
        self.assertEqual(self.manager.get(record['id'])['state'], 'expired')
        self.authority.assert_not_called()

    def test_connector_change_requires_new_scope(self):
        record = self.prepare()
        self.target['config_digest'] = 'changed'
        with self.assertRaises(HandoffError) as caught:
            self.decide(record)
        self.assertEqual(caught.exception.code, 'scope_changed')
        self.authority.assert_not_called()

    def test_options_tampering_cannot_reuse_revision(self):
        record = self.prepare()
        def tamper(current):
            current['scope']['options']['select']['limit'] = 99
        self.manager._record(record['id']).update(tamper)
        with self.assertRaises(HandoffError) as caught:
            self.decide(record)
        self.assertEqual(caught.exception.code, 'scope_changed')

    def test_authority_failure_leaves_pending_and_releases_capacity(self):
        record = self.prepare()
        self.authority.side_effect = HandoffError('human_required', 'Sign in', 403)
        with self.assertRaises(HandoffError):
            self.decide(record)
        self.assertEqual(self.manager.get(record['id'])['state'], 'pending')
        self.authority.side_effect = None
        self.decide(record)
        self.assertTrue(self.finished.wait(2))
        self.settle(record)

    def test_disk_failure_never_launches_worker(self):
        record = self.prepare()
        with mock.patch('boards.store.JsonRecord._write_unlocked', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.decide(record)
        self.assertEqual([], self.executed)

    def test_crash_recovery_never_redispatches(self):
        record = self.prepare()
        def crash(current):
            current.update(state='starting', run_id='run-1-dead')
        self.manager._record(record['id']).update(crash)
        self.manager.recover()
        self.assertEqual(self.manager.get(record['id'])['state'], 'interrupted')
        self.assertEqual([], self.executed)

    def test_recovery_recognizes_already_persisted_run(self):
        record = self.prepare()
        self.manager._record(record['id']).update(lambda r: r.update(state='starting', run_id='run-1-dead'))
        self.runs['run-1-dead'] = dict(id='run-1-dead', origin_request_id=record['id'], items={})
        self.manager.recover()
        self.assertEqual(self.manager.get(record['id'])['state'], 'started')
        self.assertEqual([], self.executed)

    def test_corrupt_record_is_not_approved(self):
        record = self.prepare()
        with open(self.manager._record(record['id']).path, 'w') as file:
            file.write('{bad json')
        with self.assertRaises(HandoffError):
            self.decide(record)

    @unittest.skipUnless(real_flock(), 'requires real Linux locks')
    def test_concurrent_clicks_start_at_most_once(self):
        record = self.prepare()
        results = []
        def click():
            try:
                results.append(self.decide(record))
            except HandoffError as exc:
                self.assertIn(exc.status, (409, 429))
        threads = [threading.Thread(target=click) for _ in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(3)
        self.assertTrue(self.finished.wait(2))
        self.settle(record)
        self.assertEqual(len(self.executed), 1)

    def test_strict_option_types_and_scope(self):
        for change in ({'concurrency': True}, {'select': {'limit': True}}, {'mode': 'autonomous'},
                       {'select': {'ignore_processed': True}}, {'isolate': False}, {'unknown': 1}):
            with self.subTest(change=change), self.assertRaises(HandoffError):
                options(dict(self.body['options'], **change))


class AuthPolicy(unittest.TestCase):
    def test_shared_bearer_and_forwarded_identity_do_not_approve(self):
        handler = mock.Mock(headers={'Authorization': 'Bearer workspace-token',
                                    'X-Auth-Request-User': 'admin', 'Origin': 'https://workspace'},
                            _bearer_only=False)
        with self.assertRaises(HandoffError) as caught:
            approval_auth.browser_proof(handler)
        self.assertEqual(caught.exception.status, 403)

    def test_missing_authority_is_fail_closed(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(HandoffError) as caught:
            approval_auth.authority_call('human', {}, {'Cookie': 'forged'})
        self.assertEqual(caught.exception.code, 'approval_not_configured')

    def test_thread_binding_comes_from_issued_context(self):
        token = approval_auth.issue_context('thread-1')
        self.assertEqual(approval_auth.runner_thread({'X-KC-Runner-Context': token}), 'thread-1')
        self.assertIsNone(approval_auth.runner_thread({'X-KC-Runner-Context': 'thread-1'}))

    def test_repository_identity_preserves_host(self):
        self.assertEqual(board_handoff.repository_identity('git@github.com:Acme/API.git'), 'https://github.com/acme/api')
        self.assertNotEqual(board_handoff.repository_identity('https://github.example/acme/api'),
                            board_handoff.repository_identity('acme/api'))

    def test_routes_and_reserved_identifier(self):
        from boards.schema import RESERVED_BOARD_IDS
        self.assertIn('run-requests', RESERVED_BOARD_IDS)
        self.assertIsNotNone(board_handoff.ROUTES.match('POST', '/api/boards/run-requests', ''))


class BrokenBoardsPackage(unittest.TestCase):
    def test_a_broken_boards_module_degrades_instead_of_stopping_the_dashboard(self):
        workspace = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = ("import sys; sys.path.insert(0, %r); sys.modules['boards.handoff'] = None; "
                  "import server; print('available=%%s' %% server._BOARDS_AVAILABLE)" % workspace)
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True,
                                    timeout=60, env=dict(os.environ, KC_WORKSPACE_HOME=home))
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn('available=False', result.stdout)


class ApprovalGateRoutes(_Base):
    """The real dispatch, so the gate is judged on the path the route tables see."""

    def setUp(self):
        super().setUp()
        for board_id in ('acme-jira', 'run-requests-2'):
            self._board(board_id)
        env = mock.patch.dict(os.environ, {'KC_BOARD_HANDOFF_ENABLED': 'true'})
        env.start()
        self.addCleanup(env.stop)
        human = mock.patch.object(approval_auth, 'require_human',
                                  side_effect=HandoffError('human_required', 'Sign in', 403))
        self.human = human.start()
        self.addCleanup(human.stop)

    def assertGated(self, method, path, body=None):
        self.human.reset_mock()
        status, payload = self._call(method, path, body)
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload.get('code'), 'human_required')
        self.human.assert_called_once()

    def _call(self, method, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', data=data, method=method,
                                         headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read() or b'{}')
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b'{}')

    def test_a_query_string_cannot_name_the_exempt_routes(self):
        self.assertGated('GET', '/api/boards/acme-jira/items?x=/run-requests')
        self.assertGated('GET', '/oauth/api/boards/acme-jira/items?x=/run-requests')
        self.assertGated('GET', '/bearer-api/api/boards/acme-jira/items?x=/run-requests')

    def test_a_path_the_dispatcher_normalizes_to_items_is_gated(self):
        self.assertGated('GET', '/api/boards/acme-jira/ite/oauthms')

    def test_a_board_id_starting_with_run_requests_is_gated(self):
        self.assertGated('GET', '/api/boards/run-requests-2/items')
        self.assertGated('POST', '/api/boards/run-requests-2/runs', {'select': {'limit': 1}})
        self.assertGated('POST', '/api/boards/run-requests-2/items/1/actions', {})
        self.assertGated('PUT', '/api/boards/run-requests-2', {'display_name': 'x'})
        self.assertGated('DELETE', '/api/boards/run-requests-2')
        self.assertEqual(RM.list_runs('run-requests-2'), [])

    def test_the_real_run_request_routes_still_work(self):
        request_id = 'brq-' + 'a' * 32
        record = dict(id=request_id, state='pending', thread_id='thread-1', board_id='acme-jira', run_id=None)
        manager = mock.Mock()
        manager.prepare.return_value = manager.get.return_value = manager.decide.return_value = record
        session = mock.Mock()
        session.read_meta.return_value = {}
        session.current_turn.return_value = 4
        runner = {'X-KC-Runner-Context': approval_auth.issue_context('thread-1')}
        with mock.patch.object(board_handoff, 'manager', return_value=manager), \
                mock.patch.object(server.HypervisorSession, 'get', return_value=session):
            status, body = self._call('POST', '/api/boards/run-requests', {'board_id': 'acme-jira'}, runner)
            self.assertEqual((status, body['id']), (201, request_id), body)
            status, body = self._call('GET', f'/oauth/api/boards/run-requests/{request_id}', headers=runner)
            self.assertEqual((status, body['id']), (200, request_id), body)
            status, body = self._call('POST', f'/api/boards/run-requests/{request_id}/decision',
                                      dict(decision='reject'), {'Origin': 'https://workspace'})
            self.assertEqual((status, body['id']), (200, request_id), body)
        self.human.assert_not_called()


if __name__ == '__main__':
    unittest.main()
