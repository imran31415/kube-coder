"""Every memory schema migration must stay writable by older code (#600).

Migrations have no down-step, and kube-coder rolls images back as a routine
operation (per-workspace pinned tags + "Restart & update"). So a migration
that tightens the schema is a one-way door: the workspace migrates, the
operator rolls back, and older code's writes start failing in production.

This file is the standing audit. For each migration it builds a database at
the PREVIOUS schema version, lets the previous version's statements write to
it, applies the migration, and runs those same statements again. The harness
is `tests/rollback_compat.py`; the convention is in CONTRIBUTING.md.

The tests that matter most here are the NEGATIVE ones. A check that passes on
all four existing migrations proves nothing on its own — it might be asserting
nothing at all. `HarnessCatchesRealBreakageTests` builds deliberately
incompatible migrations (a UNIQUE index, a NOT NULL column, a CHECK
constraint) and asserts the harness FAILS on each. If someone weakens the
harness into a no-op, those tests go red.

`TrapTests` pins the two non-obvious facts that cost #598 three review
rounds: `CREATE INDEX IF NOT EXISTS` is a silent no-op under a UNIQUE index
of the same name, and a repair for that cannot live in a version-gated
migration.

No test here opens a real database. Fixtures are built in `tempfile` from the
project's own migration functions, and the harness refuses to write anywhere
under `/home/dev` (#599).

Run with:  python3 -m unittest tests.memory_rollback_compat_test
(from charts/workspace/)
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import memory.store as _store  # noqa: E402
from rollback_compat import (  # noqa: E402
    HarnessMisuse,
    RollbackIncompatible,
    assert_rollback_compatible,
)

_META_DDL = ('CREATE TABLE IF NOT EXISTS _meta ('
             ' key TEXT PRIMARY KEY, value TEXT NOT NULL)')
_SET_VERSION = ("INSERT INTO _meta(key, value) VALUES('schema_version', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value")

NS = 'user.rollbackprobe'
ANCHOR_KEY = 'anchor'


def _probe_key(run: str) -> str:
    """Distinct per replay so `UNIQUE(namespace, key)` — which predates every
    migration under test — does not masquerade as a regression."""
    return 'probe-{}'.format(run)


def _probe_id(conn, run):
    return conn.execute('SELECT id FROM memories WHERE namespace=? AND key=?',
                        (NS, _probe_key(run))).fetchone()[0]


def _anchor_id(conn, run=None):
    """The memory the fixture already held before the migration ran."""
    return conn.execute('SELECT id FROM memories WHERE namespace=? AND key=?',
                        (NS, ANCHOR_KEY)).fetchone()[0]


# The statements older code executes on the write path, with the column lists
# spelled out the way that code spells them. Reconstructed from
# memory/manager.py as it stood before these migrations: the pre-#597
# `embeddings_pending` INSERT is verbatim (it is the statement the near-miss
# broke), the rest are the surrounding writes that share the transaction.
#
# `summary` is deliberately absent from the memories INSERT: code older than
# migration 003 does not know that column exists, which is exactly the
# ignorance a rollback reintroduces.
LEGACY_WRITES = (
    ('INSERT INTO memories ('
     '  namespace, key, value, kind, tags, importance,'
     '  confidence, source, created_at, updated_at, version, expires_at'
     ') VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
     lambda c, run: (NS, _probe_key(run), 'a value', 'semantic', '',
                     0.5, 1.0, '', 1.0, 1.0, 1, None)),

    ('INSERT INTO memory_history ('
     '  memory_id, version, value, tags, importance, confidence,'
     '  updated_at, updated_by, op'
     ') VALUES (?,?,?,?,?,?,?,?,?)',
     lambda c, run: (_probe_id(c, run), 1, 'a value', '', 0.5, 1.0,
                     1.0, 'unknown', 'create')),

    # Verbatim pre-#597. The statement the UNIQUE index would have killed.
    ('INSERT INTO embeddings_pending (memory_id, enqueued_at) VALUES (?, ?)',
     lambda c, run: (_probe_id(c, run), 1.0)),

    ('INSERT INTO memory_refs ('
     '  memory_id, ref_kind, ref_id, access_kind, at'
     ') VALUES (?,?,?,?,?)',
     lambda c, run: (_probe_id(c, run), 'task', 't-1', 'write', 1.0)),

    ('INSERT INTO relations ('
     '  src_id, dst_id, kind, weight, created_at, created_by'
     ') VALUES (?,?,?,?,?,?)',
     lambda c, run: (_probe_id(c, run), _probe_id(c, run),
                     'relates-{}'.format(run), 1.0, 1.0, 'unknown')),

    ('INSERT INTO embeddings ('
     '  memory_id, model, dim, created_at'
     ') VALUES (?,?,?,?)',
     lambda c, run: (_probe_id(c, run), 'test-model', 1024, 1.0)),

    ('UPDATE memories SET'
     '  value=?, kind=?, tags=?, importance=?, confidence=?,'
     '  source=?, updated_at=?, version=?, expires_at=?, deleted_at=NULL'
     ' WHERE id=?',
     lambda c, run: ('a new value', 'semantic', '', 0.5, 1.0, '', 2.0, 2,
                     None, _probe_id(c, run))),

    ('UPDATE memories SET deleted_at=?, updated_at=?, version=? WHERE id=?',
     lambda c, run: (3.0, 3.0, 3, _probe_id(c, run))),

    # --- writes against a memory that ALREADY EXISTED before the migration ---
    #
    # This half is where a rollback actually bites, and leaving it out makes
    # the whole check much weaker: every statement above touches a row it just
    # created, with an id the pre-migration database never saw, so a
    # per-row constraint like `UNIQUE(memory_id)` slips straight past. Older
    # code mostly writes to memories that are already there. Re-enqueueing an
    # existing memory is the precise statement the #597 UNIQUE index broke.
    ('INSERT INTO embeddings_pending (memory_id, enqueued_at) VALUES (?, ?)',
     lambda c, run: (_anchor_id(c), 4.0)),

    ('INSERT INTO memory_history ('
     '  memory_id, version, value, tags, importance, confidence,'
     '  updated_at, updated_by, op'
     ') VALUES (?,?,?,?,?,?,?,?,?)',
     lambda c, run: (_anchor_id(c), 2, 'edited', '', 0.5, 1.0,
                     4.0, 'unknown', 'update')),

    ('INSERT INTO memory_refs ('
     '  memory_id, ref_kind, ref_id, access_kind, at'
     ') VALUES (?,?,?,?,?)',
     lambda c, run: (_anchor_id(c), 'task', 't-2', 'write', 4.0)),

    ('UPDATE memories SET'
     '  value=?, kind=?, tags=?, importance=?, confidence=?,'
     '  source=?, updated_at=?, version=?, expires_at=?, deleted_at=NULL'
     ' WHERE id=?',
     lambda c, run: ('edited by rolled-back code', 'semantic', '', 0.5, 1.0,
                     '', 4.0, 2, None, _anchor_id(c))),
)


def _build_at(version: int):
    """A database at exactly `version`, built by the project's own migrations.

    Seeded with one memory and one queued row, because a workspace that gets
    upgraded is never empty — and rows written before the migration are what
    a constraint added by the migration has to coexist with.
    """
    def _build(db_path: str) -> None:
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            conn.execute(_META_DDL)
            for n in range(1, version + 1):
                getattr(_store, '_migration_{:03d}'.format(n))(conn)
            conn.execute(
                'INSERT INTO memories ('
                '  namespace, key, value, kind, tags, importance,'
                '  confidence, source, created_at, updated_at, version'
                ') VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (NS, ANCHOR_KEY, 'written before the upgrade', 'semantic', '',
                 0.5, 1.0, '', 0.0, 0.0, 1))
            conn.execute(
                'INSERT INTO embeddings_pending (memory_id, enqueued_at)'
                ' VALUES (?, ?)',
                (conn.execute('SELECT id FROM memories WHERE namespace=? AND key=?',
                              (NS, ANCHOR_KEY)).fetchone()[0], 0.0))
            conn.execute(_SET_VERSION, (str(version),))
        finally:
            conn.close()
    return _build


def _apply(fn, version: int):
    """Apply one migration function and bump the version, as `_migrate` does."""
    def _migrate(db_path: str) -> None:
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            fn(conn)
            conn.execute(_SET_VERSION, (str(version),))
        finally:
            conn.close()
    return _migrate


class _ScratchDBTestCase(unittest.TestCase):
    """One throwaway directory per test. Never the workspace's real store."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_init = _store._INITIALIZED
        self.addCleanup(self._restore)

    def _restore(self):
        _store._INITIALIZED = self._orig_init

    def db(self, name='scratch.db'):
        path = os.path.join(self._tmp.name, name)
        # Assert WHERE before anything writes, so a broken resolver fails loudly
        # instead of redirecting onto the live database (#599).
        self.assertNotEqual(path, _store.DEFAULT_DB_PATH)
        self.assertTrue(path.startswith(self._tmp.name))
        return path


