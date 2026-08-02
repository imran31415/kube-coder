"""Rollback-compatibility harness for schema migrations (#600).

THE RULE. A migration must not add a constraint that older code cannot
satisfy. kube-coder pins a workspace's image tag and ships a "Restart &
update" path, so rolling an image *back* is a routine operation. Migrations
have no down-step: once a workspace has migrated, a rolled-back pod runs
older code against the newer schema. If the migration tightened the schema,
that older code's writes start failing — in production, silently, for
everyone on that workspace.

The near-miss this comes from (#597 / #598): a queue table was bounded with
`UNIQUE(memory_id)`. Correct-looking, fully tested, green CI, and a one-way
door — on a rolled-back image every memory *update* died with
`UNIQUE constraint failed: embeddings_pending.memory_id` while creates kept
working. The shipped fix moved the bound into code and used a NON-unique
index, so a plain `INSERT` from older code is still legal.

WHAT THIS MODULE CHECKS. `assert_rollback_compatible` reproduces the exact
operator sequence:

    build a database at the PREVIOUS schema version
      -> let the PREVIOUS version's statements write to it   (they must work)
      -> apply the migration
      -> run those SAME statements verbatim again            (they must STILL work)

Both halves matter. The pre-migration run is not ceremony: it proves the
statements are genuinely something the old code could run, so a failure
afterwards is the migration's fault and not a typo in the test. And the
helper refuses to pass on a `migrate` that did nothing — a check that can
succeed vacuously is worse than no check.

WHAT IT CANNOT CHECK. Semantics. A migration that rewrites values, or drops
a column older code reads, is a rollback problem this will not see; it only
watches writes. Nor does it know about data older code would produce later.
It catches the specific, common, expensive mistake: a tightened schema.

USAGE

    from rollback_compat import assert_rollback_compatible

    assert_rollback_compatible(
        db_path,                       # a scratch file; never a live database
        PREVIOUS_VERSION_STATEMENTS,   # [(sql, params_or_callable), ...]
        build_previous=_build_at(3),   # callable(db_path) -> DB at version N-1
        migrate=_apply_only(4),        # callable(db_path) -> applies the migration
    )

`params` is a tuple, or a callable `(conn, run) -> tuple` where `run` is
`'before'` or `'after'`. The callable form exists so the SQL text can stay
verbatim while the bound values stay unique across the two runs (otherwise
a pre-existing `UNIQUE(namespace, key)` would fail the replay and be
misreported as a regression) and so a statement can look up an id the way
real code does.

Not named `*_test.py`, so `unittest discover -p '*_test.py'` imports it as a
library rather than collecting it. See `tests/memory_rollback_compat_test.py`
for the audit of the migrations that exist today, and `CONTRIBUTING.md`
("Schema migrations must be rollback-compatible") for the convention.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(HERE)
if WORKSPACE not in sys.path:
    sys.path.insert(0, WORKSPACE)

import memory.store as _store  # noqa: E402

Params = Union[Tuple[Any, ...], Callable[[sqlite3.Connection, str], Tuple[Any, ...]]]
Statement = Tuple[str, Params]


class RollbackIncompatible(AssertionError):
    """A migrated database rejects a write the previous version made freely.

    Raised only when the *migration* is at fault. Getting this means the
    change is a one-way door: decide deliberately, don't silence the test.
    """


class HarnessMisuse(AssertionError):
    """The check is not proving anything — fix the test, not the migration.

    A separate type from `RollbackIncompatible` on purpose: a negative test
    that expects a real catch must not be satisfied by a broken harness.
    """


# Roots that only exist inside a provisioned workspace pod, where the live
# memory database lives. Two agents have already damaged it; this harness
# writes, so it refuses to run anywhere near it (see #599 and
# tests/memory_db_path_test.py). Fixtures belong in `tempfile`.
_LIVE_ROOTS = ('/home/dev', '/home/ubuntu')


def _assert_scratch_path(db_path: str, when: str) -> None:
    """Assert WHERE we are about to write, before writing. See #599."""
    real = os.path.abspath(db_path)
    live = (real == _store.DEFAULT_DB_PATH
            or any(real == r or real.startswith(r + os.sep) for r in _LIVE_ROOTS))
    if live:
        raise HarnessMisuse(
            'rollback check would write to {!r} ({}) — that is live workspace '
            'state, not a fixture. Build the DB in a tempfile directory using '
            'the code\'s own schema; no real data is needed.'.format(real, when))


