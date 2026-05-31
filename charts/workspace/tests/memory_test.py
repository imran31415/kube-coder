"""Unit tests for the memory subsystem (memory/{store,manager}.py).

Covers the contracts the rest of the codebase relies on:
  * upsert/get/list/search basic CRUD
  * Soft-delete (deleted_at IS NULL) on reads
  * TTL filtering (expires_at) on reads — regression guard
  * top_for_prompt min_score floor + byte budget + secret-tag skip
  * History appends a version per write
  * Neighbors walks relations + filters expired
  * Concurrent writers don't deadlock under BEGIN IMMEDIATE retry

Each test gets a fresh temp SQLite file by swapping MemoryManager._store,
which keeps the production singleton intact between tests.

Run with:    python3 -m unittest tests.memory_test
(from charts/workspace/)
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import memory.store as _store_mod  # noqa: E402
from memory.manager import MemoryManager, NotFound  # noqa: E402
from memory.store import MemoryStore  # noqa: E402


class MemoryTestCase(unittest.TestCase):
    """Base: gives every test an isolated SQLite file.

    The store module gates schema bootstrap behind a process-level
    `_INITIALIZED` flag (initialize() short-circuits on the second call).
    Reset it per test so a fresh tmp DB always runs migrations.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._db_path = os.path.join(self._tmpdir.name, 'memory.db')
        self._original_store = MemoryManager._store
        self._original_initialized = _store_mod._INITIALIZED
        _store_mod._INITIALIZED = False
        MemoryManager._store = MemoryStore(self._db_path)

    def tearDown(self):
        MemoryManager._store = self._original_store
        _store_mod._INITIALIZED = self._original_initialized


class UpsertGetTests(MemoryTestCase):
    def test_upsert_creates_then_get_round_trips(self):
        m = MemoryManager.upsert(
            namespace='user', key='name', value='Imran',
            kind='preference', importance=0.8,
        )
        self.assertEqual(m['namespace'], 'user')
        self.assertEqual(m['key'], 'name')
        self.assertEqual(m['value'], 'Imran')
        self.assertEqual(m['version'], 1)

        got = MemoryManager.get(namespace='user', key='name')
        self.assertIsNotNone(got)
        self.assertEqual(got['value'], 'Imran')

    def test_upsert_bumps_version_on_replace(self):
        MemoryManager.upsert(namespace='user', key='role', value='dev')
        m2 = MemoryManager.upsert(namespace='user', key='role', value='staff')
        self.assertEqual(m2['version'], 2)
        self.assertEqual(m2['value'], 'staff')

    def test_get_filters_soft_deleted(self):
        MemoryManager.upsert(namespace='user', key='gone', value='x')
        MemoryManager.soft_delete(namespace='user', key='gone')
        self.assertIsNone(MemoryManager.get(namespace='user', key='gone'))
        # include_deleted=True surfaces the tombstone for an audit view.
        revealed = MemoryManager.get(
            namespace='user', key='gone', include_deleted=True)
        self.assertIsNotNone(revealed)

    # NOTE: an "upsert resurrects soft-deleted" test would belong here but
    # surfaces a separate FTS5-trigger interaction that needs its own fix
    # (the AFTER UPDATE trigger errors trying to re-insert a previously
    # deleted FTS rowid). Deferred — see review item: memory FTS5 trigger
    # on resurrect (MEDIUM).


