"""Write-time summarization + graceful injection budgeting (#359).

Long memories used to be hard-cut at 280 chars mid-sentence on every injection,
and entries past the block budget were dropped entirely. These tests pin the
replacement: a summary computed once at write time (with `value` preserved
untouched), word-boundary trimming, and shrink-rather-than-drop budgeting.

Run with:  python3 -m unittest tests.memory_summarize_test  (from charts/workspace/)
"""

import os
import sqlite3
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import memory_inject_hook as hook  # noqa: E402
from memory import store as _store_mod  # noqa: E402
from memory import summarize as S  # noqa: E402
from memory.store import MemoryStore  # noqa: E402
from memory.manager import MemoryManager, injection_text  # noqa: E402

LONG = ('The deploy pipeline pushes to DigitalOcean first. '
        'It then runs helm upgrade against the ws namespace. '
        'If the rollout stalls the operator must check the kaniko builder pod. '
        'Historically this failed because the regcred secret was not replicated. '
        'A follow-up added namespace seeding to make that automatic.')


class ExtractiveSummaryTests(unittest.TestCase):
    def test_short_text_needs_no_summary(self):
        self.assertIsNone(S.summarize('Postgres runs on port 5432.'))

    def test_empty_text_needs_no_summary(self):
        self.assertIsNone(S.summarize(''))
        self.assertIsNone(S.summarize('   '))

    def test_long_text_is_shortened(self):
        out = S.summarize(LONG)
        self.assertIsNotNone(out)
        self.assertLess(len(out), len(LONG))

    def test_summary_ends_on_a_sentence_boundary(self):
        # The whole point: no mid-sentence cut.
        out = S.summarize(LONG)
        self.assertTrue(out.rstrip('… ').endswith('.'), out)

    def test_summary_keeps_the_leading_fact(self):
        out = S.summarize(LONG)
        self.assertTrue(out.startswith('The deploy pipeline pushes'), out)

    def test_single_giant_sentence_degrades_to_word_boundary(self):
        # No sentence fits, so we fall back to a clean word-boundary trim
        # rather than slicing mid-word.
        giant = 'alpha beta gamma delta epsilon ' * 40
        out = S.summarize(giant)
        self.assertIsNotNone(out)
        self.assertTrue(out.endswith('…'))
        for fragment in ('alph…', 'bet…', 'gamm…', 'delt…'):
            self.assertFalse(out.endswith(fragment), out)

    def test_newlines_are_collapsed(self):
        out = S.summarize('First line.\nSecond line.\n' * 20)
        self.assertNotIn('\n', out)

    def test_summarizer_failure_is_swallowed(self):
        # A summarization failure must never fail a memory write.
        def boom(_value, _target):
            raise RuntimeError('provider down')
        S.set_summarizer(boom)
        self.addCleanup(S.set_summarizer, None)
        self.assertIsNone(S.summarize(LONG))

    def test_custom_summarizer_is_used(self):
        S.set_summarizer(lambda value, target: 'CUSTOM')
        self.addCleanup(S.set_summarizer, None)
        self.assertEqual(S.summarize(LONG), 'CUSTOM')

    def test_truncate_on_boundary_never_splits_a_word(self):
        out = S.truncate_on_boundary('alpha beta gamma delta', 14)
        self.assertTrue(out.endswith('…'))
        self.assertNotIn('gam…', out)

    def test_truncate_handles_a_single_unbroken_token(self):
        out = S.truncate_on_boundary('x' * 100, 10)
        self.assertEqual(len(out), 10)


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db = os.path.join(self._tmpdir.name, 'memory.db')
        self._orig_store = MemoryManager._store
        self._orig_init = _store_mod._INITIALIZED
        _store_mod._INITIALIZED = False
        MemoryManager._store = MemoryStore(self.db)

    def tearDown(self):
        MemoryManager._store = self._orig_store
        _store_mod._INITIALIZED = self._orig_init


class WritePathTests(StoreTestCase):
    def test_upsert_stores_summary_and_preserves_value(self):
        row = MemoryManager.upsert(namespace='p.a', key='k', value=LONG)
        # The user's full text is never modified — this is the whole reason the
        # summary is a separate, derived column.
        self.assertEqual(row['value'], LONG)
        self.assertIsNotNone(row['summary'])
        self.assertLess(len(row['summary']), len(LONG))

    def test_short_value_has_no_summary(self):
        row = MemoryManager.upsert(namespace='p.a', key='k', value='Short fact.')
        self.assertIsNone(row['summary'])

    def test_injection_text_prefers_summary_then_falls_back(self):
        long_row = MemoryManager.upsert(namespace='p.a', key='k1', value=LONG)
        short_row = MemoryManager.upsert(namespace='p.a', key='k2', value='Short.')
        self.assertEqual(injection_text(long_row), long_row['summary'])
        self.assertEqual(injection_text(short_row), 'Short.')

    def test_summary_is_recomputed_when_value_changes(self):
        MemoryManager.upsert(namespace='p.a', key='k', value=LONG)
        row = MemoryManager.update_partial(namespace='p.a', key='k',
                                           value='Now brief.')
        self.assertIsNone(row['summary'])
        self.assertEqual(row['value'], 'Now brief.')

    def test_metadata_only_update_keeps_summary(self):
        MemoryManager.upsert(namespace='p.a', key='k', value=LONG)
        row = MemoryManager.update_partial(namespace='p.a', key='k',
                                           importance=0.9)
        self.assertIsNotNone(row['summary'])
        self.assertEqual(row['value'], LONG)

    def test_search_still_matches_text_only_in_the_full_value(self):
        # The summary is NOT in the FTS index, so search must still find a term
        # that only appears in the elided tail.
        MemoryManager.upsert(namespace='p.a', key='k', value=LONG)
        rows = MemoryManager.search(q='seeding', limit=10)
        self.assertEqual([r['key'] for r in rows], ['k'])