def _default_migrate(db_path: str) -> None:
    """Run every pending migration, the way a booting process would."""
    orig = _store._INITIALIZED
    _store._INITIALIZED = False
    try:
        _store.initialize(db_path)
    finally:
        _store._INITIALIZED = orig


def _open(db_path: str) -> sqlite3.Connection:
    """A connection shaped like the one the store hands to real code."""
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def _schema_version(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                            " AND name='_meta'").fetchone():
            return 0
        row = conn.execute(
            "SELECT value FROM _meta WHERE key='schema_version'").fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def constraint_shape(db_path: str) -> Dict[str, Any]:
    """Everything about a schema that can reject a write, as plain data.

    Used to diff a database across a migration so a failure can say *which*
    constraint appeared, rather than only that something broke.

    Virtual tables are skipped, not fatal: `vec_memories` is a vec0 table and
    `PRAGMA table_info` on it raises "no such module: vec0" unless the
    extension happens to be loaded — which it is in a workspace pod and is not
    on a CI runner. Neither one carries constraints old code has to satisfy.
    """
    conn = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%'")]
        indexes: Dict[Tuple[str, str], bool] = {}
        not_null: set = set()
        table_sql: Dict[str, str] = {}
        for t in tables:
            try:
                index_rows = conn.execute('PRAGMA index_list("{}")'.format(t)).fetchall()
                info_rows = conn.execute('PRAGMA table_info("{}")'.format(t)).fetchall()
            except sqlite3.OperationalError:
                continue                      # virtual table, module not loaded
            for r in index_rows:
                indexes[(t, r[1])] = bool(r[2])
            for r in info_rows:
                # (cid, name, type, notnull, dflt_value, pk) — a PK is exempt:
                # INTEGER PRIMARY KEY autofills, so old code never supplies it.
                if r[3] and r[4] is None and not r[5]:
                    not_null.add((t, r[1]))
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (t,)).fetchone()
            table_sql[t] = (row[0] if row and row[0] else '')
    finally:
        conn.close()
    return {'indexes': indexes, 'not_null_no_default': not_null,
            'tables': table_sql}


