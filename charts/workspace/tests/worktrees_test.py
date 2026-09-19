"""`worktrees.py` — the worktree primitive (#701), against real git.

Nothing here stubs git. Where a failure has to be forced (a manifest write
that dies mid-create, a `worktree remove` that git refuses) the one seam is
`worktrees._RUN` / `worktrees._write_json`, patched for that test only.

Run:  python3 -m unittest tests.worktrees_test   (from charts/workspace/)
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from tests import git_fixtures as gf  # noqa: E402  (installs the fcntl shim)
from tests.envassert import assert_env_has  # noqa: E402
from tests.git_fixtures import commit, git, make_remote, make_repo, write  # noqa: E402
import worktrees as wt  # noqa: E402

WorktreeError = wt.WorktreeError


def never_live(_task_id):
    return False


class SlugTests(unittest.TestCase):

    def test_slugify(self):
        cases = {
            'Fix Login!': 'fix-login',
            '  --weird__Name--  ': 'weird-name',
            'ISSUE 701': 'issue-701',
            '': '',
            '////': '',
            'a' * 60: 'a' * 40,
            # Capping must not leave a trailing hyphen behind.
            ('x' * 39) + '-y': 'x' * 39,
        }
        for raw, want in cases.items():
            self.assertEqual(wt.slugify(raw), want, raw)

    def test_valid_slug(self):
        for ok in ('a', 'issue-701', 't-1789-ab12', 'x' * 40):
            self.assertTrue(wt.valid_slug(ok), ok)
        for bad in ('', '-a', 'a-', 'A', 'a_b', 'a.b', 'x' * 41, 'a/b', None):
            self.assertFalse(wt.valid_slug(bad), bad)

    def test_board_slug_is_valid_short_and_collision_safe(self):
        # GitHub GraphQL ids carry ':' and '='; `A:1` and `a-1` slugify alike.
        pairs = [('acme-github', 'I_kwDOA:4102'), ('acme-github', 'A:1'),
                 ('acme-github', 'a-1'), ('b' * 50, 'c' * 50),
                 ('jira/PROJ', '10001'), ('x', '=')]
        slugs = [wt.board_slug(b, i) for b, i in pairs]
        self.assertEqual(len(set(slugs)), len(slugs))
        for s in slugs:
            self.assertTrue(wt.valid_slug(s), s)
            self.assertLessEqual(len(s), wt.SLUG_MAX)
        self.assertEqual(wt.board_slug('acme', 'I_kwDOA:4102'),
                         wt.board_slug('acme', 'I_kwDOA:4102'))

    @gf.requires_git
    def test_every_slug_is_a_valid_git_branch(self):
        for s in (wt.board_slug('acme-github', 'I_kwDOA:4102'),
                  wt.slugify('Issue 701: worktrees!'), 't-1789739398-ab12cd34'):
            proc = subprocess.run(['git', 'check-ref-format',
                                   'refs/heads/' + wt.branch_for(s)],
                                  capture_output=True)
            self.assertEqual(proc.returncode, 0, s)

    def test_ref_text(self):
        for ok in ('main', 'origin/main', 'HEAD', 'HEAD~1', 'HEAD^', 'v1.2.3',
                   'a' * 40, 'feat/issue-701'):
            wt.validate_ref_text(ok)
        for bad in ('--upload-pack=x', '-x', 'a..b', 'HEAD:f', ';rm -rf /',
                    'a b', 'x\n', 'main@{1}', 'a//b', 'a/', 'a.lock', '', None,
                    'x' * 201):
            with self.assertRaises(WorktreeError, msg=repr(bad)) as cm:
                wt.validate_ref_text(bad)
            self.assertEqual(cm.exception.code, 'bad_ref')


@gf.requires_git
class ResolveTests(gf.GitTestCase):

    def test_resolve_plain_repo(self):
        repo = make_repo(self.home)
        info = wt.resolve_repo(repo, home_root=self.home, wt_root=self.wt_root)
        self.assertEqual(info['root'], repo)
        self.assertEqual(info['subdir'], '')
        self.assertEqual(info['head_sha'], git(repo, 'rev-parse', 'HEAD'))

    def test_subdir_is_kept(self):
        repo = make_repo(self.home)
        commit(repo, 'web/src/app.js', 'x\n')
        info = wt.resolve_repo(os.path.join(repo, 'web', 'src'),
                               home_root=self.home)
        self.assertEqual(info['subdir'], 'web/src')

    def test_confinement(self):
        repo = make_repo(self.home)
        lookalike = self.home + 'ious'
        os.makedirs(lookalike)
        f = os.path.join(repo, 'f0.txt')
        cases = [(self.tmp, 'outside_home'),
                 (os.path.join(repo, '..', '..'), 'outside_home'),
                 (lookalike, 'outside_home'),
                 (f, 'not_dir'),
                 (os.path.join(self.home, 'missing'), 'not_dir'),
                 ('', 'not_dir')]
        for path, code in cases:
            with self.assertRaises(WorktreeError, msg=path) as cm:
                wt.resolve_repo(path, home_root=self.home)
            self.assertEqual(cm.exception.code, code, path)

    def test_not_git(self):
        plain = os.path.join(self.home, 'notes')
        os.makedirs(plain)
        with self.assertRaises(WorktreeError) as cm:
            wt.resolve_repo(plain, home_root=self.home)
        self.assertEqual(cm.exception.code, 'not_git')
        self.assertIn('cannot be isolated', cm.exception.message)

    def test_home_itself_a_repo_is_refused(self):
        git(self.home, 'init', '-q', '-b', 'main')
        commit(self.home, 'dotfile', 'x\n')
        plain = os.path.join(self.home, 'project')
        os.makedirs(plain)
        with self.assertRaises(WorktreeError) as cm:
            wt.resolve_repo(plain, home_root=self.home)
        self.assertEqual(cm.exception.code, 'home_is_repo')

    def test_empty_repo_is_refused(self):
        repo = make_repo(self.home, empty=True)
        with self.assertRaises(WorktreeError) as cm:
            wt.resolve_repo(repo, home_root=self.home)
        self.assertEqual(cm.exception.code, 'empty_repo')

    def test_bare_repo_is_refused(self):
        bare = os.path.join(self.home, 'bare.git')
        git(self.home, 'init', '-q', '--bare', bare)
        with self.assertRaises(WorktreeError) as cm:
            wt.resolve_repo(bare, home_root=self.home)
        self.assertIn(cm.exception.code, ('bare_repo', 'not_git'))

    def test_linked_worktree_resolves_to_main_checkout(self):
        repo = make_repo(self.home)
        linked = os.path.join(self.home, 'linked')
        git(repo, 'worktree', 'add', '-q', '-b', 'side', linked)
        commit(linked, 'side.txt', 'side\n')
        info = wt.resolve_repo(linked, home_root=self.home)
        self.assertEqual(info['root'], repo)
        self.assertEqual(info['toplevel'], os.path.realpath(linked))
        # The default base is the LINKED checkout's own HEAD — a sub-agent
        # branches from what its parent has, not from main.
        self.assertEqual(info['head_sha'], git(linked, 'rev-parse', 'HEAD'))

    def test_resolve_base(self):
        repo = make_repo(self.home, commits=2)
        info = wt.resolve_repo(repo, home_root=self.home)
        first = git(repo, 'rev-parse', 'HEAD~1')
        self.assertEqual(wt.resolve_base(info), ('HEAD', info['head_sha']))
        self.assertEqual(wt.resolve_base(info, 'HEAD~1'), ('HEAD~1', first))
        self.assertEqual(wt.resolve_base(info, None, first), (first, first))
        with self.assertRaises(WorktreeError) as cm:
            wt.resolve_base(info, 'no-such-branch')
        self.assertEqual(cm.exception.code, 'unknown_ref')
        with self.assertRaises(WorktreeError) as cm:
            wt.resolve_base(info, '--upload-pack=touch /tmp/pwned')
        self.assertEqual(cm.exception.code, 'bad_ref')
        with self.assertRaises(WorktreeError) as cm:
            wt.resolve_base(info, None, 'f' * 40)
        self.assertEqual(cm.exception.code, 'unknown_ref')


@gf.requires_git
class EnsureTests(gf.GitTestCase):

    def setUp(self):
        super().setUp()
        self.repo_path = make_repo(self.home)
        self.repo = wt.resolve_repo(self.repo_path, home_root=self.home,
                                    wt_root=self.wt_root)

    def ensure(self, slug='t-1', **kw):
        kw.setdefault('wt_root', self.wt_root)
        kw.setdefault('listening', set())
        kw.setdefault('is_owner_live', never_live)
        return wt.ensure(self.repo, slug, **kw)

    def branches(self):
        return git(self.repo_path, 'branch', '--list', 'kc/*',
                   '--format=%(refname:short)').split()

    def test_creates_worktree_branch_manifest_and_port(self):
        info = self.ensure('t-1', task_id='task-1')
        path = os.path.join(self.wt_root, 'app', 't-1')
        self.assertEqual(info['path'], os.path.realpath(path))
        self.assertEqual(info['branch'], 'kc/t-1')
        self.assertTrue(info['created'])
        self.assertTrue(info['branch_created'])
        self.assertFalse(info['reused'])
        self.assertEqual(info['base_sha'], self.repo['head_sha'])
        self.assertEqual(git(path, 'rev-parse', '--abbrev-ref', 'HEAD'), 'kc/t-1')
        lo, hi = wt.port_range()
        self.assertTrue(lo <= info['port'] <= hi)
        m = wt.read_manifest(path)
        self.assertEqual(m['version'], 2)
        self.assertEqual(m['task_id'], 'task-1')
        self.assertEqual(m['source_root'], self.repo_path)
        self.assertTrue(m['branch_created'])
        # Our files are excluded, so a fresh worktree is clean.
        self.assertEqual(git(path, 'status', '--porcelain'), '')
        # The main checkout is untouched.
        self.assertEqual(git(self.repo_path, 'rev-parse', '--abbrev-ref', 'HEAD'),
                         'main')
        self.assertEqual(git(self.repo_path, 'status', '--porcelain'), '')

    def test_no_upstream_tracking(self):
        make_remote(self.tmp, self.repo_path)
        git(self.repo_path, 'push', '-q', 'origin', 'main')
        git(self.repo_path, 'fetch', '-q', 'origin')
        info = self.ensure('t-up', base_ref='origin/main')
        self.assertEqual(git(self.repo_path, 'config', '--get',
                             'branch.kc/t-up.merge', check=False), '')
        self.assertEqual(info['base_ref'], 'origin/main')

    def test_hooks_do_not_run(self):
        hooks = os.path.join(self.tmp, 'hooks')
        sentinel = os.path.join(self.tmp, 'hook-ran')
        write(os.path.join(hooks, 'post-checkout'),
              f'#!/bin/sh\ntouch {sentinel}\n')
        os.chmod(os.path.join(hooks, 'post-checkout'), 0o755)
        git(self.repo_path, 'config', 'core.hooksPath', hooks)
        self.ensure('t-hook')
        self.assertFalse(os.path.exists(sentinel))

    def test_subdir_cwd(self):
        commit(self.repo_path, 'web/app.js', 'x\n')
        repo = wt.resolve_repo(os.path.join(self.repo_path, 'web'),
                               home_root=self.home)
        info = wt.ensure(repo, 't-sub', wt_root=self.wt_root, listening=set())
        self.assertEqual(info['cwd'], os.path.join(info['path'], 'web'))

    def test_untracked_subdir_falls_back_to_the_root(self):
        os.makedirs(os.path.join(self.repo_path, 'scratch'))
        repo = wt.resolve_repo(os.path.join(self.repo_path, 'scratch'),
                               home_root=self.home)
        info = wt.ensure(repo, 't-scr', wt_root=self.wt_root, listening=set())
        self.assertEqual(info['cwd'], info['path'])

    def test_reuse_keeps_path_port_and_base_and_records_history(self):
        first = self.ensure('t-r', task_id='a')
        commit(first['path'], 'work.txt', 'done\n')
        again = self.ensure('t-r', task_id='b')
        self.assertTrue(again['reused'])
        self.assertFalse(again['created'])
        self.assertEqual(again['path'], first['path'])
        self.assertEqual(again['port'], first['port'])
        self.assertEqual(again['base_sha'], first['base_sha'])
        m = wt.read_manifest(first['path'])
        self.assertEqual(m['task_id'], 'b')
        self.assertEqual(m['history'], ['a'])
        self.assertTrue(os.path.exists(os.path.join(first['path'], 'work.txt')))

    def test_busy_when_owner_is_live(self):
        self.ensure('t-b', task_id='a')
        with self.assertRaises(WorktreeError) as cm:
            self.ensure('t-b', task_id='b', is_owner_live=lambda t: t == 'a')
        self.assertEqual(cm.exception.code, 'busy')
        self.assertEqual(cm.exception.detail['owner_task_id'], 'a')
        # The owner itself, or an explicitly allowed one, may reuse it.
        self.ensure('t-b', task_id='a', is_owner_live=lambda t: True)
        self.ensure('t-b', task_id='c', allow_owner='a',
                    is_owner_live=lambda t: t == 'a')

    def test_existing_branch_is_reused_with_its_commits(self):
        # A branch left behind by the shell skill, or by a removed worktree.
        git(self.repo_path, 'branch', 'kc/t-e')
        side = os.path.join(self.tmp, 'side')
        git(self.repo_path, 'worktree', 'add', '-q', side, 'kc/t-e')
        tip = commit(side, 'kept.txt', 'kept\n')
        git(self.repo_path, 'worktree', 'remove', side)
        info = self.ensure('t-e')
        self.assertFalse(info['branch_created'])
        self.assertEqual(git(info['path'], 'rev-parse', 'HEAD'), tip)
        self.assertEqual(info['base_sha'], self.repo['head_sha'])

    def test_recorded_base_wins_on_readd(self):
        info = self.ensure('t-x', base_ref='HEAD')
        base = info['base_sha']
        commit(info['path'], 'w.txt', 'w\n')
        wt.remove(info['path'], wt_root=self.wt_root)
        commit(self.repo_path, 'later.txt', 'later\n')     # main moves on
        again = self.ensure('t-x', base_sha=base)
        self.assertEqual(again['base_sha'], base)
        self.assertTrue(os.path.exists(os.path.join(again['path'], 'w.txt')))

    def test_branch_in_use(self):
        git(self.repo_path, 'branch', 'kc/t-u')
        other = os.path.join(self.tmp, 'other')
        git(self.repo_path, 'worktree', 'add', '-q', other, 'kc/t-u')
        with self.assertRaises(WorktreeError) as cm:
            self.ensure('t-u')
        self.assertEqual(cm.exception.code, 'branch_in_use')
        self.assertFalse(os.path.exists(os.path.join(self.wt_root, 'app', 't-u')))

    def test_foreign_path_is_a_conflict_and_is_left_alone(self):
        path = os.path.join(self.wt_root, 'app', 't-f')
        write(os.path.join(path, 'precious.txt'), 'do not delete\n')
        # The key dir must exist and belong to this repo for the path to be
        # reached at all; a first ensure creates it.
        self.ensure('t-other')
        with self.assertRaises(WorktreeError) as cm:
            self.ensure('t-f')
        self.assertEqual(cm.exception.code, 'path_conflict')
        with open(os.path.join(path, 'precious.txt'), encoding='utf-8') as f:
            self.assertEqual(f.read(), 'do not delete\n')
        self.assertNotIn('kc/t-f', self.branches())

    def test_empty_leftover_dir_is_reclaimed(self):
        self.ensure('t-other')
        os.makedirs(os.path.join(self.wt_root, 'app', 't-empty'))
        info = self.ensure('t-empty')
        self.assertTrue(info['created'])

    def test_directory_deleted_by_hand_is_readded_with_commits(self):
        info = self.ensure('t-gone')
        tip = commit(info['path'], 'w.txt', 'w\n')
        shutil.rmtree(info['path'])
        again = self.ensure('t-gone', base_sha=info['base_sha'])
        self.assertTrue(again['created'])
        self.assertEqual(git(again['path'], 'rev-parse', 'HEAD'), tip)

    def test_repo_key_collision_gets_a_hashed_key(self):
        self.ensure('t-1')
        other_parent = os.path.join(self.home, 'clients')
        os.makedirs(other_parent)
        other = make_repo(other_parent, 'app')
        repo2 = wt.resolve_repo(other, home_root=self.home)
        info = wt.ensure(repo2, 't-1', wt_root=self.wt_root, listening=set())
        self.assertNotEqual(info['repo_key'], 'app')
        self.assertTrue(info['repo_key'].startswith('app-'))
        # Both exist, each on its own repository.
        self.assertEqual(len(wt.list_all(self.wt_root)), 2)

    @gf.posix_only
    def test_symlinked_key_dir_is_refused(self):
        os.makedirs(self.wt_root)
        elsewhere = os.path.join(self.tmp, 'elsewhere')
        os.makedirs(elsewhere)
        os.symlink(elsewhere, os.path.join(self.wt_root, 'app'))
        info = self.ensure('t-s')
        # The symlinked key is skipped, never followed.
        self.assertNotEqual(info['repo_key'], 'app')
        self.assertEqual(os.listdir(elsewhere), [])

    def test_ports_skip_reserved_leased_and_listening(self):
        a = self.ensure('t-p1', rng=(7680, 7690), listening={7682})
        self.assertEqual(a['port'], 7680)
        b = self.ensure('t-p2', rng=(7680, 7690), listening={7682})
        self.assertEqual(b['port'], 7683)            # 7681 reserved, 7682 busy
        with self.assertRaises(WorktreeError) as cm:
            self.ensure('t-p3', rng=(7680, 7682), listening={7682})
        self.assertEqual(cm.exception.code, 'no_port')
        self.assertFalse(os.path.exists(os.path.join(self.wt_root, 'app', 't-p3')))
        self.assertNotIn('kc/t-p3', self.branches())

    def test_reserved_ports_match_server(self):
        import server
        self.assertEqual(wt.RESERVED_PORTS, server.AppsManager.INTERNAL_PORTS)
        sh = os.path.join(HERE, '..', '..', '..', '.claude', 'skills',
                          'worktree', 'worktree.sh')
        with open(sh, encoding='utf-8') as f:
            line = next(l for l in f if l.startswith('RESERVED='))
        self.assertEqual({int(p) for p in line.split('"')[1].split()},
                         set(wt.RESERVED_PORTS))

    def test_cap_refuses_and_creates_nothing(self):
        self.ensure('t-c1', max_worktrees=2)
        self.ensure('t-c2', max_worktrees=2)
        with self.assertRaises(WorktreeError) as cm:
            self.ensure('t-c3', max_worktrees=2)
        self.assertEqual(cm.exception.code, 'worktree_cap')
        self.assertEqual(cm.exception.detail, {'count': 2, 'max': 2})
        self.assertIn('Settings', cm.exception.message)
        self.assertFalse(os.path.exists(os.path.join(self.wt_root, 'app', 't-c3')))
        self.assertNotIn('kc/t-c3', self.branches())
        # Reusing an existing one is not "another" worktree.
        self.assertTrue(self.ensure('t-c1', max_worktrees=2)['reused'])

    def test_cap_reclaims_before_refusing(self):
        self.ensure('t-c1', max_worktrees=1)
        calls = []

        def reclaim():
            calls.append(1)
            wt._remove_locked(self.wt_root,
                              os.path.join(self.wt_root, 'app', 't-c1'),
                              force=False)
        info = self.ensure('t-c2', max_worktrees=1, reclaim=reclaim)
        self.assertEqual(calls, [1])
        self.assertTrue(info['created'])

    def test_failure_after_add_rolls_everything_back(self):
        with mock.patch.object(wt, '_write_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.ensure('t-fail')
        self.assertFalse(os.path.exists(os.path.join(self.wt_root, 'app', 't-fail')))
        self.assertNotIn('kc/t-fail', self.branches())
        self.assertNotIn('t-fail', git(self.repo_path, 'worktree', 'list'))

    def test_failure_keeps_a_branch_this_call_did_not_create(self):
        git(self.repo_path, 'branch', 'kc/t-keep')
        with mock.patch.object(wt, '_write_json', side_effect=OSError('boom')):
            with self.assertRaises(OSError):
                self.ensure('t-keep')
        self.assertIn('kc/t-keep', self.branches())
        self.assertFalse(os.path.exists(os.path.join(self.wt_root, 'app', 't-keep')))

    def test_unknown_base_creates_nothing(self):
        with self.assertRaises(WorktreeError) as cm:
            self.ensure('t-nb', base_ref='nope')
        self.assertEqual(cm.exception.code, 'unknown_ref')
        self.assertFalse(os.path.exists(os.path.join(self.wt_root, 'app', 't-nb')))
        self.assertNotIn('kc/t-nb', self.branches())

    def test_public_rollback_only_undoes_what_was_created(self):
        info = self.ensure('t-rb')
        reused = self.ensure('t-rb')
        self.assertFalse(wt.rollback(reused, wt_root=self.wt_root))
        self.assertTrue(os.path.isdir(info['path']))
        self.assertTrue(wt.rollback(info, wt_root=self.wt_root))
        self.assertFalse(os.path.exists(info['path']))
        self.assertNotIn('kc/t-rb', self.branches())

    def test_bad_slug(self):
        with self.assertRaises(WorktreeError) as cm:
            self.ensure('../escape')
        self.assertEqual(cm.exception.code, 'bad_slug')


@gf.requires_git
class StatusDiffTests(gf.GitTestCase):

    def setUp(self):
        super().setUp()
        self.repo_path = make_repo(self.home)
        commit(self.repo_path, 'keep.txt', 'a\nb\n')
        self.repo = wt.resolve_repo(self.repo_path, home_root=self.home)
        self.info = wt.ensure(self.repo, 't-s', wt_root=self.wt_root,
                              listening=set())
        self.path = self.info['path']
        self.base = self.info['base_sha']

    def st(self, **kw):
        return wt.status(self.path, base_sha=self.base, use_cache=False, **kw)

    def test_fresh_worktree_is_clean(self):
        st = self.st()
        self.assertEqual((st['files_changed'], st['dirty'], st['untracked'],
                          st['ahead']), (0, 0, 0, 0))
        self.assertEqual(st['branch'], 'kc/t-s')

    def test_counts_committed_uncommitted_untracked_and_binary(self):
        commit(self.path, 'committed.txt', '1\n2\n3\n')
        write(os.path.join(self.path, 'keep.txt'), 'a\nB\nc\n')        # modified
        write(os.path.join(self.path, 'new.txt'), 'n\n')               # untracked
        with open(os.path.join(self.path, 'img.bin'), 'wb') as f:
            f.write(b'\x00\x01\x02binary')
        git(self.path, 'add', 'img.bin')
        st = self.st()
        by = {f['path']: f for f in st['files']}
        self.assertEqual(set(by), {'committed.txt', 'keep.txt', 'new.txt', 'img.bin'})
        self.assertEqual((by['committed.txt']['added'], by['committed.txt']['status']),
                         (3, 'A'))
        self.assertFalse(by['committed.txt']['uncommitted'])
        self.assertTrue(by['keep.txt']['uncommitted'])
        self.assertEqual(by['new.txt']['status'], '?')
        self.assertTrue(by['img.bin']['binary'])
        self.assertEqual(st['ahead'], 1)
        self.assertEqual(st['dirty'], 2)          # keep.txt + staged img.bin
        self.assertEqual(st['untracked'], 1)
        self.assertEqual(st['insertions'], 3 + 2)
        self.assertEqual(st['deletions'], 1)

    def test_own_files_never_count(self):
        # Even without the exclude line (a v1 repo the shell skill touched).
        exclude = os.path.join(self.repo_path, '.git', 'info', 'exclude')
        with open(exclude, 'w', encoding='utf-8') as f:
            f.write('')
        write(os.path.join(self.path, '.kc-issue-prompt.md'), 'prompt\n')
        st = self.st()
        self.assertEqual((st['files_changed'], st['untracked']), (0, 0))

    def test_behind_a_named_base(self):
        info = wt.ensure(self.repo, 't-bh', wt_root=self.wt_root,
                         listening=set(), base_ref='main')
        commit(self.repo_path, 'upstream.txt', 'u\n')
        st = wt.status(info['path'], base_sha=info['base_sha'], base_ref='main',
                       use_cache=False)
        self.assertEqual(st['behind'], 1)
        self.assertIsNone(self.st(base_ref='HEAD')['behind'])

    def test_truncation_keeps_totals(self):
        for i in range(7):
            write(os.path.join(self.path, f'n{i}.txt'), 'x\n')
        st = self.st(max_files=3)
        self.assertEqual(st['files_changed'], 7)
        self.assertEqual(len(st['files']), 3)
        self.assertTrue(st['truncated'])

    def test_cache_and_snapshot(self):
        a = wt.status(self.path, base_sha=self.base)
        write(os.path.join(self.path, 'late.txt'), 'x\n')
        self.assertEqual(wt.status(self.path, base_sha=self.base)['files_changed'],
                         a['files_changed'])                  # cached
        self.assertEqual(self.st()['files_changed'], 1)       # fresh
        snap = wt.stat_snapshot(self.st())
        self.assertEqual(set(snap), {'files_changed', 'insertions', 'deletions',
                                     'ahead', 'dirty', 'untracked', 'branch', 'at'})

    def test_missing_path(self):
        with self.assertRaises(WorktreeError) as cm:
            wt.status(os.path.join(self.tmp, 'nope'), base_sha=self.base)
        self.assertEqual(cm.exception.code, 'missing')

    def test_diff_only_serves_changed_files(self):
        commit(self.path, 'keep.txt', 'a\nchanged\n')
        d = wt.diff(self.path, base_sha=self.base, file='keep.txt')
        self.assertIn('+changed', d['diff'])
        self.assertFalse(d['truncated'])
        for f in ('f0.txt', '../../etc/passwd', '--output=/tmp/x', ''):
            with self.assertRaises(WorktreeError, msg=f) as cm:
                wt.diff(self.path, base_sha=self.base, file=f)
            self.assertEqual(cm.exception.code, 'not_changed')

    def test_diff_of_an_option_shaped_name_is_safe(self):
        name = '--output=pwned.txt'
        write(os.path.join(self.path, name), 'data\n')
        git(self.path, 'add', '--', name)
        git(self.path, 'commit', '-q', '-m', 'odd name')
        d = wt.diff(self.path, base_sha=self.base, file=name)
        self.assertIn('+data', d['diff'])
        self.assertFalse(os.path.exists(os.path.join(self.path, 'pwned.txt')))

    def test_diff_untracked_and_truncated(self):
        write(os.path.join(self.path, 'big.txt'), 'y' * 5000 + '\n')
        d = wt.diff(self.path, base_sha=self.base, file='big.txt', max_bytes=200)
        self.assertTrue(d['truncated'])
        self.assertEqual(d['status'], '?')
        self.assertLessEqual(len(d['diff'].encode('utf-8')), 200)


@gf.requires_git
class RemoveTests(gf.GitTestCase):

    def setUp(self):
        super().setUp()
        self.repo_path = make_repo(self.home)
        self.repo = wt.resolve_repo(self.repo_path, home_root=self.home)
        self.info = wt.ensure(self.repo, 't-rm', wt_root=self.wt_root,
                              listening=set(), task_id='owner-1')

    def test_clean_removal_keeps_the_branch(self):
        commit(self.info['path'], 'w.txt', 'w\n')
        out = wt.remove(self.info['path'], wt_root=self.wt_root)
        self.assertTrue(out['removed'])
        self.assertTrue(out['branch_kept'])
        self.assertIn('branch -D kc/t-rm', out['delete_branch_command'])
        self.assertFalse(os.path.exists(self.info['path']))
        self.assertIn('kc/t-rm', git(self.repo_path, 'branch', '--list', 'kc/*'))
        # A second removal is a no-op, not an error.
        self.assertTrue(wt.remove(self.info['path'], wt_root=self.wt_root)['already'])

    def test_dirty_refuses_until_forced(self):
        write(os.path.join(self.info['path'], 'wip.txt'), 'wip\n')
        with self.assertRaises(WorktreeError) as cm:
            wt.remove(self.info['path'], wt_root=self.wt_root)
        self.assertEqual(cm.exception.code, 'dirty')
        self.assertEqual(cm.exception.detail, {'dirty': 0, 'untracked': 1})
        self.assertTrue(os.path.exists(os.path.join(self.info['path'], 'wip.txt')))
        wt.remove(self.info['path'], wt_root=self.wt_root, force=True)
        self.assertFalse(os.path.exists(self.info['path']))
        self.assertIn('kc/t-rm', git(self.repo_path, 'branch', '--list', 'kc/*'))

    def test_live_owner_refuses_even_with_force(self):
        for force in (False, True):
            with self.assertRaises(WorktreeError) as cm:
                wt.remove(self.info['path'], wt_root=self.wt_root, force=force,
                          is_owner_live=lambda t: t == 'owner-1')
            self.assertEqual(cm.exception.code, 'live')
        self.assertTrue(os.path.isdir(self.info['path']))

    def test_repo_missing(self):
        shutil.rmtree(self.repo_path)
        with self.assertRaises(WorktreeError) as cm:
            wt.remove(self.info['path'], wt_root=self.wt_root)
        self.assertEqual(cm.exception.code, 'repo_missing')
        out = wt.remove(self.info['path'], wt_root=self.wt_root, force=True)
        self.assertTrue(out['repo_missing'])
        self.assertFalse(os.path.exists(self.info['path']))

    def test_manifest_is_restored_when_git_refuses(self):
        real_run = wt._RUN

        def fake(argv, **kw):
            if 'worktree' in argv and 'remove' in argv:
                return subprocess.CompletedProcess(argv, 1, '', 'fatal: nope')
            return real_run(argv, **kw)
        with mock.patch.object(wt, '_RUN', side_effect=fake):
            with self.assertRaises(WorktreeError) as cm:
                wt.remove(self.info['path'], wt_root=self.wt_root)
        self.assertEqual(cm.exception.code, 'git_failed')
        self.assertIsNotNone(wt.read_manifest(self.info['path']))

    def test_outside_paths_are_refused(self):
        for p in (self.repo_path, self.wt_root, os.path.join(self.tmp, 'x')):
            with self.assertRaises(WorktreeError, msg=p) as cm:
                wt.remove(p, wt_root=self.wt_root)
            self.assertEqual(cm.exception.code, 'not_worktree')

    def test_submodule_worktree_removes_when_clean(self):
        sub_src = make_repo(self.tmp, 'subsrc')
        git(self.repo_path, '-c', 'protocol.file.allow=always', 'submodule',
            'add', '-q', sub_src, 'vendor/sub')
        git(self.repo_path, 'commit', '-q', '-m', 'add submodule')
        repo = wt.resolve_repo(self.repo_path, home_root=self.home)
        info = wt.ensure(repo, 't-sm', wt_root=self.wt_root, listening=set())
        git(info['path'], '-c', 'protocol.file.allow=always', 'submodule',
            'update', '-q', '--init')
        out = wt.remove(info['path'], wt_root=self.wt_root)
        self.assertTrue(out['removed'])
        self.assertFalse(os.path.exists(info['path']))


@gf.requires_git
class SweepTests(gf.GitTestCase):

    def setUp(self):
        super().setUp()
        self.repo_path = make_repo(self.home)
        self.repo = wt.resolve_repo(self.repo_path, home_root=self.home)
        self.meta = {}
        self.live = set()

    def make(self, slug, owner='', status='completed', finished_ago_days=30):
        info = wt.ensure(self.repo, slug, wt_root=self.wt_root, listening=set(),
                         task_id=owner)
        if owner:
            self.meta[owner] = {'status': status,
                                'finished_at': time.time() - finished_ago_days * 86400}
        return info

    def sweep(self, **kw):
        kw.setdefault('gc_days', 7)
        kw.setdefault('grace_s', 0)
        return wt.sweep(wt_root=self.wt_root, is_owner_live=lambda t: t in self.live,
                        owner_meta=self.meta.get, **kw)

    def reasons(self, report):
        return {k['slug']: k['reason'] for k in report['kept']}

    def branches(self):
        return git(self.repo_path, 'branch', '--list', 'kc/*',
                   '--format=%(refname:short)').split()

    def test_pristine_finished_is_removed_with_its_branch(self):
        info = self.make('t-p', owner='o1', finished_ago_days=0)
        rep = self.sweep()
        self.assertEqual([r['slug'] for r in rep['removed']], ['t-p'])
        self.assertTrue(rep['removed'][0]['branch_deleted'])
        self.assertFalse(os.path.exists(info['path']))
        self.assertNotIn('kc/t-p', self.branches())

    def test_pristine_but_pre_existing_branch_is_kept(self):
        git(self.repo_path, 'branch', 'kc/t-pre')
        self.make('t-pre', owner='o1')
        rep = self.sweep()
        self.assertFalse(rep['removed'][0]['branch_deleted'])
        self.assertIn('kc/t-pre', self.branches())

    def test_keep_rules(self):
        live = self.make('t-live', owner='live1')
        self.live.add('live1')
        self.make('t-run', owner='run1', status='running')
        dirty = self.make('t-dirty', owner='d1')
        write(os.path.join(dirty['path'], 'wip.txt'), 'w\n')
        unpushed = self.make('t-unp', owner='u1')
        commit(unpushed['path'], 'w.txt', 'w\n')
        recent = self.make('t-recent', owner='r1', finished_ago_days=1)
        commit(recent['path'], 'w.txt', 'w\n')
        rep = self.sweep()
        self.assertEqual(rep['removed'], [])
        self.assertEqual(self.reasons(rep), {
            't-live': 'live', 't-run': 'not_terminal', 't-dirty': 'dirty',
            't-unp': 'unpushed', 't-recent': 'recent'})
        for info in (live, dirty, unpushed, recent):
            self.assertTrue(os.path.isdir(info['path']))

    def test_grace_period_protects_new_worktrees(self):
        self.make('t-g', owner='o1')
        rep = self.sweep(grace_s=3600)
        self.assertEqual(self.reasons(rep), {'t-g': 'recent'})

    def test_pushed_and_old_is_removed_but_branch_kept(self):
        make_remote(self.tmp, self.repo_path)
        info = self.make('t-pushed', owner='o1', finished_ago_days=30)
        commit(info['path'], 'w.txt', 'w\n')
        git(info['path'], 'push', '-q', 'origin', 'kc/t-pushed')
        rep = self.sweep()
        self.assertEqual([r['reason'] for r in rep['removed']], ['pushed'])
        self.assertFalse(os.path.exists(info['path']))
        self.assertIn('kc/t-pushed', self.branches())

    def test_manual_young_pristine_is_kept_old_is_removed(self):
        # No owner: made by hand with the shell skill. Pristine alone is not
        # enough — it must also be older than gc_days.
        info = self.make('t-man')
        rep = self.sweep()
        self.assertEqual(self.reasons(rep), {'t-man': 'recent'})
        rep = self.sweep(now=time.time() + 30 * 86400)
        self.assertEqual([r['slug'] for r in rep['removed']], ['t-man'])
        self.assertFalse(os.path.exists(info['path']))

    def test_dry_run_changes_nothing(self):
        info = self.make('t-dry', owner='o1')
        rep = self.sweep(dry_run=True)
        self.assertEqual([r['slug'] for r in rep['removed']], ['t-dry'])
        self.assertTrue(os.path.isdir(info['path']))
        self.assertIn('kc/t-dry', self.branches())

    def test_keep_branches_switch(self):
        self.make('t-kb', owner='o1')
        self.sweep(delete_pristine_branches=False)
        self.assertIn('kc/t-kb', self.branches())

    def test_cas_delete_refuses_a_moved_branch(self):
        info = self.make('t-cas', owner='o1')
        commit(info['path'], 'moved.txt', 'm\n')
        self.assertFalse(wt._cas_delete_branch(self.repo_path, 'kc/t-cas',
                                               info['base_sha']))
        self.assertIn('kc/t-cas', self.branches())

    def test_repo_missing_is_reported(self):
        self.make('t-rm', owner='o1')
        shutil.rmtree(self.repo_path)
        self.assertEqual(self.reasons(self.sweep()), {'t-rm': 'repo_missing'})


@gf.requires_git
class ConcurrencyTests(gf.GitTestCase):

    def setUp(self):
        super().setUp()
        if not wt.real_flock():
            self.skipTest('fcntl is shimmed here; mutual exclusion is unproven')
        self.repo_path = make_repo(self.home)
        self.repo = wt.resolve_repo(self.repo_path, home_root=self.home)

    def test_parallel_threads_and_processes_get_distinct_worktrees(self):
        results, errors = [], []

        def worker(i):
            try:
                results.append(wt.ensure(self.repo, f't-th{i}',
                                         wt_root=self.wt_root, listening=set()))
            except Exception as e:  # pragma: no cover - reported below
                errors.append(e)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        script = os.path.join(os.path.dirname(HERE), 'worktrees.py')
        procs = [subprocess.Popen(
            [sys.executable, script, 'ensure', '--repo', self.repo_path,
             '--slug', f't-pr{i}', '--json'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        for p in procs:
            out, err = p.communicate(timeout=180)
            self.assertEqual(p.returncode, 0, err)
            results.append(json.loads(out))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 12)
        self.assertEqual(len({r['port'] for r in results}), 12)
        self.assertEqual(len({r['path'] for r in results}), 12)
        self.assertEqual(git(self.repo_path, 'status', '--porcelain'), '')

    def test_lock_timeout(self):
        held = threading.Event()
        release = threading.Event()

        def holder():
            with wt.locked(self.wt_root):
                held.set()
                release.wait(10)
        t = threading.Thread(target=holder)
        t.start()
        held.wait(10)
        try:
            with self.assertRaises(WorktreeError) as cm:
                wt.ensure(self.repo, 't-lt', wt_root=self.wt_root,
                          listening=set(), timeout=0.3)
            self.assertEqual(cm.exception.code, 'lock_timeout')
        finally:
            release.set()
            t.join(10)


@gf.requires_git
class CliTests(gf.GitTestCase):

    def setUp(self):
        super().setUp()
        self.repo_path = make_repo(self.home)
        self.script = os.path.join(os.path.dirname(HERE), 'worktrees.py')

    def cli(self, *args, cwd=None):
        return subprocess.run([sys.executable, self.script, *args],
                              capture_output=True, text=True,
                              cwd=cwd or self.repo_path, timeout=120)

    def test_api_version(self):
        self.assertEqual(self.cli('--api-version').stdout.strip(),
                         str(wt.API_VERSION))

    def test_ensure_emit_shell_list_remove(self):
        proc = self.cli('ensure', '--slug', 'Cli Test', '--emit-shell')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        env = dict(line[len('export '):].split(' ', 1)[0].split('=', 1)
                   for line in proc.stdout.splitlines() if line.startswith('export '))
        self.assertEqual(env['KC_WT_BRANCH'], 'kc/cli-test')
        self.assertTrue(os.path.isdir(env['KC_WT']))
        assert_env_has(self, env, 'PORT')
        listed = json.loads(self.cli('list', '--json').stdout)
        self.assertEqual([m['slug'] for m in listed], ['cli-test'])
        write(os.path.join(env['KC_WT'], 'wip.txt'), 'w\n')
        refused = self.cli('remove', '--slug', 'cli-test', '--json')
        self.assertEqual(refused.returncode, 3)
        self.assertEqual(json.loads(refused.stdout)['code'], 'dirty')
        done = self.cli('remove', '--slug', 'cli-test', '--force')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('KEPT', done.stderr)
        self.assertFalse(os.path.exists(env['KC_WT']))

    def test_refusal_exit_code(self):
        plain = os.path.join(self.home, 'plain')
        os.makedirs(plain)
        proc = self.cli('ensure', '--repo', plain, '--json')
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(json.loads(proc.stdout)['code'], 'not_git')


if __name__ == '__main__':
    unittest.main()
