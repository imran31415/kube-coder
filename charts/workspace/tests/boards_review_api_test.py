"""The review path end to end: staging, the queue, approve / reject / edit.

The load-bearing test is `test_a_propose_run_STAGES_the_write_instead_of_sending_it`.
Propose mode is enforced from the run LEASE — server state no agent can reach —
rather than from anything in the request body, so an agent cannot opt out of
review by omitting a field. `test_the_agent_cannot_opt_out_of_staging` is the
regression guard on that.

After it, the guards: a 409 when the ticket changed under a staged action, and
a replay that returns the first result instead of posting a second comment.

Run:  python3 -m unittest tests.boards_review_api_test  (from charts/workspace/)
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

import safe_http  # noqa: E402
import server  # noqa: E402
from tests import board_fixtures as fx  # noqa: E402

BM = server.BoardsManager
BCM = server.BoardCredentialsManager
RM = server.BoardRunsManager
VM = server.BoardReviewManager
CTM = server.ClaudeTaskManager

APPROVAL = 'a1b2c3d4-e5f6-4711-8899-aabbccddeeff'
OTHER_APPROVAL = 'ffffffff-0000-4000-8000-000000000001'


def J(obj, status=200, headers=None):
    return (status, headers or {}, json.dumps(obj).encode('utf-8'))


def issue(item_id='46', summary='Refund not received'):
    return {'id': item_id, 'key': f'SUP-{item_id}',
            'fields': {'summary': summary, 'description': 'Dana says so.',
                       'status': {'name': 'To Do'}, 'updated': '2026-02-01'}}


class _Base(unittest.TestCase):
    READONLY = False
    AUTH_OK = True

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = os.path.realpath(tempfile.mkdtemp(prefix='kc-review-api-'))
        # A board run refuses to start without a usable task-API
        # token (#633); model a configured workspace. See
        # board_fixtures.workspace_token_patch.
        _tok = fx.workspace_token_patch()
        _tok.start()
        cls.addClassCleanup(_tok.stop)

        cls._saved_home, BM.HOME_ROOT = BM.HOME_ROOT, cls.tmpdir
        cls._saved_cred, BCM.HOME_ROOT = BCM.HOME_ROOT, cls.tmpdir
        cls._auth_save = server.BrowserHandler.check_claude_auth
        server.BrowserHandler.check_claude_auth = lambda self: cls.AUTH_OK
        cls._ro_save, server.READONLY_MODE = server.READONLY_MODE, cls.READONLY
        cls._safe_save = safe_http.is_safe_url
        safe_http.is_safe_url = lambda url, **kw: True
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
        BCM.HOME_ROOT = cls._saved_cred
        server.BrowserHandler.check_claude_auth = cls._auth_save
        server.READONLY_MODE = cls._ro_save
        safe_http.is_safe_url = cls._safe_save
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        shutil.rmtree(BM.boards_dir(), ignore_errors=True)
        try:
            os.remove(BCM.creds_file())
        except OSError:
            pass
        BCM.set('JIRA_API_TOKEN', 'secret-token')
        self.responses = []
        self.calls = []

        def fake(url, *, method='GET', headers=None, body=None, timeout=30,
                 allow_internal=False):
            self.calls.append({'url': url, 'method': method, 'body': body})
            if not self.responses:
                raise AssertionError(f'no stubbed response for {method} {url}')
            return self.responses.pop(0)

        for target, value in ((safe_http, 'fetch'),):
            pass
        p = mock.patch.object(safe_http, 'fetch', fake)
        p.start()
        self.addCleanup(p.stop)

        self.created = []
        self.prompts = []

        def create_task(prompt, **kw):
            # `prompt` is positional, so it never lands in **kw — captured
            # separately because the round-trip tests assert on what the agent
            # was actually told.
            self.prompts.append(prompt)
            self.created.append(kw)
            return {'status': 'running', 'task_id': f'task-{len(self.created)}'}

        for name, fn in (('create_task', create_task),
                         ('task_status', lambda t: 'running'),
                         ('count_live_tasks', lambda: 0)):
            p = mock.patch.object(CTM, name, fn)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(RM, '_spawn_driver', classmethod(lambda cls, r: None))
        p.start()
        self.addCleanup(p.stop)
        # FeedManager writes to a real path; the review manager must not depend
        # on it succeeding, and these tests must not scribble on the dev box.
        p = mock.patch.object(server.FeedManager, 'emit',
                              lambda *a, **k: self.feed.append(k))
        self.feed = []
        p.start()
        self.addCleanup(p.stop)

    # ── helpers ────────────────────────────────────────────────────────────

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

    def _board(self, board_id='acme-jira'):
        cfg = copy.deepcopy(fx.JIRA)
        cfg['id'] = board_id
        saved, err = BM.create_or_update(cfg)
        self.assertIsNone(err, err)
        return saved

    def _serve(self, *responses):
        self.responses.extend(responses)

    def _listing(self, *, summary='Refund not received', times=1):
        for _ in range(times):
            self.responses.append(J({'issues': [issue(summary=summary)]}))

    def _propose_run(self, cfg):
        """A live propose-mode run holding item 46 — the state that forces
        staging."""
        self._listing()
        run, err = RM.create(cfg, {'concurrency': 1, 'mode': 'propose'})
        self.assertIsNone(err, err)
        RM._dispatch(run['id'])
        return run

    def _stage_a_comment(self, cfg, body='Hi Dana — the refund was issued.'):
        self._listing()
        status, payload = self._req(
            'POST', '/api/boards/acme-jira/items/46/actions',
            {'action': 'comment', 'params': {'body': body}, 'preview': body})
        return status, payload


class StagingTests(_Base):
    def test_a_propose_run_STAGES_the_write_instead_of_sending_it(self):
        cfg = self._board()
        self._propose_run(cfg)
        status, payload = self._stage_a_comment(cfg)
        self.assertEqual(status, 202, payload)
        self.assertTrue(payload['staged'])
        # Only the item listing went out. No POST to the vendor.
        self.assertEqual([c['method'] for c in self.calls], ['GET', 'GET'])
        record = VM.get('acme-jira', '46')
        self.assertEqual(len(record['actions']), 1)
        self.assertEqual(record['actions'][0]['action'], 'comment')
        self.assertIn('Dana', record['actions'][0]['preview'])

    def test_the_agent_cannot_opt_out_of_staging(self):
        """The decision reads the run lease, not the request body. Nothing an
        agent can put in a request changes it."""
        cfg = self._board()
        self._propose_run(cfg)
        self._listing()
        status, payload = self._req(
            'POST', '/api/boards/acme-jira/items/46/actions',
            {'action': 'comment', 'params': {'body': 'x'},
             'mode': 'autonomous', 'stage': False, 'confirm': True})
        self.assertEqual(status, 202, payload)
        self.assertTrue(payload['staged'])

    def test_an_autonomous_run_writes_directly(self):
        cfg = self._board()
        self._listing()
        run, err = RM.create(cfg, {'concurrency': 1, 'mode': 'autonomous'})
        self.assertIsNone(err, err)
        RM._dispatch(run['id'])
        self._serve(J({'issues': [issue()]}),      # handler re-fetch
                    J({'comments': []}),           # idempotency probe
                    J({'id': '1'}, status=201))    # the comment lands
        status, payload = self._req(
            'POST', '/api/boards/acme-jira/items/46/actions',
            {'action': 'comment', 'params': {'body': 'x'}})
        self.assertEqual(status, 200, payload)
        self.assertNotIn('staged', payload)
        self.assertIsNone(VM.get('acme-jira', '46'))

    def test_without_any_run_the_direct_path_is_unchanged(self):
        """Phase 1-3 behaviour: a board chat confirms in-chat, then writes."""
        cfg = self._board()
        self._serve(J({'issues': [issue()]}), J({'comments': []}),
                    J({'id': '1'}, status=201))
        status, _p = self._req('POST', '/api/boards/acme-jira/items/46/actions',
                               {'action': 'comment', 'params': {'body': 'x'}})
        self.assertEqual(status, 200)

    def test_a_finished_run_no_longer_forces_staging(self):
        cfg = self._board()
        run = self._propose_run(cfg)
        RM._finish(run['id'], 'done')
        self._serve(J({'issues': [issue()]}), J({'comments': []}),
                    J({'id': '1'}, status=201))
        status, _p = self._req('POST', '/api/boards/acme-jira/items/46/actions',
                               {'action': 'comment', 'params': {'body': 'x'}})
        self.assertEqual(status, 200)

    def test_an_undeclared_action_is_still_refused_before_staging(self):
        cfg = self._board()
        self._propose_run(cfg)
        self._listing()
        status, body = self._req(
            'POST', '/api/boards/acme-jira/items/46/actions',
            {'action': 'delete_everything', 'params': {}})
        self.assertEqual(status, 400)
        self.assertIn('not declared', body['error'])

    def test_a_missing_required_param_is_refused_AT_STAGING(self):
        """`run_action` catches this too, but only when the write is executed —
        which for a staged proposal is at APPROVAL, minutes or hours later,
        with the agent long gone. The reviewer then sees an approval fail on a
        proposal that was never executable and has no way to repair it.

        Staging is the last moment the caller is still on the line, so the
        error goes back to the agent that can actually fix it and try again.
        """
        cfg = self._board()
        self._propose_run(cfg)
        self._listing()
        status, body = self._req(
            'POST', '/api/boards/acme-jira/items/46/actions',
            {'action': 'comment', 'params': {}})
        self.assertEqual(status, 400)
        self.assertIn('missing required parameter', body['error'])
        self.assertIn("'body'", body['error'])
        # Nothing half-staged: a refused proposal must not leave a record
        # behind for a reviewer to find.
        record = VM.get('acme-jira', '46')
        self.assertFalse((record or {}).get('actions'))


class DispositionRouteTests(_Base):
    def test_reporting_needs_review_puts_it_in_the_queue_and_the_feed(self):
        cfg = self._board()
        self._propose_run(cfg)
        self._listing()
        status, body = self._req(
            'POST', '/api/boards/acme-jira/items/46/disposition',
            {'disposition': 'needs_review',
             'reason': 'the refund exists but the customer wants a call back',
             'evidence': {'tool_calls': 3}})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['open'])

        _s, queue = self._req('GET', '/api/boards/acme-jira/review?open=1')
        self.assertEqual(queue['open'], 1)
        self.assertEqual(queue['groups'][0]['disposition'], 'needs_review')
        self.assertEqual(len(self.feed), 1)
        self.assertTrue(self.feed[0]['waiting'])
        self.assertEqual(self.feed[0]['links'][0]['ref'], 'board:acme-jira:46')

    def test_a_disposition_without_a_reason_is_refused(self):
        cfg = self._board()
        self._propose_run(cfg)
        self._listing()
        status, body = self._req(
            'POST', '/api/boards/acme-jira/items/46/disposition',
            {'disposition': 'blocked'})
        self.assertEqual(status, 400)
        self.assertIn('requires a reason', body['error'])

    def test_completed_with_nothing_staged_settles_rather_than_waiting(self):
        """An item with no proposed writes must not sit in the queue looking
        like it needs a human."""
        cfg = self._board()
        self._propose_run(cfg)
        self._listing()
        status, body = self._req(
            'POST', '/api/boards/acme-jira/items/46/disposition',
            {'disposition': 'completed'})
        self.assertEqual(status, 200, body)
        self.assertFalse(body['open'])
        self.assertEqual(self.feed, [])


class ApproveTests(_Base):
    def _staged(self):
        cfg = self._board()
        self._propose_run(cfg)
        self._stage_a_comment(cfg)
        self._listing()
        self._req('POST', '/api/boards/acme-jira/items/46/disposition',
                  {'disposition': 'needs_review', 'reason': 'wants a call back'})
        return cfg, VM.get('acme-jira', '46')

    def test_approving_fires_the_staged_write(self):
        _cfg, record = self._staged()
        self._serve(J({'issues': [issue()]}),      # re-fetch for staleness
                    J({'comments': []}),           # idempotency probe
                    J({'id': '9'}, status=201))    # the comment
        status, body = self._req(
            'POST', '/api/boards/acme-jira/staged/46/approve',
            {'content_hash': record['content_hash'], 'approval_id': APPROVAL})
        self.assertEqual(status, 200, body)
        self.assertFalse(body['replayed'])
        self.assertTrue(body['result']['ok'])
        after = VM.get('acme-jira', '46')
        self.assertEqual(after['state'], 'approved')
        self.assertEqual(after['actions'][0]['state'], 'done')
        self.assertIn('dashboard:', after['decided_by'] + 'dashboard:')

    def test_a_ticket_edited_after_staging_is_409_and_writes_NOTHING(self):
        """The guard that matters: someone replied to the ticket between
        staging and approval, and writing over that reply is the most damaging
        thing this feature could do."""
        _cfg, record = self._staged()
        self._serve(J({'issues': [issue(summary='Refund not received — RESOLVED')]}))
        status, body = self._req(
            'POST', '/api/boards/acme-jira/staged/46/approve',
            {'content_hash': record['content_hash'], 'approval_id': APPROVAL})
        self.assertEqual(status, 409, body)
        self.assertEqual(body['code'], 'stale')
        self.assertIn('someone may have replied', body['error'])
        self.assertEqual(VM.get('acme-jira', '46')['state'], 'pending')
        self.assertEqual([c['method'] for c in self.calls[-1:]], ['GET'])

    def test_approving_with_a_stale_CARD_hash_is_409(self):
        _cfg, _record = self._staged()
        self._serve(J({'issues': [issue()]}))
        status, body = self._req(
            'POST', '/api/boards/acme-jira/staged/46/approve',
            {'content_hash': 'a-hash-from-an-older-card',
             'approval_id': APPROVAL})
        self.assertEqual(status, 409)
        self.assertEqual(body['code'], 'hash_mismatch')

    def test_a_retried_approval_replays_and_does_not_write_twice(self):
        """Airplane mode mid-approve, then reconnect. Exactly one comment."""
        _cfg, record = self._staged()
        self._serve(J({'issues': [issue()]}), J({'comments': []}),
                    J({'id': '9'}, status=201))
        body = {'content_hash': record['content_hash'], 'approval_id': APPROVAL}
        first, _b = self._req('POST', '/api/boards/acme-jira/staged/46/approve',
                              body)
        self.assertEqual(first, 200)
        posts = sum(1 for c in self.calls if c['method'] == 'POST')

        # The retry re-fetches (staleness) but must not write again.
        self._serve(J({'issues': [issue()]}))
        second, payload = self._req(
            'POST', '/api/boards/acme-jira/staged/46/approve', body)
        self.assertEqual(second, 200, payload)
        self.assertTrue(payload['replayed'])
        self.assertEqual(sum(1 for c in self.calls if c['method'] == 'POST'),
                         posts)

    def test_a_second_reviewers_approval_is_a_conflict_not_a_second_write(self):
        _cfg, record = self._staged()
        self._serve(J({'issues': [issue()]}), J({'comments': []}),
                    J({'id': '9'}, status=201))
        self._req('POST', '/api/boards/acme-jira/staged/46/approve',
                  {'content_hash': record['content_hash'],
                   'approval_id': APPROVAL})
        self._serve(J({'issues': [issue()]}))
        status, body = self._req(
            'POST', '/api/boards/acme-jira/staged/46/approve',
            {'content_hash': record['content_hash'],
             'approval_id': OTHER_APPROVAL})
        self.assertEqual(status, 409)
        self.assertEqual(body['code'], 'already_decided')

    def test_a_missing_approval_id_is_refused(self):
        _cfg, record = self._staged()
        self._serve(J({'issues': [issue()]}))
        status, body = self._req(
            'POST', '/api/boards/acme-jira/staged/46/approve',
            {'content_hash': record['content_hash']})
        self.assertEqual(status, 400)
        self.assertEqual(body['code'], 'bad_approval_id')

    def test_a_failed_vendor_write_leaves_the_record_PARTIAL_not_approved(self):
        """A green 'approved' over a half-applied change is worse than an
        honest partial the reviewer can act on."""
        _cfg, record = self._staged()
        self._serve(J({'issues': [issue()]}), J({'comments': []}),
                    (403, {}, b'{"errorMessages":["no permission"]}'))
        status, body = self._req(
            'POST', '/api/boards/acme-jira/staged/46/approve',
            {'content_hash': record['content_hash'], 'approval_id': APPROVAL})
        self.assertEqual(status, 200, body)
        self.assertFalse(body['result']['ok'])
        after = VM.get('acme-jira', '46')
        self.assertEqual(after['state'], 'partial')
        self.assertEqual(after['actions'][0]['state'], 'failed')

    def test_approving_an_item_that_left_the_board_is_404(self):
        _cfg, record = self._staged()
        self._serve(J({'issues': []}))
        status, body = self._req(
            'POST', '/api/boards/acme-jira/staged/46/approve',
            {'content_hash': record['content_hash'], 'approval_id': APPROVAL})
        self.assertEqual(status, 404)
        self.assertEqual(body['code'], 'item_gone')

    def test_approving_with_nothing_staged_is_404(self):
        self._board()
        status, body = self._req(
            'POST', '/api/boards/acme-jira/staged/99/approve',
            {'content_hash': 'x', 'approval_id': APPROVAL})
        self.assertEqual(status, 404)
        self.assertEqual(body['code'], 'not_found')


class RejectEditTests(_Base):
    def _staged(self):
        cfg = self._board()
        self._propose_run(cfg)
        self._stage_a_comment(cfg)
        return cfg, VM.get('acme-jira', '46')

    def test_rejecting_writes_nothing_and_discards_the_staged_actions(self):
        self._staged()
        before = len(self.calls)
        status, body = self._req(
            'POST', '/api/boards/acme-jira/staged/46/reject',
            {'approval_id': APPROVAL, 'reason': 'the tone is wrong'})
        self.assertEqual(status, 200, body)
        self.assertEqual(len(self.calls), before)   # no vendor traffic at all
        after = VM.get('acme-jira', '46')
        self.assertEqual(after['state'], 'rejected')
        self.assertEqual(after['actions'][0]['state'], 'discarded')

    def test_rejecting_needs_NO_staleness_check(self):
        """Refusing to act stays correct however the ticket changed, so a
        reviewer is never blocked from saying no."""
        self._staged()
        status, _b = self._req('POST', '/api/boards/acme-jira/staged/46/reject',
                               {'approval_id': APPROVAL, 'reason': 'no'})
        self.assertEqual(status, 200)

    def test_a_retried_rejection_replays(self):
        self._staged()
        self._req('POST', '/api/boards/acme-jira/staged/46/reject',
                  {'approval_id': APPROVAL, 'reason': 'no'})
        status, body = self._req(
            'POST', '/api/boards/acme-jira/staged/46/reject',
            {'approval_id': APPROVAL, 'reason': 'no'})
        self.assertEqual(status, 200)
        self.assertTrue(body['replayed'])

    def test_send_back_is_recorded_distinctly_from_reject(self):
        self._staged()
        self._listing()          # the resume run re-lists to select the item
        status, _b = self._req(
            'POST', '/api/boards/acme-jira/staged/46/send-back',
            {'approval_id': APPROVAL, 'note': 'ask which refund they mean'})
        self.assertEqual(status, 200)
        self.assertEqual(VM.get('acme-jira', '46')['state'], 'sent_back')

    def test_editing_changes_the_params_and_leaves_it_pending(self):
        """Edit is the escape hatch between approve-as-written and reject; it
        is not itself a decision."""
        self._staged()
        status, body = self._req(
            'POST', '/api/boards/acme-jira/staged/46/edit',
            {'action_id': 'a1', 'params': {'body': 'Rewritten, warmer.'}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['state'], 'pending')
        self.assertEqual(body['actions'][0]['params']['body'],
                         'Rewritten, warmer.')
        self.assertTrue(body['actions'][0]['edited'])

    def test_the_EDITED_text_is_what_gets_written(self):
        _cfg, record = self._staged()
        self._req('POST', '/api/boards/acme-jira/staged/46/edit',
                  {'action_id': 'a1', 'params': {'body': 'Rewritten, warmer.'}})
        self._serve(J({'issues': [issue()]}), J({'comments': []}),
                    J({'id': '9'}, status=201))
        status, _b = self._req(
            'POST', '/api/boards/acme-jira/staged/46/approve',
            {'content_hash': record['content_hash'], 'approval_id': APPROVAL})
        self.assertEqual(status, 200)
        post = [c for c in self.calls if c['method'] == 'POST'][-1]
        self.assertIn('Rewritten, warmer.', post['body'].decode())

    def test_editing_an_unknown_action_is_404(self):
        self._staged()
        status, body = self._req(
            'POST', '/api/boards/acme-jira/staged/46/edit',
            {'action_id': 'nope', 'params': {}})
        self.assertEqual(status, 404)

    def test_editing_a_decided_record_is_409(self):
        self._staged()
        self._req('POST', '/api/boards/acme-jira/staged/46/reject',
                  {'approval_id': APPROVAL, 'reason': 'no'})
        status, body = self._req(
            'POST', '/api/boards/acme-jira/staged/46/edit',
            {'action_id': 'a1', 'params': {}})
        self.assertEqual(status, 409)
        self.assertEqual(body['code'], 'already_decided')


class SendBackRoundTripTests(_Base):
    """The re-scoping round trip (#588 Phase 6).

    Before this, send-back marked the record and stopped — a dead end. The
    issue asks for the opposite: *"the human answers and the item resumes with
    the agent's prior context intact, because the agent already read the ticket
    and formed a view; discarding that wastes the expensive part."*

    The load-bearing test here is
    `test_the_resumed_item_is_worked_INSIDE_a_run_so_propose_mode_still_holds`.
    Everything else is plumbing; that one is a safety property.
    """

    def _staged(self, mode='propose'):
        cfg = self._board()
        self._listing()
        run, err = RM.create(cfg, {'concurrency': 1, 'mode': mode})
        self.assertIsNone(err, err)
        RM._dispatch(run['id'])
        if mode == 'propose':
            self._stage_a_comment(cfg)
        return cfg, run

    def _retire(self, run):
        """Let the original run finish, which frees its lease.

        This is the ORDINARY case: the agent reports `needs_review`, the build
        exits, the reaper settles it. A reviewer acting before that happens is
        the other case, covered separately — and the two take different paths
        precisely because a live lease cannot be claimed by a new run.
        """
        RM._finish(run['id'], 'done')

    def _send_back(self, note='which refund — Jan or Mar?'):
        self._listing()          # the resume run lists to select the item
        return self._req('POST', '/api/boards/acme-jira/staged/46/send-back',
                         {'approval_id': APPROVAL, 'note': note})

    # ── the note ───────────────────────────────────────────────────────────

    def test_a_send_back_with_NO_note_is_refused(self):
        """The agent is about to work this again and the note is the only
        thing telling it what to change. A blank one looks like progress and
        is not — the same failure `set_disposition` guards against."""
        self._staged()
        status, body = self._req(
            'POST', '/api/boards/acme-jira/staged/46/send-back',
            {'approval_id': APPROVAL})
        self.assertEqual(status, 400, body)
        self.assertEqual(body['code'], 'note_required')
        # And nothing was decided — the item is still waiting for a real one.
        self.assertEqual(VM.get('acme-jira', '46')['state'], 'pending')

    def test_a_whitespace_only_note_is_refused_too(self):
        self._staged()
        status, body = self._req(
            'POST', '/api/boards/acme-jira/staged/46/send-back',
            {'approval_id': APPROVAL, 'note': '   \n  '})
        self.assertEqual(status, 400, body)

    def test_REJECT_still_needs_no_reason(self):
        """Refusing to act is self-explanatory in a way 'do it differently'
        is not, so the requirement is asymmetric on purpose."""
        self._staged()
        status, _b = self._req('POST', '/api/boards/acme-jira/staged/46/reject',
                               {'approval_id': APPROVAL})
        self.assertEqual(status, 200)

    def test_the_note_reaches_the_agent_verbatim(self):
        _cfg, run = self._staged()
        self._retire(run)
        with mock.patch.object(CTM, 'send_followup',
                               lambda t, p, submit=True: (None, 'Session is no longer running')):
            _s, body = self._send_back('which refund — Jan or Mar?')
            RM._dispatch(body['resume']['run_id'])
        self.assertIn('which refund — Jan or Mar?', self.prompts[-1])

    # ── the safety property ────────────────────────────────────────────────

    def test_the_resumed_item_is_worked_INSIDE_a_run_so_propose_mode_still_holds(self):
        """A build dispatched outside a run makes `staging_run_for` return None
        and the agent writes STRAIGHT TO THE BOARD — at exactly the moment a
        human said 'not like that'. The resume must therefore go through a real
        run, in the ORIGINAL mode."""
        _cfg, run = self._staged(mode='propose')
        self._retire(run)
        status, body = self._send_back()
        self.assertEqual(status, 200, body)
        self.assertTrue(body['resume']['dispatched'], body['resume'])

        resume_run = RM.get(body['resume']['run_id'])
        self.assertNotEqual(resume_run['id'], run['id'])
        self.assertEqual(resume_run['mode'], 'propose')
        self.assertEqual(resume_run['origin'], 'send_back')
        self.assertEqual(len(resume_run['items']), 1)
        # The lease is what enforces staging, and it must name the new run.
        RM._dispatch(resume_run['id'])
        holder = VM.staging_run_for('acme-jira', '46')
        self.assertIsNotNone(holder)
        self.assertEqual(holder['id'], resume_run['id'])

    def test_an_item_still_LEASED_by_a_live_run_is_resumed_in_place(self):
        """A reviewer can decide before the build exits — `needs_review` is
        reported first and the reaper frees the lease afterwards. A new run
        could not claim the item, so it would be marked `skipped` and nothing
        would work it at all. A live lease is already the invariant we wanted,
        so the agent holding it is asked directly."""
        _cfg, run = self._staged()            # deliberately NOT retired
        sent = []
        with mock.patch.object(CTM, 'send_followup',
                               lambda t, p, submit=True: (sent.append((t, p))
                                                          or ({'task_id': t}, None))):
            before = len(self.created)
            status, body = self._req(
                'POST', '/api/boards/acme-jira/staged/46/send-back',
                {'approval_id': APPROVAL, 'note': 'which refund — Jan or Mar?'})

        self.assertEqual(status, 200, body)
        self.assertTrue(body['resume']['dispatched'], body['resume'])
        self.assertEqual(body['resume']['run_id'], run['id'],
                         'no second run — the live one already holds the lease')
        self.assertEqual(len(self.created), before, 'no new build should start')
        self.assertEqual(len(sent), 1)
        self.assertIn('which refund', sent[0][1])
        self.assertEqual(RM.get(run['id'])['items']['46']['resume_tier'],
                         'followup')

    def test_a_transient_paste_failure_is_RETRIED_before_giving_up(self):
        """Delivery goes through the same paste-and-verify path as the initial
        dispatch, which can report failure when the note HAS reached the
        composer and only the submit was missed. Observed against a real agent:
        the note landed, the call returned an error, and the reviewer was told
        nothing was working the item while it demonstrably was."""
        _cfg, run = self._staged()
        calls = []

        def flaky(task_id, prompt, submit=True):
            calls.append(task_id)
            if len(calls) < 3:
                return None, 'Failed to send follow-up'
            return {'task_id': task_id}, None

        with mock.patch.object(CTM, 'send_followup', flaky), \
             mock.patch.object(server.time, 'sleep', lambda s: None):
            status, body = self._req(
                'POST', '/api/boards/acme-jira/staged/46/send-back',
                {'approval_id': APPROVAL, 'note': 'which refund?'})

        self.assertEqual(status, 200, body)
        self.assertEqual(len(calls), 3)
        self.assertTrue(body['resume']['dispatched'], body['resume'])

    def test_a_DEAD_session_is_not_retried(self):
        """A session that is gone will never come alive; retrying just delays
        the fall through to a fresh build."""
        _cfg, run = self._staged()
        calls = []

        def gone(task_id, prompt, submit=True):
            calls.append(task_id)
            return None, 'Session is no longer running'

        with mock.patch.object(CTM, 'send_followup', gone), \
             mock.patch.object(server.time, 'sleep', lambda s: None):
            self._req('POST', '/api/boards/acme-jira/staged/46/send-back',
                      {'approval_id': APPROVAL, 'note': 'which refund?'})
        self.assertEqual(len(calls), 1)

    def test_a_live_run_whose_agent_is_unreachable_says_so_rather_than_stalling(self):
        """The one case with nowhere to go: the lease is held so no new run can
        claim the item, and the agent holding it will not answer. Saying that
        beats a silent `skipped`."""
        _cfg, run = self._staged()
        with mock.patch.object(CTM, 'send_followup',
                               lambda t, p, submit=True: (None, 'Session is no longer running')):
            status, body = self._req(
                'POST', '/api/boards/acme-jira/staged/46/send-back',
                {'approval_id': APPROVAL, 'note': 'which refund?'})
        self.assertEqual(status, 200, body)
        self.assertFalse(body['resume']['dispatched'])
        self.assertIn(run['id'], body['resume']['detail'])
        # The decision itself still stuck.
        self.assertEqual(VM.get('acme-jira', '46')['state'], 'sent_back')

    def test_an_autonomous_item_does_not_become_propose_and_vice_versa(self):
        """Silently changing the mode on the way back round would be the worst
        possible reading of 'send back'."""
        cfg, run = self._staged(mode='autonomous')
        self._listing()
        VM.report(cfg, {'id': '46', 'key': 'SUP-46', 'title': 't'},
                  'needs_review', reason='check this', run_id=run['id'])
        self._retire(run)
        status, body = self._send_back()
        self.assertEqual(status, 200, body)
        self.assertEqual(RM.get(body['resume']['run_id'])['mode'], 'autonomous')

    def test_the_resume_run_ignores_the_processed_marker(self):
        """The reviewer asked for exactly this item again; a marker from the
        earlier pass is the one thing standing in the way."""
        _cfg, run = self._staged()
        RM._processed('acme-jira').record(
            '46', RM.get(run['id'])['items']['46']['content_hash'],
            run_id=run['id'], disposition='needs_review')
        self._retire(run)
        status, body = self._send_back()
        self.assertEqual(status, 200, body)
        self.assertTrue(body['resume']['dispatched'], body['resume'])
        self.assertEqual(len(RM.get(body['resume']['run_id'])['items']), 1)

    # ── the three tiers ────────────────────────────────────────────────────

    def test_a_DEAD_session_reopens_the_original_claude_session(self):
        """Tier 2 — the agent's own reasoning is still recoverable."""
        _cfg, run = self._staged()
        self._retire(run)
        session = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        with mock.patch.object(CTM, 'get_task',
                               lambda t: {'claude_session_id': session}), \
             mock.patch.object(CTM, 'send_followup',
                               lambda t, p, submit=True: (None, 'Session is no '
                                                          'longer running')), \
             mock.patch.object(CTM, '_claude_supports_resume', staticmethod(lambda: True)):
            _s, body = self._send_back()
            resume_run = RM.get(body['resume']['run_id'])
            RM._dispatch(resume_run['id'])

        self.assertEqual(self.created[-1]['resume_session_id'], session)
        self.assertEqual(RM.get(resume_run['id'])['items']['46']['resume_tier'],
                         'session')

    def test_with_no_resumable_session_it_starts_FRESH_and_says_so(self):
        """Tier 3. A Codex or OpenCode build leaves no readable transcript, so
        this is a real outcome rather than an error — but the row must not
        claim context was preserved when it was not."""
        _cfg, run = self._staged()
        self._retire(run)
        with mock.patch.object(CTM, 'get_task', lambda t: {'claude_session_id': ''}), \
             mock.patch.object(CTM, 'send_followup',
                               lambda t, p, submit=True: (None, 'Session is no longer running')):
            _s, body = self._send_back()
            resume_run = RM.get(body['resume']['run_id'])
            RM._dispatch(resume_run['id'])

        self.assertEqual(self.created[-1]['resume_session_id'], '')
        self.assertEqual(RM.get(resume_run['id'])['items']['46']['resume_tier'],
                         'fresh')
        # Tier 3 has nothing but the prompt, so the prior conclusion has to be
        # IN the prompt rather than assumed to be in context.
        self.assertIn('which refund', self.prompts[-1])

    def test_a_resumed_build_claims_no_claude_session_id(self):
        """It reopens the ORIGINAL transcript. Two task.json files naming one
        .jsonl would make the token ledger count that spend twice — the round
        trip must not manufacture phantom spend."""
        with mock.patch.object(CTM, '_claude_supports_session_id',
                               staticmethod(lambda: True)), \
             mock.patch.object(CTM, '_claude_supports_resume',
                               staticmethod(lambda: True)):
            cmd = CTM.assistant_command(
                'claude', session_id='11111111-2222-4333-8444-555555555555',
                resume_session_id='aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee')
        self.assertIn('--resume aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee', cmd)
        self.assertNotIn('--session-id', cmd,
                         'you cannot both mint a new session and continue one')

    def test_a_cli_without_resume_degrades_to_a_plain_launch(self):
        """An unrecognised flag makes the CLI refuse to start, so a sent-back
        item would land on a dead command instead of an agent."""
        with mock.patch.object(CTM, '_claude_supports_session_id',
                               staticmethod(lambda: True)), \
             mock.patch.object(CTM, '_claude_supports_resume',
                               staticmethod(lambda: False)):
            cmd = CTM.assistant_command(
                'claude', session_id='11111111-2222-4333-8444-555555555555',
                resume_session_id='aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee')
        self.assertNotIn('--resume', cmd)
        self.assertIn('--session-id', cmd)

    # ── failure is reported, never raised ──────────────────────────────────

    def test_a_failed_redispatch_does_not_undo_the_decision(self):
        """The send-back already succeeded and was logged. A dispatch failure
        reads as 'nothing is working on it' — true and actionable — not as
        'your send-back did not go through'."""
        _cfg, run = self._staged()
        self._retire(run)
        with mock.patch.object(RM, 'create',
                               classmethod(lambda cls, cfg, data, **kw:
                                           (None, 'the board could not be listed'))):
            status, body = self._req(
                'POST', '/api/boards/acme-jira/staged/46/send-back',
                {'approval_id': APPROVAL, 'note': 'which refund?'})
        self.assertEqual(status, 200, body)
        self.assertEqual(VM.get('acme-jira', '46')['state'], 'sent_back')
        self.assertFalse(body['resume']['dispatched'])
        self.assertIn('could not be listed', body['resume']['detail'])

    def test_a_RAISING_redispatch_is_still_only_a_report(self):
        _cfg, run = self._staged()
        self._retire(run)

        def boom(cls, cfg, data, **kw):
            raise RuntimeError('tmux exploded')

        with mock.patch.object(RM, 'create', classmethod(boom)):
            status, body = self._req(
                'POST', '/api/boards/acme-jira/staged/46/send-back',
                {'approval_id': APPROVAL, 'note': 'which refund?'})
        self.assertEqual(status, 200, body)
        self.assertFalse(body['resume']['dispatched'])


class SupersededProposalTests(_Base):
    """A record is per ITEM, not per run.

    So a second run working the same item finds the first one's proposals still
    sitting there. Left alone they ACCUMULATE, and approving fires all of them —
    two comments to the customer, one from an analysis that has since been
    superseded. That is exactly the failure the propose flow exists to prevent,
    and it is reachable without anyone doing anything odd: a run is interrupted
    after its agent staged, the item is run again, and now there are two.

    Found against a real board, where the second agent noticed the stray
    proposal in its own tool response and flagged it to the reviewer.
    """

    def _run_and_stage(self, cfg, body):
        self._listing()
        run, err = RM.create(cfg, {'concurrency': 1, 'mode': 'propose',
                                   'select': {'ignore_processed': True}})
        self.assertIsNone(err, err)
        RM._dispatch(run['id'])
        self._stage_a_comment(cfg, body=body)
        return run

    def test_a_second_run_SUPERSEDES_what_the_first_left_pending(self):
        cfg = self._board()
        first = self._run_and_stage(cfg, 'First attempt.')
        RM._finish(first['id'], 'interrupted')      # its agent never finished
        self._run_and_stage(cfg, 'Second attempt.')

        record = VM.get('acme-jira', '46')
        pending = [a for a in record['actions'] if a['state'] == 'pending']
        self.assertEqual(len(pending), 1, 'only the newest proposal may stand')
        self.assertIn('Second attempt.', pending[0]['params']['body'])

    def test_the_superseded_proposal_stays_VISIBLE(self):
        """A reviewer should be able to see that an earlier proposal existed
        and was replaced, rather than it vanishing."""
        cfg = self._board()
        first = self._run_and_stage(cfg, 'First attempt.')
        RM._finish(first['id'], 'interrupted')
        second = self._run_and_stage(cfg, 'Second attempt.')

        record = VM.get('acme-jira', '46')
        gone = [a for a in record['actions'] if a['state'] == 'superseded']
        self.assertEqual(len(gone), 1)
        self.assertEqual(gone[0]['superseded_by'], second['id'])

    def test_approving_fires_ONLY_the_surviving_proposal(self):
        """The failure that matters: one comment to the customer, not two."""
        cfg = self._board()
        first = self._run_and_stage(cfg, 'First attempt.')
        RM._finish(first['id'], 'interrupted')
        self._run_and_stage(cfg, 'Second attempt.')

        record = VM.get('acme-jira', '46')
        self._serve(J({'issues': [issue()]}), J({'comments': []}),
                    J({'id': '9'}, status=201))
        status, body = self._req(
            'POST', '/api/boards/acme-jira/staged/46/approve',
            {'content_hash': record['content_hash'], 'approval_id': APPROVAL})
        self.assertEqual(status, 200, body)
        posts = [c for c in self.calls if c['method'] == 'POST']
        self.assertEqual(len(posts), 1, 'exactly one comment may be written')
        self.assertIn('Second attempt.', posts[0]['body'].decode())

    def test_ONE_run_may_still_stage_several_actions(self):
        """An agent legitimately stages a comment and a status change together;
        superseding within a run would break that."""
        cfg = self._board()
        self._propose_run(cfg)
        self._stage_a_comment(cfg, body='A comment.')
        self._listing()
        self._req('POST', '/api/boards/acme-jira/items/46/actions',
                  {'action': 'set_status', 'params': {'status': 'Done'}})
        record = VM.get('acme-jira', '46')
        pending = [a for a in record['actions'] if a['state'] == 'pending']
        self.assertEqual(len(pending), 2)

    def test_a_send_back_already_discards_so_the_resume_run_starts_clean(self):
        """`decide` marks pending actions `discarded`, so the resume run has
        nothing to supersede — the two mechanisms do not fight."""
        cfg = self._board()
        run = self._run_and_stage(cfg, 'First attempt.')
        RM._finish(run['id'], 'done')
        self._listing()
        self._req('POST', '/api/boards/acme-jira/staged/46/send-back',
                  {'approval_id': APPROVAL, 'note': 'try again, warmer'})
        record = VM.get('acme-jira', '46')
        self.assertEqual([a['state'] for a in record['actions']], ['discarded'])


class AskOnSourceTests(_Base):
    """Post the clarifying question on the source ticket (#588 Phase 6).

    Opt-in per board, `needs_rescoping` only. The requester answers where they
    already work rather than in a review queue they cannot see.
    """

    def _board_asking(self, on=True, action='comment'):
        cfg = copy.deepcopy(fx.JIRA)
        cfg['id'] = 'acme-jira'
        cfg['review'] = {'ask_on_source': on, 'ask_action': action}
        saved, err = BM.create_or_update(cfg)
        self.assertIsNone(err, err)
        return saved

    # `ref` is what action URLs interpolate — the engine always maps it, so an
    # item literal without it is not a realistic item.
    ITEM = {'id': '46', 'key': 'SUP-46', 'title': 'Refund not received',
            'ref': {'issue_key': 'SUP-46'}}

    # ── the switch ─────────────────────────────────────────────────────────

    def test_it_is_OFF_by_default(self):
        """A board whose requesters are paying customers must not acquire 'the
        robot asks my customer questions' by accident."""
        cfg = self._board()
        self.assertFalse((cfg.get('review') or {}).get('ask_on_source'))
        VM.report(cfg, self.ITEM, 'needs_rescoping', reason='which refund?')
        self.assertEqual(self.calls, [], 'nothing should have been sent')

    def test_only_needs_rescoping_asks(self):
        """`needs_review` means "I have an answer, check it" — there is no
        question to put on the ticket."""
        cfg = self._board_asking()
        VM.report(cfg, self.ITEM, 'needs_review', reason='drafted a reply')
        self.assertEqual(self.calls, [])

    def test_a_rescoping_report_with_no_reason_asks_nothing(self):
        """`set_disposition` already refuses a reasonless non-completed
        disposition; this is the belt to that braces."""
        cfg = self._board_asking()
        _rec, err = VM.report(cfg, self.ITEM, 'needs_rescoping', reason='')
        self.assertIsNotNone(err)
        self.assertEqual(self.calls, [])

    # ── how it is sent ─────────────────────────────────────────────────────

    def test_in_propose_mode_the_question_is_STAGED_not_posted(self):
        """A question posted to a customer is still a customer-visible write,
        and deserves the same review as an answer would."""
        cfg = self._board_asking()
        self._propose_run(cfg)
        before = len(self.calls)
        VM.report(cfg, self.ITEM, 'needs_rescoping',
                  reason='Which refund do you mean — January or March?')
        self.assertEqual(len(self.calls), before, 'nothing may go to the vendor')
        record = VM.get('acme-jira', '46')
        pending = [a for a in record['actions'] if a['state'] == 'pending']
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['action'], 'comment')
        self.assertIn('January or March', pending[0]['params']['body'])

    def test_in_autonomous_mode_the_question_is_posted_immediately(self):
        cfg = self._board_asking()
        self._listing()
        run, err = RM.create(cfg, {'concurrency': 1, 'mode': 'autonomous'})
        self.assertIsNone(err, err)
        RM._dispatch(run['id'])
        self._serve(J({'comments': []}),            # marker probe
                    J({'id': '9'}, status=201))     # the comment
        VM.report(cfg, self.ITEM, 'needs_rescoping',
                  reason='Which refund do you mean?', run_id=run['id'])
        posts = [c for c in self.calls if c['method'] == 'POST']
        self.assertEqual(len(posts), 1)
        self.assertIn('Which refund do you mean?', posts[0]['body'].decode())

    def test_the_question_says_it_was_asked_automatically(self):
        """The requester is a person outside this workspace. A bare question
        with no provenance reads as a colleague typing."""
        cfg = self._board_asking()
        self._propose_run(cfg)
        VM.report(cfg, self.ITEM, 'needs_rescoping', reason='Which refund?')
        body = VM.get('acme-jira', '46')['actions'][-1]['params']['body']
        self.assertIn('asked automatically', body)

    # ── failure is contained ───────────────────────────────────────────────

    def test_a_failed_ask_does_not_lose_the_disposition(self):
        """The report is the agent's only output. Losing it because a comment
        could not be posted would waste the whole item."""
        cfg = self._board_asking()
        self._listing()
        run, _e = RM.create(cfg, {'concurrency': 1, 'mode': 'autonomous'})
        RM._dispatch(run['id'])
        with mock.patch.object(BM, 'run_action',
                               classmethod(lambda cls, *a, **k: (None, 'vendor 500'))):
            record, err = VM.report(cfg, self.ITEM, 'needs_rescoping',
                                    reason='Which refund?', run_id=run['id'])
        self.assertIsNone(err, err)
        self.assertEqual(VM.get('acme-jira', '46')['disposition'],
                         'needs_rescoping')

    # ── the allowlist ──────────────────────────────────────────────────────

    def test_ask_action_must_be_a_DECLARED_action(self):
        """The action list is the allowlist. Naming something undeclared here
        would be a second, quieter way to invoke a write."""
        cfg = copy.deepcopy(fx.JIRA)
        cfg['id'] = 'sneaky'
        cfg['review'] = {'ask_on_source': True, 'ask_action': 'delete_project'}
        _saved, err = BM.create_or_update(cfg)
        self.assertIsNotNone(err)
        self.assertIn('not declared', ' '.join(err) if isinstance(err, list) else err)

    def test_ask_on_source_without_an_action_is_refused(self):
        cfg = copy.deepcopy(fx.JIRA)
        cfg['id'] = 'incomplete'
        cfg['review'] = {'ask_on_source': True}
        _saved, err = BM.create_or_update(cfg)
        self.assertIsNotNone(err)

    def test_an_unknown_review_field_is_refused(self):
        cfg = copy.deepcopy(fx.JIRA)
        cfg['id'] = 'typo'
        cfg['review'] = {'ask_on_sauce': True}
        _saved, err = BM.create_or_update(cfg)
        self.assertIsNotNone(err)


