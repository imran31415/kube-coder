"""The embedding queue is bounded by the memory count, not the write count (#597).

`embeddings_pending` used to take one row per memory write. The worker folds
duplicates away, but it only runs when an embedding provider is configured —
which most workspaces never do — so on those the table grew forever on the
persistent volume and nothing ever errored.

The bound is enforced in `manager._enqueue_embedding`, NOT by a UNIQUE index —
a constraint would be a one-way door that breaks older code after an image
rollback. That makes these tests the only thing holding the invariant up, and
the rollback-compatibility tests the thing stopping someone from "tightening"
it back into a constraint.

Covered here:
  * upsert / update_partial re-enqueue in place: N writes over M memories leave
    at most M rows, with the newest `enqueued_at` and reset attempts/last_error
  * a rolled-back image's plain INSERT still works against a migrated DB
  * the signal survives, so configuring a provider later still embeds every
    memory that was written while it was off
  * ON DELETE CASCADE still removes a memory's pending row
  * migration 004 collapses a legacy duplicate-laden queue, keeps the newest
    row per memory, touches nothing else, and is safe to run twice

Run with:  python3 -m unittest tests.memory_pending_queue_test
(from charts/workspace/)
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import memory.store as _store_mod  # noqa: E402
from memory.store import MemoryStore  # noqa: E402
from memory.manager import MemoryManager  # noqa: E402
from rollback_compat import assert_rollback_compatible  # noqa: E402


class QueueTestCase(unittest.TestCase):
    """Isolated tmp DB per test (mirrors memory_test.MemoryTestCase)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db = os.path.join(self._tmpdir.name, 'memory.db')
        self._orig_store = MemoryManager._store
        self._orig_init = _store_mod._INITIALIZED
        self.addCleanup(self._restore)
        _store_mod._INITIALIZED = False
        self.store = MemoryStore(self.db)
        MemoryManager._store = self.store

    def _restore(self):
        MemoryManager._store = self._orig_store
        _store_mod._INITIALIZED = self._orig_init

    def _pending(self):
        with self.store.conn() as c:
            return c.execute(
                'SELECT memory_id, attempts, last_error, enqueued_at '
                'FROM embeddings_pending ORDER BY memory_id').fetchall()