def added_constraints(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    """Human-readable list of the ways `after` is stricter than `before`."""
    out: List[str] = []
    for key, unique in sorted(after['indexes'].items()):
        table, name = key
        if not unique:
            continue
        if before['indexes'].get(key) is not True:
            out.append('UNIQUE index {} on {}'.format(name, table))
    for table, col in sorted(after['not_null_no_default'] - before['not_null_no_default']):
        out.append('NOT NULL column without a default: {}.{}'.format(table, col))
    for table, sql in sorted(after['tables'].items()):
        old = before['tables'].get(table)
        if old is not None and old != sql and 'CHECK' in sql.upper():
            out.append('{} was rebuilt and now carries a CHECK constraint'.format(table))
    return out


def _materialize(params: Params, conn: sqlite3.Connection, run: str) -> Tuple[Any, ...]:
    return params(conn, run) if callable(params) else params


def replay(db_path: str, statements: Sequence[Statement], run: str) -> int:
    """Execute `statements` verbatim in one transaction. Returns rows changed.

    Committed, not rolled back: rolled-back code really does write, and the
    rows it leaves are what the migration then has to cope with.
    """
    conn = _open(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        start = conn.total_changes
        try:
            for sql, params in statements:
                conn.execute(sql, _materialize(params, conn, run))
        except BaseException:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
        changed = conn.total_changes - start
        conn.execute('COMMIT')
        return changed
    finally:
        conn.close()


def assert_rollback_compatible(
        db_path: str,
        previous_statements: Sequence[Statement],
        *,
        build_previous: Callable[[str], None],
        migrate: Optional[Callable[[str], None]] = None,
        label: str = '',
        require_version_bump: bool = True,
) -> Dict[str, Any]:
    """Assert older code can still write to a database this migration touched.

    Raises `RollbackIncompatible` when the migration is the problem, and
    `HarnessMisuse` when the check itself would have proved nothing. Returns
    a small report (versions, rows changed, constraints added) on success.
    """
    what = label or os.path.basename(db_path)
    migrate = migrate or _default_migrate

    _assert_scratch_path(db_path, 'before building the fixture')
    build_previous(db_path)
    _assert_scratch_path(db_path, 'after building the fixture')
    if not os.path.exists(db_path):
        raise HarnessMisuse(
            '{}: build_previous did not create {}'.format(what, db_path))

    before_version = _schema_version(db_path)
    before_shape = constraint_shape(db_path)

    # 1. The statements must be genuine previous-version statements. If they
    #    do not run against the OLD schema, a later failure says nothing
    #    about the migration.
    try:
        pre_changes = replay(db_path, previous_statements, 'before')
    except sqlite3.Error as exc:
        raise HarnessMisuse(
            '{}: the "previous version" statements do not run against the '
            'pre-migration database (schema version {}), so this check proves '
            'nothing about the migration. Fix the statements.\n    {}: {}'
            .format(what, before_version, type(exc).__name__, exc)) from exc
    if pre_changes == 0:
        raise HarnessMisuse(
            '{}: the previous-version statements changed no rows. A statement '
            'list that writes nothing passes any migration. Include the writes '
            'older code actually performs.'.format(what))

    # 2. Apply the migration to a database holding data older code wrote.
    try:
        migrate(db_path)
    except Exception as exc:
        raise RollbackIncompatible(
            '{}: the migration itself failed against data the previous version '
            'wrote.\n    {}: {}\nThis usually means the migration adds a '
            'constraint that existing rows violate — which is also the shape '
            'that breaks older code after a rollback.'
            .format(what, type(exc).__name__, exc)) from exc

    after_version = _schema_version(db_path)
    if require_version_bump and after_version <= before_version:
        raise HarnessMisuse(
            '{}: schema version did not advance ({} -> {}), so `migrate` did '
            'nothing and the replay below would pass trivially. Pass '
            'require_version_bump=False only if the step deliberately does not '
            'bump.'.format(what, before_version, after_version))

    after_shape = constraint_shape(db_path)
    added = added_constraints(before_shape, after_shape)

    # 3. The whole point: the SAME statements, verbatim, against the migrated
    #    database — which is what a rolled-back image runs.
    try:
        post_changes = replay(db_path, previous_statements, 'after')
    except sqlite3.Error as exc:
        raise RollbackIncompatible(
            '{}: a statement the previous version ran freely now FAILS against '
            'the migrated database.\n    {}: {}\n'
            '{}\n'
            'An operator who rolls this workspace\'s image back gets exactly '
            'this error, in production, on every write of that shape. There is '
            'no down-migration.\n\n'
            'Fixes, in order of preference:\n'
            '  1. Drop the constraint and enforce the invariant in code (what '
            '#598 did: a NON-unique index plus an update-or-insert helper).\n'
            '  2. Give the column a DEFAULT so older INSERTs still satisfy it.\n'
            '  3. Decide explicitly that this release is not rollback-safe, say '
            'so in the PR and the release notes, and take the ordering '
            'requirement that comes with it.'
            .format(what, type(exc).__name__, exc,
                    ('    the migration added: ' + '; '.join(added)
                     if added else
                     '    (no new UNIQUE index / NOT NULL column detected — the '
                     'rejection came from a trigger, a CHECK, or a foreign key)')
                    )) from exc

    if post_changes < pre_changes:
        raise RollbackIncompatible(
            '{}: the previous version\'s statements still run but no longer '
            'write everything they used to ({} rows changed before the '
            'migration, {} after). Something is silently swallowing writes — a '
            'trigger, or an INSERT that became a no-op. Silent data loss is '
            'worse than an error.'.format(what, pre_changes, post_changes))

    return {
        'label': what,
        'schema_version_before': before_version,
        'schema_version_after': after_version,
        'rows_changed_before': pre_changes,
        'rows_changed_after': post_changes,
        'constraints_added': added,
    }
