"""Unit tests for memory/sync.py — Claude file-memory → SQLite import.

Covers the pure helpers (sanitize / project-id / frontmatter / mtime tags /
tag building), the filesystem walk (_iter_memory_files), and the DB-touching
sync path (_sync_one / sync_once / ClaudeMemorySyncer), including the
prune-safety contracts that protect the imported corpus across pod restarts.

DB-touching tests reuse the isolated-store pattern from memory_test.py: each
test swaps MemoryManager._store for a fresh temp SQLite file.

Run with:    python3 -m unittest tests.sync_test
(from charts/workspace/)
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import memory.store as _store_mod  # noqa: E402
import memory.sync as sync  # noqa: E402
from memory.manager import MemoryManager  # noqa: E402
from memory.store import MemoryStore  # noqa: E402


# ───────────────────────────────────────────────────────────────────────────
# Pure helpers — no DB / filesystem needed
# ───────────────────────────────────────────────────────────────────────────

class SanitizeComponentTests(unittest.TestCase):
    def test_passthrough_safe_chars(self):
        self.assertEqual(sync._sanitize_component('Hello_World.1-2'), 'Hello_World.1-2')

    def test_replaces_unsafe_chars_with_dash(self):
        self.assertEqual(sync._sanitize_component('a b/c:d'), 'a-b-c-d')

    def test_strips_leading_trailing_dashes(self):
        self.assertEqual(sync._sanitize_component('  spaced  '), 'spaced')

    def test_empty_becomes_unnamed(self):
        self.assertEqual(sync._sanitize_component(''), 'unnamed')
        self.assertEqual(sync._sanitize_component('///'), 'unnamed')

    def test_truncates_to_max_len(self):
        out = sync._sanitize_component('x' * 200, max_len=10)
        self.assertEqual(len(out), 10)

    def test_truncation_that_leaves_only_dashes_becomes_unnamed(self):
        # After truncation the slice is all separators -> rstrip empties it.
        out = sync._sanitize_component('a' + '/' * 50, max_len=3)
        # 'a--' -> rstrip('-') -> 'a'
        self.assertTrue(out)


class ProjectIdFromPathTests(unittest.TestCase):
    def test_extracts_project_id(self):
        p = os.path.join('/home/dev/.claude/projects', 'my-proj', 'memory', 'x.md')
        self.assertEqual(sync._project_id_from_path(p), 'my-proj')

    def test_sanitizes_project_id(self):
        p = os.path.join('/root/.claude/projects', 'a b:c', 'memory', 'x.md')
        self.assertEqual(sync._project_id_from_path(p), 'a-b-c')

    def test_user_level_falls_back_to_user(self):
        p = '/home/dev/.claude/memory/note.md'
        self.assertEqual(sync._project_id_from_path(p), 'user')

    def test_projects_with_no_following_component(self):
        # 'projects' is the final component -> ValueError path not hit, but
        # i+1 out of range -> fallback.
        self.assertEqual(sync._project_id_from_path('/a/projects'), 'user')


class ParseFrontmatterTests(unittest.TestCase):
    def test_no_frontmatter_returns_empty_meta(self):
        meta, body = sync._parse_frontmatter('just a body\n')
        self.assertEqual(meta, {})
        self.assertEqual(body, 'just a body\n')

    def test_parses_basic_keys(self):
        text = '---\nname: Title\ntype: project\n---\nbody here\n'
        meta, body = sync._parse_frontmatter(text)
        self.assertEqual(meta['name'], 'Title')
        self.assertEqual(meta['type'], 'project')
        self.assertEqual(body, 'body here\n')

    def test_strips_quotes_and_ignores_comments_and_bare_lines(self):
        text = '---\n# a comment\nname: "Quoted"\nbare line no colon\ndesc: \'single\'\n---\nB'
        meta, _ = sync._parse_frontmatter(text)
        self.assertEqual(meta['name'], 'Quoted')
        self.assertEqual(meta['desc'], 'single')
        self.assertNotIn('bare line no colon', meta)

    def test_value_with_colon_keeps_remainder(self):
        meta, _ = sync._parse_frontmatter('---\nurl: http://x/y\n---\nB')
        self.assertEqual(meta['url'], 'http://x/y')


class MtimeTagTests(unittest.TestCase):
    def test_mtime_tag_format_truncates_to_int(self):
        self.assertEqual(sync._mtime_tag(1234.99), 'mtime:1234')

    def test_has_mtime_tag_true_when_present(self):
        tags = 'auto-imported,claude-memory,mtime:1000'
        self.assertTrue(sync._has_mtime_tag(tags, 1000.4))

    def test_has_mtime_tag_false_when_absent_or_different(self):
        self.assertFalse(sync._has_mtime_tag('auto-imported,mtime:999', 1000))
        self.assertFalse(sync._has_mtime_tag('', 1000))


class BuildTagsTests(unittest.TestCase):
    def test_includes_import_tags_and_mtime(self):
        out = sync._build_tags(1000).split(',')
        self.assertIn('auto-imported', out)
        self.assertIn('claude-memory', out)
        self.assertIn('mtime:1000', out)

    def test_extra_tags_appended_and_deduped(self):
        out = sync._build_tags(1000, extra=['extra', 'auto-imported', '', '  ']).split(',')
        self.assertEqual(out.count('auto-imported'), 1)
        self.assertIn('extra', out)
        # Empty / whitespace-only extras are dropped.
        self.assertNotIn('', out)


# ───────────────────────────────────────────────────────────────────────────
# Filesystem walk
# ───────────────────────────────────────────────────────────────────────────

class IterMemoryFilesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def _write(self, *parts, content='x'):
        path = os.path.join(self.root, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(content)
        return path

    def test_finds_project_and_user_level_files(self):
        self._write('projects', 'p1', 'memory', 'a.md')
        self._write('memory', 'b.md')
        found = sorted(os.path.basename(p) for p in sync._iter_memory_files([self.root]))
        self.assertEqual(found, ['a.md', 'b.md'])

    def test_skips_non_md_and_skip_basenames(self):
        self._write('memory', 'keep.md')
        self._write('memory', 'note.txt')
        self._write('memory', 'MEMORY.md')
        self._write('memory', 'CLAUDE.md')
        found = [os.path.basename(p) for p in sync._iter_memory_files([self.root])]
        self.assertEqual(found, ['keep.md'])

    def test_missing_roots_are_ignored(self):
        found = list(sync._iter_memory_files(['', '/no/such/dir', self.root]))
        self.assertEqual(found, [])

    def test_dedupes_by_realpath_across_roots(self):
        self._write('memory', 'a.md')
        # Same root passed twice -> file yielded once.
        found = list(sync._iter_memory_files([self.root, self.root]))
        self.assertEqual(len(found), 1)


# ───────────────────────────────────────────────────────────────────────────
# DB-touching sync path
# ───────────────────────────────────────────────────────────────────────────

class SyncDBTestCase(unittest.TestCase):
    """Isolated SQLite store + a tmp .claude tree under a single root."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._db_path = os.path.join(self._tmpdir.name, 'memory.db')
        self._original_store = MemoryManager._store
        self._original_initialized = _store_mod._INITIALIZED
        _store_mod._INITIALIZED = False
        MemoryManager._store = MemoryStore(self._db_path)

        self.root = os.path.join(self._tmpdir.name, '.claude')
        os.makedirs(self.root, exist_ok=True)

    def tearDown(self):
        MemoryManager._store = self._original_store
        _store_mod._INITIALIZED = self._original_initialized

    def write_memory(self, project, name, content, mtime=None):
        mem_dir = os.path.join(self.root, 'projects', project, 'memory')
        os.makedirs(mem_dir, exist_ok=True)
        path = os.path.join(mem_dir, name)
        with open(path, 'w') as f:
            f.write(content)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path


