"""What a board is DOING, and who gets told (#712).

Three surfaces of the same complaint — "the Board page is empty", "it doesn't
remember which board I was on", "I can't tell what state the board is in", "the
notification takes me to the build, not the approval":

1. `boards.state.standing` — the vocabulary itself, as a pure function.
2. `/api/boards/<id>/standing` — the endpoint the phone polls, which is the
   only board route that reads nothing but local state.
3. `FeedManager.emit_task_terminal` — a board worker's build now links at the
   APPROVAL it left behind, ahead of the build, so a tap lands on the decision.

The run-already-in-flight refusal is asserted where the runs live:
`boards_e2e_test.LeaseTests.test_a_second_run_while_one_is_live_is_refused`.

Run:  python3 -m unittest tests.boards_state_test   (from charts/workspace/)
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

try:
    import fcntl  # noqa: F401
except ImportError:  # pragma: no cover - platform shim
    import types
    _shim = types.ModuleType('fcntl')
    _shim.flock = lambda *a, **k: None
    _shim.lockf = lambda *a, **k: None
    _shim.LOCK_EX = _shim.LOCK_UN = _shim.LOCK_SH = _shim.LOCK_NB = 0
    _shim._kube_coder_shim = True
    sys.modules['fcntl'] = _shim

import server  # noqa: E402
from boards import runs, state  # noqa: E402
from tests import board_fixtures as fx  # noqa: E402

sys.path.insert(0, HERE)
from live_state import isolate_feed_and_push  # noqa: E402

BM = server.BoardsManager
BCM = server.BoardCredentialsManager
RM = server.BoardRunsManager
VM = server.BoardReviewManager
FM = server.FeedManager


def summary(status='running', counts=None, **over):
    """A run summary in the shape `list_runs` returns."""
    tally = {s: 0 for s in runs.ITEM_STATES}
    tally.update(counts or {})
    out = {'id': 'run-1700000000-abcd', 'board_id': 'b1', 'mode': 'propose',
           'status': status, 'counts': tally,
           'total': sum(tally.values()),
           'done': tally['done'], 'failed': tally['failed'],
           'skipped': tally['skipped']}
    out.update(over)
    return out


class StandingVocabularyTests(unittest.TestCase):
    """The pure function. One state wins; the detail line carries the rest."""

    def test_a_board_with_no_credential_says_so_before_anything_else(self):
        # Nothing else is actionable: no run of any kind could authenticate.
        s = state.standing(board={'credential_set': False},
                           runs=[summary()], awaiting=3)
        self.assertEqual(s['state'], 'needs_credential')
        self.assertFalse(s['can_start_run'])
        self.assertIn('authenticate', s['blocked_reason'])

    def test_a_live_run_wins_over_items_awaiting_a_decision(self):
        s = state.standing(
            board={'credential_set': True},
            runs=[summary(counts={'working': 2, 'pending': 3, 'done': 4})],
            awaiting=2)
        self.assertEqual(s['state'], 'running')
        self.assertEqual(s['label'], 'Runs in progress')
        self.assertTrue(s['live'])
        self.assertEqual((s['working'], s['queued'], s['settled']), (2, 3, 4))
        # The thing the badge could not say is in the line under it.
        self.assertIn('2 working', s['detail'])
        self.assertIn('2 awaiting you', s['detail'])

    def test_claimed_counts_as_working_not_as_nothing(self):
        # `claimed` is the moment between the lease and the build starting. A
        # board with three of them is not idle.
        s = state.standing(board={'credential_set': True},
                           runs=[summary(counts={'claimed': 3})], awaiting=0)
        self.assertEqual(s['working'], 3)

    def test_items_awaiting_a_decision_when_nothing_is_running(self):
        s = state.standing(board={'credential_set': True},
                           runs=[summary(status='done',
                                         counts={'done': 4})],
                           awaiting=1,
                           awaiting_breakdown={'needs_review': 1})
        self.assertEqual(s['state'], 'awaiting_human')
        self.assertEqual(s['label'], 'Waiting on you')
        self.assertIn('1 item is waiting', s['detail'])
        self.assertIn('needs review', s['detail'])
        self.assertTrue(s['can_start_run'])

    def test_a_board_nobody_has_run_is_distinct_from_an_idle_one(self):
        never = state.standing(board={'credential_set': True}, runs=[])
        self.assertEqual(never['state'], 'never_run')
        self.assertIn('Nothing has been worked', never['detail'])

        idle = state.standing(board={'credential_set': True},
                              runs=[summary(status='done', counts={'done': 2})])
        self.assertEqual(idle['state'], 'idle')
        self.assertIn('has been decided', idle['detail'])

    def test_a_run_whose_items_are_all_terminal_does_not_block_the_next_one(self):
        """The failure this guards: a run record left at `running` after its
        last item settled — or after the process died under it — would
        otherwise read as "in flight" forever and lock the board."""
        stale = summary(status='running', counts={'done': 3})
        self.assertFalse(runs.is_live(stale))
        s = state.standing(board={'credential_set': True}, runs=[stale])
        self.assertNotEqual(s['state'], 'running')
        self.assertTrue(s['can_start_run'])

    def test_a_live_run_names_itself_in_the_refusal(self):
        s = state.standing(board={'credential_set': True},
                           runs=[summary(counts={'working': 1})])
        self.assertFalse(s['can_start_run'])
        self.assertIn('run-1700000000-abcd', s['blocked_reason'])

    def test_every_state_has_a_label(self):
        self.assertEqual(sorted(state.STATES), sorted(state.LABELS))


class _RouteBase(unittest.TestCase):
    """A real handler on a real port, against a temp PVC."""

    AUTH_OK = True

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = os.path.realpath(tempfile.mkdtemp(prefix='kc-standing-'))
        cls._saved_home, BM.HOME_ROOT = BM.HOME_ROOT, cls.tmpdir
        cls._saved_cred, BCM.HOME_ROOT = BCM.HOME_ROOT, cls.tmpdir
        cls._auth_save = server.BrowserHandler.check_claude_auth
        server.BrowserHandler.check_claude_auth = lambda self: cls.AUTH_OK
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
        BM.HOME_ROOT = cls._saved_home
        BCM.HOME_ROOT = cls._saved_cred
        server.BrowserHandler.check_claude_auth = cls._auth_save
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        shutil.rmtree(BM.boards_dir(), ignore_errors=True)
        try:
            os.remove(BCM.creds_file())
        except OSError:
            pass
        BCM.set('JIRA_API_TOKEN', 'secret-token')

    def _get(self, path):
        r = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}')
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

    def _board(self, board_id='acme-jira'):
        cfg = copy.deepcopy(fx.JIRA)
        cfg['id'] = board_id
        saved, err = BM.create_or_update(cfg)
        self.assertIsNone(err, err)
        return saved


class StandingRouteTests(_RouteBase):
    """The endpoint the phone polls."""

    def test_a_board_nobody_has_run_reports_never_run(self):
        self._board()
        status, body = self._get('/api/boards/acme-jira/standing')
        self.assertEqual(status, 200, body)
        self.assertEqual(body['state'], 'never_run')
        self.assertEqual(body['board_id'], 'acme-jira')
        self.assertTrue(body['display_name'])
        self.assertTrue(body['can_start_run'])

    def test_it_costs_no_vendor_call(self):
        """The whole point: the phone polls this on the same 15-second timer it
        polls the review queue, and a board's rate limit is account-wide."""
        self._board()
        with mock.patch('safe_http.fetch',
                        side_effect=AssertionError('reached the vendor')):
            status, body = self._get('/api/boards/acme-jira/standing')
        self.assertEqual(status, 200, body)

    def test_an_unknown_board_is_404(self):
        status, _b = self._get('/api/boards/ghost/standing')
        self.assertEqual(status, 404)

    def test_a_staged_item_puts_the_board_in_waiting_on_you(self):
        cfg = self._board()
        VM._book('acme-jira').put({
            'board_id': 'acme-jira', 'item_id': '46', 'item_key': 'SUP-46',
            'item_title': 'Refund not received', 'item_url': '',
            'content_hash': 'h', 'state': 'pending',
            'disposition': 'needs_review', 'reason': 'needs a human',
            'evidence': {}, 'actions': [], 'decided_by': '',
            'created_at': 0, 'updated_at': 0,
        })
        status, body = self._get(f'/api/boards/{cfg["id"]}/standing')
        self.assertEqual(status, 200, body)
        self.assertEqual(body['state'], 'awaiting_human')
        self.assertEqual(body['awaiting'], 1)
        self.assertIn('needs review', body['detail'])


