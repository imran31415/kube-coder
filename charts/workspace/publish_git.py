"""Git primitives for reviewed Build publication. No server import, shell, or AI."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading

import worktrees

_RUN = subprocess.run
MAX_BYTES = 32 * 1024 * 1024
MAX_FILES = 1000


class PublishError(Exception):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.status = code, status


def digest(value):
    return hashlib.sha256(value).hexdigest()


def fingerprint(value):
    return digest(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())


def command(path, args, env=None):
    child = worktrees._git_env()
    for key in list(child):
        if key.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_')) or key in (
                'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS', 'GIT_EXTERNAL_DIFF'):
            child.pop(key, None)
    if env:
        child.update(env)
    return (['git', '--no-optional-locks', '--literal-pathspecs',
             '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
             '-C', str(path), *args], child)


def git(path, *args, data=None, env=None, check=True, timeout=30):
    argv, child = command(path, args, env)
    try:
        r = _RUN(argv, input=data, capture_output=True,
                 timeout=timeout, env=child)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PublishError('git_unavailable', 'Git is unavailable or timed out.', 503) from e
    if check and r.returncode:
        # Do not echo remote URLs, credential-helper output or arbitrary stderr.
        raise PublishError('git_failed', f'Git {args[0]} failed; review the repository state.')
    return r


def text(path, *args, **kw):
    return git(path, *args, **kw).stdout.decode('utf-8', 'surrogateescape').strip()


def identity(meta):
    wt = meta.get('worktree') or {}
    path = wt.get('path') or ''
    if not path:
        raise PublishError('shared_checkout', 'Publishing requires an isolated Build worktree.')
    if wt.get('removed_at') or not os.path.isdir(path):
        raise PublishError('worktree_missing', 'This Build worktree has been removed.')
    manifest = worktrees.read_manifest(path) or {}
    if manifest.get('task_id') != meta['task_id']:
        raise PublishError('owner_changed', 'This worktree belongs to another Build now.')
    common = text(path, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    root = wt.get('repo_root') or ''
    if (not root or os.path.realpath(common) != os.path.realpath(
            text(root, 'rev-parse', '--path-format=absolute', '--git-common-dir'))):
        raise PublishError('owner_changed', 'The repository identity has changed.')
    registered = git(root, 'worktree', 'list', '--porcelain', '-z').stdout.split(b'\0')
    wanted = b'worktree ' + os.fsencode(os.path.realpath(path))
    if wanted not in registered:
        raise PublishError('owner_changed', 'The worktree is no longer registered.')
    branch = text(path, 'symbolic-ref', '-q', 'HEAD', check=False)
    if not branch:
        raise PublishError('detached_head', 'Choose the Build branch before publishing.')
    if branch != 'refs/heads/' + wt.get('branch', ''):
        raise PublishError('owner_changed', 'The Build branch changed; restore it before publishing.')
    for marker in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply'):
        if os.path.exists(text(path, 'rev-parse', '--path-format=absolute', '--git-path', marker)):
            raise PublishError('conflicts', 'Finish the in-progress Git operation first.')
    if git(path, 'ls-files', '-u', '-z').stdout:
        raise PublishError('conflicts', 'Resolve merge conflicts before publishing.')
    return {'path': os.path.realpath(path), 'common': os.path.realpath(common),
            'branch': branch, 'owner': manifest['task_id'],
            'history': manifest.get('history', []),
            'head': text(path, 'rev-parse', 'HEAD')}


def snapshot(meta, destination, directory, pin=''):
    ident = identity(meta)
    path = ident['path']
    index_before = git(path, 'ls-files', '--stage', '-z').stdout
    tracked = git(path, 'ls-files', '-z').stdout
    untracked = git(path, 'ls-files', '--others', '--exclude-standard', '-z').stdout
    # Even `diff --name-only` can execute a clean filter while comparing the
    # working tree. Inspect attributes BEFORE any content comparison/add.
    all_names = tracked + untracked
    attrs = git(path, 'check-attr', '-z', '--stdin', 'filter', data=all_names).stdout.split(b'\0') if all_names else []
    if any(v not in (b'unspecified', b'unset') for v in attrs[2::3]):
        raise PublishError('unsupported_git_feature', 'LFS or custom Git filters need manual publishing.')
    if text(path, 'config', '--bool', 'core.sparseCheckout', check=False) == 'true':
        raise PublishError('unsupported_git_feature', 'Sparse checkouts need manual publishing.')
    paths = set(git(path, 'diff', '--name-only', '--no-ext-diff', '--no-textconv', '-z', 'HEAD', '--').stdout.split(b'\0'))
    paths.update(untracked.split(b'\0'))
    paths.discard(b'')
    paths.difference_update(os.fsencode(p) for p in worktrees.OWN_FILES)
    if len(paths) > MAX_FILES:
        raise PublishError('too_large', 'More than 1,000 changed files; reduce the change first.', 413)
    total = 0
    for name in paths:
        f = os.path.join(path, os.fsdecode(name))
        if os.path.lexists(f):
            total += os.lstat(f).st_size
        if os.path.isdir(f) and not os.path.islink(f):
            raise PublishError('unsupported_git_feature', 'Submodule changes need manual publishing.')
    if total > MAX_BYTES:
        raise PublishError('too_large', 'Changed content exceeds the 32 MiB publishing limit.', 413)
    os.makedirs(directory, exist_ok=True)
    fd, idx = tempfile.mkstemp(dir=directory, prefix='candidate-')
    os.close(fd)
    os.unlink(idx)
    env = {'GIT_INDEX_FILE': idx}
    try:
        git(path, 'read-tree', ident['head'], env=env)
        if paths:
            git(path, 'add', '-A', '--pathspec-from-file=-', '--pathspec-file-nul',
                data=b'\0'.join(sorted(paths)) + b'\0', env=env)
        tree = text(path, 'write-tree', env=env)
    finally:
        if os.path.exists(idx):
            os.unlink(idx)
    if ident != identity(meta) or index_before != git(path, 'ls-files', '--stage', '-z').stdout:
        raise PublishError('stale_preparation', 'Git state changed while preparing. Review again.')
    base = destination['base_sha']
    merge_base = text(path, 'merge-base', base, ident['head'])
    raw = git(path, 'diff', '--numstat', '-z', '--no-renames', '--no-ext-diff',
              '--no-textconv', merge_base, tree, '--').stdout
    files = []
    for entry in raw.split(b'\0'):
        if not entry:
            continue
        add, delete, name = entry.split(b'\t', 2)
        files.append({'path': os.fsdecode(name), 'binary': add == b'-',
                      'added': int(add) if add != b'-' else None,
                      'deleted': int(delete) if delete != b'-' else None})
    if len(files) > MAX_FILES:
        raise PublishError('too_large', 'PR changes exceed 1,000 files.', 413)
    author = {'name': text(path, 'config', 'user.name', check=False),
              'email': text(path, 'config', 'user.email', check=False)}
    if tree != text(path, 'rev-parse', 'HEAD^{tree}') and not all(author.values()):
        raise PublishError('identity_required', 'Set your Git name and email in Settings first.')
    fields = {**ident, 'tree': tree, 'index': digest(index_before),
              'destination': destination, 'author': author,
              'signing': {k: text(path, 'config', k, check=False) for k in
                          ('commit.gpgsign', 'user.signingkey', 'gpg.format')}}
    if pin:
        git(path, 'update-ref', 'refs/kube-coder/publish/' + pin, tree)
    return {**fields, 'fingerprint': fingerprint(fields), 'merge_base': merge_base,
            'files': files, 'pin': pin}


def diff(prepared, file=None, limit=256 * 1024):
    names = [f['path'] for f in prepared['files']]
    if file is not None and file not in names:
        raise PublishError('not_changed', 'The file is not in this reviewed change.', 400)
    args = ['diff', '--no-color', '--no-ext-diff', '--no-textconv',
            prepared['merge_base'], prepared['tree'], '--']
    if file is not None:
        args.append(file)
    # Bound memory while reading, not after capturing a potentially huge
    # committed diff. The timer also bounds a blocked pipe read.
    argv, env = command(prepared['path'], args)
    expired = threading.Event()
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    except OSError as e:
        raise PublishError('git_unavailable', 'Git diff is unavailable.', 503) from e
    def stop():
        expired.set()
        process.kill()
    timer = threading.Timer(30, stop)
    timer.start()
    try:
        data = process.stdout.read(limit + 1)
        if len(data) > limit:
            process.kill()
        process.wait()
        if expired.is_set():
            raise PublishError('git_unavailable', 'Git diff timed out.', 503)
        if process.returncode and len(data) <= limit:
            raise PublishError('git_failed', 'The reviewed diff could not be read.')
    finally:
        timer.cancel()
        process.stdout.close()
    return {'diff': data[:limit].decode('utf-8', 'replace'), 'truncated': len(data) > limit}


def commit(prepared, operation, save, directory):
    """Journaled ref/index transaction. Does not modify working files."""
    path = prepared['path']
    current = text(path, 'rev-parse', prepared['branch'])
    if current not in (prepared['head'], operation.get('commit_sha')):
        raise PublishError('stale_preparation', 'The branch advanced during publishing.')
    if not operation.get('commit_sha'):
        if prepared['tree'] == text(path, 'rev-parse', prepared['head'] + '^{tree}'):
            operation['commit_sha'] = prepared['head']
            operation['commit_done'] = True
            save()
            return operation['commit_sha']
        env = {'GIT_AUTHOR_NAME': prepared['author']['name'],
               'GIT_AUTHOR_EMAIL': prepared['author']['email'],
               'GIT_COMMITTER_NAME': prepared['author']['name'],
               'GIT_COMMITTER_EMAIL': prepared['author']['email'],
               'GIT_AUTHOR_DATE': operation['commit_date'],
               'GIT_COMMITTER_DATE': operation['commit_date']}
        args = ['commit-tree', prepared['tree'], '-p', prepared['head']]
        if text(path, 'config', '--bool', 'commit.gpgsign', check=False) == 'true':
            args.append('-S')
        operation['commit_sha'] = text(path, *args, data=(operation['title'] + '\n').encode(), env=env)
        save()
    sha = operation['commit_sha']
    if current not in (prepared['head'], sha):
        raise PublishError('stale_preparation', 'The branch advanced during publishing.')
    if operation.get('commit_done'):
        return sha
    index = Path(text(path, 'rev-parse', '--path-format=absolute', '--git-path', 'index'))
    lock = Path(str(index) + '.lock')
    after = Path(directory) / 'index-after'
    if not operation.get('index_before'):
        if digest(git(path, 'ls-files', '--stage', '-z').stdout) != prepared['index']:
            raise PublishError('stale_preparation', 'The staging area changed after review. It has been preserved.')
        operation['index_before'] = digest(index.read_bytes()) if index.exists() else digest(b'')
        git(path, 'read-tree', prepared['tree'], env={'GIT_INDEX_FILE': str(after)})
        operation['index_after'] = digest(after.read_bytes())
        save()
    actual = digest(index.read_bytes()) if index.exists() else digest(b'')
    if actual == operation['index_after'] and current == sha:
        operation['commit_done'] = True
        save()
        return sha
    if actual != operation['index_before']:
        raise PublishError('recovery_required', 'The staging area changed; preserve it and review recovery.')
    if lock.exists():
        if not operation.get('index_lock_owned') or digest(lock.read_bytes()) != operation['index_after']:
            raise PublishError('recovery_required', 'Another Git index lock exists; no staging changes were made.')
    else:
        # Intent alone cannot authorize deleting a foreign lock; recovery also
        # checks its exact bytes and the branch/index recorded for this operation.
        operation['index_lock_owned'] = True
        save()
        try:
            with lock.open('xb') as f:
                f.write(after.read_bytes())
                f.flush()
                os.fsync(f.fileno())
        except FileExistsError as e:
            raise PublishError('recovery_required', 'Git is using the staging area.') from e
    if current != sha:
        git(path, 'update-ref', prepared['branch'], sha, prepared['head'])
    os.replace(lock, index)
    index_directory = os.open(index.parent, os.O_RDONLY)
    try:
        os.fsync(index_directory)
    finally:
        os.close(index_directory)
    operation['commit_done'] = True
    save()
    return sha


def remote_sha(path, remote, branch, env=None):
    out = text(path, 'ls-remote', '--heads', remote, 'refs/heads/' + branch, env=env)
    return out.split()[0] if out else ''


def push(prepared, sha, env=None):
    d = prepared['destination']
    remote, branch = d['push_url'], d['head_branch']
    if remote_sha(prepared['path'], remote, branch, env) == sha:
        return
    result = git(prepared['path'], 'push', '--porcelain', '--no-verify', remote,
                 sha + ':refs/heads/' + branch, env=env, check=False, timeout=120)
    # Even an error can be a lost response after an accepted push.
    observed = remote_sha(prepared['path'], remote, branch, env)
    if observed == sha:
        return
    if result.returncode:
        raise PublishError('push_failed', 'Push was rejected or interrupted. Check access and branch divergence; retry reconciles the remote.')
    raise PublishError('remote_diverged', 'The remote branch changed during publishing. Review it before continuing.')