class ExistingMigrationsTests(_ScratchDBTestCase):
    """The audit. Every migration in memory/store.py, one boundary at a time."""

    # 001 is the bootstrap — it runs against an empty file, so there is no
    # "previous version" whose writes could break. It is covered by
    # `test_migration_001_is_the_bootstrap` below instead of by a replay.
    BOUNDARIES = (
        (2, _store._migration_002),
        (3, _store._migration_003),
        (4, _store._migration_004),
    )

    def test_each_migration_is_rollback_compatible(self):
        for version, fn in self.BOUNDARIES:
            with self.subTest(migration=version):
                report = assert_rollback_compatible(
                    self.db('m{:03d}.db'.format(version)),
                    LEGACY_WRITES,
                    build_previous=_build_at(version - 1),
                    migrate=_apply(fn, version),
                    label='_migration_{:03d}'.format(version),
                )
                self.assertEqual(report['schema_version_before'], version - 1)
                self.assertEqual(report['schema_version_after'], version)
                self.assertEqual(report['constraints_added'], [])

    def test_every_old_version_survives_a_jump_to_head(self):
        """The realistic upgrade: a workspace several versions behind boots the
        newest image, then gets rolled back to the code it was running."""
        for version in range(1, _store.SCHEMA_VERSION):
            with self.subTest(from_version=version):
                report = assert_rollback_compatible(
                    self.db('head-from-{}.db'.format(version)),
                    LEGACY_WRITES,
                    build_previous=_build_at(version),
                    label='v{} -> head'.format(version),
                )
                self.assertEqual(report['schema_version_after'],
                                 _store.SCHEMA_VERSION)
                self.assertEqual(report['constraints_added'], [])

    def test_migration_001_is_the_bootstrap(self):
        """No previous version exists below 001, so rollback safety is vacuous.

        Recorded as a fact rather than skipped: if 001 ever stops being the
        initial schema, the audit above needs a boundary for it.
        """
        path = self.db('bootstrap.db')
        conn = sqlite3.connect(path, isolation_level=None)
        try:
            conn.execute(_META_DDL)
            _store._migration_001(conn)
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertIn('memories', tables)
        self.assertIn('embeddings_pending', tables)

    def test_the_audit_covers_every_migration_that_exists(self):
        """A guard that silently stops covering new migrations is decoration."""
        defined = sorted(int(n.rsplit('_', 1)[1]) for n in dir(_store)
                         if n.startswith('_migration_'))
        self.assertEqual(defined, list(range(1, _store.SCHEMA_VERSION + 1)),
                         'SCHEMA_VERSION and the _migration_* functions disagree')
        covered = [1] + [v for v, _ in self.BOUNDARIES]
        self.assertEqual(covered, defined, (
            'a migration exists that this audit does not check — add it to '
            'ExistingMigrationsTests.BOUNDARIES'))


