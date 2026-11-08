"""GitHub publishing transport. Credentials are read anew for each stage."""
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from publish_git import PublishError, git, text

_RUN = subprocess.run
_REPO = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')


def repository(url):
    m = re.fullmatch(r'(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^\s?#]+?)(?:\.git)?/?', url)
    if not m or not _REPO.fullmatch(m[1]) or '..' in m[1].split('/'):
        raise PublishError('destination_required', 'Choose a GitHub remote without embedded credentials.')
    return m[1]


class GitHub:
    def __init__(self, mode_file='/home/dev/.credentials/.github-auth-mode',
                 token_file='/home/dev/.credentials/.github-token', home='/home/dev'):
        self.mode_file, self.token_file, self.home = mode_file, token_file, home

    def credentials(self):
        try:
            mode = Path(self.mode_file).read_text().strip()
        except OSError:
            mode = 'app'
        mode = 'personal' if mode == 'personal' else 'app'
        if mode == 'app':
            try:
                token = Path(self.token_file).read_text().strip()
            except OSError:
                token = ''
        else:
            env = {k: v for k, v in os.environ.items() if k not in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_HOST')}
            env['HOME'] = self.home
            try:
                r = _RUN(['gh', 'auth', 'token', '--hostname', 'github.com'], env=env,
                         capture_output=True, text=True, timeout=10)
                token = r.stdout.strip() if r.returncode == 0 else ''
            except (OSError, subprocess.TimeoutExpired):
                token = ''
        if not token:
            raise PublishError('auth_required', 'Connect GitHub in Settings before publishing.', 403)
        return mode, token

    def request(self, path, body=None):
        _, token = self.credentials()
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request('https://api.github.com/' + path, data=data,
                                     headers={'Authorization': 'Bearer ' + token,
                                              'Accept': 'application/vnd.github+json',
                                              'Content-Type': 'application/json',
                                              'User-Agent': 'kube-coder-publishing'})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 429):
                limited = e.code == 429 or e.headers.get('Retry-After') or e.headers.get('X-RateLimit-Remaining') == '0'
                raise PublishError('rate_limited' if limited else 'permission_denied',
                                   'GitHub rate limit reached; retry later.' if limited else
                                   'GitHub access was refused. Check the selected account and repository permissions.', 403) from e
            raise PublishError('github_validation' if e.code == 422 else 'github_unavailable',
                               'GitHub could not complete the request. Retry will check for an existing PR.', 503) from e
        except (OSError, ValueError) as e:
            raise PublishError('github_unavailable', 'GitHub is unreachable; retry will check what succeeded.', 503) from e

    def identity(self):
        mode, _ = self.credentials()
        if mode == 'personal':
            return 'personal:' + str(self.request('user')['id'])
        # Installation tokens do not support GET /user. The managed App identity
        # is configured independently of its hourly rotating token.
        app, installation = os.environ.get('GITHUB_APP_ID'), os.environ.get('GITHUB_APP_INSTALLATION_ID')
        if not app or not installation:
            raise PublishError('auth_required', 'The GitHub App installation identity is unavailable.', 403)
        return 'app:' + app + ':' + installation

    def git_env(self):
        _, token = self.credentials()
        auth = base64.b64encode(('x-access-token:' + token).encode()).decode()
        return {'HOME': self.home, 'GIT_CONFIG_COUNT': '3',
                'GIT_CONFIG_KEY_0': 'http.https://github.com/.extraheader', 'GIT_CONFIG_VALUE_0': '',
                'GIT_CONFIG_KEY_1': 'http.https://github.com/.extraheader',
                'GIT_CONFIG_VALUE_1': 'AUTHORIZATION: basic ' + auth,
                'GIT_CONFIG_KEY_2': 'credential.helper', 'GIT_CONFIG_VALUE_2': ''}

    def resolve(self, meta, selection=None):
        wt = meta['worktree']
        path, branch = wt['path'], wt['branch']
        remotes = {}
        for name in text(path, 'remote').splitlines():
            try:
                remotes[name] = repository(text(path, 'remote', 'get-url', '--push', name))
            except PublishError:
                continue
        if not remotes:
            raise PublishError('destination_required', 'Add a GitHub remote to this project first.')
        selection = selection or {}
        selected = selection.get('remote') or text(path, 'config', f'branch.{branch}.pushRemote', check=False) or text(path, 'config', 'remote.pushDefault', check=False)
        if not selected:
            selected = next(iter(remotes)) if len(remotes) == 1 else ('fork' if 'fork' in remotes else '')
        if selected not in remotes:
            raise PublishError('destination_required', 'Select a push remote: ' + ', '.join(remotes))
        head = self.request('repos/' + remotes[selected])
        base_name = selection.get('base_repo') or ((head.get('parent') or {}).get('full_name')) or head['full_name']
        allowed = set(remotes.values()) | {head['full_name'], (head.get('parent') or {}).get('full_name')}
        if base_name not in allowed:
            raise PublishError('destination_required', 'The base must be a configured repository or fork parent.', 400)
        base = self.request('repos/' + base_name)
        if head.get('archived') or base.get('archived'):
            raise PublishError('permission_denied', 'Archived repositories cannot be published.', 403)
        if head.get('permissions', {}).get('push') is False:
            raise PublishError('permission_denied', 'Choose a configured fork or account with push access.', 403)
        ref = selection.get('base_branch') or wt.get('base_ref') or base['default_branch']
        for prefix in ('refs/remotes/', 'refs/heads/'):
            if ref.startswith(prefix):
                ref = ref[len(prefix):]
        for name in remotes:
            if ref.startswith(name + '/'):
                ref = ref[len(name) + 1:]
                break
        if ref == 'HEAD' or re.fullmatch(r'[0-9a-f]{40,64}', ref):
            ref = base['default_branch']
        if git(path, 'check-ref-format', 'refs/heads/' + ref, check=False).returncode:
            raise PublishError('destination_required', 'Choose a valid base branch.', 400)
        if base['full_name'].lower() == head['full_name'].lower() and ref == branch:
            raise PublishError('destination_required', 'The PR base cannot be the Build branch.')
        base_info = self.request('repos/' + base['full_name'] + '/branches/' + urllib.parse.quote(ref, safe=''))
        sha = base_info['commit']['sha']
        git(path, 'fetch', '--no-tags', 'https://github.com/' + base['full_name'] + '.git',
            'refs/heads/' + ref, env=self.git_env(), timeout=120)
        return {'head_repo': head['full_name'], 'head_repo_id': head['id'],
                'head_branch': branch, 'base_repo': base['full_name'], 'base_repo_id': base['id'],
                'base_branch': ref, 'base_sha': sha, 'remote': selected,
                'push_url': 'https://github.com/' + head['full_name'] + '.git',
                'identity': self.identity()}

    def find(self, destination):
        d = destination
        query = urllib.parse.urlencode({'state': 'all', 'head': d['head_repo'].split('/')[0] + ':' + d['head_branch'],
                                        'base': d['base_branch'], 'per_page': 100, 'sort': 'created', 'direction': 'desc'})
        rows = self.request('repos/' + d['base_repo'] + '/pulls?' + query)
        for row in rows:
            if ((row.get('head', {}).get('repo') or {}).get('id') == d['head_repo_id']
                    and row['head']['ref'] == d['head_branch'] and row['base']['ref'] == d['base_branch']):
                return row
        return None

    def publish(self, destination, operation):
        d = destination
        row = self.find(d)
        if row and row['state'] != 'open':
            raise PublishError('pr_closed', 'This branch already has a closed or merged PR. Start a new Build branch for new work.')
        if row is None:
            body = {'head': d['head_repo'].split('/')[0] + ':' + d['head_branch'],
                    'head_repo': d['head_repo'].split('/')[1], 'base': d['base_branch'],
                    'title': operation['title'], 'body': operation['body'], 'draft': operation['draft']}
            try:
                row = self.request('repos/' + d['base_repo'] + '/pulls', body)
            except PublishError:
                row = self.find(d)
                if row is None:
                    raise
        row = self.request('repos/' + d['base_repo'] + '/pulls/' + str(row['number']))
        if (row['head']['sha'] != operation['commit_sha'] or
                (row['head'].get('repo') or {}).get('id') != d['head_repo_id'] or
                row['head']['ref'] != d['head_branch'] or row['base']['ref'] != d['base_branch'] or
                (row['base'].get('repo') or {}).get('id') != d['base_repo_id']):
            raise PublishError('remote_diverged', 'The PR head changed; review the remote branch.')
        return {'number': row['number'], 'url': row['html_url'], 'state': row['state'],
                'draft': row['draft'], 'head_sha': row['head']['sha'], 'repo': d['base_repo']}
