"""Unsupported Git states and exact-content invariants, using real Git."""
import os
from pathlib import Path
import subprocess
from unittest import mock

from tests.build_publish_test import PublishFixture
from tests.git_fixtures import git, write
import publish_git as pg
import worktrees


class SnapshotTests(PublishFixture):
    def test_binary_symlink_executable_and_deletion_match_review_tree(self):
        Path(self.path, 'f0.txt').unlink()
        Path(self.path, 'binary.bin').write_bytes(b'\0\xff\0')
        write(os.path.join(self.path, 'run.sh'), '#!/bin/sh\nexit 0\n')
        os.chmod(os.path.join(self.path, 'run.sh'), 0o755)
        os.symlink('run.sh', os.path.join(self.path, 'link'))
        p = self.prepare()
        expected = pg.snapshot(self.meta, self.dest, Path(self.tmp) / 'check')['tree']
        self.submit(p)
        self.service.run('t1')
        self.assertEqual(self.service.status('t1')['operation']['stage'], 'published')
        self.assertEqual(git(self.path, 'rev-parse', 'HEAD^{tree}'), expected)
        entries = git(self.path, 'ls-tree', 'HEAD')
        self.assertIn('100755', entries)
        self.assertIn('120000', entries)
        self.assertNotIn('f0.txt', entries)

    def test_detached_head_is_blocked(self):
        git(self.path, 'checkout', '--detach')
        self.assertEqual(self.service.status('t1')['eligibility']['reason']['code'], 'detached_head')

    def test_merge_in_progress_is_blocked(self):
        marker = pg.text(self.path, 'rev-parse', '--path-format=absolute', '--git-path', 'MERGE_HEAD')
        Path(marker).write_text(self.dest['base_sha'])
        self.assertEqual(self.service.status('t1')['eligibility']['reason']['code'], 'conflicts')

    def test_custom_clean_filter_never_executes(self):
        sentinel = Path(self.tmp) / 'filter-ran'
        git(self.path, 'config', 'filter.bad.clean', f'touch {sentinel}')
        write(os.path.join(self.path, '.gitattributes'), '*.txt filter=bad\n')
        result = self.service.prepare('t1')
        self.assertEqual(result['preparation_error']['code'], 'unsupported_git_feature')
        self.assertFalse(sentinel.exists())

    def test_size_limit_does_not_mutate_index(self):
        index = git(self.path, 'diff', '--cached')
        with mock.patch.object(pg, 'MAX_BYTES', 2):
            status = self.service.prepare('t1')
        self.assertEqual(status['preparation_error']['code'], 'too_large')
        self.assertEqual(git(self.path, 'diff', '--cached'), index)

    def test_signing_failure_leaves_branch_and_index_unchanged(self):
        git(self.path, 'config', 'commit.gpgsign', 'true')
        git(self.path, 'config', 'gpg.program', '/nonexistent/kc-signing-program')
        before = git(self.path, 'rev-parse', 'HEAD')
        self.submit()
        self.service.run('t1')
        self.assertEqual(self.service.status('t1')['operation']['stage'], 'failed')
        self.assertEqual(git(self.path, 'rev-parse', 'HEAD'), before)
        self.assertEqual(git(self.path, 'diff', '--cached'), '')

    def test_lost_push_response_is_verified_against_remote(self):
        p = self.prepare()
        self.submit(p)
        run = pg._RUN
        def lose_response(argv, **kwargs):
            result = run(argv, **kwargs)
            if 'push' in argv and result.returncode == 0:
                return subprocess.CompletedProcess(argv, 1, b'', b'lost response')
            return result
        with mock.patch.object(pg, '_RUN', side_effect=lose_response):
            self.service.run('t1')
        self.assertEqual(self.service.status('t1')['operation']['stage'], 'published')

    def test_external_staging_change_at_commit_boundary_is_preserved(self):
        p = self.prepare()
        record = __import__('build_publish').read(self.service.path('t1'))['preparation']
        write(os.path.join(self.path, 'another.txt'), 'separately staged\n')
        git(self.path, 'add', 'another.txt')
        staged = git(self.path, 'diff', '--cached')
        with self.assertRaisesRegex(pg.PublishError, 'staging area changed'):
            pg.commit(record, {'title': p['title'], 'commit_date': '2026-09-21T12:00:00Z'}, lambda: None, self.root)
        self.assertEqual(git(self.path, 'diff', '--cached'), staged)