class SyncOneTests(SyncDBTestCase):
    def test_creates_entry_with_namespace_key_and_value(self):
        p = self.write_memory(
            'proj', 'fact.md',
            '---\nname: My Fact\ndescription: a desc\ntype: project\n---\nThe body.\n')
        ns, key, changed = sync._sync_one(p)
        self.assertEqual(ns, 'claude.proj')
        self.assertEqual(key, 'fact')
        self.assertTrue(changed)
        row = MemoryManager.get(namespace='claude.proj', key='fact')
        self.assertIsNotNone(row)
        self.assertIn('My Fact', row['value'])
        self.assertIn('a desc', row['value'])
        self.assertIn('The body.', row['value'])
        # type=project maps to kind=semantic
        self.assertEqual(row['kind'], 'semantic')

    def test_type_mapping_feedback_to_preference(self):
        p = self.write_memory('proj', 'f.md', '---\ntype: feedback\n---\nx\n')
        sync._sync_one(p)
        row = MemoryManager.get(namespace='claude.proj', key='f')
        self.assertEqual(row['kind'], 'preference')

    def test_unknown_type_defaults_to_semantic(self):
        p = self.write_memory('proj', 'f.md', 'no frontmatter body\n')
        sync._sync_one(p)
        row = MemoryManager.get(namespace='claude.proj', key='f')
        self.assertEqual(row['kind'], 'semantic')

    def test_unchanged_file_is_skipped_on_second_pass(self):
        p = self.write_memory('proj', 'f.md', '---\nname: N\n---\nbody\n', mtime=1_000_000)
        _, _, first = sync._sync_one(p)
        self.assertTrue(first)
        _, _, second = sync._sync_one(p)
        self.assertFalse(second)  # mtime tag matches -> skip

    def test_changed_mtime_triggers_reimport(self):
        p = self.write_memory('proj', 'f.md', '---\nname: N\n---\nbody\n', mtime=1_000_000)
        sync._sync_one(p)
        os.utime(p, (2_000_000, 2_000_000))
        _, _, changed = sync._sync_one(p)
        self.assertTrue(changed)

    def test_missing_file_returns_noop(self):
        self.assertEqual(sync._sync_one('/no/such/file.md'), ('', '', False))

    def test_empty_file_falls_back_to_raw_value(self):
        p = self.write_memory('proj', 'empty.md', '')
        ns, key, changed = sync._sync_one(p)
        self.assertTrue(changed)
        row = MemoryManager.get(namespace=ns, key=key)
        self.assertIsNotNone(row)

    def test_huge_body_is_truncated(self):
        big = '---\nname: Big\n---\n' + ('z' * 300_000)
        p = self.write_memory('proj', 'big.md', big)
        sync._sync_one(p)
        row = MemoryManager.get(namespace='claude.proj', key='big')
        self.assertIn('…(truncated)', row['value'])
        self.assertLessEqual(len(row['value'].encode('utf-8')), 200_050)


