"""Publication contracts against real Git, a local bare remote and Linux flock.

Only GitHub and the model boundary are stubbed. No real account is contacted.
"""
import json
import os
from pathlib import Path
import threading
from unittest import mock

from tests.git_fixtures import GitTestCase, git, make_repo, make_remote, write
import worktrees
import publish_git as pg
from build_publish import Publisher, read


class FakeGitHub:
    def __init__(self, destination):
        self.destination = destination
        self.pr = None
        self.creates = 0
        self.fail = False

    def resolve(self, meta, selection=None):
        return dict(self.destination)

    def identity(self):
        return self.destination['identity']

    def git_env(self):
        return {}

    def find(self, d):
        if self.pr is None:
            return None
        return {**self.pr, 'html_url': self.pr['url'],
                'head': {'sha': self.pr['head_sha'], 'ref': d['head_branch'],
                         'repo': {'id': d['head_repo_id']}},
                'base': {'ref': d['base_branch'], 'repo': {'id': d['base_repo_id']}}}

    def publish(self, d, op):
        if self.fail:
            raise pg.PublishError('github_unavailable', 'Offline')
        if self.pr is None:
            self.creates += 1
            self.pr = {'number': 9, 'url': 'https://github.com/acme/app/pull/9',
                       'state': 'open', 'draft': op['draft'], 'repo': d['base_repo']}
        self.pr['head_sha'] = op['commit_sha']
        return self.pr


class PublishFixture(GitTestCase):
    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.home)
        self.remote = make_remote(self.tmp, self.repo)
        git(self.repo, 'push', 'origin', 'main')
        repo = worktrees.resolve_repo(self.repo, home_root=self.home, wt_root=self.wt_root)
        wt = worktrees.ensure(repo, 'publish', wt_root=self.wt_root, task_id='t1',
                              is_owner_live=lambda _: False)
        self.path = wt['path']
        self.meta = {'task_id': 't1', 'worktree': wt, 'prompt': 'Fix the app'}
        self.dest = {'push_url': self.remote, 'remote': 'origin', 'identity': 'personal:1',
                     'head_repo': 'acme/app', 'head_repo_id': 1, 'head_branch': wt['branch'],
                     'base_repo': 'acme/app', 'base_repo_id': 1, 'base_branch': 'main',
                     'base_sha': git(self.repo, 'rev-parse', 'HEAD')}
        self.github = FakeGitHub(self.dest)
        self.events = []
        self.root = Path(self.tmp) / 'publish'
        self.service = self.new_service()
        write(os.path.join(self.path, 'f0.txt'), 'reviewed\n')

    def new_service(self):
        return Publisher(self.root, lambda _: self.meta, lambda _: None,
                         wt_root=self.wt_root, github=self.github, background=False,
                         summary=lambda p, prompt: {'title': 'Fix app', 'body': 'Reviewed change',
                                                    'summary_status': 'ready'},
                         emit=lambda m, e, s: self.events.append(e))

    def prepare(self):
        status = self.service.prepare('t1')
        self.assertIsNone(status['preparation_error'], status)
        self.assertIsNotNone(status['preparation'])
        return status['preparation']

    def submit(self, p=None, key='request-1'):
        p = p or self.prepare()
        body = {'idempotency_key': key, 'preparation_id': p['id'],
                'fingerprint': p['fingerprint'], 'draft_revision': p['draft_revision']}
        self.service.submit('t1', body)
        return body

