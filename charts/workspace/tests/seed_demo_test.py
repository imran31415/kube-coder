"""Unit tests for seed_demo.py — the idempotent demo-data seeder.

Verifies the idempotency guards (skip when data already present), the
write paths (memories upserted, task transcript files written), the
READONLY_MODE gate on main(), and the demo dataset's structural integrity.

Memory writes use the isolated-SQLite-store pattern; TASKS_DIR is
redirected to a tempdir. The real /home/dev paths are never touched.

Run with:    python3 -m unittest tests.seed_demo_test
(from charts/workspace/)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import memory.store as _store_mod  # noqa: E402
import seed_demo  # noqa: E402
from memory.manager import MemoryManager  # noqa: E402
from memory.store import MemoryStore  # noqa: E402


class SeedTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        # Isolated memory store. Resetting _INITIALIZED *before* constructing
        # the store makes its __init__ migrate the temp DB; seed_demo's own
        # initialize() then short-circuits and never touches the real DB.
        self._orig_store = MemoryManager._store
        self._orig_init = _store_mod._INITIALIZED
        _store_mod._INITIALIZED = False
        MemoryManager._store = MemoryStore(os.path.join(self._tmpdir.name, 'm.db'))
        self.addCleanup(self._restore_store)
        # Isolated tasks dir.
        self.tasks_dir = os.path.join(self._tmpdir.name, 'tasks')
        p = mock.patch.object(seed_demo, 'TASKS_DIR', self.tasks_dir)
        p.start()
        self.addCleanup(p.stop)

    def _restore_store(self):
        MemoryManager._store = self._orig_store
        _store_mod._INITIALIZED = self._orig_init


class SeedMemoriesTests(SeedTestCase):
    def test_seeds_into_empty_store(self):
        n = seed_demo.seed_memories()
        self.assertEqual(n, len(seed_demo.DEMO_MEMORIES))
        # A known entry is retrievable.
        row = MemoryManager.get(namespace='project', key='name')
        self.assertIsNotNone(row)
        self.assertEqual(row['source'], 'seed_demo')

    def test_skips_when_already_populated(self):
        MemoryManager.upsert(namespace='user', key='pre', value='existing')
        n = seed_demo.seed_memories()
        self.assertEqual(n, 0)


class SeedTasksTests(SeedTestCase):
    def test_seeds_into_empty_dir(self):
        n = seed_demo.seed_tasks()
        self.assertEqual(n, len(seed_demo.DEMO_TASKS))
        entries = [d for d in os.listdir(self.tasks_dir) if not d.startswith('.')]
        self.assertEqual(len(entries), len(seed_demo.DEMO_TASKS))
        # Each task dir has the three expected files with sane meta.
        first = sorted(entries)[0]
        td = os.path.join(self.tasks_dir, first)
        for fname in ('task.json', 'prompt.txt', 'output.log'):
            self.assertTrue(os.path.isfile(os.path.join(td, fname)), fname)
        meta = json.load(open(os.path.join(td, 'task.json')))
        self.assertEqual(meta['status'], 'completed')
        self.assertEqual(meta['source'], 'seed_demo')
        self.assertIn('finished_at', meta)

    def test_skips_when_dir_nonempty(self):
        os.makedirs(self.tasks_dir, exist_ok=True)
        os.makedirs(os.path.join(self.tasks_dir, 'existing-task'))
        self.assertEqual(seed_demo.seed_tasks(), 0)


class MainGateTests(SeedTestCase):
    def test_refuses_without_readonly_mode(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('READONLY_MODE', None)
            with mock.patch.object(seed_demo, 'seed_memories') as sm, \
                 mock.patch.object(seed_demo, 'seed_tasks') as st:
                rc = seed_demo.main()
        self.assertEqual(rc, 0)
        sm.assert_not_called()
        st.assert_not_called()

    def test_seeds_when_readonly_true(self):
        with mock.patch.dict(os.environ, {'READONLY_MODE': 'true'}, clear=False):
            rc = seed_demo.main()
        self.assertEqual(rc, 0)
        # Both stores populated.
        self.assertIsNotNone(MemoryManager.get(namespace='project', key='name'))
        entries = [d for d in os.listdir(self.tasks_dir) if not d.startswith('.')]
        self.assertEqual(len(entries), len(seed_demo.DEMO_TASKS))


class DatasetIntegrityTests(unittest.TestCase):
    def test_demo_memories_are_six_tuples(self):
        for entry in seed_demo.DEMO_MEMORIES:
            self.assertEqual(len(entry), 6)
            ns, key, value, kind, tags, importance = entry
            self.assertTrue(ns and key and value)
            self.assertIn(kind, ('semantic', 'episodic', 'procedural', 'preference'))
            self.assertTrue(0.0 <= importance <= 1.0)

    def test_demo_tasks_are_four_tuples(self):
        for entry in seed_demo.DEMO_TASKS:
            self.assertEqual(len(entry), 4)
            prompt, output, status, assistant = entry
            self.assertTrue(prompt and output)
            self.assertEqual(status, 'completed')

    def test_no_duplicate_memory_keys(self):
        keys = [(ns, k) for ns, k, *_ in seed_demo.DEMO_MEMORIES]
        self.assertEqual(len(keys), len(set(keys)))


if __name__ == '__main__':
    unittest.main()
