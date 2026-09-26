"""Real, throwaway git repositories for the worktree tests (#701).

Every other suite in this directory fakes git's on-disk layout by writing a
`.git` file by hand, which is right for code that only READS those files. The
worktree primitive runs git itself — `worktree add`, `update-ref`, `status` —
and the only honest test of that is git.

Two things keep these repos hermetic:
- `GIT_CONFIG_GLOBAL` points at a temp file and `GIT_CONFIG_NOSYSTEM=1`, so a
  developer's `commit.gpgsign`, `core.hooksPath` or `init.defaultBranch` can
  never make a test pass on one machine and fail on another;
- every repository lives under a fresh temp dir removed by the test.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

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

GIT = shutil.which('git')
requires_git = unittest.skipUnless(GIT, 'git is not installed')
posix_only = unittest.skipIf(os.name == 'nt', 'needs POSIX symlinks/bash')

# The real subprocess.run, captured before any test patches the module
# attribute — fixtures must keep working inside a test that stubs tmux.
_REAL_RUN = subprocess.run


def git_config_env(tmp):
    cfg = os.path.join(tmp, 'gitconfig')
    with open(cfg, 'w', encoding='utf-8') as f:
        f.write('[user]\n\tname = kc test\n\temail = kc@example.test\n'
                '[init]\n\tdefaultBranch = main\n'
                '[commit]\n\tgpgsign = false\n'
                '[advice]\n\tdetachedHead = false\n')
    return {'GIT_CONFIG_GLOBAL': cfg, 'GIT_CONFIG_NOSYSTEM': '1'}


def git(cwd, *args, check=True):
    proc = _REAL_RUN(['git', '-C', cwd, *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f'git {" ".join(args)} failed: {proc.stderr}')
    return proc.stdout.strip()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def commit(repo, name='file.txt', text='x\n', msg='change'):
    write(os.path.join(repo, name), text)
    git(repo, 'add', '--', name)
    git(repo, 'commit', '-q', '-m', msg)
    return git(repo, 'rev-parse', 'HEAD')


def make_repo(parent, name='app', commits=1, empty=False):
    path = os.path.join(parent, name)
    os.makedirs(path)
    git(path, 'init', '-q', '-b', 'main')
    if not empty:
        for i in range(commits):
            commit(path, f'f{i}.txt', f'line {i}\n', f'commit {i}')
    return os.path.realpath(path)


def make_remote(parent, repo, name='origin'):
    """A bare repo wired up as `name` on `repo`. Returns its path."""
    bare = os.path.join(parent, f'{name}.git')
    git(parent, 'init', '-q', '--bare', bare)
    git(repo, 'remote', 'add', name, bare)
    return bare


class GitTestCase(unittest.TestCase):
    """A temp HOME with a `.worktrees` root and hermetic git config."""

    def setUp(self):
        super().setUp()
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='kcwt-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, 'home')
        os.makedirs(self.home)
        self.wt_root = os.path.join(self.home, '.worktrees')
        env = git_config_env(self.tmp)
        env['KC_WORKTREE_ROOT'] = self.wt_root
        env['KC_WORKSPACE_HOME'] = self.home
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