class PublishTests(PublishFixture):
    def test_independent_publishers_serialize_and_block_worktree_reuse(self):
        import multiprocessing
        context = multiprocessing.get_context('fork')
        entered, release = context.Event(), context.Event()
        self.submit()
        before = git(self.path, 'rev-parse', 'HEAD')
        calls = self.root / 'github-effects.txt'
        def run():
            service = self.new_service()
            identity = service.github.identity
            publish = service.github.publish
            def pause():
                entered.set()
                if not release.wait(15):
                    raise RuntimeError('Test did not release worker')
                return identity()
            def record_effect(destination, operation):
                with calls.open('a') as output:
                    output.write(operation['commit_sha'] + '\n')
                return publish(destination, operation)
            service.github.identity = pause
            service.github.publish = record_effect
            service.run('t1')
        workers = [context.Process(target=run) for _ in range(2)]
        try:
            for worker in workers:
                worker.start()
            self.assertTrue(entered.wait(10))
            repo = worktrees.resolve_repo(self.repo, home_root=self.home, wt_root=self.wt_root)
            with self.assertRaises(worktrees.WorktreeError):
                worktrees.ensure(repo, 'publish', wt_root=self.wt_root,
                                 task_id='replacement', is_owner_live=lambda _: False)
            runner = mock.Mock()
            self.assertEqual(worktrees.launch_writer(self.path, ['tmux', 'new-session'],
                runner=runner, wt_root=self.wt_root).returncode, 1)
            runner.assert_not_called()
        finally:
            release.set()
            for worker in workers:
                worker.join(20)
                if worker.is_alive():
                    worker.kill()
                    worker.join()
        for worker in workers:
            self.assertEqual(worker.exitcode, 0)
        status = self.service.status('t1')
        self.assertEqual(status['operation']['stage'], 'published', status)
        self.assertEqual(git(self.path, 'rev-list', '--count', before + '..HEAD'), '1')
        self.assertEqual(calls.read_text().splitlines(), [status['operation']['commit_sha']])

    def test_probe_and_preparation_share_one_readiness_event(self):
        self.service.queue_probe('t1')
        self.service.probe('t1')
        self.service.deliver('t1')
        self.prepare()
        self.service.deliver('t1')
        self.assertEqual([event['event'] for event in self.events], ['ready'])

    def test_description_saved_during_generation_wins_over_ai(self):
        first = self.prepare()
        def generate(prepared, prompt):
            self.service.draft('t1', {'preparation_id': first['id'],
                'draft_revision': first['draft_revision'], 'title': 'Human title',
                'body': 'Human summary', 'draft': True})
            return {'title': 'Generated title', 'body': 'Generated summary', 'summary_status': 'ready'}
        self.service.summary = generate
        refreshed = self.prepare()
        self.assertEqual(refreshed['title'], 'Human title')
        self.assertEqual(refreshed['body'], 'Human summary')
        self.assertTrue(refreshed['draft'])
        self.assertNotEqual(refreshed['id'], first['id'])

    def test_process_death_after_each_durable_publish_stage(self):
        import multiprocessing
        import build_publish
        for stage in ('validating', 'committing', 'pushing', 'creating_pr', 'published'):
            with self.subTest(stage=stage):
                fixture = PublishFixture()
                fixture.setUp()
                try:
                    fixture.submit()
                    before = git(fixture.path, 'rev-parse', 'HEAD')
                    def crash():
                        write_state = build_publish.write
                        def stop(path, value):
                            write_state(path, value)
                            if (value.get('operation') or {}).get('stage') == stage:
                                os._exit(72)
                        with mock.patch.object(build_publish, 'write', side_effect=stop):
                            fixture.service.run('t1')
                    worker = multiprocessing.get_context('fork').Process(target=crash)
                    worker.start()
                    worker.join(20)
                    if worker.is_alive():
                        worker.kill()
                        worker.join()
                    self.assertEqual(worker.exitcode, 72)
                    resumed = fixture.new_service()
                    resumed.run('t1')
                    status = resumed.status('t1')
                    self.assertEqual(status['operation']['stage'], 'published', status)
                    self.assertEqual(git(fixture.path, 'rev-list', '--count', before + '..HEAD'), '1')
                    self.assertEqual(pg.remote_sha(fixture.repo, fixture.remote,
                        fixture.dest['head_branch']), status['operation']['commit_sha'])
                    self.assertEqual(git(fixture.repo, 'status', '--porcelain'), '')
                    self.assertFalse(worktrees.read_manifest(fixture.path).get('publication'))
                finally:
                    fixture.doCleanups()

    def test_publish_exact_snapshot_with_tricky_paths_and_duplicate_requests(self):
        for name in ['space name.txt', '-option', 'new\nline.txt', '日本語.txt']:
            write(os.path.join(self.path, name), 'included\n')
        write(os.path.join(self.path, '.gitignore'), 'secret\n')
        write(os.path.join(self.path, 'secret'), 'excluded\n')
        body = self.submit()
        threads = [threading.Thread(target=self.service.run, args=('t1',)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        status = self.service.status('t1')
        self.assertEqual(status['operation']['stage'], 'published', status)
        sha = status['operation']['commit_sha']
        self.assertEqual(pg.remote_sha(self.repo, self.remote, self.dest['head_branch']), sha)
        self.assertEqual(git(self.path, 'status', '--porcelain'), '')
        self.assertEqual(self.github.creates, 1)
        self.service.submit('t1', body)
        self.service.run('t1')
        self.assertEqual(self.github.creates, 1)
        self.assertEqual(git(self.path, 'rev-list', '--count', 'main..HEAD'), '1')
        self.assertNotIn('secret', git(self.path, 'ls-tree', '-r', '--name-only', 'HEAD').splitlines())
        self.assertFalse(worktrees.read_manifest(self.path).get('publication'))

    def test_changed_content_after_review_never_publishes_and_can_refresh(self):
        self.submit()
        write(os.path.join(self.path, 'f0.txt'), 'later edit\n')
        self.service.run('t1')
        status = self.service.status('t1')
        self.assertEqual(status['operation']['error']['code'], 'stale_preparation')
        self.assertEqual(pg.remote_sha(self.repo, self.remote, self.dest['head_branch']), '')
        self.submit(key='fresh')
        self.service.run('t1')
        self.assertEqual(self.service.status('t1')['operation']['stage'], 'published')

    def test_restart_after_push_preserves_commit_and_creates_one_pr(self):
        self.github.fail = True
        self.submit()
        self.service.run('t1')
        op = self.service.status('t1')['operation']
        self.assertEqual(op['stage'], 'failed')
        self.assertEqual(pg.remote_sha(self.repo, self.remote, self.dest['head_branch']), op['commit_sha'])
        self.service = self.new_service()
        self.github.fail = False
        self.service.retry('t1', op['id'])
        self.service.run('t1')
        status = self.service.status('t1')
        self.assertEqual(status['operation']['stage'], 'published', status)
        self.assertEqual(status['operation']['commit_sha'], op['commit_sha'])
        self.assertEqual(self.github.creates, 1)

    def test_draft_revision_and_idempotency_conflict(self):
        p = self.prepare()
        edit = {'preparation_id': p['id'], 'draft_revision': 1,
                'title': 'My title', 'body': 'My text', 'draft': True}
        changed = self.service.draft('t1', edit)['preparation']
        self.assertEqual(changed['draft_revision'], 2)
        with self.assertRaisesRegex(pg.PublishError, 'another device'):
            self.service.draft('t1', edit)
        body = self.submit(changed)
        with self.assertRaisesRegex(pg.PublishError, 'different content'):
            self.service.submit('t1', dict(body, draft_revision=3))

    def test_preparation_preserves_partial_staging_and_diff_is_immutable(self):
        git(self.path, 'add', 'f0.txt')
        write(os.path.join(self.path, 'f0.txt'), 'unstaged\n')
        before = git(self.path, 'diff', '--cached')
        p = self.prepare()
        self.assertEqual(git(self.path, 'diff', '--cached'), before)
        write(os.path.join(self.path, 'f0.txt'), 'newer\n')
        diff = self.service.reviewed_diff('t1', p['id'], 'f0.txt')['diff']
        self.assertIn('+unstaged', diff)
        self.assertNotIn('+newer', diff)

    def test_foreign_index_lock_is_never_deleted(self):
        self.submit()
        lock = Path(pg.text(self.path, 'rev-parse', '--path-format=absolute', '--git-path', 'index.lock'))
        lock.write_bytes(b'foreign')
        self.service.run('t1')
        self.assertEqual(lock.read_bytes(), b'foreign')
        self.assertEqual(self.service.status('t1')['operation']['stage'], 'failed')

    def test_corrupt_state_fails_closed(self):
        self.submit()
        self.service.path('t1').write_text('{broken')
        with self.assertRaisesRegex(pg.PublishError, 'unreadable'):
            self.service.submit('t1', {'idempotency_key': 'valid'})

    def test_owner_change_and_live_writer_block(self):
        with mock.patch.object(self.service, 'assert_idle', side_effect=pg.PublishError('writer_active', 'Active')):
            self.assertFalse(self.service.status('t1')['eligibility']['can_prepare'])
        manifest = worktrees.read_manifest(self.path)
        manifest['task_id'] = 'other'
        worktrees._write_json(os.path.join(self.path, worktrees.MANIFEST), manifest)
        self.assertEqual(self.service.status('t1')['eligibility']['reason']['code'], 'owner_changed')

    def test_reservation_blocks_removal_and_reuse(self):
        manifest = worktrees.read_manifest(self.path)
        manifest['publication'] = 'pub-retained'
        worktrees._write_json(os.path.join(self.path, worktrees.MANIFEST), manifest)
        repo = worktrees.resolve_repo(self.repo, home_root=self.home, wt_root=self.wt_root)
        with self.assertRaisesRegex(worktrees.WorktreeError, 'published'):
            worktrees.ensure(repo, 'publish', wt_root=self.wt_root, task_id='t1', is_owner_live=lambda _: False)

    def test_ai_failure_uses_manual_editable_fallback(self):
        from publish_summary import generate
        with mock.patch('publish_summary._RUN', side_effect=FileNotFoundError()):
            self.service.summary = generate
            self.assertEqual(self.prepare()['summary_status'], 'manual')

    def test_process_death_between_ref_and_index_recovers_without_second_commit(self):
        import multiprocessing
        self.submit()
        index = pg.text(self.path, 'rev-parse', '--path-format=absolute', '--git-path', 'index')
        def crash():
            replace = os.replace
            def stop(src, dst):
                if str(dst) == index:
                    os._exit(71)
                return replace(src, dst)
            with mock.patch('os.replace', side_effect=stop):
                self.service.run('t1')
        process = multiprocessing.get_context('fork').Process(target=crash)
        process.start()
        process.join(20)
        self.assertEqual(process.exitcode, 71)
        sha = git(self.path, 'rev-parse', 'HEAD')
        self.assertTrue(Path(index + '.lock').exists())
        self.service = self.new_service()
        self.service.run('t1')
        status = self.service.status('t1')
        self.assertEqual(status['operation']['stage'], 'published', status)
        self.assertEqual(status['operation']['commit_sha'], sha)
        self.assertEqual(git(self.path, 'status', '--porcelain'), '')
        self.assertFalse(Path(index + '.lock').exists())

    def test_remote_divergence_never_forces_push(self):
        self.submit()
        # A separate writer creates a conflicting head on the same remote ref.
        git(self.repo, 'checkout', '-b', 'other')
        from tests.git_fixtures import commit
        remote_tip = commit(self.repo, 'other.txt', 'other writer\n')
        git(self.repo, 'push', 'origin', 'HEAD:refs/heads/' + self.dest['head_branch'])
        self.service.run('t1')
        self.assertEqual(self.service.status('t1')['operation']['error']['code'], 'push_failed')
        self.assertEqual(pg.remote_sha(self.repo, self.remote, self.dest['head_branch']), remote_tip)

    def test_committed_work_needs_no_empty_commit(self):
        git(self.path, 'add', 'f0.txt')
        git(self.path, 'commit', '-m', 'Agent already committed')
        before = git(self.path, 'rev-parse', 'HEAD')
        self.submit()
        self.service.run('t1')
        self.assertEqual(self.service.status('t1')['operation']['commit_sha'], before)

    def test_new_writer_launch_is_blocked_during_publication(self):
        manifest = worktrees.read_manifest(self.path)
        manifest['publication'] = 'pub-active'
        worktrees._write_json(os.path.join(self.path, worktrees.MANIFEST), manifest)
        runner = mock.Mock()
        result = worktrees.launch_writer(self.path, ['tmux', 'new-session'], runner=runner, wt_root=self.wt_root)
        self.assertEqual(result.returncode, 1)
        runner.assert_not_called()

    def test_abandoned_preparation_can_be_recovered(self):
        p = self.prepare()
        with self.service.record('t1') as state:
            state['preparing'] = 'crashed-process'
        self.new_service().recover_preparation('t1')
        status = self.service.status('t1')
        self.assertIsNone(status['preparing'])
        self.assertEqual(status['preparation']['id'], p['id'])
        self.assertEqual(status['preparation_error']['code'], 'preparation_interrupted')

    def test_http_auth_readonly_and_publish_contract(self):
        import http.server
        import urllib.request
        import urllib.error
        import server
        from tests.live_state import isolate_feed_and_push
        isolate_feed_and_push(self)
        self.authed = True
        def auth(handler, **kw):
            return self.authed
        with mock.patch.object(server, '_PUBLISHER', self.service), \
             mock.patch.object(server.BrowserHandler, 'check_claude_auth', auth), \
             mock.patch.object(server, 'READONLY_MODE', False):
            httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), server.BrowserHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            def call(method, suffix='', body=None):
                url = f'http://127.0.0.1:{httpd.server_port}/api/claude/tasks/t1/publish' + suffix
                data = json.dumps(body).encode() if body is not None else None
                request = urllib.request.Request(url, data=data, method=method, headers={'Content-Type': 'application/json'})
                try:
                    with urllib.request.urlopen(request, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as e:
                    with e:
                        return e.code, json.load(e)
            try:
                self.authed = False
                self.assertEqual(call('GET')[0], 401)
                self.authed = True
                with mock.patch.object(server, 'READONLY_MODE', True):
                    self.assertEqual(call('POST', '/prepare', {})[0], 403)
                    self.assertEqual(call('GET')[0], 200)
                code, status = call('POST', '/prepare', {})
                self.assertEqual(code, 202, status)
                p = status['preparation']
                code, result = call('GET', '/diff?preparation_id=' + p['id'] + '&file=f0.txt')
                self.assertEqual(code, 200, result)
                self.assertIn('+reviewed', result['diff'])
                code, status = call('POST', '', {'idempotency_key': 'http-request', 'preparation_id': p['id'],
                    'fingerprint': p['fingerprint'], 'draft_revision': p['draft_revision']})
                self.assertEqual(code, 202, status)
                self.service.run('t1')
                self.assertEqual(call('GET')[1]['operation']['stage'], 'published')
            finally:
                httpd.shutdown()
                httpd.server_close()
