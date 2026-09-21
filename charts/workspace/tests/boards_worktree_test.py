"""Board runs with a repository (#701) — isolation per item, and the three
holes in the first plan:

1. **send-back keeps isolation** — the re-worked item lands in the SAME
   worktree (same path, same branch, earlier commits present), whether the
   original run is still on record or has been pruned, and even after the
   worktree folder itself was removed;
2. **the default no longer collides silently** — a run that points several
   agents at one checkout carries a `shared_tree` warning;
3. **the review card can show the code** — every review record knows which
   Build worked it (report-only ones too), and the review list carries that
   Build's branch and change counts.

Real git, the real `create_task`, the real run/lease/review machinery. Only
tmux and the vendor API are stubbed.

Run:  python3 -m unittest tests.boards_worktree_test   (from charts/workspace/)
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

from tests import git_fixtures as gf  # noqa: E402  (installs the fcntl shim)
from tests.git_fixtures import commit, git, make_repo, write  # noqa: E402
import safe_http  # noqa: E402
import server  # noqa: E402
import worktrees  # noqa: E402
from boards import runs  # noqa: E402
from tests import board_fixtures as fx  # noqa: E402

BM = server.BoardsManager
BCM = server.BoardCredentialsManager
RM = server.BoardRunsManager
VM = server.BoardReviewManager
CTM = server.ClaudeTaskManager
WM = server.WorktreeManager

APPROVAL = 'a1b2c3d4-e5f6-4711-8899-aabbccddeeff'
BOARD = 'acme-jira'


def J(obj, status=200, headers=None):
    return (status, headers or {}, json.dumps(obj).encode('utf-8'))


def issue(item_id='46', summary='Refund not received'):
    return {'id': item_id, 'key': f'SUP-{item_id}',
            'fields': {'summary': summary, 'description': 'Dana says so.',
                       'status': {'name': 'To Do'}, 'updated': '2026-02-01'}}


class RepoConfigTests(unittest.TestCase):
    """The pure shape checks in boards/runs.py."""

    def test_absent_is_a_tracker_only_run(self):
        cfg, errors = runs.validate_repo_config({})
        self.assertEqual((cfg, errors), ({'workdir': '', 'isolate': False,
                                          'base_ref': ''}, []))

    def test_valid(self):
        cfg, errors = runs.validate_repo_config(
            {'workdir': '/home/dev/app', 'isolate': True, 'base_ref': 'main'})
        self.assertEqual(errors, [])
        self.assertEqual(cfg, {'workdir': '/home/dev/app', 'isolate': True,
                               'base_ref': 'main'})

    def test_refusals(self):
        cases = [
            ({'isolate': True}, 'isolate needs a workdir'),
            ({'workdir': 'relative/path'}, 'absolute path'),
            ({'workdir': 5}, 'absolute path'),
            ({'workdir': '/x', 'isolate': 'yes'}, 'true or false'),
            ({'workdir': '/x', 'base_ref': 'main'}, 'only applies with isolate'),
            ({'workdir': '/x', 'isolate': True, 'base_ref': 'x' * 201}, 'branch, tag'),
            ({'workdir': '/' + 'a' * 5000}, 'absolute path'),
        ]
        for body, needle in cases:
            _cfg, errors = runs.validate_repo_config(body)
            self.assertTrue(any(needle in e for e in errors), (body, errors))

    def test_shared_tree_warning_truth_table(self):
        self.assertEqual(runs.shared_tree_warning('/home/dev/app', False, 3), ['shared_tree'])
        self.assertEqual(runs.shared_tree_warning('/home/dev/app', True, 3), [])
        self.assertEqual(runs.shared_tree_warning('/home/dev/app', False, 1), [])
        self.assertEqual(runs.shared_tree_warning('', False, 8), [])
        self.assertEqual(runs.shared_tree_warning('/x', False, 'junk'), [])

    def test_run_record_and_summary_carry_the_settings(self):
        run = runs.new_run('run-1', BOARD, workdir='/home/dev/app', isolate=True,
                           base_ref='main', warnings=['shared_tree'])
        for rec in (run, runs.summary(run)):
            self.assertEqual((rec['workdir'], rec['isolate'], rec['base_ref'],
                              rec['warnings'], rec['worktree_skipped']),
                             ('/home/dev/app', True, 'main', ['shared_tree'], 0))
        plain = runs.summary(runs.new_run('run-2', BOARD))
        self.assertEqual((plain['workdir'], plain['isolate'], plain['warnings']),
                         ('', False, []))


@gf.requires_git
class _Base(gf.GitTestCase):

    @classmethod
    def setUpClass(cls):
        cls.boards_tmp = os.path.realpath(tempfile.mkdtemp(prefix='kc-board-wt-'))
        tok = fx.workspace_token_patch()
        tok.start()
        cls.addClassCleanup(tok.stop)
        cls._saved_home, BM.HOME_ROOT = BM.HOME_ROOT, cls.boards_tmp
        cls._saved_cred, BCM.HOME_ROOT = BCM.HOME_ROOT, cls.boards_tmp
        cls._auth_save = server.BrowserHandler.check_claude_auth
        server.BrowserHandler.check_claude_auth = lambda self, *a, **k: True
        cls._ro_save, server.READONLY_MODE = server.READONLY_MODE, False
        cls._safe_save = safe_http.is_safe_url
        safe_http.is_safe_url = lambda url, **kw: True
        cls.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0),
                                                    server.BrowserHandler)
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
        shutil.rmtree(cls.boards_tmp, ignore_errors=True)

    def setUp(self):
        super().setUp()
        shutil.rmtree(BM.boards_dir(), ignore_errors=True)
        try:
            os.remove(BCM.creds_file())
        except OSError:
            pass
        BCM.set('JIRA_API_TOKEN', 'secret-token')
        self.responses = []
        self.live = set()
        self.status = {}
        self.created = []
        self.prompts = []
        self.tasks_dir = os.path.join(self.tmp, 'tasks')
        os.makedirs(self.tasks_dir)

        def fetch(url, *, method='GET', headers=None, body=None, timeout=30,
                  allow_internal=False):
            if not self.responses:
                raise AssertionError(f'no stubbed response for {method} {url}')
            return self.responses.pop(0)

        real_create = CTM.create_task

        def create_task(prompt, **kw):
            self.prompts.append(prompt)
            self.created.append(kw)
            return real_create(prompt, **kw)

        def tmux(argv, *a, **kw):
            # Sessions are alive exactly when the test says so (`self.live`).
            if argv[:2] == ['tmux', 'kill-session']:
                self.live.discard(argv[-1].lstrip('='))
            if argv[:2] == ['tmux', 'has-session']:
                alive = argv[-1].lstrip('=') in self.live
                return mock.Mock(returncode=0 if alive else 1, stdout='',
                                 stderr='' if alive else 'no session')
            return mock.Mock(returncode=0, stdout='', stderr='')

        patches = [
            mock.patch.object(safe_http, 'fetch', fetch),
            mock.patch.object(CTM, 'TASKS_DIR', self.tasks_dir),
            mock.patch.object(CTM, 'create_task', create_task),
            mock.patch.object(CTM, 'task_status', lambda t: self.status.get(t, 'running')),
            mock.patch.object(CTM, 'count_live_tasks', lambda: 0),
            mock.patch.object(CTM, '_ensure_claude_trust', lambda *a, **k: True),
            mock.patch.object(CTM, '_wait_for_pane_ready', lambda *a, **k: None),
            mock.patch.object(CTM, '_deliver_prompt', lambda *a, **k: True),
            mock.patch('server.subprocess.run', side_effect=tmux),
            mock.patch.object(RM, '_spawn_driver', classmethod(lambda cls, r: None)),
            mock.patch.object(WM, 'HOME_ROOT', self.home),
            mock.patch.object(WM, 'SNAPSHOT_INLINE', True),
            mock.patch.object(WM, 'live_sessions', lambda: set(self.live)),
            mock.patch.object(worktrees, 'listen_ports', lambda: set()),
            mock.patch.object(server.FeedManager, 'emit', lambda *a, **k: None),
            mock.patch.object(server.FeedManager, 'emit_task_terminal', lambda *a, **k: None),
            mock.patch.object(server.FeedManager, 'emit_task_waiting', lambda *a, **k: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.repo = make_repo(self.home)
        self.board = self._board()

    # ── helpers ────────────────────────────────────────────────────────────

    def _board(self):
        cfg = copy.deepcopy(fx.JIRA)
        cfg['id'] = BOARD
        saved, err = BM.create_or_update(cfg)
        self.assertIsNone(err, err)
        return saved

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {'Content-Type': 'application/json'} if data else {}
        r = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}',
                                   data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            e.close()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw

    def _listing(self, *ids, summary='Refund not received'):
        self.responses.append(J({'issues': [issue(i, summary) for i in (ids or ('46',))]}))

    def _run(self, *ids, **body):
        self._listing(*ids)
        body.setdefault('concurrency', 1)
        body.setdefault('mode', 'propose')
        return RM.create(self.board, body)

    def _isolated_run(self, *ids, **body):
        run, err = self._run(*ids, workdir=self.repo, isolate=True, **body)
        self.assertIsNone(err, err)
        RM._dispatch(run['id'])
        return RM.get(run['id'])

    def _stage_comment(self, body='Fixed on the branch.'):
        self._listing()
        return self._req('POST', f'/api/boards/{BOARD}/items/46/actions',
                         {'action': 'comment', 'params': {'body': body},
                          'preview': body})

    def _report(self, disposition='needs_review', reason='please check the diff'):
        self._listing()
        return self._req('POST', f'/api/boards/{BOARD}/items/46/disposition',
                         {'disposition': disposition, 'reason': reason})

    def _send_back(self, note='use the March refund, not January'):
        self._listing()           # the resume run lists to select the item
        return self._req('POST', f'/api/boards/{BOARD}/staged/46/send-back',
                         {'approval_id': APPROVAL, 'note': note})

    def _meta(self, task_id):
        return CTM.read_meta(task_id)

    def _row(self, run, item='46'):
        return RM.get(run['id'])['items'][item]

    def _branches(self):
        return git(self.repo, 'branch', '--list', 'kc/*',
                   '--format=%(refname:short)').split()


class IsolatedRunTests(_Base):

    def test_a_tracker_only_run_launches_exactly_as_before(self):
        run, err = self._run()
        self.assertIsNone(err, err)
        RM._dispatch(run['id'])
        kw = self.created[-1]
        for key in ('isolate', 'workdir', 'worktree_slug', 'base_ref', 'base_sha'):
            self.assertNotIn(key, kw)
        self.assertNotIn('git repository', kw['system_preamble'])
        self.assertNotIn('worktree', self._meta(self._row(run)['task_id']))
        self.assertEqual((run['workdir'], run['isolate'], run['warnings']), ('', False, []))
        self.assertFalse(os.path.exists(self.wt_root))

    def test_a_shared_repo_run_warns_and_every_agent_works_in_the_folder(self):
        run, err = self._run('46', '47', workdir=self.repo, concurrency=2)
        self.assertIsNone(err, err)
        self.assertEqual(run['warnings'], ['shared_tree'])
        RM._dispatch(run['id'])
        self.assertEqual([kw['workdir'] for kw in self.created], [self.repo, self.repo])
        self.assertTrue(all('isolate' not in kw for kw in self.created))
        self.assertIn('git repository', self.created[0]['system_preamble'])
        # One at a time is not a collision. The board takes one run at a time
        # (#712), so the first has to be finished before the next can start.
        RM._finish(run['id'], 'done')
        run, _e = self._run('48', workdir=self.repo, concurrency=1)
        self.assertEqual(run['warnings'], [])

    def test_an_isolated_run_gives_every_item_its_own_worktree(self):
        run = self._isolated_run('46', '47', concurrency=2)
        self.assertEqual(run['warnings'], [])
        rows = run['items']
        paths = {rows[i]['worktree']['path'] for i in ('46', '47')}
        branches = {rows[i]['worktree']['branch'] for i in ('46', '47')}
        self.assertEqual((len(paths), len(branches)), (2, 2))
        for item in ('46', '47'):
            slug = worktrees.board_slug(BOARD, item)
            self.assertEqual(rows[item]['worktree']['slug'], slug)
            meta = self._meta(rows[item]['task_id'])
            self.assertEqual(meta['worktree']['branch'], f'kc/{slug}')
            self.assertEqual(meta['workdir'], meta['worktree']['path'])
            self.assertEqual(meta['board_item_id'], item)
        self.assertEqual(git(self.repo, 'status', '--porcelain'), '')
        self.assertTrue(all(kw['isolate'] for kw in self.created))

    def test_run_settings_are_validated_before_the_board_is_listed(self):
        plain = os.path.join(self.home, 'plain')
        os.makedirs(plain)
        for body, needle in (
                ({'isolate': True}, 'isolate needs a workdir'),
                ({'workdir': self.tmp, 'isolate': True}, 'not inside'),
                ({'workdir': plain, 'isolate': True}, 'not inside a git repository'),
                ({'workdir': self.repo, 'isolate': True, 'base_ref': 'nope'}, 'does not name a commit'),
                ({'workdir': self.repo, 'base_ref': 'main'}, 'only applies with isolate')):
            run, err = RM.create(self.board, dict(body, concurrency=1))
            self.assertIsNone(run)
            self.assertIn(needle, err, body)
        self.assertEqual(self.responses, [])     # nothing was fetched
        status, payload = self._req('POST', f'/api/boards/{BOARD}/runs',
                                    {'isolate': True, 'concurrency': 1})
        self.assertEqual(status, 400, payload)

    def test_the_worktree_limit_clamps_the_run_and_says_why(self):
        with mock.patch.dict(os.environ, {'KC_MAX_WORKTREES': '1'}):
            run, err = self._run('46', '47', workdir=self.repo, isolate=True)
        self.assertIsNone(err, err)
        self.assertEqual(list(run['items']), ['46'])
        self.assertEqual(run['worktree_skipped'], 1)
        self.assertIn('1 item left out', run['worktree_clamp_reason'])
        self.assertIn('Settings → Worktrees', run['worktree_clamp_reason'])

    def test_an_item_that_already_has_its_worktree_costs_nothing(self):
        first = self._isolated_run('46')
        self.live.add(self._meta(self._row(first)['task_id'])['tmux_session'])
        RM._finish(first['id'], 'done')
        with mock.patch.dict(os.environ, {'KC_MAX_WORKTREES': '1'}):
            run, err = self._run('46', '47', workdir=self.repo, isolate=True,
                                 select={'ignore_processed': True})
            self.assertIsNone(err, err)
            self.assertEqual(list(run['items']), ['46'])     # 47 needed a new one
            self.live.clear()
            RM._finish(run['id'], 'done')                    # one run at a time (#712)
            only_new, err = self._run('47', workdir=self.repo, isolate=True)
        self.assertIsNone(only_new)
        self.assertIn('no free worktree', err)

    def test_a_refused_launch_fails_the_item_in_its_own_words(self):
        run, err = self._run(workdir=self.repo, isolate=True)
        self.assertIsNone(err, err)
        shutil.rmtree(self.repo)          # the repository vanished mid-run
        RM._dispatch(run['id'])
        row = self._row(run)
        self.assertEqual(row['state'], 'failed')
        self.assertIn('is not a directory', row['error'])


class SupersedeTests(_Base):

    def _second_run(self):
        run, err = self._run(workdir=self.repo, isolate=True,
                             select={'ignore_processed': True})
        self.assertIsNone(err, err)
        RM._dispatch(run['id'])
        return RM.get(run['id'])

    def test_an_idle_build_of_the_same_item_hands_its_worktree_over(self):
        first = self._isolated_run()
        old = self._row(first)['task_id']
        old_path = self._meta(old)['worktree']['path']
        self.live.add(self._meta(old)['tmux_session'])
        CTM._atomic_update_meta(os.path.join(self.tasks_dir, old),
                                lambda m: m.__setitem__('status', 'waiting-for-input'))
        RM._finish(first['id'], 'done')
        second = self._second_run()
        row = self._row(second)
        self.assertEqual(row['superseded_task_id'], old)
        self.assertEqual(self._meta(old)['status'], 'killed')
        self.assertEqual(self._meta(row['task_id'])['worktree']['path'], old_path)

    def test_a_WORKING_build_is_never_killed_to_take_its_worktree(self):
        first = self._isolated_run()
        old = self._row(first)['task_id']
        self.live.add(self._meta(old)['tmux_session'])      # still 'running'
        RM._finish(first['id'], 'done')
        second = self._second_run()
        row = self._row(second)
        self.assertEqual(row['state'], 'failed')
        self.assertIn('in use by Build', row['error'])
        self.assertEqual(self._meta(old)['status'], 'running')


class SendBackTests(_Base):
    """Hole 1 — a send-back works the item in the SAME worktree."""

    def _worked(self):
        """An isolated propose run whose agent committed, staged a reply and
        reported; then the run ends and the agent's session is gone."""
        run = self._isolated_run()
        task = self._row(run)['task_id']
        wt = self._meta(task)['worktree']
        commit(wt['path'], 'fix.py', 'print("refund")\n', 'refund fix')
        status, body = self._stage_comment()
        self.assertEqual(status, 202, body)
        self._report()
        RM._finish(run['id'], 'done')
        return run, task, wt

    def _dispatch_resume(self, body):
        resume_run = RM.get(body['resume']['run_id'])
        RM._dispatch(resume_run['id'])
        return RM.get(resume_run['id'])

    def test_the_reworked_item_lands_in_the_same_worktree_with_its_commits(self):
        _run, task, wt = self._worked()
        with mock.patch.object(CTM, 'send_followup',
                               lambda t, p, submit=True: (None, 'Session is no longer running')):
            status, body = self._send_back()
            self.assertEqual(status, 200, body)
            resume_run = self._dispatch_resume(body)
        self.assertEqual((resume_run['workdir'], resume_run['isolate']), (self.repo, True))
        kw = self.created[-1]
        self.assertTrue(kw['isolate'])
        self.assertEqual(kw['worktree_slug'], wt['slug'])
        self.assertEqual(kw['workdir'], wt['source_workdir'])
        new = self._meta(self._row(resume_run)['task_id'])
        self.assertEqual(new['worktree']['path'], wt['path'])
        self.assertEqual(new['workdir'], self._meta(task)['workdir'])
        self.assertTrue(new['worktree']['reused'])
        self.assertTrue(os.path.exists(os.path.join(wt['path'], 'fix.py')))
        self.assertIn('RE-WORKED', self.prompts[-1])

    def test_tier_2_resumes_the_session_because_the_folder_is_the_same(self):
        with mock.patch.object(CTM, '_claude_supports_session_id', staticmethod(lambda: True)), \
                mock.patch.object(CTM, '_claude_supports_resume', staticmethod(lambda: True)):
            _run, task, wt = self._worked()
            session = self._meta(task)['claude_session_id']
            self.assertTrue(session)
            with mock.patch.object(CTM, 'send_followup',
                                   lambda t, p, submit=True: (None, 'Session is no longer running')):
                _s, body = self._send_back()
                resume_run = self._dispatch_resume(body)
        self.assertEqual(self.created[-1]['resume_session_id'], session)
        self.assertEqual(self._row(resume_run)['resume_tier'], 'session')

    def test_a_second_send_back_resumes_the_same_conversation_again(self):
        # Found live: a resumed Build records the conversation it reopened as
        # `resumed_session_id` (so its spend is not counted twice), and the
        # next send-back read only `claude_session_id` — so the second round
        # trip on one ticket fell to a fresh start and lost the agent's context.
        dead = lambda t, p, submit=True: (None, 'Session is no longer running')
        with mock.patch.object(CTM, '_claude_supports_session_id', staticmethod(lambda: True)), \
                mock.patch.object(CTM, '_claude_supports_resume', staticmethod(lambda: True)), \
                mock.patch.object(CTM, 'send_followup', dead):
            _run, task, wt = self._worked()
            session = self._meta(task)['claude_session_id']
            _s, body = self._send_back()
            first = self._dispatch_resume(body)
            self.assertEqual(self._row(first)['resume_tier'], 'session')
            self.assertEqual(self._stage_comment('Now with the March refund.')[0], 202)
            self._report()
            RM._finish(first['id'], 'done')
            self._listing()
            status, body = self._req(
                'POST', f'/api/boards/{BOARD}/staged/46/send-back',
                {'approval_id': 'b2c3d4e5-f6a7-4811-9900-bbccddeeff00',
                 'note': 'and say which account it went to'})
            self.assertEqual(status, 200, body)
            second = self._dispatch_resume(body)
        self.assertEqual(self._row(second)['resume_tier'], 'session')
        self.assertEqual(self.created[-1]['resume_session_id'], session)
        self.assertEqual(self.created[-1]['worktree_slug'], wt['slug'])

    def test_tier_1_talks_to_the_live_agent_in_its_own_folder(self):
        run = self._isolated_run()
        self._stage_comment()
        self._report()
        launches = len(self.created)
        with mock.patch.object(CTM, 'send_followup',
                               lambda t, p, submit=True: ({'task_id': t}, None)):
            status, body = self._send_back()      # the run still holds the item
        self.assertEqual(status, 200, body)
        self.assertEqual(len(self.created), launches)
        self.assertEqual(self._row(run)['resume_tier'], 'followup')

    def test_tier_1_after_the_run_ended_names_the_worktree_on_the_row(self):
        # Found live: the run had finished but the agent's REPL was still
        # open, so the send-back run reached it in place — and its row had no
        # worktree, so the Runs panel showed no branch for that item.
        _run, task, wt = self._worked()
        launches = len(self.created)
        with mock.patch.object(CTM, 'send_followup',
                               lambda t, p, submit=True: ({'task_id': t}, None)):
            status, body = self._send_back()
            self.assertEqual(status, 200, body)
            resume_run = self._dispatch_resume(body)
        row = self._row(resume_run)
        self.assertEqual(len(self.created), launches)
        self.assertEqual((row['resume_tier'], row['task_id']), ('followup', task))
        self.assertEqual(row['worktree'], {k: wt[k] for k in ('slug', 'branch', 'path')})

    def test_it_still_isolates_after_the_prior_run_record_is_pruned(self):
        run, _task, wt = self._worked()
        RM._run_record(run['id']).delete()
        with mock.patch.object(CTM, 'send_followup',
                               lambda t, p, submit=True: (None, 'Session is no longer running')):
            _s, body = self._send_back()
            resume_run = self._dispatch_resume(body)
        self.assertEqual((resume_run['workdir'], resume_run['isolate']),
                         (wt['source_workdir'], True))
        self.assertEqual(self._meta(self._row(resume_run)['task_id'])['worktree']['path'],
                         wt['path'])

    def test_a_removed_worktree_comes_back_from_its_branch(self):
        _run, task, wt = self._worked()
        body, status = WM.remove_for_task(task, force=True)
        self.assertEqual(status, 200, body)
        self.assertFalse(os.path.exists(wt['path']))
        with mock.patch.object(CTM, 'send_followup',
                               lambda t, p, submit=True: (None, 'Session is no longer running')):
            _s, body = self._send_back()
            resume_run = self._dispatch_resume(body)
        new = self._meta(self._row(resume_run)['task_id'])['worktree']
        self.assertEqual(new['path'], wt['path'])
        self.assertTrue(os.path.exists(os.path.join(wt['path'], 'fix.py')))
        self.assertEqual(new['base_sha'], wt['base_sha'])

    def test_an_unreachable_live_agent_is_ended_so_the_rework_can_start(self):
        run = self._isolated_run()
        task = self._row(run)['task_id']
        self._stage_comment()
        self._report()
        RM._finish(run['id'], 'done')
        self.live.add(self._meta(task)['tmux_session'])      # alive, but deaf
        with mock.patch.object(CTM, 'send_followup',
                               lambda t, p, submit=True: (None, 'paste failed')), \
                mock.patch.object(RM, 'RESUME_FOLLOWUP_BACKOFF', 0):
            _s, body = self._send_back()
            resume_run = self._dispatch_resume(body)
        row = self._row(resume_run)
        self.assertEqual(row['superseded_task_id'], task)
        self.assertEqual(row['state'], 'working')


