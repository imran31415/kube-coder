"""Unit tests for memory lifecycle ops (#107): GC/purge, unlink, export/import.

Covers:
  * unlink / unlink_by_id — relation removal by (src,dst[,kind]) and by id
  * purge_deleted — hard-deletes soft-deleted rows, cascades children,
    prunes orphaned embeddings_pending, VACUUMs, reports bytes reclaimed,
    and respects the older_than_days cutoff
  * export_json / import_json — faithful round-trip (memories + relations),
    merge vs skip modes, and resilience to bad rows

Run with:    python3 -m unittest tests.memory_lifecycle_test
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
from memory.store import MemoryStore  # noqa: E402
from memory.manager import MemoryManager, ValidationError  # noqa: E402


class LifecycleTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._db_path = os.path.join(self._tmpdir.name, 'memory.db')
        self._orig_store = MemoryManager._store
        self._orig_init = _store_mod._INITIALIZED
        _store_mod._INITIALIZED = False
        MemoryManager._store = MemoryStore(self._db_path)

    def tearDown(self):
        MemoryManager._store = self._orig_store
        _store_mod._INITIALIZED = self._orig_init

    def _count(self, table):
        with MemoryManager.store().conn() as c:
            return c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]


# ───────────────────────────────────────────────────────────────────────────
# Unlink
# ───────────────────────────────────────────────────────────────────────────

class UnlinkTests(LifecycleTestCase):
    def setUp(self):
        super().setUp()
        MemoryManager.upsert(namespace='a', key='x', value='vx')
        MemoryManager.upsert(namespace='b', key='y', value='vy')

    def test_unlink_by_pair_removes_edge(self):
        MemoryManager.link(src_namespace='a', src_key='x',
                           dst_namespace='b', dst_key='y', kind='related-to')
        self.assertEqual(self._count('relations'), 1)
        removed = MemoryManager.unlink(src_namespace='a', src_key='x',
                                       dst_namespace='b', dst_key='y')
        self.assertEqual(removed, 1)
        self.assertEqual(self._count('relations'), 0)

    def test_unlink_specific_kind_only(self):
        MemoryManager.link(src_namespace='a', src_key='x',
                           dst_namespace='b', dst_key='y', kind='related-to')
        MemoryManager.link(src_namespace='a', src_key='x',
                           dst_namespace='b', dst_key='y', kind='caused-by')
        removed = MemoryManager.unlink(src_namespace='a', src_key='x',
                                       dst_namespace='b', dst_key='y',
                                       kind='caused-by')
        self.assertEqual(removed, 1)
        self.assertEqual(self._count('relations'), 1)  # related-to survives

    def test_unlink_no_match_returns_zero(self):
        removed = MemoryManager.unlink(src_namespace='a', src_key='x',
                                       dst_namespace='b', dst_key='y')
        self.assertEqual(removed, 0)

    def test_relations_lists_with_ids_and_direction(self):
        rel = MemoryManager.link(src_namespace='a', src_key='x',
                                 dst_namespace='b', dst_key='y', kind='related-to')
        # From a/x the edge is outgoing; from b/y it's incoming.
        out = MemoryManager.relations(namespace='a', key='x')
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['id'], rel['id'])
        self.assertEqual(out[0]['direction'], 'out')
        self.assertEqual((out[0]['other_namespace'], out[0]['other_key']),
                         ('b', 'y'))
        inc = MemoryManager.relations(namespace='b', key='y')
        self.assertEqual(inc[0]['direction'], 'in')
        self.assertEqual((inc[0]['other_namespace'], inc[0]['other_key']),
                         ('a', 'x'))

    def test_relations_excludes_edges_to_deleted(self):
        MemoryManager.link(src_namespace='a', src_key='x',
                           dst_namespace='b', dst_key='y')
        MemoryManager.soft_delete(namespace='b', key='y')
        self.assertEqual(MemoryManager.relations(namespace='a', key='x'), [])

    def test_unlink_by_id_scoped_to_src(self):
        rel = MemoryManager.link(src_namespace='a', src_key='x',
                                 dst_namespace='b', dst_key='y')
        # Wrong owner → no delete.
        self.assertEqual(
            MemoryManager.unlink_by_id(relation_id=rel['id'],
                                       namespace='b', key='y'), 0)
        # Correct owner → deletes.
        self.assertEqual(
            MemoryManager.unlink_by_id(relation_id=rel['id'],
                                       namespace='a', key='x'), 1)


# ───────────────────────────────────────────────────────────────────────────
# Purge / GC
# ───────────────────────────────────────────────────────────────────────────

class PurgeTests(LifecycleTestCase):
    def test_purge_hard_deletes_and_cascades(self):
        MemoryManager.upsert(namespace='u', key='keep', value='stays')
        MemoryManager.upsert(namespace='u', key='gone', value='temp')
        MemoryManager.soft_delete(namespace='u', key='gone')
        # Pre: row is tombstoned but still present; history rows exist.
        self.assertEqual(self._count('memories'), 2)
        self.assertGreater(self._count('memory_history'), 0)

        res = MemoryManager.purge_deleted()

        self.assertEqual(res['purged_memories'], 1)
        self.assertEqual(self._count('memories'), 1)  # only the live one
        self.assertTrue(res['vacuumed'])
        # History for the purged row cascaded away; the live row's remains.
        with MemoryManager.store().conn() as c:
            rows = c.execute(
                'SELECT COUNT(*) FROM memory_history h '
                'JOIN memories m ON m.id = h.memory_id').fetchone()[0]
        self.assertEqual(rows, self._count('memory_history'))

    def test_purge_prunes_orphaned_pending(self):
        MemoryManager.upsert(namespace='u', key='gone', value='temp')
        MemoryManager.soft_delete(namespace='u', key='gone')
        self.assertGreater(self._count('embeddings_pending'), 0)
        MemoryManager.purge_deleted()
        self.assertEqual(self._count('embeddings_pending'), 0)

    def test_purge_respects_older_than_cutoff(self):
        MemoryManager.upsert(namespace='u', key='recent', value='v')
        MemoryManager.soft_delete(namespace='u', key='recent')
        # deleted just now → a 1-day cutoff must NOT purge it.
        res = MemoryManager.purge_deleted(older_than_days=1)
        self.assertEqual(res['purged_memories'], 0)
        self.assertEqual(self._count('memories'), 1)

    def test_purge_empty_is_noop(self):
        MemoryManager.upsert(namespace='u', key='live', value='v')
        res = MemoryManager.purge_deleted()
        self.assertEqual(res['purged_memories'], 0)
        self.assertEqual(self._count('memories'), 1)

    def test_purge_reports_bytes_reclaimed(self):
        for i in range(50):
            MemoryManager.upsert(namespace='u', key=f'k{i}', value='x' * 2000)
            MemoryManager.soft_delete(namespace='u', key=f'k{i}')
        res = MemoryManager.purge_deleted()
        self.assertEqual(res['purged_memories'], 50)
        self.assertGreaterEqual(res['bytes_reclaimed'], 0)
        self.assertIn('db_size_bytes', res)


# ───────────────────────────────────────────────────────────────────────────
# Export / Import
# ───────────────────────────────────────────────────────────────────────────

class ExportImportTests(LifecycleTestCase):
    def _seed(self):
        MemoryManager.upsert(namespace='user', key='name', value='Imran',
                             kind='preference', tags='a,b', importance=0.9)
        MemoryManager.upsert(namespace='proj', key='lang', value='Go')
        MemoryManager.link(src_namespace='user', src_key='name',
                           dst_namespace='proj', dst_key='lang',
                           kind='related-to')

    def test_export_shape(self):
        self._seed()
        exp = MemoryManager.export_json()
        self.assertEqual(exp['version'], 1)
        self.assertEqual(len(exp['memories']), 2)
        self.assertEqual(len(exp['relations']), 1)
        r = exp['relations'][0]
        self.assertEqual((r['src_namespace'], r['src_key']), ('user', 'name'))
        self.assertEqual((r['dst_namespace'], r['dst_key']), ('proj', 'lang'))

    def test_export_excludes_deleted(self):
        self._seed()
        MemoryManager.soft_delete(namespace='proj', key='lang')
        exp = MemoryManager.export_json()
        keys = {(m['namespace'], m['key']) for m in exp['memories']}
        self.assertNotIn(('proj', 'lang'), keys)
        # Relation to the deleted memory is dropped too.
        self.assertEqual(len(exp['relations']), 0)

    def test_round_trip_into_fresh_db(self):
        self._seed()
        exp = MemoryManager.export_json()

        # Fresh DB.
        _store_mod._INITIALIZED = False
        other = os.path.join(self._tmpdir.name, 'other.db')
        MemoryManager._store = MemoryStore(other)

        res = MemoryManager.import_json(exp)
        self.assertEqual(res['imported'], 2)
        self.assertEqual(res['relations_imported'], 1)
        got = MemoryManager.get(namespace='user', key='name')
        self.assertEqual(got['value'], 'Imran')
        self.assertEqual(got['tags'], 'a,b')
        nb = MemoryManager.neighbors(namespace='user', key='name')
        self.assertEqual({(n['namespace'], n['key']) for n in nb},
                         {('proj', 'lang')})

    def test_import_skip_mode_preserves_existing(self):
        MemoryManager.upsert(namespace='user', key='name', value='Original')
        res = MemoryManager.import_json(
            {'memories': [{'namespace': 'user', 'key': 'name',
                           'value': 'Replaced'}]}, mode='skip')
        self.assertEqual(res['skipped'], 1)
        self.assertEqual(res['imported'], 0)
        self.assertEqual(
            MemoryManager.get(namespace='user', key='name')['value'], 'Original')

    def test_import_merge_mode_overwrites(self):
        MemoryManager.upsert(namespace='user', key='name', value='Original')
        res = MemoryManager.import_json(
            {'memories': [{'namespace': 'user', 'key': 'name',
                           'value': 'Replaced'}]}, mode='merge')
        self.assertEqual(res['imported'], 1)
        self.assertEqual(
            MemoryManager.get(namespace='user', key='name')['value'], 'Replaced')

    def test_import_bad_rows_are_counted_not_fatal(self):
        res = MemoryManager.import_json({'memories': [
            {'namespace': 'ok', 'key': 'good', 'value': 'v'},
            {'namespace': 'BAD NS!!', 'key': 'x', 'value': 'v'},  # invalid ns
            'not-a-dict',
        ]})
        self.assertEqual(res['imported'], 1)
        self.assertEqual(res['failed'], 2)
        self.assertTrue(res['errors'])

    def test_import_rejects_non_object(self):
        with self.assertRaises(ValidationError):
            MemoryManager.import_json([])  # type: ignore[arg-type]

    def test_import_rejects_bad_mode(self):
        with self.assertRaises(ValidationError):
            MemoryManager.import_json({'memories': []}, mode='nope')


if __name__ == '__main__':
    unittest.main()