class QueueIsBoundedTests(QueueTestCase):
    def test_repeated_upsert_leaves_one_row(self):
        for i in range(20):
            MemoryManager.upsert(namespace='user', key='name', value=f'v{i}')
        self.assertEqual(len(self._pending()), 1)

    def test_n_writes_over_m_memories_leaves_at_most_m_rows(self):
        # The acceptance criterion: 50 writes spread over 5 memories.
        for i in range(50):
            MemoryManager.upsert(namespace='user', key=f'k{i % 5}', value=f'v{i}')
        rows = self._pending()
        self.assertEqual(len(rows), 5)
        self.assertEqual(len({r['memory_id'] for r in rows}), 5)

    def test_update_partial_reenqueues_in_place(self):
        MemoryManager.upsert(namespace='user', key='k', value='v')
        for i in range(10):
            MemoryManager.update_partial(namespace='user', key='k', value=f'v{i}')
        self.assertEqual(len(self._pending()), 1)

    def test_metadata_only_update_does_not_enqueue(self):
        # Unchanged behaviour: no value/tags change → no re-embed needed.
        MemoryManager.upsert(namespace='user', key='k', value='v')
        with self.store.tx() as c:
            c.execute('DELETE FROM embeddings_pending')
        MemoryManager.update_partial(namespace='user', key='k', importance=0.9)
        self.assertEqual(len(self._pending()), 0)

    def test_enqueued_at_tracks_the_latest_write(self):
        MemoryManager.upsert(namespace='user', key='k', value='first')
        first = self._pending()[0]['enqueued_at']
        time.sleep(0.01)
        MemoryManager.upsert(namespace='user', key='k', value='second')
        self.assertGreater(self._pending()[0]['enqueued_at'], first)

    def test_reenqueue_resets_attempts_and_last_error(self):
        MemoryManager.upsert(namespace='user', key='k', value='v')
        with self.store.tx() as c:
            c.execute("UPDATE embeddings_pending SET attempts=3, last_error='boom'")
        MemoryManager.upsert(namespace='user', key='k', value='v2')
        row = self._pending()[0]
        # A fresh edit is a fresh attempt — a stale error must not count
        # against MAX_ATTEMPTS and suppress the retry.
        self.assertEqual(row['attempts'], 0)
        self.assertIsNone(row['last_error'])

    def test_signal_survives_for_a_provider_configured_later(self):
        # Writes with no provider must still leave every memory queued, so
        # turning embeddings on later embeds the whole store.
        for i in range(5):
            for j in range(3):
                MemoryManager.upsert(namespace='user', key=f'k{i}', value=f'v{j}')
        queued = {r['memory_id'] for r in self._pending()}
        with self.store.conn() as c:
            live = {r[0] for r in c.execute('SELECT id FROM memories')}
        self.assertEqual(queued, live)

    def test_delete_still_cascades_the_pending_row(self):
        row = MemoryManager.upsert(namespace='user', key='k', value='v')
        MemoryManager.soft_delete(namespace='user', key='k')
        with self.store.tx() as c:
            c.execute('DELETE FROM memories WHERE id=?', (row['id'],))
        self.assertEqual(len(self._pending()), 0)

    def test_memory_id_index_exists_and_is_not_unique(self):
        # Not-unique is deliberate, not an oversight. See
        # test_old_plain_insert_still_works_after_migration below.
        with self.store.conn() as c:
            idx = {r['name']: r['unique'] for r in c.execute(
                "PRAGMA index_list('embeddings_pending')")}
        self.assertIn('idx_embeddings_pending_memory', idx)
        self.assertFalse(idx['idx_embeddings_pending_memory'])

    def test_old_plain_insert_still_works_after_migration(self):
        """ROLLBACK SAFETY — do not "fix" this test by adding a UNIQUE index.

        Migration 004 bumps the DB to schema 4 on first boot and there is no
        down-migration. An operator who rolls the workspace image BACK (a real
        kube-coder operation — image versions are pinned per workspace and
        Restart-and-update exists) then runs older code that still does the
        plain INSERT below on every memory update. Under a UNIQUE(memory_id)
        index that INSERT raises IntegrityError, so memory updates fail across
        the whole workspace while creates still succeed — a silent, confusing,
        workspace-wide breakage.

        Duplicate rows are the far cheaper failure: they are the nuisance this
        issue fixes, and `_load_pending_batch` already folds them away. So the
        pre-#597 statement MUST keep working against a migrated database.
        """
        row = MemoryManager.upsert(namespace='user', key='k', value='v')
        with self.store.tx() as c:
            c.execute(                      # verbatim pre-#597 statement
                'INSERT INTO embeddings_pending (memory_id, enqueued_at) '
                'VALUES (?, ?)', (row['id'], 123.0))
        self.assertEqual(len(self._pending()), 2)   # allowed, just wasteful

    def test_helper_does_not_grow_a_queue_that_already_has_duplicates(self):
        # Duplicates predating the migration (or written by an older image
        # during a rollback) are refreshed in place, not added to. Removing
        # them is migration 004's job; the worker folds them meanwhile.
        row = MemoryManager.upsert(namespace='user', key='k', value='v')
        with self.store.tx() as c:
            for i in range(4):
                c.execute('INSERT INTO embeddings_pending (memory_id, enqueued_at) '
                          'VALUES (?, ?)', (row['id'], float(i)))
        self.assertEqual(len(self._pending()), 5)
        MemoryManager.upsert(namespace='user', key='k', value='v2')
        self.assertEqual(len(self._pending()), 5)   # no growth
        for r in self._pending():
            self.assertEqual(r['attempts'], 0)