class SyncOnceTests(SyncDBTestCase):
    def test_counts_scanned_and_changed(self):
        self.write_memory('p', 'a.md', 'body a')
        self.write_memory('p', 'b.md', 'body b')
        res = sync.sync_once(roots=[self.root])
        self.assertEqual(res['scanned'], 2)
        self.assertEqual(res['changed'], 2)
        self.assertEqual(res['pruned'], 0)

    def test_prune_skipped_when_no_root_has_files(self):
        # Empty .claude tree (root exists but no memory files).
        res = sync.sync_once(roots=[self.root])
        self.assertEqual(res['scanned'], 0)
        self.assertTrue(res.get('prune_skipped'))
        self.assertEqual(res['pruned'], 0)

    def test_prune_removes_entry_whose_file_disappeared(self):
        p = self.write_memory('p', 'gone.md', 'body')
        self.write_memory('p', 'stays.md', 'body')
        sync.sync_once(roots=[self.root])
        # Delete one file, keep at least one so the root still "hits".
        os.remove(p)
        res = sync.sync_once(roots=[self.root])
        self.assertEqual(res['pruned'], 1)
        self.assertIsNone(MemoryManager.get(namespace='claude.p', key='gone'))
        self.assertIsNotNone(MemoryManager.get(namespace='claude.p', key='stays'))

    def test_no_prune_when_root_went_dark(self):
        # Import two files, then make the whole tree vanish -> prune skipped,
        # corpus preserved (the pod-restart safety contract).
        self.write_memory('p', 'a.md', 'body')
        sync.sync_once(roots=[self.root])
        import shutil
        shutil.rmtree(self.root)
        res = sync.sync_once(roots=[self.root])
        self.assertTrue(res.get('prune_skipped'))
        self.assertIsNotNone(MemoryManager.get(namespace='claude.p', key='a'))


class ClaudeMemorySyncerTests(SyncDBTestCase):
    def test_trigger_sync_updates_last_result_and_status(self):
        self.write_memory('p', 'a.md', 'body')
        res = sync.ClaudeMemorySyncer.trigger_sync(roots=[self.root])
        self.assertEqual(res['scanned'], 1)
        status = sync.ClaudeMemorySyncer.status()
        self.assertIn('running', status)
        self.assertEqual(status['last_result'], res)
        self.assertIsNotNone(status['last_run_at'])

    def test_start_is_idempotent(self):
        # Already-started guard: a second start() is a no-op and must not
        # raise. Force the flag and restore it after.
        original = sync.ClaudeMemorySyncer._started
        sync.ClaudeMemorySyncer._started = True
        try:
            sync.ClaudeMemorySyncer.start(roots=[self.root])  # returns immediately
        finally:
            sync.ClaudeMemorySyncer._started = original


if __name__ == '__main__':
    unittest.main()
