import json
import os
from pathlib import Path
import urllib.error
from unittest import TestCase, mock

from publish_github import GitHub, repository
from publish_git import PublishError
from tests.git_fixtures import GitTestCase, git, make_repo
import publish_git


class GitHubTests(TestCase):
    def setUp(self):
        self.github = GitHub()
        self.d = {'head_repo': 'me/project', 'head_repo_id': 1, 'head_branch': 'kc/change',
                  'base_repo': 'team/project', 'base_branch': 'develop', 'base_repo_id': 2}
        self.row = {'number': 5, 'html_url': 'https://github.com/team/project/pull/5', 'state': 'open',
                    'draft': False, 'head': {'sha': 'abc', 'repo': {'id': 1}, 'ref': 'kc/change'},
                    'base': {'ref': 'develop', 'repo': {'id': 2}}}

    def test_remote_parsing_rejects_embedded_credentials_and_non_github(self):
        for url in ['https://github.com/me/project.git', 'git@github.com:me/project.git', 'ssh://git@github.com/me/project']:
            self.assertEqual(repository(url), 'me/project')
        for url in ['https://token@github.com/me/project', 'https://evil.test/me/project', 'https://github.com/../project']:
            with self.assertRaises(PublishError):
                repository(url)

    def test_lost_create_response_discovers_and_verifies_existing_pr(self):
        calls = []
        def request(path, body=None):
            calls.append((path, body))
            if body is not None:
                raise PublishError('github_unavailable', 'response lost')
            if '/pulls?' in path:
                return [] if len(calls) == 1 else [self.row]
            return self.row
        with mock.patch.object(self.github, 'request', side_effect=request):
            pr = self.github.publish(self.d, {'title': 'Title', 'body': 'Body', 'draft': False, 'commit_sha': 'abc'})
        self.assertEqual(pr['number'], 5)
        self.assertEqual(sum(body is not None for _, body in calls), 1)

    def test_closed_pr_is_not_recreated(self):
        self.row['state'] = 'closed'
        with mock.patch.object(self.github, 'request', return_value=[self.row]) as request:
            with self.assertRaisesRegex(PublishError, 'closed or merged'):
                self.github.publish(self.d, {})
        self.assertEqual(request.call_count, 1)

    def test_head_from_a_different_repo_does_not_match(self):
        self.row['head']['repo']['id'] = 99
        with mock.patch.object(self.github, 'request', return_value=[self.row]):
            self.assertIsNone(self.github.find(self.d))

    def test_rate_limit_and_permission_errors_are_redacted(self):
        for status, headers, expected in [(403, {}, 'permission_denied'),
                                          (403, {'X-RateLimit-Remaining': '0'}, 'rate_limited'),
                                          (429, {'Retry-After': '10'}, 'rate_limited')]:
            error = urllib.error.HTTPError('https://api.github.com', status, 'SECRET', headers, None)
            with mock.patch.object(self.github, 'credentials', return_value=('personal', 'secret-token')), \
                 mock.patch('urllib.request.urlopen', side_effect=error):
                with self.assertRaises(PublishError) as raised:
                    self.github.request('user')
                self.assertEqual(raised.exception.code, expected)
                self.assertNotIn('secret', str(raised.exception).lower())


class DestinationTests(GitTestCase):
    def test_configured_fork_and_parent_release_branch(self):
        repo = make_repo(self.home)
        git(repo, 'remote', 'add', 'upstream', 'git@github.com:team/project.git')
        git(repo, 'remote', 'add', 'mine', 'git@github.com:me/project.git')
        git(repo, 'config', 'branch.kc/change.pushRemote', 'mine')
        adapter = GitHub()
        head = {'id': 1, 'full_name': 'me/project', 'default_branch': 'trunk',
                'parent': {'full_name': 'team/project'}, 'permissions': {'push': True}}
        base = {'id': 2, 'full_name': 'team/project', 'default_branch': 'develop'}
        replies = {'repos/me/project': head, 'repos/team/project': base,
                   'repos/team/project/branches/release%2Fnext': {'commit': {'sha': 'abc'}}}
        real_git = publish_git.git
        def transport(path, *args, **kwargs):
            if args[0] != 'fetch':
                return real_git(path, *args, **kwargs)
            self.assertEqual(args[2:], ('https://github.com/team/project.git', 'refs/heads/release/next'))
            self.assertEqual(kwargs['env'], {'TEST_AUTH': 'fresh'})
        with mock.patch.object(adapter, 'request', side_effect=lambda p: replies[p]), \
             mock.patch.object(adapter, 'identity', return_value='personal:7'), \
             mock.patch.object(adapter, 'git_env', return_value={'TEST_AUTH': 'fresh'}), \
             mock.patch('publish_github.git', side_effect=transport):
            result = adapter.resolve({'worktree': {'path': repo, 'branch': 'kc/change',
                                                   'base_ref': 'upstream/release/next'}})
        self.assertEqual(result['remote'], 'mine')
        self.assertEqual(result['base_branch'], 'release/next')
        self.assertEqual(result['head_repo_id'], 1)
        self.assertEqual(result['base_repo_id'], 2)
        self.assertEqual(result['push_url'], 'https://github.com/me/project.git')

    def test_ambiguous_remotes_require_selection(self):
        repo = make_repo(self.home)
        for name in ('alpha', 'beta'):
            git(repo, 'remote', 'add', name, 'https://github.com/' + name + '/project.git')
        adapter = GitHub()
        with mock.patch.object(adapter, 'request') as request:
            with self.assertRaises(PublishError) as raised:
                adapter.resolve({'worktree': {'path': repo, 'branch': 'kc/change'}})
        self.assertEqual(raised.exception.code, 'destination_required')
        request.assert_not_called()

    def test_app_identity_survives_token_rotation_but_requires_installation(self):
        adapter = GitHub()
        with mock.patch.object(adapter, 'credentials', return_value=('app', 'rotating-token')), \
             mock.patch.dict(os.environ, {'GITHUB_APP_ID': '11', 'GITHUB_APP_INSTALLATION_ID': '22'}):
            self.assertEqual(adapter.identity(), 'app:11:22')
        with mock.patch.object(adapter, 'credentials', return_value=('app', 'rotating-token')), \
             mock.patch.dict(os.environ, {'GITHUB_APP_ID': '11', 'GITHUB_APP_INSTALLATION_ID': ''}):
            with self.assertRaises(PublishError):
                adapter.identity()
