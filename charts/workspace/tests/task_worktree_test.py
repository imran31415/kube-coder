"""Isolated Builds (#701) — `create_task(isolate=True)`, the worktree routes and
the registry, against real git with only tmux stubbed.

The golden test is the one to read first: with `isolate=False` the launch
command, the session env and the task.json keys are exactly what they were
before this feature, because the feature is opt-in and a regression there
would reach every Build, webhook, cron and Board item in every workspace.

Run:  python3 -m unittest tests.task_worktree_test   (from charts/workspace/)
"""

import http.server
import json
import os
import shlex
import sys
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from tests import git_fixtures as gf  # noqa: E402  (installs the fcntl shim)
from tests.git_fixtures import commit, git, make_repo, write  # noqa: E402
import server  # noqa: E402
import worktrees  # noqa: E402

CTM = server.ClaudeTaskManager
WM = server.WorktreeManager

#: task.json keys every non-isolated Build had before #701.
BASE_META_KEYS = {
    'task_id', 'session_id', 'claude_session_id', 'prompt', 'workdir',
    'project_id', 'status', 'created_at', 'tmux_session', 'assistant', 'model',
    'effort', 'parent_task_id', 'board_id', 'board_item_id',
    'resumed_session_id', 'sub_task_ids', 'memory_injected',
}
WT_ENV = ('KC_WT', 'KC_WT_BRANCH', 'PORT', 'KC_PORT')


class _Tmux:
    """Records every tmux call; `fail_new_session` makes the launch fail."""

    def __init__(self):
        self.calls = []
        self.fail_new_session = False
        self.lock = threading.Lock()

    def __call__(self, argv, *a, **kw):
        with self.lock:
            self.calls.append(list(argv))
        if (self.fail_new_session and len(argv) > 1 and argv[0] == 'tmux'
                and argv[1] == 'new-session'):
            return mock.Mock(returncode=1, stdout='', stderr='no server')
        return mock.Mock(returncode=0, stdout='', stderr='')

    def launches(self):
        with self.lock:
            return [c for c in self.calls
                    if len(c) > 1 and c[0] == 'tmux' and c[1] == 'new-session']


def env_of(argv):
    out = {}
    for i, a in enumerate(argv):
        if a == '-e' and i + 1 < len(argv):
            k, _, v = argv[i + 1].partition('=')
            out[k] = v
    return out