# --------------------------------------------------------------------------
# Deliberately incompatible migrations. These are NOT shipped anywhere; they
# exist so the harness has to prove it catches the real failure shapes.
# --------------------------------------------------------------------------

def _bad_unique_index(conn):
    """The first revision of #598, restored. The near-miss itself.

    Collapses duplicates and then bounds the queue with a UNIQUE index —
    correct-looking, and a one-way door: older code's plain INSERT dies.
    """
    conn.execute('DELETE FROM embeddings_pending WHERE id NOT IN ('
                 '  SELECT MAX(id) FROM embeddings_pending GROUP BY memory_id)')
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_embeddings_pending_memory'
                 '  ON embeddings_pending(memory_id)')


def _bad_not_null_column(conn):
    """Add a required column with no default, via the table rebuild SQLite
    forces you into. Older code's INSERT does not supply it."""
    conn.executescript("""
        ALTER TABLE memory_refs RENAME TO memory_refs_old;
        CREATE TABLE memory_refs (
            id          INTEGER PRIMARY KEY,
            memory_id   INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            ref_kind    TEXT NOT NULL,
            ref_id      TEXT NOT NULL,
            access_kind TEXT NOT NULL,
            at          REAL NOT NULL,
            actor       TEXT NOT NULL
        );
        INSERT INTO memory_refs
            SELECT id, memory_id, ref_kind, ref_id, access_kind, at, 'legacy'
            FROM memory_refs_old;
        DROP TABLE memory_refs_old;
    """)


