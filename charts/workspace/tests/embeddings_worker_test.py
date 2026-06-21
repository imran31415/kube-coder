"""Unit tests for the Phase-2 embedding worker + hybrid search (#90).

Covers, without needing the sqlite-vec extension or any network:
  * _rrf_fuse  — reciprocal-rank fusion math (pure function)
  * EmbeddingWorker.run_once — drains embeddings_pending, writes embeddings
    rows + vectors (vector sink stubbed), is a no-op when the queue is empty
  * provider-error path — pending rows are retained, attempts bumped, and a
    row is dropped once it exhausts MAX_ATTEMPTS (poison-pill guard)
  * soft-deleted memories — their stale pending rows are dropped, not embedded
  * EmbeddingWorker.start — returns False (disabled) when no provider is set
  * MemoryManager.search — fuses a stubbed vector pass with FTS (RRF), and
    is byte-for-byte the Phase-1 ranking when the vector pass returns nothing

Run with:    python3 -m unittest tests.embeddings_worker_test
(from charts/workspace/)
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import memory.store as _store_mod  # noqa: E402
from memory.store import MemoryStore  # noqa: E402
from memory.manager import MemoryManager  # noqa: E402
from memory import embeddings_worker as ew  # noqa: E402
from memory.embeddings_worker import EmbeddingWorker, _load_pending_batch  # noqa: E402
from memory.manager import _rrf_fuse  # noqa: E402


class FakeProvider:
    """Deterministic, offline embedding provider for tests."""

    name = 'fake'
    model = 'fake-model'
    dim = 4

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError('boom')
        # Encode text length so vectors are distinct + reproducible.
        return [[float(len(t)), 1.0, 0.0, 0.0] for t in texts]


class WorkerTestCase(unittest.TestCase):
    """Isolated tmp DB per test (mirrors memory_test.MemoryTestCase), plus a
    stubbed vector sink so the worker runs without the sqlite-vec extension."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._db_path = os.path.join(self._tmpdir.name, 'memory.db')
        self._orig_store = MemoryManager._store
        self._orig_init = _store_mod._INITIALIZED
        self._orig_provider = MemoryManager._provider
        _store_mod._INITIALIZED = False
        self._store = MemoryStore(self._db_path)
        MemoryManager._store = self._store
        MemoryManager._provider = None  # disable real provider lookup in search

        # In-memory stand-in for the vec_memories table.
        self.sink = {}
        self._patches = [
            mock.patch.object(_store_mod, 'upsert_vector',
                              side_effect=lambda c, eid, vec: self.sink.__setitem__(eid, list(vec))),
            mock.patch.object(_store_mod, 'vectors_available', return_value=True),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        MemoryManager._store = self._orig_store
        MemoryManager._provider = self._orig_provider
        _store_mod._INITIALIZED = self._orig_init


# ───────────────────────────────────────────────────────────────────────────
# RRF fusion (pure)
# ───────────────────────────────────────────────────────────────────────────

class RRFFuseTests(unittest.TestCase):
    def test_agreement_outranks_single_list(self):
        # id 2 is #1 in the vector list and #2 in FTS; id 1 only tops FTS.
        scores = _rrf_fuse([1, 2, 3], [2, 9, 8])
        self.assertGreater(scores[2], scores[1])

    def test_absent_ids_score_from_present_lists_only(self):
        scores = _rrf_fuse([1], [2])
        # k=60 → each appears once at rank 0 → 1/60 each.
        self.assertAlmostEqual(scores[1], 1.0 / 60)
        self.assertAlmostEqual(scores[2], 1.0 / 60)

    def test_rank_decay_is_monotonic(self):
        scores = _rrf_fuse([10, 20, 30])
        self.assertGreater(scores[10], scores[20])
        self.assertGreater(scores[20], scores[30])

    def test_empty_inputs(self):
        self.assertEqual(_rrf_fuse([], []), {})


# ───────────────────────────────────────────────────────────────────────────
# Worker drain
# ───────────────────────────────────────────────────────────────────────────

class RunOnceTests(WorkerTestCase):
    def _pending_count(self):
        with self._store.conn() as c:
            return c.execute('SELECT COUNT(*) FROM embeddings_pending').fetchone()[0]

    def _embeddings_count(self):
        with self._store.conn() as c:
            return c.execute('SELECT COUNT(*) FROM embeddings').fetchone()[0]

    def test_empty_queue_is_noop(self):
        res = EmbeddingWorker.run_once(FakeProvider(), self._store)
        self.assertEqual(res, {'embedded': 0, 'failed': 0, 'dropped': 0,
                               'remaining': 0})

    def test_drains_queue_and_writes_vectors(self):
        MemoryManager.upsert(namespace='user', key='name', value='Imran')
        MemoryManager.upsert(namespace='proj', key='lang', value='Go and Python')
        self.assertEqual(self._pending_count(), 2)

        res = EmbeddingWorker.run_once(FakeProvider(), self._store)

        self.assertEqual(res['embedded'], 2)
        self.assertEqual(res['remaining'], 0)
        self.assertEqual(self._pending_count(), 0)
        self.assertEqual(self._embeddings_count(), 2)
        self.assertEqual(len(self.sink), 2)  # two vectors stored

    def test_reembed_reuses_embedding_id(self):
        MemoryManager.upsert(namespace='user', key='name', value='Imran')
        EmbeddingWorker.run_once(FakeProvider(), self._store)
        first_ids = set(self.sink.keys())
        # Update → enqueues again → re-embed must reuse the same embedding row.
        MemoryManager.upsert(namespace='user', key='name', value='Imran K')
        EmbeddingWorker.run_once(FakeProvider(), self._store)
        self.assertEqual(self._embeddings_count(), 1)
        self.assertEqual(set(self.sink.keys()), first_ids)

    def test_provider_error_retains_and_bumps_attempts(self):
        MemoryManager.upsert(namespace='user', key='name', value='Imran')
        res = EmbeddingWorker.run_once(FakeProvider(fail=True), self._store)
        self.assertEqual(res['failed'], 1)
        self.assertEqual(res['embedded'], 0)
        self.assertEqual(self._pending_count(), 1)  # kept for retry
        with self._store.conn() as c:
            attempts, err = c.execute(
                'SELECT attempts, last_error FROM embeddings_pending'
            ).fetchone()
        self.assertEqual(attempts, 1)
        self.assertIn('boom', err)

    def test_poison_pill_dropped_after_max_attempts(self):
        MemoryManager.upsert(namespace='user', key='name', value='Imran')
        for _ in range(ew.MAX_ATTEMPTS):
            EmbeddingWorker.run_once(FakeProvider(fail=True), self._store)
        # Exhausted retries → row dropped so the queue can't wedge forever.
        self.assertEqual(self._pending_count(), 0)

    def test_soft_deleted_memory_pending_is_dropped(self):
        MemoryManager.upsert(namespace='user', key='gone', value='temp')
        MemoryManager.soft_delete(namespace='user', key='gone')
        res = EmbeddingWorker.run_once(FakeProvider(), self._store)
        self.assertEqual(res['dropped'], 1)
        self.assertEqual(res['embedded'], 0)
        self.assertEqual(self._pending_count(), 0)
        self.assertEqual(len(self.sink), 0)


class LoadBatchTests(WorkerTestCase):
    def test_folds_multiple_pending_rows_per_memory(self):
        MemoryManager.upsert(namespace='user', key='k', value='v1')
        MemoryManager.upsert(namespace='user', key='k', value='v2')  # 2nd enqueue
        with self._store.conn() as c:
            items = _load_pending_batch(c, 32)
        self.assertEqual(len(items), 1)          # one work item
        self.assertEqual(len(items[0]['pids']), 2)  # both pending rows folded in

    def test_batch_cap_limits_distinct_memories(self):
        for i in range(5):
            MemoryManager.upsert(namespace='user', key=f'k{i}', value=str(i))
        with self._store.conn() as c:
            items = _load_pending_batch(c, 2)
        self.assertEqual(len(items), 2)


class StartTests(WorkerTestCase):
    def test_start_disabled_without_provider(self):
        # No KC_EMBED_PROVIDER in env → get_provider() → None → not started.
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(EmbeddingWorker.start(provider=None))
        self.assertFalse(EmbeddingWorker._started)


# ───────────────────────────────────────────────────────────────────────────
# Hybrid search fusion
# ───────────────────────────────────────────────────────────────────────────

class HybridSearchTests(WorkerTestCase):
    def test_fts_only_when_vector_pass_empty(self):
        MemoryManager.upsert(namespace='user', key='a', value='kubernetes deploys')
        MemoryManager.upsert(namespace='user', key='b', value='python testing')
        with mock.patch.object(MemoryManager, '_vector_search', return_value=[]):
            res = MemoryManager.search(q='kubernetes', limit=5)
        self.assertTrue(res)
        self.assertEqual(res[0]['key'], 'a')

    def test_vector_only_hit_is_included_and_ranked(self):
        # 'cluster' shares no keyword with the query, so FTS misses it; the
        # stubbed vector pass surfaces it and fusion must include it.
        a = MemoryManager.upsert(namespace='user', key='a', value='kubernetes deploys')
        c = MemoryManager.upsert(namespace='user', key='c',
                                 value='container cluster orchestration')
        with mock.patch.object(MemoryManager, '_vector_search',
                               return_value=[c['id'], a['id']]):
            res = MemoryManager.search(q='kubernetes', limit=5)
        keys = {r['key'] for r in res}
        self.assertIn('c', keys)   # vector-only hit surfaced
        self.assertIn('a', keys)
        for r in res:
            self.assertIn('_score', r)

    def test_vector_filters_respect_namespace_gate(self):
        MemoryManager.upsert(namespace='user', key='a', value='kubernetes deploys')
        other = MemoryManager.upsert(namespace='secret', key='z',
                                     value='unrelated note')
        # Vector pass returns a row outside the requested namespace; the
        # ns-gated _fetch_by_ids must drop it.
        with mock.patch.object(MemoryManager, '_vector_search',
                               return_value=[other['id']]):
            res = MemoryManager.search(q='kubernetes', namespaces=['user'], limit=5)
        self.assertTrue(all(r['namespace'] == 'user' for r in res))


if __name__ == '__main__':
    unittest.main()