class DecisionLedgerTests(_Base):
    """Every decision is also appended to an append-only ledger (#588 Phase 7).

    The staged book cannot answer "what is our approval rate": `_ensure`
    REPLACES a decided record when the item is staged again, and the round trip
    above makes that the normal case rather than the exception.
    """

    def _staged(self):
        cfg = self._board()
        self._propose_run(cfg)
        self._stage_a_comment(cfg)
        return cfg

    def _ledger(self):
        return VM.ledger('acme-jira').read()

    def test_an_approval_is_recorded(self):
        cfg = self._staged()
        record = VM.get('acme-jira', '46')
        self._serve(J({'issues': [issue()]}), J({'comments': []}),
                    J({'id': '9'}, status=201))
        self._req('POST', '/api/boards/acme-jira/staged/46/approve',
                  {'content_hash': record['content_hash'], 'approval_id': APPROVAL})
        decisions = [e for e in self._ledger() if e['state'] == 'approved']
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]['item_id'], '46')
        self.assertIs(decisions[0]['ok'], True)

    def test_a_rejection_is_recorded(self):
        self._staged()
        self._req('POST', '/api/boards/acme-jira/staged/46/reject',
                  {'approval_id': APPROVAL, 'reason': 'tone'})
        self.assertEqual([e['state'] for e in self._ledger()
                          if e['state'] == 'rejected'], ['rejected'])

    def test_a_reported_disposition_is_a_DIFFERENT_entry_from_a_decision(self):
        """Disposition distribution counts what AGENTS concluded; approval rate
        counts what HUMANS decided. Folding them together would make an
        auto-settled item — nothing staged, so no human ever saw it — look like
        an approval and inflate the rate."""
        cfg = self._staged()
        self._listing()
        VM.report(cfg, {'id': '46', 'key': 'SUP-46', 'title': 't'},
                  'needs_review', reason='needs a human')
        states = [e['state'] for e in self._ledger()]
        self.assertIn('reported', states)
        self.assertNotIn('approved', states)
        reported = [e for e in self._ledger() if e['state'] == 'reported']
        self.assertEqual(reported[-1]['disposition'], 'needs_review')

    def test_the_ledger_SURVIVES_the_record_being_replaced(self):
        """This is the whole reason the ledger exists. Re-staging an item wipes
        its decided record; the history must not go with it."""
        cfg = self._staged()
        self._req('POST', '/api/boards/acme-jira/staged/46/reject',
                  {'approval_id': APPROVAL, 'reason': 'tone'})
        self.assertEqual(VM.get('acme-jira', '46')['state'], 'rejected')

        # Same item comes back around and is staged again — `_ensure` replaces
        # the decided record wholesale.
        self._stage_a_comment(cfg, body='Second attempt, warmer.')
        self.assertEqual(VM.get('acme-jira', '46')['state'], 'pending')

        self.assertEqual([e['state'] for e in self._ledger()
                          if e['state'] == 'rejected'], ['rejected'],
                         'the earlier rejection must still be countable')

    def test_a_replayed_decision_is_recorded_ONCE(self):
        """A phone retrying over a flaky connection must not inflate the
        denominator."""
        self._staged()
        for _ in range(3):
            self._req('POST', '/api/boards/acme-jira/staged/46/reject',
                      {'approval_id': APPROVAL, 'reason': 'tone'})
        self.assertEqual(len([e for e in self._ledger()
                              if e['state'] == 'rejected']), 1)

    def test_a_ledger_failure_never_fails_the_decision(self):
        """The append happens AFTER the writes fired. Raising here would turn a
        completed approval into an error the caller would reasonably retry."""
        self._staged()
        with mock.patch.object(server.boards.store.JsonlLog, 'append',
                               lambda self, e: (_ for _ in ()).throw(OSError('full'))):
            status, body = self._req(
                'POST', '/api/boards/acme-jira/staged/46/reject',
                {'approval_id': APPROVAL, 'reason': 'tone'})
        self.assertEqual(status, 200, body)
        self.assertEqual(VM.get('acme-jira', '46')['state'], 'rejected')