def _bad_check_constraint(conn):
    """Add a CHECK older rows happen to satisfy but older writes do not."""
    conn.executescript("""
        ALTER TABLE embeddings RENAME TO embeddings_old;
        CREATE TABLE embeddings (
            id          INTEGER PRIMARY KEY,
            memory_id   INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            model       TEXT NOT NULL CHECK (model LIKE 'voyage-%'),
            dim         INTEGER NOT NULL,
            created_at  REAL NOT NULL,
            UNIQUE(memory_id, model)
        );
        DROP TABLE embeddings_old;
    """)


def _does_nothing(conn):
    """A migration that forgot to do anything."""


class HarnessCatchesRealBreakageTests(_ScratchDBTestCase):
    """Mutation check. The harness must FAIL on each of these.

    Without this class the audit above is decoration: a helper that asserts
    nothing passes every migration ever written.
    """

    def _run_bad(self, fn, name):
        return assert_rollback_compatible(
            self.db(name + '.db'),
            LEGACY_WRITES,
            build_previous=_build_at(_store.SCHEMA_VERSION),
            migrate=_apply(fn, _store.SCHEMA_VERSION + 1),
            label=name,
        )

    def test_catches_a_unique_index(self):
        with self.assertRaises(RollbackIncompatible) as ctx:
            self._run_bad(_bad_unique_index, 'unique_index')
        msg = str(ctx.exception)
        self.assertIn('UNIQUE constraint failed', msg)
        self.assertIn('UNIQUE index idx_embeddings_pending_memory', msg)

    def test_catches_a_not_null_column_without_a_default(self):
        with self.assertRaises(RollbackIncompatible) as ctx:
            self._run_bad(_bad_not_null_column, 'not_null')
        msg = str(ctx.exception)
        self.assertIn('NOT NULL constraint failed', msg)
        self.assertIn('memory_refs.actor', msg)

    def test_catches_an_added_check_constraint(self):
        with self.assertRaises(RollbackIncompatible) as ctx:
            self._run_bad(_bad_check_constraint, 'check_constraint')
        self.assertIn('CHECK constraint failed', str(ctx.exception))

    def test_rejects_a_migration_that_did_nothing(self):
        """A no-op `migrate` would let every replay pass trivially."""
        with self.assertRaises(HarnessMisuse) as ctx:
            assert_rollback_compatible(
                self.db('noop.db'),
                LEGACY_WRITES,
                build_previous=_build_at(_store.SCHEMA_VERSION),
                migrate=lambda p: _does_nothing(None),
                label='noop',
            )
        self.assertIn('did not advance', str(ctx.exception))

    def test_rejects_statements_that_write_nothing(self):
        """A statement list of pure reads passes any migration."""
        with self.assertRaises(HarnessMisuse) as ctx:
            assert_rollback_compatible(
                self.db('reads.db'),
                (('SELECT COUNT(*) FROM memories', ()),),
                build_previous=_build_at(3),
                migrate=_apply(_store._migration_004, 4),
                label='reads-only',
            )
        self.assertIn('changed no rows', str(ctx.exception))

    def test_rejects_statements_the_previous_version_could_not_run(self):
        """Distinguishes "the migration broke it" from "this SQL never worked"."""
        with self.assertRaises(HarnessMisuse) as ctx:
            assert_rollback_compatible(
                self.db('typo.db'),
                (('INSERT INTO no_such_table (a) VALUES (1)', ()),),
                build_previous=_build_at(3),
                migrate=_apply(_store._migration_004, 4),
                label='typo',
            )
        self.assertIn('do not run against the pre-migration database',
                      str(ctx.exception))

    def test_refuses_to_touch_the_live_database(self):
        """The harness writes. It must never be pointed at workspace state."""
        with self.assertRaises(HarnessMisuse) as ctx:
            assert_rollback_compatible(
                _store.DEFAULT_DB_PATH,
                LEGACY_WRITES,
                build_previous=lambda p: self.fail('must not get this far'),
            )
        self.assertIn('live workspace state', str(ctx.exception))