@gf.requires_git
class _IsolatedBase(gf.GitTestCase):

    def setUp(self):
        super().setUp()
        self.tasks_dir = os.path.join(self.tmp, 'tasks')
        os.makedirs(self.tasks_dir)
        self.tmux = _Tmux()
        self.live = set()
        self.trust = mock.Mock(return_value=True)
        patches = [
            mock.patch.object(CTM, 'TASKS_DIR', self.tasks_dir),
            mock.patch.object(WM, 'HOME_ROOT', self.home),
            mock.patch.object(WM, 'SNAPSHOT_INLINE', True),
            mock.patch.object(WM, 'live_sessions', lambda: set(self.live)),
            mock.patch('server.subprocess.run', side_effect=self.tmux),
            mock.patch.object(CTM, '_ensure_claude_trust', self.trust),
            mock.patch.object(CTM, '_wait_for_pane_ready', lambda *a, **k: None),
            mock.patch.object(CTM, '_deliver_prompt', lambda *a, **k: True),
            mock.patch.object(worktrees, 'listen_ports', lambda: set()),
            mock.patch.object(server.FeedManager, 'emit_task_terminal',
                              lambda *a, **k: None),
            mock.patch.object(server.FeedManager, 'emit_task_waiting',
                              lambda *a, **k: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.repo = make_repo(self.home)

    def create(self, prompt='do the thing', **kw):
        kw.setdefault('workdir', self.repo)
        return CTM.create_task(prompt, **kw)

    def task_dirs(self):
        return sorted(os.listdir(self.tasks_dir))

    def kc_branches(self):
        return git(self.repo, 'branch', '--list', 'kc/*',
                   '--format=%(refname:short)').split()

    def session_of(self, meta):
        return meta['tmux_session']


class IsolatedCreateTests(_IsolatedBase):

    def test_isolate_false_is_unchanged(self):
        meta = self.create()
        self.assertNotIn('worktree', meta)
        self.assertEqual(set(meta) - {'source', 'response_url', 'response_secret',
                                      'memory_injection_disabled'},
                         BASE_META_KEYS)
        self.assertEqual(meta['workdir'], self.repo)
        launch = self.tmux.launches()[-1]
        self.assertTrue(launch[-1].startswith(f'cd {shlex.quote(self.repo)} && '))
        self.assertFalse(set(env_of(launch)) & set(WT_ENV))
        self.trust.assert_called_once()
        self.assertEqual(self.trust.call_args.args[0], self.repo)
        self.assertEqual(os.listdir(self.home), ['app'])   # no .worktrees
        with open(os.path.join(self.tasks_dir, meta['task_id'], 'prompt.txt')) as f:
            self.assertEqual(f.read(), 'do the thing')

    def test_isolated_build_runs_in_its_own_worktree(self):
        meta = self.create(isolate=True)
        wt = meta['worktree']
        self.assertEqual(meta['status'], 'running')
        self.assertEqual(wt['branch'], f'kc/t-{meta["task_id"]}')
        self.assertEqual(meta['workdir'], wt['path'])
        self.assertEqual(wt['source_workdir'], self.repo)
        self.assertEqual(wt['repo_root'], self.repo)
        self.assertTrue(os.path.isdir(wt['path']))
        launch = self.tmux.launches()[-1]
        self.assertTrue(launch[-1].startswith(f'cd {shlex.quote(wt["path"])} && '))
        env = env_of(launch)
        self.assertEqual(env['KC_WT'], wt['path'])
        self.assertEqual(env['KC_WT_BRANCH'], wt['branch'])
        self.assertEqual(env['PORT'], str(wt['port']))
        self.assertEqual(env['KC_PORT'], str(wt['port']))
        self.assertEqual(env['KC_TASK_ID'], meta['task_id'])
        # Claude's folder trust is seeded for where the agent actually runs.
        self.assertEqual(self.trust.call_args.args[0], wt['path'])
        with open(os.path.join(self.tasks_dir, meta['task_id'], 'prompt.txt')) as f:
            text = f.read()
        self.assertIn('ISOLATED git worktree', text)
        self.assertTrue(text.endswith('do the thing'))
        # The main checkout is untouched.
        self.assertEqual(git(self.repo, 'status', '--porcelain'), '')
        self.assertEqual(git(self.repo, 'rev-parse', '--abbrev-ref', 'HEAD'), 'main')
        # And the stored copy says the same as the returned one.
        stored = CTM.read_meta(meta['task_id'])
        self.assertEqual(stored['worktree']['path'], wt['path'])

    def test_subdir_is_preserved(self):
        commit(self.repo, 'web/app.js', 'x\n')
        meta = self.create(isolate=True, workdir=os.path.join(self.repo, 'web'))
        wt = meta['worktree']
        self.assertEqual(meta['workdir'], os.path.join(wt['path'], 'web'))
        self.assertEqual(wt['subdir'], 'web')

    def test_attribution_and_devcontainer_use_the_source_folder(self):
        seen = {}

        def attribute(path):
            seen['project'] = path
            return ''

        def dc_env(path):
            seen['dc'] = path
            return {'PORT': '9999', 'NODE_ENV': 'development'}
        with mock.patch.object(server.ProjectsManager, 'project_for_workdir',
                               side_effect=attribute), \
                mock.patch.object(server, '_DEVCONTAINER_AVAILABLE', True), \
                mock.patch.object(server.DevcontainerManager, 'env_for_workdir',
                                  side_effect=dc_env):
            meta = self.create(isolate=True)
        self.assertEqual(seen, {'project': self.repo, 'dc': self.repo})
        env = env_of(self.tmux.launches()[-1])
        # The repo's devcontainer PORT loses to the leased one; the rest stays.
        self.assertEqual(env['PORT'], str(meta['worktree']['port']))
        self.assertEqual(env['NODE_ENV'], 'development')
        self.assertEqual(sum(1 for i, a in enumerate(self.tmux.launches()[-1])
                             if a == '-e' and self.tmux.launches()[-1][i + 1]
                             .startswith('PORT=')), 1)

    def test_two_isolated_builds_never_share(self):
        out = []
        threads = [threading.Thread(target=lambda: out.append(self.create(isolate=True)))
                   for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        paths = {m['worktree']['path'] for m in out}
        ports = {m['worktree']['port'] for m in out}
        branches = {m['worktree']['branch'] for m in out}
        self.assertEqual((len(paths), len(ports), len(branches)), (2, 2, 2))
        a, b = out
        write(os.path.join(a['worktree']['path'], 'app.txt'), 'from-A\n')
        write(os.path.join(b['worktree']['path'], 'app.txt'), 'from-B\n')
        for m, want in ((a, 'from-A\n'), (b, 'from-B\n')):
            with open(os.path.join(m['worktree']['path'], 'app.txt')) as f:
                self.assertEqual(f.read(), want)
        self.assertFalse(os.path.exists(os.path.join(self.repo, 'app.txt')))

    def test_non_git_folder_creates_nothing(self):
        plain = os.path.join(self.home, 'notes')
        os.makedirs(plain)
        out = self.create(isolate=True, workdir=plain)
        self.assertEqual((out['status'], out['code'], out['task_id']),
                         ('invalid', 'not_git', None))
        self.assertEqual(self.task_dirs(), [])
        self.assertEqual(self.tmux.launches(), [])
        self.assertFalse(os.path.exists(self.wt_root))

    def test_bad_base_and_slug_create_nothing(self):
        for kw, code in (({'base_ref': '--upload-pack=x'}, 'bad_ref'),
                         ({'base_ref': 'nope'}, 'unknown_ref'),
                         ({'worktree_slug': '////'}, 'bad_slug')):
            out = self.create(isolate=True, **kw)
            self.assertEqual(out['code'], code, kw)
        self.assertEqual(self.task_dirs(), [])
        self.assertEqual(self.kc_branches(), [])

    def test_cap_refuses_before_any_task_exists(self):
        with mock.patch.dict(os.environ, {'KC_MAX_WORKTREES': '1'}):
            first = self.create(isolate=True)
            self.live.add(self.session_of(first))
            out = self.create(isolate=True)
        self.assertEqual((out['status'], out['code']), ('rejected', 'worktree_cap'))
        self.assertIn('Settings', out['error'])
        self.assertEqual(self.task_dirs(), [first['task_id']])

    def test_cap_reclaims_a_dead_pristine_worktree(self):
        with mock.patch.dict(os.environ, {'KC_MAX_WORKTREES': '1',
                                          'KC_WORKTREE_GRACE_S': '0'}):
            first = self.create(isolate=True)
            CTM.delete_task(first['task_id'])          # finished, changed nothing
            second = self.create(isolate=True)
        self.assertEqual(second['status'], 'running')
        self.assertFalse(os.path.exists(first['worktree']['path']))

    def test_tmux_failure_rolls_the_worktree_back(self):
        self.tmux.fail_new_session = True
        meta = self.create(isolate=True)
        self.assertEqual(meta['status'], 'error')
        self.assertTrue(meta['worktree']['rollback'])
        self.assertTrue(meta['worktree']['removed_at'])
        self.assertFalse(os.path.exists(meta['worktree']['path']))
        self.assertEqual(self.kc_branches(), [])
        self.assertTrue(CTM.read_meta(meta['task_id'])['worktree']['rollback'])

    def test_an_exception_mid_launch_rolls_back_and_propagates(self):
        with mock.patch.object(WM, 'meta_for', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self.create(isolate=True)
        self.assertEqual(os.listdir(os.path.join(self.wt_root, 'app')), ['.kc-repo'])
        self.assertEqual(self.kc_branches(), [])

    def test_a_reused_worktree_survives_a_failed_launch(self):
        first = self.create(isolate=True, worktree_slug='shared')
        commit(first['worktree']['path'], 'work.txt', 'work\n')
        self.tmux.fail_new_session = True
        again = self.create(isolate=True, worktree_slug='shared')
        self.assertEqual(again['status'], 'error')
        self.assertTrue(again['worktree']['reused'])
        self.assertTrue(os.path.exists(os.path.join(first['worktree']['path'],
                                                    'work.txt')))

    def test_named_slug_is_busy_while_its_owner_runs(self):
        first = self.create(isolate=True, worktree_slug='issue-701')
        self.live.add(self.session_of(first))
        out = self.create(isolate=True, worktree_slug='issue-701')
        self.assertEqual((out['status'], out['code']), ('conflict', 'busy'))
        self.assertEqual(out['owner_task_id'], first['task_id'])
        self.assertEqual(self.task_dirs(), [first['task_id']])
        # Once it is gone, the same name continues the same worktree.
        self.live.clear()
        again = self.create(isolate=True, worktree_slug='issue-701')
        self.assertEqual(again['worktree']['path'], first['worktree']['path'])
        self.assertTrue(again['worktree']['reused'])

    def test_base_ref(self):
        tip = commit(self.repo, 'later.txt', 'later\n')
        meta = self.create(isolate=True, base_ref='HEAD~1')
        self.assertEqual(meta['worktree']['base_ref'], 'HEAD~1')
        self.assertNotEqual(meta['worktree']['base_sha'], tip)
        self.assertFalse(os.path.exists(os.path.join(meta['worktree']['path'],
                                                     'later.txt')))

    def test_empty_prompt_stays_empty(self):
        meta = self.create(prompt='', isolate=True)
        with open(os.path.join(self.tasks_dir, meta['task_id'], 'prompt.txt')) as f:
            self.assertEqual(f.read(), '')

    def test_non_claude_assistant(self):
        with mock.patch.object(CTM, 'resolve_assistant', return_value='ante'):
            meta = self.create(isolate=True, assistant='ante')
        self.trust.assert_not_called()
        self.assertEqual(env_of(self.tmux.launches()[-1])['KC_WT'],
                         meta['worktree']['path'])

    def test_kill_keeps_the_worktree(self):
        meta = self.create(isolate=True)
        commit(meta['worktree']['path'], 'w.txt', 'w\n')
        CTM.delete_task(meta['task_id'])
        self.assertTrue(os.path.isdir(meta['worktree']['path']))

    def test_list_tasks_carries_a_brief_only_for_isolated_builds(self):
        plain = self.create()
        iso = self.create(isolate=True)
        rows = {r['task_id']: r for r in CTM.list_tasks()}
        self.assertNotIn('worktree', rows[plain['task_id']])
        brief = rows[iso['task_id']]['worktree']
        self.assertEqual(brief['branch'], iso['worktree']['branch'])
        self.assertFalse(brief['removed'])

    def test_a_finished_build_records_what_it_changed(self):
        meta = self.create(isolate=True)
        write(os.path.join(meta['worktree']['path'], 'new.txt'), 'x\n')
        commit(meta['worktree']['path'], 'done.txt', 'a\nb\n')
        self.tmux.calls.clear()
        with mock.patch('server.subprocess.run',
                        side_effect=lambda argv, *a, **k: mock.Mock(
                            returncode=1 if argv[:2] == ['tmux', 'has-session']
                            else 0, stdout='', stderr='')):
            self.assertEqual(CTM.task_status(meta['task_id']), 'completed')
        stat = CTM.read_meta(meta['task_id'])['worktree']['stat']
        self.assertEqual((stat['files_changed'], stat['ahead'], stat['untracked']),
                         (2, 1, 1))
        self.assertEqual(stat['insertions'], 2)


@gf.requires_git
class WorktreeRouteTests(_IsolatedBase):

    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0),
                                                    server.BrowserHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        super().setUp()
        self.authed = True
        self.public = False       # AUTH_MODE=none: open to anyone who may

        def check(h, allow_none_mode=True):
            return self.authed or (self.public and allow_none_mode)
        for p in (mock.patch.object(server.BrowserHandler, 'check_claude_auth', check),
                  mock.patch.object(server, 'READONLY_MODE', False)):
            p.start()
            self.addCleanup(p.stop)

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}',
                                     data=data, method=method,
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read() or b'{}')
        except urllib.error.HTTPError as e:
            raw = e.read()
            e.close()
            try:
                return e.code, json.loads(raw or b'{}')
            except ValueError:
                return e.code, {'raw': raw.decode('utf-8', 'replace')}

    def post_task(self, **body):
        body.setdefault('prompt', 'hi')
        body.setdefault('workdir', self.repo)
        return self.call('POST', '/api/claude/tasks', body)

    def test_create_statuses(self):
        plain = os.path.join(self.home, 'plain')
        os.makedirs(plain)
        status, body = self.post_task(isolate=True, workdir=plain)
        self.assertEqual((status, body['code']), (400, 'not_git'))
        # Only a JSON true isolates.
        status, body = self.post_task(isolate='true')
        self.assertEqual(status, 201)
        self.assertNotIn('worktree', body)
        status, body = self.post_task(base_ref='main')
        self.assertEqual(status, 400)
        status, body = self.post_task(isolate=True, base_ref=['x'])
        self.assertEqual((status, body['code']), (400, 'bad_ref'))
        status, body = self.post_task(isolate=True)
        self.assertEqual(status, 201)
        self.assertEqual(body['worktree']['branch'], f'kc/t-{body["task_id"]}')
        self.live.add(body['tmux_session'])
        with mock.patch.dict(os.environ, {'KC_MAX_WORKTREES': '1'}):
            status, body = self.post_task(isolate=True)
        self.assertEqual((status, body['code']), (429, 'worktree_cap'))

    def test_status_diff_and_remove(self):
        _s, task = self.post_task(isolate=True)
        tid, wt = task['task_id'], task['worktree']
        commit(wt['path'], 'feature.txt', 'one\ntwo\n')
        write(os.path.join(wt['path'], 'wip.txt'), 'wip\n')
        self.live.add(task['tmux_session'])

        status, body = self.call('GET', f'/api/claude/tasks/{tid}/worktree')
        self.assertEqual(status, 200)
        st = body['status']
        self.assertEqual((st['files_changed'], st['ahead'], st['untracked']), (2, 1, 1))
        self.assertTrue(body['live'])
        self.assertEqual(body['remove_blocked'], 'live')
        self.assertTrue(body['branch_exists'])
        self.assertIsNone(body['push_remote'])         # no remotes configured
        # The GET wrote the snapshot lists read.
        self.assertEqual(CTM.read_meta(tid)['worktree']['stat']['files_changed'], 2)

        status, body = self.call(
            'GET', f'/api/claude/tasks/{tid}/worktree/diff?file=feature.txt')
        self.assertEqual(status, 200)
        self.assertIn('+two', body['diff'])
        status, body = self.call(
            'GET', f'/api/claude/tasks/{tid}/worktree/diff?file=f0.txt')
        self.assertEqual((status, body['code']), (400, 'not_changed'))

        status, body = self.call('DELETE', f'/api/claude/tasks/{tid}/worktree?force=1')
        self.assertEqual((status, body['code']), (409, 'live'))
        self.live.clear()
        status, body = self.call('DELETE', f'/api/claude/tasks/{tid}/worktree')
        self.assertEqual((status, body['code'], body['untracked']), (409, 'dirty', 1))
        status, body = self.call('DELETE', f'/api/claude/tasks/{tid}/worktree?force=1')
        self.assertEqual(status, 200)
        self.assertTrue(body['branch_kept'])
        self.assertFalse(os.path.exists(wt['path']))
        self.assertIn(wt['branch'], self.kc_branches())
        self.assertTrue(CTM.read_meta(tid)['worktree']['removed_at'])
        status, body = self.call('DELETE', f'/api/claude/tasks/{tid}/worktree')
        self.assertEqual((status, body.get('already')), (200, True))
        status, body = self.call('GET', f'/api/claude/tasks/{tid}/worktree')
        self.assertEqual((status, body['exists'], body['remove_blocked']),
                         (200, False, 'removed'))

    def test_not_found_cases(self):
        _s, plain = self.post_task()
        status, body = self.call('GET', f'/api/claude/tasks/{plain["task_id"]}/worktree')
        self.assertEqual((status, body['code']), (404, 'no_worktree'))
        status, body = self.call('GET', '/api/claude/tasks/nope-123/worktree')
        self.assertEqual((status, body['code']), (404, 'not_found'))
        status, body = self.call('DELETE', '/api/claude/tasks/nope-123/worktree')
        self.assertEqual(status, 404)

    def test_registry_list_remove_and_sweep(self):
        _s, a = self.post_task(isolate=True)
        _s, b = self.post_task(isolate=True)
        self.live.add(a['tmux_session'])
        status, body = self.call('GET', '/api/worktrees')
        self.assertEqual(status, 200)
        self.assertEqual((body['count'], body['max']), (2, 20))
        rows = {r['task_id']: r for r in body['worktrees']}
        self.assertTrue(rows[a['task_id']]['live'])
        self.assertFalse(rows[b['task_id']]['live'])
        self.assertEqual(rows[b['task_id']]['repo'], 'app')

        status, body = self.call('DELETE', '/api/worktrees/..%2F..%2Fetc/passwd')
        self.assertIn(status, (400, 404))
        status, body = self.call('DELETE', '/api/worktrees/app/Not_A_Slug')
        self.assertEqual(status, 400)

        CTM.delete_task(b['task_id'])
        with mock.patch.dict(os.environ, {'KC_WORKTREE_GRACE_S': '0'}):
            status, body = self.call('POST', '/api/worktrees/sweep', {'dry_run': True})
            self.assertEqual(status, 200)
            self.assertEqual([r['task_id'] for r in body['removed']], [b['task_id']])
            self.assertTrue(os.path.isdir(b['worktree']['path']))
            status, body = self.call('POST', '/api/worktrees/sweep', {})
        self.assertEqual([r['reason'] for r in body['removed']], ['pristine'])
        self.assertFalse(os.path.exists(b['worktree']['path']))
        self.assertTrue(CTM.read_meta(b['task_id'])['worktree']['removed_at'])
        kept = {k['path']: k['reason'] for k in body['kept']}
        self.assertEqual(kept[a['worktree']['path']], 'live')
        status, body = self.call('GET', '/api/worktrees')
        self.assertEqual(body['count'], 1)
        self.assertEqual(body['worktrees'][0]['keep_reason'], 'live')

        self.live.clear()
        slug = a['worktree']['slug']
        status, body = self.call('DELETE', f'/api/worktrees/app/{slug}')
        self.assertEqual(status, 200)
        self.assertFalse(os.path.exists(a['worktree']['path']))
        self.assertTrue(CTM.read_meta(a['task_id'])['worktree']['removed_at'])

    def test_auth_and_readonly(self):
        _s, task = self.post_task(isolate=True)
        tid = task['task_id']
        self.authed = False
        for method, path in (('GET', f'/api/claude/tasks/{tid}/worktree'),
                             ('GET', '/api/worktrees'),
                             ('DELETE', f'/api/claude/tasks/{tid}/worktree')):
            self.assertEqual(self.call(method, path)[0], 401, path)
        self.authed = True
        with mock.patch.object(server, 'READONLY_MODE', True):
            self.assertEqual(self.call('DELETE', f'/api/claude/tasks/{tid}/worktree')[0], 403)
            self.assertEqual(self.call('POST', '/api/worktrees/sweep', {})[0], 403)
            self.assertEqual(self.call('GET', f'/api/claude/tasks/{tid}/worktree')[0], 200)
        self.assertTrue(os.path.isdir(task['worktree']['path']))

    def test_a_public_demo_never_serves_file_contents(self):
        _s, task = self.post_task(isolate=True)
        tid = task['task_id']
        commit(task['worktree']['path'], 'secret.txt', 'api_key=abc')
        self.authed, self.public = False, True
        self.assertEqual(self.call('GET', f'/api/claude/tasks/{tid}/worktree')[0], 200)
        status, _body = self.call(
            'GET', f'/api/claude/tasks/{tid}/worktree/diff?file=secret.txt')
        self.assertEqual(status, 401)


if __name__ == '__main__':
    unittest.main()