class ExpiresAtTests(MemoryTestCase):
    """Regression guard: TTLs are written but were silently ignored on read.

    See top_for_prompt + manager.get/list/search/_search_like/neighbors.
    """

    def _upsert_expired(self, *, namespace='user', key='temp', value='v'):
        past = time.time() - 60
        return MemoryManager.upsert(
            namespace=namespace, key=key, value=value, expires_at=past)

    def test_get_filters_expired(self):
        self._upsert_expired()
        self.assertIsNone(MemoryManager.get(namespace='user', key='temp'))
        # include_deleted=True surfaces expired rows for an audit view.
        revealed = MemoryManager.get(
            namespace='user', key='temp', include_deleted=True)
        self.assertIsNotNone(revealed)

    def test_list_filters_expired(self):
        self._upsert_expired()
        MemoryManager.upsert(namespace='user', key='live', value='ok')
        rows = MemoryManager.list(namespace='user')
        keys = {r['key'] for r in rows}
        self.assertIn('live', keys)
        self.assertNotIn('temp', keys)

    def test_search_filters_expired(self):
        # Same value, one live, one expired — search must only return the live.
        self._upsert_expired(key='dead', value='cucumber salad')
        MemoryManager.upsert(namespace='user', key='alive', value='cucumber salad')
        results = MemoryManager.search(q='cucumber')
        keys = {r['key'] for r in results}
        self.assertIn('alive', keys)
        self.assertNotIn('dead', keys)

    def test_future_expires_at_kept(self):
        future = time.time() + 3600
        MemoryManager.upsert(
            namespace='user', key='lateexp', value='soon-but-not-yet',
            expires_at=future,
        )
        self.assertIsNotNone(MemoryManager.get(namespace='user', key='lateexp'))


class SearchTests(MemoryTestCase):
    def test_search_returns_score(self):
        MemoryManager.upsert(namespace='user', key='hobby', value='kitesurfing')
        results = MemoryManager.search(q='kitesurfing')
        self.assertEqual(len(results), 1)
        self.assertIn('_score', results[0])
        self.assertGreater(results[0]['_score'], 0)

    def test_search_filters_by_namespace(self):
        MemoryManager.upsert(namespace='user', key='cli', value='vim user')
        MemoryManager.upsert(namespace='project.foo', key='cli', value='vim default')
        only_user = MemoryManager.search(q='vim', namespaces=['user'])
        self.assertEqual([r['namespace'] for r in only_user], ['user'])

    def test_empty_query_returns_empty(self):
        MemoryManager.upsert(namespace='user', key='k', value='v')
        self.assertEqual(MemoryManager.search(q=''), [])
        self.assertEqual(MemoryManager.search(q='   '), [])


class TopForPromptTests(MemoryTestCase):
    def test_excludes_secret_tagged(self):
        MemoryManager.upsert(
            namespace='user', key='public', value='deploy via make ship',
            tags='deploy', importance=0.9, kind='procedural',
        )
        MemoryManager.upsert(
            namespace='user', key='private', value='deploy uses staging creds',
            tags='deploy,secret', importance=0.9, kind='procedural',
        )
        results = MemoryManager.top_for_prompt('how do I deploy', k=8)
        keys = {r['key'] for r in results}
        self.assertIn('public', keys)
        self.assertNotIn('private', keys)

    def test_byte_budget_enforced(self):
        big = 'a' * 1000
        for i in range(20):
            MemoryManager.upsert(
                namespace='project.x', key=f'note{i}', value=big,
                kind='procedural',
            )
        results = MemoryManager.top_for_prompt('a', k=20, max_chars=2000)
        # Each row would be ~1000+ chars; budget should cap at ~2 rows.
        total = sum(len(r['value']) for r in results)
        self.assertLessEqual(total, 4000)  # generous slack

    def test_min_score_floor_skips_irrelevant(self):
        # Two memories: one related, one unrelated. min_score=0.99 forces
        # everything below to be skipped; the related one passes the FTS
        # match but should still fall short.
        MemoryManager.upsert(
            namespace='user', key='cooking', value='roast chicken at 400F')
        MemoryManager.upsert(
            namespace='user', key='gear', value='snowboard waxed last fall')
        # min_score=0.99 is unrealistic; even the matching row won't clear it.
        results = MemoryManager.top_for_prompt('chicken', k=8, min_score=0.99)
        self.assertEqual(results, [])

    def test_empty_terms_falls_back_to_recent_preferences(self):
        MemoryManager.upsert(
            namespace='user', key='editor', value='neovim',
            kind='preference', importance=0.9,
        )
        MemoryManager.upsert(
            namespace='user', key='random', value='just a note',
            kind='semantic', importance=0.1,
        )
        # "the" is a stopword; _extract_terms returns [].
        results = MemoryManager.top_for_prompt('the', k=8)
        # Preference should surface; semantic 'random' should not.
        keys = {r['key'] for r in results}
        self.assertIn('editor', keys)
        self.assertNotIn('random', keys)