class Migration004Tests(unittest.TestCase):
    """A v3 database full of duplicates must collapse in place, losing nothing."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db = os.path.join(self._tmpdir.name, 'legacy.db')
        self._orig_store = MemoryManager._store
        self._orig_init = _store_mod._INITIALIZED
        self.addCleanup(self._restore)
        self._build_v3()

    def _restore(self):
        MemoryManager._store = self._orig_store
        _store_mod._INITIALIZED = self._orig_init

    def _build_v3(self):
        """A genuine pre-#597 DB: migrations 001-003, no unique index, dupes."""
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE IF NOT EXISTS _meta ('
                     ' key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        _store_mod._migration_001(conn)
        _store_mod._migration_002(conn)
        _store_mod._migration_003(conn)
        conn.execute("INSERT INTO _meta(key,value) VALUES('schema_version','3')")
        now = time.time()
        # 3 memories; queued 20x, 1x and 4x respectively (the real workspace in
        # the issue had memory_id 61 queued 20 times).
        for i, dupes in enumerate((20, 1, 4), start=1):
            conn.execute(
                'INSERT INTO memories (id,namespace,key,value,kind,tags,importance,'
                'confidence,source,created_at,updated_at,version)'
                ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (i, 'user', f'k{i}', f'value {i}', 'semantic', '', 0.5, 1.0, '',
                 now, now, 1))
            for n in range(dupes):
                conn.execute('INSERT INTO embeddings_pending '
                             '(memory_id, attempts, last_error, enqueued_at) '
                             'VALUES (?,?,?,?)', (i, 0, None, now + n))
        conn.commit()
        conn.close()

    def _open(self):
        """Boot the store, which runs pending migrations."""
        _store_mod._INITIALIZED = False
        MemoryManager._store = MemoryStore(self.db)
        return MemoryManager._store

    def _rows(self, table, cols='*'):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return [tuple(r) for r in
                    conn.execute(f'SELECT {cols} FROM {table} ORDER BY id')]
        finally:
            conn.close()

    def _newest_per_memory(self):
        conn = sqlite3.connect(self.db)
        try:
            return dict(conn.execute(
                'SELECT memory_id, MAX(enqueued_at) FROM embeddings_pending '
                'GROUP BY memory_id').fetchall())
        finally:
            conn.close()

    def _memories_digest(self):
        conn = sqlite3.connect(self.db)
        try:
            blob = repr(conn.execute(
                'SELECT * FROM memories ORDER BY id').fetchall()).encode()
        finally:
            conn.close()
        return hashlib.sha256(blob).hexdigest()

    def test_collapses_duplicates_keeping_newest_per_memory(self):
        self.assertEqual(len(self._rows('embeddings_pending')), 25)
        newest = self._newest_per_memory()

        self._open()

        rows = self._rows('embeddings_pending', 'memory_id, enqueued_at')
        self.assertEqual(len(rows), 3)                       # one per memory
        self.assertEqual({r[0] for r in rows}, {1, 2, 3})
        self.assertEqual({r[0]: r[1] for r in rows}, newest)  # newest survived

    def test_memories_table_is_untouched(self):
        before = self._rows('memories')
        digest = self._memories_digest()
        self._open()
        self.assertEqual(self._rows('memories'), before)
        self.assertEqual(self._memories_digest(), digest)
        self._open()   # and still untouched after a second run
        self.assertEqual(self._memories_digest(), digest)

    def test_is_idempotent(self):
        self._open()
        after_first = self._rows('embeddings_pending')
        self._open()   # boot again on the already-migrated DB
        self.assertEqual(self._rows('embeddings_pending'), after_first)

    def test_reruns_cleanly_when_the_version_bump_was_lost(self):
        """Interrupted between migration and version bump → re-run must be safe."""
        self._open()
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE _meta SET value='3' WHERE key='schema_version'")
        conn.commit()
        conn.close()
        before = self._rows('embeddings_pending')
        self._open()
        self.assertEqual(self._rows('embeddings_pending'), before)

    def test_schema_version_advances(self):
        self._open()
        conn = sqlite3.connect(self.db)
        try:
            v = conn.execute(
                "SELECT value FROM _meta WHERE key='schema_version'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(int(v), _store_mod.SCHEMA_VERSION)

    def test_writes_after_migration_stay_bounded(self):
        store = self._open()
        for i in range(10):
            MemoryManager.upsert(namespace='user', key='k1', value=f'new {i}')
        with store.conn() as c:
            n = c.execute(
                'SELECT COUNT(*) FROM embeddings_pending WHERE memory_id=1'
            ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_rolled_back_code_can_still_write_to_a_migrated_db(self):
        """ROLLBACK SAFETY on a real upgraded DB — see the twin test above.

        This is the exact sequence an operator produces: a populated legacy
        database is migrated by the new image, then the image is rolled back
        and the old plain-INSERT code writes to it again. It must not raise.
        """
        self._open()
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(                   # verbatim pre-#597 statement
                'INSERT INTO embeddings_pending (memory_id, enqueued_at) '
                'VALUES (?, ?)', (1, 999.0))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(len(self._rows('embeddings_pending')), 4)

    def test_index_is_created_and_is_not_unique(self):
        self._open()
        conn = sqlite3.connect(self.db)
        try:
            idx = {r[1]: r[2] for r in
                   conn.execute("PRAGMA index_list('embeddings_pending')")}
        finally:
            conn.close()
        self.assertIn('idx_embeddings_pending_memory', idx)
        self.assertFalse(idx['idx_embeddings_pending_memory'])

    def test_heals_a_db_stuck_at_v4_with_a_unique_index(self):
        """A STUCK database must heal itself on open.

        This is the state an intermediate build of this change actually leaves,
        and the one a real workspace ended up in: `schema_version = 4` AND a
        UNIQUE index. Because `_migration_004` is version-gated, it never runs
        again on such a DB — so if the index-shape check lived inside it, the
        unique index would survive forever and every memory UPDATE from
        rolled-back code would keep failing. Hence the check is unconditional,
        re-asserted on every open. Fixture starts in the stuck state, not below
        it, or this tests something adjacent to the bug instead of the bug.
        """
        self._open()                      # normal upgrade to schema 4
        conn = sqlite3.connect(self.db)
        conn.execute('DELETE FROM embeddings_pending')
        conn.execute('DROP INDEX IF EXISTS idx_embeddings_pending_memory')
        conn.execute('CREATE UNIQUE INDEX idx_embeddings_pending_memory '
                     'ON embeddings_pending(memory_id)')
        conn.execute('INSERT INTO embeddings_pending (memory_id, enqueued_at)'
                     ' VALUES (1, 1.0)')
        conn.commit()
        # Precondition: schema says 4 (so 004 is skipped) and the door is shut.
        self.assertEqual(
            conn.execute("SELECT value FROM _meta WHERE key='schema_version'"
                         ).fetchone()[0], '4')
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute('INSERT INTO embeddings_pending '
                         '(memory_id, enqueued_at) VALUES (1, 2.0)')
        conn.close()

        self._open()                      # new code opens the stuck DB

        conn = sqlite3.connect(self.db)
        try:
            idx = {r[1]: r[2] for r in
                   conn.execute("PRAGMA index_list('embeddings_pending')")}
            self.assertIn('idx_embeddings_pending_memory', idx)
            self.assertFalse(idx['idx_embeddings_pending_memory'])  # healed
            # And the door is open again: old code can write.
            conn.execute('INSERT INTO embeddings_pending '
                         '(memory_id, enqueued_at) VALUES (1, 2.0)')
            conn.commit()
            self.assertEqual(conn.execute(
                'SELECT COUNT(*) FROM embeddings_pending').fetchone()[0], 2)
        finally:
            conn.close()

    def test_recreates_a_missing_index_on_open(self):
        # Same unconditional check, other direction: a DB at 4 whose index was
        # dropped by hand (as happened when restoring service) gets it back.
        self._open()
        conn = sqlite3.connect(self.db)
        conn.execute('DROP INDEX idx_embeddings_pending_memory')
        conn.commit()
        conn.close()

        self._open()

        conn = sqlite3.connect(self.db)
        try:
            idx = {r[1]: r[2] for r in
                   conn.execute("PRAGMA index_list('embeddings_pending')")}
        finally:
            conn.close()
        self.assertIn('idx_embeddings_pending_memory', idx)
        self.assertFalse(idx['idx_embeddings_pending_memory'])

    def test_empty_queue_migrates_fine(self):
        conn = sqlite3.connect(self.db)
        conn.execute('DELETE FROM embeddings_pending')
        conn.commit()
        conn.close()
        self._open()
        self.assertEqual(self._rows('embeddings_pending'), [])

    def test_migration_004_passes_the_shared_rollback_check(self):
        """The same property as the two hand-written tests above, stated
        through the reusable harness (#600).

        Kept alongside them rather than replacing them: those two spell out the
        exact statement and the exact operator sequence, which is what stops
        someone "fixing" them by adding a constraint. This one is what a NEW
        migration copies. The full per-migration audit lives in
        tests/memory_rollback_compat_test.py.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'v3.db')
            self.assertNotEqual(path, _store_mod.DEFAULT_DB_PATH)   # see #599
            report = assert_rollback_compatible(
                path,
                # Verbatim pre-#597: one plain INSERT per memory write.
                (('INSERT INTO embeddings_pending (memory_id, enqueued_at)'
                  ' VALUES (?, ?)',
                  lambda c, run: (c.execute(
                      'SELECT id FROM memories ORDER BY id').fetchone()[0], 1.0)),),
                build_previous=self._build_v3_at,
                migrate=self._migrate_to_4,
                label='_migration_004',
            )
            self.assertEqual(report['schema_version_before'], 3)
            self.assertEqual(report['schema_version_after'], 4)
            self.assertEqual(report['constraints_added'], [])

    def _build_v3_at(self, db_path):
        """`_build_v3` against an arbitrary path, for the shared harness."""
        real, self.db = self.db, db_path
        try:
            self._build_v3()
        finally:
            self.db = real

    @staticmethod
    def _migrate_to_4(db_path):
        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            _store_mod._migration_004(conn)
            conn.execute("INSERT INTO _meta(key, value) VALUES('schema_version','4')"
                         " ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
