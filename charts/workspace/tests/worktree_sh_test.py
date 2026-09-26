"""The `worktree` skill's shell helper stays compatible with worktrees.py (#701).

Two modes, both tested against real git:
- **delegated** — `/tmp/browser/worktrees.py` (here: `KC_WORKTREES_PY`) exists,
  so the script execs the Python CLI; the stdout `export …` contract a human's
  `eval "$(worktree.sh new x)"` depends on must survive the delegation;
- **fallback** — no Python implementation; the bash path must take the same
  lock and write a manifest worktrees.py reads as its own.

Run:  python3 -m unittest tests.worktree_sh_test   (from charts/workspace/)
"""

import json
import os
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from tests import git_fixtures as gf  # noqa: E402
from tests.git_fixtures import git, make_repo, write  # noqa: E402
import worktrees as wt  # noqa: E402

SCRIPT = os.path.realpath(os.path.join(HERE, '..', '..', '..', '.claude',
                                       'skills', 'worktree', 'worktree.sh'))
PY = os.path.realpath(os.path.join(HERE, '..', 'worktrees.py'))
BASH = shutil.which('bash')


def parse_exports(stdout):
    env = {}
    for line in stdout.splitlines():
        if not line.startswith('export '):
            continue
        for pair in line[len('export '):].split():
            k, _, v = pair.partition('=')
            env[k] = v
    return env


@gf.requires_git
@gf.posix_only
@unittest.skipUnless(BASH, 'bash is not installed')
class _ShellBase(gf.GitTestCase):
    MODE_PY = PY

    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.home)

    def sh(self, *args, cwd=None):
        env = dict(os.environ, KC_WORKTREES_PY=self.MODE_PY, KC_TASK_ID='')
        return subprocess.run([BASH, SCRIPT, *args], capture_output=True,
                              text=True, cwd=cwd or self.repo, env=env,
                              timeout=120)

    def branches(self):
        return git(self.repo, 'branch', '--list', 'kc/*',
                   '--format=%(refname:short)').split()


class DelegatedTests(_ShellBase):

    def test_new_keeps_the_export_contract(self):
        proc = self.sh('new', 'My Change')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        env = parse_exports(proc.stdout)
        self.assertEqual(env['KC_WT_BRANCH'], 'kc/my-change')
        self.assertEqual(env['PORT'], env['KC_PORT'])
        self.assertTrue(os.path.isdir(env['KC_WT']))
        self.assertIn('worktree ready', proc.stderr)
        m = wt.read_manifest(env['KC_WT'])
        self.assertEqual(m['created_by'], 'worktree.sh')
        self.assertEqual(m['version'], 2)

    def test_new_twice_reuses(self):
        a = parse_exports(self.sh('new', 'same').stdout)
        b = parse_exports(self.sh('new', 'same').stdout)
        self.assertEqual(a, b)

    def test_list_port_and_rm(self):
        env = parse_exports(self.sh('new', 'lst').stdout)
        self.assertIn('lst', self.sh('list').stdout)
        port = int(self.sh('port').stdout.strip())
        lo, hi = wt.port_range()
        self.assertTrue(lo <= port <= hi)
        self.assertNotEqual(port, int(env['PORT']))
        write(os.path.join(env['KC_WT'], 'wip.txt'), 'w\n')
        refused = self.sh('rm', 'lst')
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('uncommitted', refused.stderr)
        done = self.sh('rm', 'lst', '--force')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('KEPT', done.stderr)
        self.assertFalse(os.path.exists(env['KC_WT']))
        self.assertIn('kc/lst', self.branches())

    def test_new_from_a_linked_worktree_keys_on_the_main_repo(self):
        env = parse_exports(self.sh('new', 'parent').stdout)
        child = parse_exports(self.sh('new', 'child', cwd=env['KC_WT']).stdout)
        self.assertEqual(os.path.dirname(child['KC_WT']),
                         os.path.dirname(env['KC_WT']))


@unittest.skipUnless(shutil.which('jq') and shutil.which('flock'),
                     'the bash fallback needs jq and flock')
class FallbackTests(_ShellBase):
    MODE_PY = '/nonexistent/worktrees.py'

    def test_fallback_writes_a_manifest_worktrees_py_owns(self):
        proc = self.sh('new', 'fb')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        env = parse_exports(proc.stdout)
        m = wt.read_manifest(env['KC_WT'])
        self.assertEqual(m['version'], 2)
        self.assertEqual(m['created_by'], 'worktree.sh')
        self.assertTrue(m['branch_created'])
        self.assertEqual(m['base_sha'], git(self.repo, 'rev-parse', 'HEAD'))
        self.assertEqual([x['slug'] for x in wt.list_all(self.wt_root)], ['fb'])
        # No upstream tracking, and our files are excluded.
        self.assertEqual(git(self.repo, 'config', '--get', 'branch.kc/fb.merge',
                             check=False), '')
        self.assertEqual(git(env['KC_WT'], 'status', '--porcelain'), '')

    def test_fallback_port_skips_a_python_lease(self):
        info = wt.ensure(wt.resolve_repo(self.repo, home_root=self.home), 'py',
                         wt_root=self.wt_root, listening=set())
        env = parse_exports(self.sh('new', 'sh').stdout)
        self.assertNotEqual(int(env['PORT']), info['port'])

    def test_fallback_rm(self):
        env = parse_exports(self.sh('new', 'fbrm').stdout)
        done = self.sh('rm', 'fbrm')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(os.path.exists(env['KC_WT']))
        self.assertIn('kc/fbrm', self.branches())


@unittest.skipUnless(BASH, 'bash is not installed')
@gf.posix_only
class SlugParityTests(unittest.TestCase):

    def test_bash_and_python_slugify_agree(self):
        corpus = ['Fix Login!', '  --weird__Name--  ', 'ISSUE 701', '////',
                  'a' * 60, ('x' * 39) + '-y', 'Émoji ✨ name', 'I_kwDOA:4102']
        # `$(…)` strips the trailing newline and an empty result stays a field.
        fn = ('eval "$(sed -n \'/^slugify()/,/^}/p\' "$0")"; '
              'for a in "$@"; do printf \'%s|\' "$(slugify "$a")"; done')
        proc = subprocess.run([BASH, '-c', fn, SCRIPT, *corpus],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split('|')[:len(corpus)],
                         [wt.slugify(c) for c in corpus])


if __name__ == '__main__':
    unittest.main()