class HistoryTests(MemoryTestCase):
    def test_history_appends_each_write(self):
        MemoryManager.upsert(namespace='user', key='k', value='v1')
        MemoryManager.upsert(namespace='user', key='k', value='v2')
        MemoryManager.upsert(namespace='user', key='k', value='v3')
        h = MemoryManager.history(namespace='user', key='k')
        self.assertEqual(len(h), 3)
        # Newest first.
        self.assertEqual(h[0]['value'], 'v3')
        self.assertEqual(h[-1]['value'], 'v1')

    def test_history_returns_empty_for_unknown_key(self):
        self.assertEqual(MemoryManager.history(namespace='x', key='y'), [])


class NeighborsTests(MemoryTestCase):
    def test_walks_explicit_relations(self):
        a = MemoryManager.upsert(namespace='a', key='1', value='root')
        b = MemoryManager.upsert(namespace='a', key='2', value='child')
        MemoryManager.link(
            src_namespace='a', src_key='1',
            dst_namespace='a', dst_key='2',
            kind='related-to',
        )
        rows = MemoryManager.neighbors(namespace='a', key='1')
        self.assertEqual([r['key'] for r in rows], ['2'])
        # Sanity-check IDs aren't garbled.
        self.assertEqual(rows[0]['id'], b['id'])
        self.assertNotEqual(rows[0]['id'], a['id'])

    def test_filters_expired_neighbor(self):
        MemoryManager.upsert(namespace='a', key='root', value='x')
        # Expired child should not surface.
        MemoryManager.upsert(
            namespace='a', key='expired', value='gone',
            expires_at=time.time() - 60,
        )
        MemoryManager.link(
            src_namespace='a', src_key='root',
            dst_namespace='a', dst_key='expired',
            kind='related-to',
        )
        self.assertEqual(MemoryManager.neighbors(namespace='a', key='root'), [])


class ConcurrencyTests(MemoryTestCase):
    """Smoke test the WAL + BEGIN IMMEDIATE retry path under contention."""

    def test_two_writers_no_deadlock(self):
        errors: list = []

        def write_many(prefix):
            try:
                for i in range(50):
                    MemoryManager.upsert(
                        namespace='race', key=f'{prefix}-{i}', value='x',
                    )
            except Exception as e:  # pragma: no cover - diagnostic
                errors.append(e)

        t1 = threading.Thread(target=write_many, args=('a',))
        t2 = threading.Thread(target=write_many, args=('b',))
        t1.start(); t2.start()
        t1.join(timeout=20); t2.join(timeout=20)
        self.assertFalse(errors, f'concurrent writers raised: {errors!r}')
        self.assertFalse(t1.is_alive() or t2.is_alive(), 'writer thread hung')
        rows = MemoryManager.list(namespace='race', limit=500)
        # Soft assert: every row should have stuck, but allow for races on
        # the exact final count if SQLite ever returned BUSY past the retry
        # budget. We mostly care that the test didn't deadlock.
        self.assertGreaterEqual(len(rows), 100)


class NotFoundTests(MemoryTestCase):
    def test_update_partial_raises_for_missing_key(self):
        with self.assertRaises(NotFound):
            MemoryManager.update_partial(
                namespace='ghost', key='x', value='new')


if __name__ == '__main__':
    unittest.main()