class ReviewCardTests(_Base):
    """Hole 3 — the review record knows its Build, and the list shows its code."""

    def test_a_report_only_record_still_knows_which_build_worked_it(self):
        run = self._isolated_run()
        task = self._row(run)['task_id']
        status, body = self._report('blocked', 'needs a product decision')
        self.assertEqual(status, 200, body)
        self.assertEqual(VM.get(BOARD, '46')['task_id'], task)

    def test_the_review_list_carries_the_branch_and_what_changed(self):
        run = self._isolated_run()
        task = self._row(run)['task_id']
        wt = self._meta(task)['worktree']
        commit(wt['path'], 'fix.py', 'a\nb\n', 'fix')
        self._stage_comment()
        self._report()
        self.status[task] = 'completed'
        WM.snapshot_now(task)                  # what the reconciler does on exit
        status, body = self._req('GET', f'/api/boards/{BOARD}/review')
        self.assertEqual(status, 200, body)
        rec = next(r for g in body['groups'] for r in g['items'] if r['item_id'] == '46')
        brief = rec['worktree']
        self.assertEqual((brief['task_id'], brief['branch']), (task, wt['branch']))
        self.assertEqual((brief['stat']['files_changed'], brief['stat']['insertions']), (1, 2))
        self.assertFalse(brief['removed'])

    def test_a_tracker_only_record_has_no_worktree_key(self):
        run, _e = self._run()
        RM._dispatch(run['id'])
        self._report('blocked', 'waiting on the customer')
        _s, body = self._req('GET', f'/api/boards/{BOARD}/review')
        rec = next(r for g in body['groups'] for r in g['items'] if r['item_id'] == '46')
        self.assertNotIn('worktree', rec)
        self.assertTrue(rec['task_id'])


if __name__ == '__main__':
    unittest.main()
