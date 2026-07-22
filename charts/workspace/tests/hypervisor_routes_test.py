"""Route-handler tests for the Hypervisor soft-delete/revive endpoints
(issue #260): DELETE /api/hypervisor/threads/{id} (soft-delete),
POST /api/hypervisor/threads/{id}/restore, and the ?deleted=1 trash filter
on GET /api/hypervisor/threads.

Exercises server.py's handler methods directly against a real
HypervisorSession backed by a temp HYPERVISOR_DIR (same style as
skills_sync_endpoint_test.py's SyncHandlerTestBase: mock.Mock(spec=...) for
auth/response capture, real domain objects underneath).

Readonly-mode enforcement is a single chokepoint (`_readonly_block()`) called
at the very top of do_POST (server.py) and do_DELETE (server.py) BEFORE any
route — including these two — is dispatched to. See
HypervisorReadonlyGateTest, which mirrors skills_sync_endpoint_test.py's
ReadonlyChokepointTest for the same reason: the handler methods themselves
never consult READONLY_MODE, only the router does.

Run:  python3 -m unittest tests.hypervisor_routes_test
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

# server.py imports fcntl (Unix-only) at module load. Provide a no-op shim so
# this pure-logic handler test also runs on non-Unix dev machines; on
# Linux/CI the real fcntl is already importable and this branch is skipped.
try:
    import fcntl  # noqa: F401
except ImportError:  # pragma: no cover - platform shim
    import types
    _shim = types.ModuleType('fcntl')
    _shim.flock = lambda *a, **k: None
    _shim.LOCK_EX = _shim.LOCK_UN = _shim.LOCK_SH = _shim.LOCK_NB = 0
    sys.modules['fcntl'] = _shim

import hypervisor_session as hs  # noqa: E402
import server  # noqa: E402


class HypervisorRouteTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_dir = hs.HYPERVISOR_DIR
        hs.HYPERVISOR_DIR = self.tmp

    def tearDown(self):
        hs.HYPERVISOR_DIR = self._orig_dir

    def _mk(self, title='x'):
        return hs.HypervisorSession.create(
            assistant='claude', workdir='/home/dev', cli_cmd='claude',
            preamble='', title=title)

    def _handler(self, authed=True, path=''):
        h = mock.Mock(spec=server.BrowserHandler)
        h.check_claude_auth.return_value = authed
        h.path = path
        self.responses = []
        h.send_json.side_effect = lambda obj, status=200: self.responses.append((obj, status))
        # The real lookup — delete/restore route through this to 404 on a
        # missing id, so let the actual implementation run instead of the
        # auto-mocked stub (which would return a bare Mock, not None/a session).
        h._hv_session_or_404.side_effect = (
            lambda tid: server.BrowserHandler._hv_session_or_404(h, tid))
        return h

    def last(self):
        self.assertTrue(self.responses, 'handler sent no response')
        return self.responses[-1]  # (obj, status)


class DeleteHandlerTest(HypervisorRouteTestBase):
    def test_soft_deletes_and_returns_ok(self):
        s = self._mk('keep-me')
        h = self._handler()
        server.BrowserHandler.handle_hypervisor_delete_thread(h, s.id)
        obj, status = self.last()
        self.assertEqual(status, 200)
        self.assertEqual(obj, {'ok': True})
        # Soft delete: files survive, deleted_at is stamped.
        self.assertTrue(os.path.isfile(s.meta_path))
        self.assertIsNotNone(s.read_meta().get('deleted_at'))

    def test_unauthorized_is_401_and_leaves_thread_untouched(self):
        s = self._mk()
        h = self._handler(authed=False)
        server.BrowserHandler.handle_hypervisor_delete_thread(h, s.id)
        self.assertEqual(self.last()[1], 401)
        self.assertIsNone(s.read_meta().get('deleted_at'))

    def test_missing_thread_is_404(self):
        h = self._handler()
        server.BrowserHandler.handle_hypervisor_delete_thread(h, 'no-such-id')
        self.assertEqual(self.last()[1], 404)


class RestoreHandlerTest(HypervisorRouteTestBase):
    def test_revives_a_deleted_thread(self):
        s = self._mk('oops')
        s.delete()
        h = self._handler()
        server.BrowserHandler.handle_hypervisor_restore_thread(h, s.id)
        obj, status = self.last()
        self.assertEqual(status, 200)
        self.assertEqual(obj, {'ok': True, 'restored': True})
        self.assertIsNone(s.read_meta().get('deleted_at'))

    def test_restoring_a_live_thread_is_a_reported_noop(self):
        s = self._mk()
        h = self._handler()
        server.BrowserHandler.handle_hypervisor_restore_thread(h, s.id)
        self.assertEqual(self.last(), ({'ok': True, 'restored': False}, 200))

    def test_unauthorized_is_401_and_leaves_tombstone_untouched(self):
        s = self._mk()
        s.delete()
        h = self._handler(authed=False)
        server.BrowserHandler.handle_hypervisor_restore_thread(h, s.id)
        self.assertEqual(self.last()[1], 401)
        self.assertIsNotNone(s.read_meta().get('deleted_at'))

    def test_missing_thread_is_404(self):
        h = self._handler()
        server.BrowserHandler.handle_hypervisor_restore_thread(h, 'no-such-id')
        self.assertEqual(self.last()[1], 404)


class ListDeletedFilterTest(HypervisorRouteTestBase):
    def test_default_list_excludes_tombstones(self):
        live = self._mk('live')
        trashed = self._mk('trashed')
        trashed.delete()
        h = self._handler(path='/api/hypervisor/threads')
        server.BrowserHandler.handle_hypervisor_list_threads(h)
        obj, status = self.last()
        self.assertEqual(status, 200)
        self.assertEqual([t['id'] for t in obj['threads']], [live.id])

    def test_deleted_query_param_returns_only_tombstones(self):
        live = self._mk('live')
        trashed = self._mk('trashed')
        trashed.delete()
        h = self._handler(path='/api/hypervisor/threads?deleted=1')
        server.BrowserHandler.handle_hypervisor_list_threads(h)
        obj, _ = self.last()
        ids = [t['id'] for t in obj['threads']]
        self.assertEqual(ids, [trashed.id])
        self.assertNotIn(live.id, ids)


class SetModelHandlerTest(HypervisorRouteTestBase):
    """POST /api/hypervisor/threads/{id}/model — the in-chat model switcher
    (#308). The handler validates against the thread's own assistant."""

    def _post(self, h, tid, body):
        h.read_json_body.return_value = body
        server.BrowserHandler.handle_hypervisor_set_model(h, tid)

    def test_sets_a_listed_model(self):
        s = self._mk()
        h = self._handler()
        self._post(h, s.id, {'model': 'opus'})
        obj, status = self.last()
        self.assertEqual(status, 200)
        self.assertEqual(obj['thread']['model'], 'opus')
        self.assertEqual(s.read_meta()['adapter']['model'], 'opus')

    def test_off_list_model_falls_back_to_default(self):
        s = self._mk()
        h = self._handler()
        self._post(h, s.id, {'model': 'totally-made-up'})
        obj, _ = self.last()
        # resolve_model defends the boundary → the assistant's default.
        self.assertEqual(obj['thread']['model'], 'default')

    def test_unauthorized_is_401(self):
        s = self._mk()
        h = self._handler(authed=False)
        self._post(h, s.id, {'model': 'opus'})
        self.assertEqual(self.last()[1], 401)
        self.assertEqual(s.read_meta()['adapter']['model'], '')

    def test_missing_thread_is_404(self):
        h = self._handler()
        self._post(h, 'no-such-id', {'model': 'opus'})
        self.assertEqual(self.last()[1], 404)


class WatcherRoutesTest(HypervisorRouteTestBase):
    """The cross-turn watcher endpoints (issue #402): POST/GET
    /api/hypervisor/threads/{id}/watchers and DELETE .../watchers/{wid}. Thin
    wrappers over hypervisor_session.WATCHERS — these tests pin the HTTP
    contract (status codes, validation mapping) against the real manager and
    a real thread in a temp HYPERVISOR_DIR."""

    def _post(self, h, tid, body):
        h.read_json_body.return_value = body
        server.BrowserHandler.handle_hypervisor_create_watcher(h, tid)

    def test_create_returns_201_and_persists(self):
        s = self._mk()
        h = self._handler()
        self._post(h, s.id, {'kind': 'task', 'target': 'task-1',
                             'note': 'the build', 'interval': 30})
        obj, status = self.last()
        self.assertEqual(status, 201)
        w = obj['watcher']
        self.assertEqual((w['kind'], w['target'], w['state']),
                         ('task', 'task-1', 'armed'))
        self.assertEqual(hs.WATCHERS.list(s.id)[0]['id'], w['id'])

    def test_create_maps_validation_to_400(self):
        s = self._mk()
        h = self._handler()
        self._post(h, s.id, {'kind': 'webhook', 'target': 'x'})
        obj, status = self.last()
        self.assertEqual(status, 400)
        self.assertIn('kind', obj['error'])

    def test_create_missing_thread_is_404_and_unauth_is_401(self):
        h = self._handler()
        self._post(h, 'no-such-id', {'kind': 'task', 'target': 't'})
        self.assertEqual(self.last()[1], 404)
        s = self._mk()
        h = self._handler(authed=False)
        self._post(h, s.id, {'kind': 'task', 'target': 't'})
        self.assertEqual(self.last()[1], 401)
        self.assertEqual(hs.WATCHERS.list(s.id), [])

    def test_list_returns_watchers(self):
        s = self._mk()
        w = hs.WATCHERS.arm(s.id, kind='command', target='true')
        h = self._handler()
        server.BrowserHandler.handle_hypervisor_list_watchers(h, s.id)
        obj, status = self.last()
        self.assertEqual(status, 200)
        self.assertEqual([x['id'] for x in obj['watchers']], [w['id']])

    def test_cancel_is_reported_and_idempotent(self):
        s = self._mk()
        w = hs.WATCHERS.arm(s.id, kind='file', target='/tmp/x')
        h = self._handler()
        server.BrowserHandler.handle_hypervisor_cancel_watcher(h, s.id, w['id'])
        self.assertEqual(self.last(), ({'ok': True, 'cancelled': True}, 200))
        self.assertEqual(hs.WATCHERS.list(s.id)[0]['state'], 'cancelled')
        server.BrowserHandler.handle_hypervisor_cancel_watcher(h, s.id, w['id'])
        self.assertEqual(self.last(), ({'ok': True, 'cancelled': False}, 200))

    def test_delete_thread_cancels_its_watchers(self):
        s = self._mk()
        hs.WATCHERS.arm(s.id, kind='task', target='task-9')
        h = self._handler()
        server.BrowserHandler.handle_hypervisor_delete_thread(h, s.id)
        self.assertEqual(self.last()[1], 200)
        self.assertEqual(hs.WATCHERS.list(s.id)[0]['state'], 'cancelled')


class HypervisorReadonlyGateTest(unittest.TestCase):
    """DELETE /api/hypervisor/threads/{id} and POST .../restore are both
    registered in do_DELETE / do_POST AFTER the shared `_readonly_block()`
    check (server.py) — so a single gate covers both mutating routes. The
    handler methods themselves never look at READONLY_MODE; this is by
    design (same convention as the skills sync route)."""

    def test_readonly_block_active(self):
        orig = server.READONLY_MODE
        try:
            server.READONLY_MODE = True
            h = mock.Mock(spec=server.BrowserHandler)
            blocked = server.BrowserHandler._readonly_block(h)
            self.assertTrue(blocked)
            h.send_json.assert_called_once()
            self.assertEqual(h.send_json.call_args[0][1], 403)
        finally:
            server.READONLY_MODE = orig

    def test_readonly_block_inactive_by_default(self):
        orig = server.READONLY_MODE
        try:
            server.READONLY_MODE = False
            h = mock.Mock(spec=server.BrowserHandler)
            self.assertFalse(server.BrowserHandler._readonly_block(h))
            h.send_json.assert_not_called()
        finally:
            server.READONLY_MODE = orig


if __name__ == '__main__':
    unittest.main()