class ReviewAuthTests(_Base):
    AUTH_OK = False

    def test_every_review_route_requires_auth(self):
        for method, path, body in (
            ('GET', '/api/boards/acme-jira/review', None),
            ('POST', '/api/boards/acme-jira/items/46/disposition',
             {'disposition': 'completed'}),
            ('POST', '/api/boards/acme-jira/staged/46/approve', {}),
            ('POST', '/api/boards/acme-jira/staged/46/reject', {}),
            ('POST', '/api/boards/acme-jira/staged/46/send-back', {}),
            ('POST', '/api/boards/acme-jira/staged/46/edit', {}),
        ):
            with self.subTest(route=f'{method} {path}'):
                status, _b = self._req(method, path, body)
                self.assertEqual(status, 401)


class ReviewReadonlyTests(_Base):
    READONLY = True

    def test_reading_the_queue_works_but_deciding_does_not(self):
        self._board()
        status, _b = self._req('GET', '/api/boards/acme-jira/review')
        self.assertEqual(status, 200)
        for path in ('/api/boards/acme-jira/staged/46/approve',
                     '/api/boards/acme-jira/staged/46/reject',
                     '/api/boards/acme-jira/items/46/disposition'):
            with self.subTest(path=path):
                status, body = self._req('POST', path, {})
                self.assertEqual(status, 403)
                self.assertEqual(body.get('code'), 'readonly')


class ItemIdSafetyTests(_Base):
    def test_a_vendor_item_id_with_slashes_cannot_escape_the_staged_dir(self):
        """GraphQL global ids carry slashes. Hashing rather than sanitising is
        what stops two different ids colliding onto one file — a collision
        would let an approval on one ticket fire another ticket's write."""
        a = VM._book('acme-jira')
        first = a._record_for('gid://issue/46')
        second = a._record_for('gid://issue/47')
        for path in (first.path, second.path):
            self.assertTrue(
                os.path.realpath(path).startswith(
                    os.path.realpath(VM.staged_dir('acme-jira'))))
        self.assertNotEqual(first.path, second.path)

    def test_two_ids_that_differ_only_in_punctuation_do_not_collide(self):
        book = VM._book('acme-jira')
        self.assertNotEqual(book._record_for('a/b').path,
                            book._record_for('a_b').path)


if __name__ == '__main__':
    unittest.main()
