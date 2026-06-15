"""Additional MemoryManager tests for the methods memory_test.py doesn't reach:
refs, log_ref, stats, format_injection_block, the LIKE search fallback,
update_partial field-by-field, link-conflict, neighbors kind filter, history.

Uses the same isolated-SQLite-store pattern as memory_test.py.

Run with:    python3 -m unittest tests.manager_extra_test
(from charts/workspace/)
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import memory.store as _store_mod  # noqa: E402
from memory.manager import (  # noqa: E402
    MemoryManager, NotFound, Conflict, ValidationError,
)
from memory.store import MemoryStore  # noqa: E402


class ManagerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._db = os.path.join(self._tmpdir.name, 'memory.db')
        self._orig_store = MemoryManager._store
        self._orig_init = _store_mod._INITIALIZED
        _store_mod._INITIALIZED = False
        MemoryManager._store = MemoryStore(self._db)
        self.addCleanup(self._restore)

    def _restore(self):
        MemoryManager._store = self._orig_store
        _store_mod._INITIALIZED = self._orig_init

    def mk(self, ns='user.t', key='k', value='v', **extra):
        return MemoryManager.upsert(namespace=ns, key=key, value=value, **extra)


class UpdatePartialTests(ManagerTestCase):
    def test_updates_each_field_and_bumps_version(self):
        self.mk(value='old', kind='semantic', importance=0.2)
        row = MemoryManager.update_partial(
            namespace='user.t', key='k', value='new', tags='a,b',
            kind='preference', importance=0.9, source='tester')
        self.assertEqual(row['value'], 'new')
        self.assertEqual(row['kind'], 'preference')
        self.assertEqual(row['importance'], 0.9)
        self.assertEqual(row['version'], 2)

    def test_partial_keeps_untouched_fields(self):
        self.mk(value='keep', kind='semantic', importance=0.3)
        row = MemoryManager.update_partial(namespace='user.t', key='k', tags='x')
        self.assertEqual(row['value'], 'keep')      # unchanged
        self.assertEqual(row['kind'], 'semantic')   # unchanged

    def test_missing_raises_notfound(self):
        with self.assertRaises(NotFound):
            MemoryManager.update_partial(namespace='no', key='where', value='x')

    def test_validation_error_on_bad_kind(self):
        self.mk()
        with self.assertRaises(ValidationError):
            MemoryManager.update_partial(namespace='user.t', key='k', kind='bogus')

    def test_history_records_each_update(self):
        self.mk(value='v1')
        MemoryManager.update_partial(namespace='user.t', key='k', value='v2')
        MemoryManager.update_partial(namespace='user.t', key='k', value='v3')
        hist = MemoryManager.history(namespace='user.t', key='k')
        self.assertGreaterEqual(len(hist), 2)
        # newest version first
        self.assertEqual(hist[0]['version'], hist[0]['version'])
        self.assertGreater(hist[0]['version'], hist[-1]['version'])


class HistoryAndRefsTests(ManagerTestCase):
    def test_history_empty_for_missing(self):
        self.assertEqual(MemoryManager.history(namespace='no', key='x'), [])

    def test_refs_empty_for_missing(self):
        self.assertEqual(MemoryManager.refs(namespace='no', key='x'), [])

    def test_refs_returns_logged_accesses(self):
        self.mk()
        MemoryManager.log_ref(namespace='user.t', key='k',
                              ref_kind='task', ref_id='t1', access_kind='read')
        MemoryManager.log_ref(namespace='user.t', key='k',
                              ref_kind='api', ref_id='a1', access_kind='write')
        refs = MemoryManager.refs(namespace='user.t', key='k')
        self.assertEqual(len(refs), 2)
        kinds = {r['access_kind'] for r in refs}
        self.assertEqual(kinds, {'read', 'write'})


class LogRefTests(ManagerTestCase):
    def test_read_bumps_access_count_and_last_accessed(self):
        self.mk()
        MemoryManager.log_ref(namespace='user.t', key='k',
                              ref_kind='task', ref_id='t1', access_kind='read')
        row = MemoryManager.get(namespace='user.t', key='k')
        self.assertEqual(row['access_count'], 1)
        self.assertIsNotNone(row['last_accessed_at'])

    def test_write_does_not_bump_access_count(self):
        self.mk()
        MemoryManager.log_ref(namespace='user.t', key='k',
                              ref_kind='api', ref_id='a', access_kind='write')
        self.assertEqual(MemoryManager.get(namespace='user.t', key='k')['access_count'], 0)

    def test_invalid_access_kind_is_noop(self):
        self.mk()
        MemoryManager.log_ref(namespace='user.t', key='k',
                              ref_kind='task', ref_id='t', access_kind='delete')
        self.assertEqual(MemoryManager.refs(namespace='user.t', key='k'), [])

    def test_missing_memory_is_noop(self):
        # Must not raise when the target doesn't exist.
        MemoryManager.log_ref(namespace='ghost', key='x',
                              ref_kind='task', ref_id='t', access_kind='read')


class FormatInjectionBlockTests(unittest.TestCase):
    def test_empty_returns_empty_string(self):
        self.assertEqual(MemoryManager.format_injection_block([]), '')

    def test_renders_entries_with_tags(self):
        block = MemoryManager.format_injection_block([
            {'namespace': 'user', 'key': 'name', 'value': 'Imran', 'tags': 'profile'},
            {'namespace': 'user', 'key': 'tz', 'value': 'UTC', 'tags': ''},
        ])
        self.assertIn('<workspace_memories>', block)
        self.assertIn('[user.name] Imran (tags: profile)', block)
        self.assertIn('[user.tz] UTC', block)
        self.assertNotIn('tz] UTC (tags', block)  # no empty tag suffix
        self.assertTrue(block.endswith('</workspace_memories>\n\n'))


class SearchLikeFallbackTests(ManagerTestCase):
    def test_search_like_matches_value_key_tags(self):
        self.mk(key='alpha', value='find me here', tags='special')
        self.mk(key='beta', value='nothing relevant')
        with MemoryManager.store().conn() as c:
            rows = MemoryManager._search_like(c, 'find me', namespaces=[], kinds=[], limit=10)
        keys = {r['key'] for r in rows}
        self.assertIn('alpha', keys)
        self.assertNotIn('beta', keys)

    def test_search_like_filters_namespace_and_kind(self):
        self.mk(ns='user.a', key='x', value='term', kind='semantic')
        self.mk(ns='user.b', key='y', value='term', kind='preference')
        with MemoryManager.store().conn() as c:
            rows = MemoryManager._search_like(
                c, 'term', namespaces=['user.a'], kinds=['semantic'], limit=10)
        self.assertEqual({r['namespace'] for r in rows}, {'user.a'})


class LinkAndNeighborsTests(ManagerTestCase):
    def test_link_duplicate_raises_conflict(self):
        self.mk(key='a')
        self.mk(key='b')
        MemoryManager.link(src_namespace='user.t', src_key='a',
                           dst_namespace='user.t', dst_key='b')
        with self.assertRaises(Conflict):
            MemoryManager.link(src_namespace='user.t', src_key='a',
                               dst_namespace='user.t', dst_key='b')

    def test_link_missing_endpoint_raises_notfound(self):
        self.mk(key='a')
        with self.assertRaises(NotFound):
            MemoryManager.link(src_namespace='user.t', src_key='a',
                               dst_namespace='user.t', dst_key='ghost')

    def test_neighbors_kind_filter(self):
        self.mk(key='a')
        self.mk(key='b')
        self.mk(key='c')
        MemoryManager.link(src_namespace='user.t', src_key='a',
                           dst_namespace='user.t', dst_key='b', kind='relates')
        MemoryManager.link(src_namespace='user.t', src_key='a',
                           dst_namespace='user.t', dst_key='c', kind='blocks')
        got = MemoryManager.neighbors(namespace='user.t', key='a', kinds=['relates'])
        self.assertEqual({n['key'] for n in got}, {'b'})

    def test_neighbors_missing_root_empty(self):
        self.assertEqual(MemoryManager.neighbors(namespace='no', key='x'), [])


class StatsTests(ManagerTestCase):
    def test_stats_counts(self):
        self.mk(key='a', kind='semantic')
        self.mk(key='b', kind='preference')
        self.mk(ns='proj.x', key='c', kind='semantic')
        MemoryManager.link(src_namespace='user.t', src_key='a',
                           dst_namespace='user.t', dst_key='b')
        st = MemoryManager.stats()
        self.assertEqual(st['total'], 3)
        self.assertEqual(st['by_kind']['semantic'], 2)
        self.assertEqual(st['relations'], 1)
        namespaces = {e['namespace'] for e in st['by_namespace']}
        self.assertIn('user.t', namespaces)
        self.assertIn('proj.x', namespaces)


if __name__ == '__main__':
    unittest.main()