class MigrationTests(unittest.TestCase):
    """A v2 database (pre-summary) must upgrade in place without data loss."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db = os.path.join(self._tmpdir.name, 'legacy.db')
        self._orig_store = MemoryManager._store
        self._orig_init = _store_mod._INITIALIZED
        self.addCleanup(self._restore)
        # Build a genuine v2 DB with raw SQL (migrations 001+002 only).
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE IF NOT EXISTS _meta ('
                     ' key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        _store_mod._migration_001(conn)
        _store_mod._migration_002(conn)
        conn.execute("INSERT INTO _meta(key,value) VALUES('schema_version','2')")
        now = time.time()
        conn.execute(
            'INSERT INTO memories (namespace,key,value,kind,tags,importance,'
            'confidence,source,created_at,updated_at,version)'
            ' VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            ('legacy.ns', 'old', LONG, 'semantic', '', 0.5, 1.0, '', now, now, 1))
        conn.commit()
        conn.close()

    def _restore(self):
        MemoryManager._store = self._orig_store
        _store_mod._INITIALIZED = self._orig_init

    def _open(self):
        _store_mod._INITIALIZED = False
        MemoryManager._store = MemoryStore(self.db)

    def test_legacy_row_survives_and_falls_back_to_value(self):
        self._open()
        row = MemoryManager.get(namespace='legacy.ns', key='old')
        self.assertEqual(row['value'], LONG)
        self.assertIsNone(row['summary'])
        self.assertEqual(injection_text(row), LONG)

    def test_migration_is_idempotent(self):
        self._open()
        self._open()  # re-run migrations on the already-upgraded DB
        self.assertEqual(
            MemoryManager.get(namespace='legacy.ns', key='old')['value'], LONG)

    def test_rewriting_a_legacy_row_backfills_its_summary(self):
        self._open()
        row = MemoryManager.upsert(namespace='legacy.ns', key='old', value=LONG)
        self.assertIsNotNone(row['summary'])


class InjectionBudgetTests(unittest.TestCase):
    """The hook's block builder: no mid-word cuts, no silently dropped entries."""

    def _mem(self, i, value, **over):
        m = {'namespace': 'p', 'key': f'k{i}', 'value': value}
        m.update(over)
        return m

    def test_long_entries_are_shrunk_not_dropped(self):
        mems = [self._mem(i, 'word ' * 400) for i in range(8)]
        block = hook._format_block(mems)
        for i in range(8):
            self.assertIn(f'p.k{i}]', block, f'entry {i} was dropped')

    def test_block_respects_the_char_budget(self):
        mems = [self._mem(i, 'word ' * 400) for i in range(8)]
        block = hook._format_block(mems)
        # Allow for the wrapper/preamble lines, which sit outside the entry budget.
        self.assertLessEqual(len(block), hook.MAX_CHARS + 400)

    def test_entries_are_not_cut_mid_word(self):
        block = hook._format_block([self._mem(0, 'alpha beta gamma ' * 60)])
        for line in block.splitlines():
            if line.startswith('- ['):
                self.assertFalse(line.rstrip('…').endswith(('alph', 'bet', 'gamm')),
                                 line)

    def test_summary_is_preferred_over_value(self):
        block = hook._format_block(
            [self._mem(0, 'the full untruncated value', summary='the digest')])
        self.assertIn('the digest', block)
        self.assertNotIn('untruncated', block)

    def test_secret_tagged_entries_are_excluded(self):
        block = hook._format_block([self._mem(0, 'hunter2', tags='secret')])
        self.assertNotIn('p.k0]', block)

    def test_empty_memories_produce_no_block(self):
        self.assertEqual(hook._format_block([]), '')

    def test_short_entries_are_untouched(self):
        block = hook._format_block([self._mem(0, 'Postgres runs on 5432.')])
        self.assertIn('Postgres runs on 5432.', block)
        self.assertNotIn('…', block)


class ScopeResolutionTests(unittest.TestCase):
    def tearDown(self):
        for k in ('KC_PROJECT_ID', 'KC_MEMORY_NS_SCOPE'):
            os.environ.pop(k, None)

    def test_no_project_means_global(self):
        os.environ.pop('KC_PROJECT_ID', None)
        os.environ.pop('KC_MEMORY_NS_SCOPE', None)
        self.assertEqual(hook._namespace_scope(), '')

    def test_project_id_maps_to_project_namespace(self):
        os.environ['KC_PROJECT_ID'] = 'kube-coder'
        self.assertEqual(hook._namespace_scope(), 'project.kube-coder')

    def test_explicit_override_wins(self):
        os.environ['KC_PROJECT_ID'] = 'kube-coder'
        os.environ['KC_MEMORY_NS_SCOPE'] = 'team.infra'
        self.assertEqual(hook._namespace_scope(), 'team.infra')


if __name__ == '__main__':
    unittest.main()