class TrapTests(_ScratchDBTestCase):
    """The two subtleties #598 needed three review rounds to find.

    Written as executable facts because both are counter-intuitive enough
    that a future reader will assume the docs are wrong.
    """

    def _v4_db(self):
        path = self.db('trap.db')
        _build_at(4)(path)
        return path

    def test_create_index_if_not_exists_is_a_silent_no_op_under_a_unique_index(self):
        """TRAP 1. `IF NOT EXISTS` matches on NAME, not on shape.

        So shipping the corrected `CREATE INDEX IF NOT EXISTS` does NOT undo a
        UNIQUE index an in-development build already created: the statement
        succeeds, changes nothing, and the door stays shut. The repair has to
        DROP first, which is what `_ensure_pending_index_shape` does.
        """
        path = self._v4_db()
        conn = sqlite3.connect(path, isolation_level=None)
        try:
            conn.execute('CREATE UNIQUE INDEX idx_embeddings_pending_memory'
                         '  ON embeddings_pending(memory_id)')
            # The "fix" — and it quietly does nothing at all.
            conn.execute('CREATE INDEX IF NOT EXISTS idx_embeddings_pending_memory'
                         '  ON embeddings_pending(memory_id)')
            shape = {r[1]: r[2] for r in
                     conn.execute("PRAGMA index_list('embeddings_pending')")}
            self.assertTrue(shape['idx_embeddings_pending_memory'],
                            'if this ever flips, SQLite changed and the '
                            'DROP-first repair can be simplified')
        finally:
            conn.close()

    def test_a_version_gated_repair_can_never_heal_a_stuck_database(self):
        """TRAP 2. The database that needs the repair is already at the new
        version, so the gate skips it forever. Repairs run unconditionally.

        `_migration_004` is the gated step; `_ensure_pending_index_shape` is
        the unconditional one. Only the second one heals this database.
        """
        path = self._v4_db()
        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute('CREATE UNIQUE INDEX idx_embeddings_pending_memory'
                     '  ON embeddings_pending(memory_id)')
        conn.close()

        # Version-gated path: _migrate sees schema_version 4 and skips 004.
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            _store._migrate(conn)
            shape = {r[1]: r[2] for r in
                     conn.execute("PRAGMA index_list('embeddings_pending')")}
            self.assertTrue(shape['idx_embeddings_pending_memory'],
                            'still stuck — a gated migration cannot fix this')

            # Unconditional path: re-asserted on every open, so it heals.
            _store._ensure_pending_index_shape(conn)
            shape = {r[1]: r[2] for r in
                     conn.execute("PRAGMA index_list('embeddings_pending')")}
            self.assertFalse(shape['idx_embeddings_pending_memory'])
        finally:
            conn.close()