class StandingAuthTests(_RouteBase):
    AUTH_OK = False

    def test_standing_requires_auth(self):
        self._board()
        status, _b = self._get('/api/boards/acme-jira/standing')
        self.assertEqual(status, 401)

    def test_an_unknown_board_is_401_not_404(self):
        # An unauthenticated caller learns nothing about which boards exist.
        status, _b = self._get('/api/boards/ghost/standing')
        self.assertEqual(status, 401)


class BoardNotificationLinkTests(unittest.TestCase):
    """Where a board worker's notification lands (#712).

    The build's feed row linked at the build, so tapping it opened a
    transcript — when what that build actually produced was a decision waiting
    to be made. `push_notify` sends the FIRST link's ref as the notification
    target, so the ordering here is the fix.
    """

    def setUp(self):
        isolate_feed_and_push(self)
        self.tmpdir = os.path.realpath(tempfile.mkdtemp(prefix='kc-bfeed-'))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        saved = BM.HOME_ROOT
        BM.HOME_ROOT = self.tmpdir
        self.addCleanup(setattr, BM, 'HOME_ROOT', saved)

    def _stage(self, item_id='46', staged_state='pending'):
        VM._book('acme-jira').put({
            'board_id': 'acme-jira', 'item_id': item_id, 'item_key': 'SUP-46',
            'item_title': 'Refund not received', 'item_url': '',
            'content_hash': 'h', 'state': staged_state,
            'disposition': 'needs_review', 'reason': 'needs a human',
            'evidence': {}, 'actions': [], 'decided_by': '',
            'created_at': 0, 'updated_at': 0,
        })

    def _meta(self, **over):
        meta = {'task_id': 't1', 'prompt': 'work SUP-46',
                'board_id': 'acme-jira', 'board_item_id': '46'}
        meta.update(over)
        return meta

    def test_a_board_build_links_at_the_approval_first(self):
        self._stage()
        with mock.patch.object(FM, '_project_for_meta', return_value=''):
            item = FM.emit_task_terminal(self._meta(), 'completed')
        refs = [lk['ref'] for lk in item['links']]
        self.assertEqual(refs, ['board:acme-jira:46', 'task:t1'])
        self.assertIn('SUP-46', item['links'][0]['label'])

    def test_the_build_stays_reachable_as_the_second_chip(self):
        # "Unless that's all that's available" cuts both ways: the transcript
        # is still how you find out WHY the agent proposed what it proposed.
        self._stage()
        with mock.patch.object(FM, '_project_for_meta', return_value=''):
            item = FM.emit_task_terminal(self._meta(), 'completed')
        self.assertEqual(item['links'][1], {'label': 'Open task',
                                            'ref': 'task:t1'})

    def test_a_decided_item_leaves_the_build_as_the_only_link(self):
        self._stage(staged_state='approved')
        with mock.patch.object(FM, '_project_for_meta', return_value=''):
            item = FM.emit_task_terminal(self._meta(), 'completed')
        self.assertEqual([lk['ref'] for lk in item['links']], ['task:t1'])

    def test_a_build_that_staged_nothing_links_at_the_build(self):
        # Autonomous mode, or a clean completion: there is no approval to open.
        with mock.patch.object(FM, '_project_for_meta', return_value=''):
            item = FM.emit_task_terminal(self._meta(), 'completed')
        self.assertEqual([lk['ref'] for lk in item['links']], ['task:t1'])

    def test_an_ordinary_build_is_untouched(self):
        with mock.patch.object(FM, '_project_for_meta', return_value=''):
            item = FM.emit_task_terminal({'task_id': 't9', 'prompt': 'hi'},
                                         'completed')
        self.assertEqual([lk['ref'] for lk in item['links']], ['task:t9'])

    def test_a_build_paused_mid_work_is_NOT_counted_as_an_approval(self):
        """`waiting=True` rows drive the dashboard's waiting badge, which reads
        a `board:` link as an item needing a decision. A build that has paused
        has not staged anything yet, so counting it would double-count the row
        the agent emits when it does."""
        self._stage()
        with mock.patch.object(FM, '_project_for_meta', return_value=''):
            item = FM.emit_task_waiting(self._meta(task_id='t2'))
        self.assertEqual([lk['ref'] for lk in item['links']], ['task:t2'])


if __name__ == '__main__':
    unittest.main()