class MigrationLintTests(unittest.TestCase):
    """A cheap grep for the shapes that are one-way doors (#600).

    The replay above is the real check; this only catches the mistake earlier
    and with a better pointer, in the spirit of tests/no_deployment_paths_test.py.
    It reads the SQL literals inside each `_migration_*` function and flags
    `UNIQUE` indexes, undefaulted `NOT NULL` columns and `CHECK`/`ADD
    CONSTRAINT` clauses.

    Deliberately dumb: it matches text, so it will occasionally be wrong. When
    it is, add the function to `EXEMPT` **with a reason** — after adding its
    boundary to `ExistingMigrationsTests.BOUNDARIES`, which is what actually
    proves the case.
    """

    STORE = os.path.join(os.path.dirname(HERE), 'memory', 'store.py')

    # name -> why it is allowed to contain constraint SQL.
    EXEMPT = {
        '_migration_001': (
            'the bootstrap schema — there is no previous version below it, so '
            'no older code can exist to break. Its constraints are the '
            'starting state, not an addition.'),
    }

    RISKY = (
        ('UNIQUE INDEX', lambda ln: 'UNIQUE INDEX' in ln),
        ('UNIQUE(...) on a table', lambda ln: 'UNIQUE(' in ln or 'UNIQUE (' in ln),
        ('NOT NULL without DEFAULT', lambda ln: ('NOT NULL' in ln
                                                 and 'DEFAULT' not in ln)),
        ('CHECK constraint', lambda ln: 'CHECK(' in ln or 'CHECK (' in ln),
        ('ADD CONSTRAINT', lambda ln: 'ADD CONSTRAINT' in ln),
    )

    def _migration_functions(self, path=None):
        """(name, lineno, sql_lines) per migration.

        The docstring is excluded and `--` comments are dropped: these
        functions explain at length *why* a UNIQUE index would be wrong, and a
        lint that flags prose about the mistake instead of the mistake is
        worse than no lint.
        """
        import ast
        path = path or self.STORE
        with open(path, encoding='utf-8') as fh:
            tree = ast.parse(fh.read(), filename=path)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef)
                    and node.name.startswith('_migration_')):
                continue
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]                       # drop the docstring
            sql = []
            for stmt in body:
                for sub in ast.walk(stmt):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        sql.extend(ln for ln in sub.value.splitlines()
                                   if not ln.strip().startswith('--'))
            yield node.name, node.lineno, sql

    def test_no_migration_quietly_adds_a_constraint(self):
        offenders = []
        for name, lineno, lines in self._migration_functions():
            if name in self.EXEMPT:
                continue
            for raw in lines:
                ln = raw.upper()
                for what, matches in self.RISKY:
                    if matches(ln):
                        offenders.append('memory/store.py:{}  {}: {} -> {!r}'
                                         .format(lineno, name, what, raw.strip()))
        self.assertEqual(offenders, [], (
            '\n\nThese migrations add a constraint older code may not satisfy. '
            'A workspace can be rolled back to an older image and there is no '
            'down-migration, so its writes would start failing in production:'
            '\n\n  ' + '\n  '.join(offenders) + '\n\n'
            'Preferred fix: enforce the invariant in code with a NON-unique '
            'index (see manager._enqueue_embedding). Otherwise add a boundary '
            'to ExistingMigrationsTests.BOUNDARIES proving older statements '
            'still work, then exempt the function here with a reason. See '
            'CONTRIBUTING.md -> "Schema migrations must be rollback-compatible".\n'))

    def test_the_lint_found_the_migrations(self):
        """A guard that silently scans nothing is decoration."""
        names = {n for n, _, _ in self._migration_functions()}
        self.assertEqual(
            names,
            {'_migration_{:03d}'.format(n)
             for n in range(1, _store.SCHEMA_VERSION + 1)})

    def _flags(self, source):
        """Run the whole extractor + matcher over a synthetic module."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'fake_store.py')
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(source)
            hits = []
            for name, _, lines in self._migration_functions(path):
                for raw in lines:
                    ln = raw.upper()
                    hits += [(name, what) for what, m in self.RISKY if m(ln)]
            return hits

    def test_the_lint_would_flag_the_near_miss(self):
        """Mutation check on the whole path, not just the patterns: the SQL that
        started all this must still be flagged when a migration contains it."""
        hits = self._flags(
            'def _migration_009(conn):\n'
            '    """Bound the queue. Looks right, is a one-way door."""\n'
            "    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_q"
            " ON embeddings_pending(memory_id)')\n")
        self.assertEqual(hits, [('_migration_009', 'UNIQUE INDEX')])

    def test_the_lint_flags_an_undefaulted_not_null_column(self):
        hits = self._flags(
            'def _migration_009(conn):\n'
            '    conn.executescript("""\n'
            '        CREATE TABLE t2 (id INTEGER PRIMARY KEY, actor TEXT NOT NULL);\n'
            '    """)\n')
        self.assertEqual(hits, [('_migration_009', 'NOT NULL without DEFAULT')])

    def test_the_lint_ignores_a_defaulted_column(self):
        self.assertEqual(self._flags(
            'def _migration_009(conn):\n'
            "    conn.execute('ALTER TABLE memories ADD COLUMN tags TEXT"
            " NOT NULL DEFAULT \\'\\'')\n"), [])

    def test_the_lint_ignores_prose_about_the_mistake(self):
        """Every migration here explains why a UNIQUE index would be wrong. A
        lint that flags the explanation instead of the code is worse than none."""
        self.assertEqual(self._flags(
            'def _migration_009(conn):\n'
            '    """A UNIQUE INDEX here would be a one-way door, so:"""\n'
            '    conn.executescript("""\n'
            '        -- deliberately not a UNIQUE INDEX; see #597\n'
            '        CREATE INDEX IF NOT EXISTS idx_q ON t(memory_id);\n'
            '    """)\n'), [])


if __name__ == '__main__':
    unittest.main()
