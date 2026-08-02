"""SQLite store for the memory subsystem.

Owns: connection management, schema, migrations, FTS5 triggers, and the
sqlite-vec virtual table (created but unused until Phase 2). WAL mode +
short `BEGIN IMMEDIATE` writers + retry-on-BUSY make it safe for two
processes (server.py and mcp_memory.py) to write the same file.

The schema is intentionally future-proofed in Phase 1: embeddings,
vec_memories, and relations tables exist even though their UIs/MCP tools
ship in later phases. This avoids any data migration on activation.
"""

from __future__ import annotations

import os
import sqlite3
import struct
import threading
import time
from contextlib import contextmanager
from typing import Iterator, List, Optional, Sequence, Tuple

DB_PATH = '/home/dev/.claude-memory/memory.db'

SCHEMA_VERSION = 4

# Width of the vec_memories FLOAT[N] column. Embedding providers must emit
# (or reduce to) this many dimensions — see memory/embeddings.py. A provider
# whose dim differs would have every insert rejected by vec0, so the worker
# warns loudly at startup instead of silently poison-dropping the queue.
VEC_DIM = 1024

# Lookup index on the embedding queue (#597). Deliberately NOT UNIQUE so a
# rolled-back image, whose code still does a plain INSERT, keeps working —
# see _migration_004. The one-row-per-memory bound lives in
# manager._enqueue_embedding instead.
_PENDING_MEMORY_INDEX = 'idx_embeddings_pending_memory'

# sqlite-vec extension paths to try in order. The Dockerfile (v1.8.0+)
# installs the .so at /usr/local/lib/vec0.so via a symlink; the unpacked
# tarball path is the secondary fallback.
_VEC_EXTENSION_CANDIDATES = (
    '/usr/local/lib/vec0',
    '/usr/local/lib/sqlite-vec/vec0',
)

# Module-level guard so we only run pragmas + migrations once per process.
_INITIALIZED = False
_INIT_LOCK = threading.Lock()


def _connect(db_path: str = DB_PATH, *, load_vec: bool = False) -> sqlite3.Connection:
    """Open a connection with sensible per-connection settings.

    `load_vec=True` is best-effort: if the extension isn't present the
    connection still works (vector features just become unavailable, which
    is the documented Phase 1 state).
    """
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=10000')
    if load_vec:
        try:
            conn.enable_load_extension(True)
            for cand in _VEC_EXTENSION_CANDIDATES:
                try:
                    conn.load_extension(cand)
                    break
                except sqlite3.OperationalError:
                    continue
            conn.enable_load_extension(False)
        except (sqlite3.OperationalError, AttributeError):
            # enable_load_extension may not be compiled in on this sqlite3.
            pass
    return conn


def _ensure_db_dir(db_path: str) -> None:
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)


def initialize(db_path: str = DB_PATH) -> None:
    """Run once per process: pragmas + migrations. Idempotent + thread-safe."""
    global _INITIALIZED
    with _INIT_LOCK:
        if _INITIALIZED:
            return
        _ensure_db_dir(db_path)
        # One-time DB-level pragmas (WAL is persistent across connections).
        conn = _connect(db_path)
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')
            _migrate(conn)
            _ensure_pending_index_shape(conn)
        finally:
            conn.close()
        _INITIALIZED = True


def _ensure_pending_index_shape(conn: sqlite3.Connection) -> None:
    """Guarantee the embeddings_pending memory_id index exists and is NOT UNIQUE.

    Deliberately UNCONDITIONAL — checked on every open, like the
    CREATE TABLE IF NOT EXISTS schema setup, rather than from inside a numbered
    migration (#597). Version-gating this would not work: a database left by an
    intermediate build is already at schema_version 4, so `_migration_004` is
    skipped and a UNIQUE index there would survive forever. That database is
    exactly the one that needs healing — under a UNIQUE index every memory
    UPDATE from older code fails, so a stuck workspace would stay stuck.

    Making it a property re-asserted at open, not a one-time step, means the
    invariant holds no matter what version a DB claims to be or how it got
    there. See `_migration_004` for why the index must not be unique.

    Costs one PRAGMA read in the common case, where the index is already the
    right shape and nothing is written.
    """
    if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
            " AND name='embeddings_pending'").fetchone():
        return                      # pre-migration DB; _migrate creates it
    shapes = {r[1]: r[2] for r in            # name -> unique flag
              conn.execute("PRAGMA index_list('embeddings_pending')").fetchall()}
    unique = shapes.get(_PENDING_MEMORY_INDEX)
    if unique == 0:
        return                      # already correct — the overwhelming case
    conn.execute('BEGIN IMMEDIATE')
    try:
        if unique:
            # CREATE INDEX IF NOT EXISTS is a silent no-op while a UNIQUE index
            # holds the name, so the door stays shut unless we drop it first.
            conn.execute(f'DROP INDEX {_PENDING_MEMORY_INDEX}')
        conn.execute(
            f'CREATE INDEX IF NOT EXISTS {_PENDING_MEMORY_INDEX}'
            '  ON embeddings_pending(memory_id)'
        )
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except sqlite3.OperationalError:
            pass
        raise
    conn.execute('COMMIT')


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply pending migrations. Reads/writes _meta.schema_version."""
    conn.execute(
        'CREATE TABLE IF NOT EXISTS _meta ('
        '  key TEXT PRIMARY KEY,'
        '  value TEXT NOT NULL'
        ')'
    )
    row = conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()
    current = int(row[0]) if row else 0

    if current < 1:
        _migration_001(conn)
        conn.execute(
            "INSERT INTO _meta(key, value) VALUES('schema_version', '1')"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )

    if current < 2:
        _migration_002(conn)
        conn.execute(
            "INSERT INTO _meta(key, value) VALUES('schema_version', '2')"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )

    if current < 3:
        _migration_003(conn)
        conn.execute(
            "INSERT INTO _meta(key, value) VALUES('schema_version', '3')"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )

    if current < 4:
        _migration_004(conn)
        conn.execute(
            "INSERT INTO _meta(key, value) VALUES('schema_version', '4')"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )


def _migration_004(conn: sqlite3.Connection) -> None:
    """Collapse duplicate `embeddings_pending` rows (#597).

    The queue only ever needed to say "this memory needs re-embedding" — one
    bit per memory — but every write inserted a fresh row. The worker folds
    duplicates by memory_id so they cost nothing *once it runs*, and it only
    runs when an embedding provider is configured, which most workspaces never
    do. So the table grew one row per memory write, forever, on the PVC.

    Keep the newest row per memory_id (MAX(id) — rowids are monotonic, so this
    is the last enqueue and never ties) and drop the rest. That is all this
    migration does: a one-time data cleanup, correctly version-gated so it runs
    once. The memory_id INDEX is not created here — its shape is re-asserted on
    every open by `_ensure_pending_index_shape`, because a version-gated check
    cannot heal a database that is already at 4 with the wrong index.

    The index is deliberately NOT UNIQUE, and that is the whole design.
    A UNIQUE index is a ONE-WAY DOOR: this migration bumps the DB to schema 4
    on first boot, but an operator who then ROLLS BACK the workspace image gets
    older code doing a plain `INSERT INTO embeddings_pending`, which the
    constraint rejects — so every memory UPDATE fails workspace-wide with
    IntegrityError while creates still succeed. kube-coder pins per-workspace
    image versions and ships a Restart-and-update path, so rollback is a real
    operation, not a hypothetical. The failure modes are wildly asymmetric:

        UNIQUE + rollback  ->  memory updates fail across the workspace
        no UNIQUE          ->  a few duplicate queue rows come back, which is
                               the nuisance we are already fixing and which
                               `_load_pending_batch` already folds away

    So the bound lives in code instead — see `manager._enqueue_embedding`,
    which UPDATEs an existing row or INSERTs when there is none. Old code
    running against this schema still works; new code keeps the table at one
    row per memory. A plain index stays for the lookup that helper does.

    We deliberately do NOT carry the older rows' `attempts` forward onto the
    survivor: the newest row is the most recent edit, and under the new
    semantics a fresh edit is a fresh attempt. Worst case a memory that was
    failing to embed gets a few extra retries.

    Only `embeddings_pending` is touched — never `memories`. Takes the write
    lock up front with BEGIN IMMEDIATE so a concurrent writer waits rather than
    interleaving, and so an interrupted run leaves the table exactly as it was.
    Idempotent: on a second run the DELETE matches nothing.
    """
    conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute(
            'DELETE FROM embeddings_pending WHERE id NOT IN ('
            '  SELECT MAX(id) FROM embeddings_pending GROUP BY memory_id'
            ')'
        )
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except sqlite3.OperationalError:
            pass
        raise
    conn.execute('COMMIT')


def _migration_003(conn: sqlite3.Connection) -> None:
    """Add the derived `summary` column (#359).

    Long memories were hard-cut mid-sentence at 280 chars every time they were
    injected. We instead compute a short summary at WRITE time and inject that,
    while `value` keeps the user's full content — so recall/search/export are
    unaffected and nothing is ever lost. NULL means "not summarized" (every
    pre-existing row, and any row short enough not to need one); readers fall
    back to `value`, so the column is purely additive.

    Deliberately NOT added to the FTS index: search must keep matching the full
    text, not a lossy digest.
    """
    cols = {r[1] for r in conn.execute('PRAGMA table_info(memories)').fetchall()}
    if 'summary' not in cols:      # idempotent: safe to re-run
        conn.execute('ALTER TABLE memories ADD COLUMN summary TEXT')


def _migration_002(conn: sqlite3.Connection) -> None:
    """Guard the FTS delete trigger on live rows only (#107).

    The original memories_fts_delete trigger removed the FTS entry for every
    deleted row. But the update trigger already removes a row's FTS entry the
    moment it's soft-deleted (deleted_at set), so hard-deleting that row later
    (Phase-2 GC / MemoryManager.purge_deleted) double-deleted from the
    external-content FTS5 index and raised "database disk image is malformed".
    Re-create the trigger with `WHEN old.deleted_at IS NULL` so it only fires
    for rows still present in the index (FTS only ever holds live rows).
    Idempotent: safe to re-run on an already-migrated DB.
    """
    conn.executescript("""
        DROP TRIGGER IF EXISTS memories_fts_delete;
        CREATE TRIGGER memories_fts_delete
        AFTER DELETE ON memories
        WHEN old.deleted_at IS NULL
        BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, namespace, key, value, tags)
            VALUES ('delete', old.id, old.namespace, old.key, old.value, old.tags);
        END;
    """)


def _migration_001(conn: sqlite3.Connection) -> None:
    """Initial schema. See plan §DB layer."""
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id               INTEGER PRIMARY KEY,
            namespace        TEXT NOT NULL,
            key              TEXT NOT NULL,
            value            TEXT NOT NULL,
            kind             TEXT NOT NULL DEFAULT 'semantic',
            tags             TEXT NOT NULL DEFAULT '',
            importance       REAL NOT NULL DEFAULT 0.5,
            confidence       REAL NOT NULL DEFAULT 1.0,
            source           TEXT NOT NULL DEFAULT '',
            created_at       REAL NOT NULL,
            updated_at       REAL NOT NULL,
            last_accessed_at REAL,
            access_count     INTEGER NOT NULL DEFAULT 0,
            version          INTEGER NOT NULL DEFAULT 1,
            expires_at       REAL,
            deleted_at       REAL,
            UNIQUE(namespace, key)
        );

        CREATE INDEX IF NOT EXISTS idx_memories_ns
            ON memories(namespace) WHERE deleted_at IS NULL;
        CREATE INDEX IF NOT EXISTS idx_memories_kind
            ON memories(kind) WHERE deleted_at IS NULL;
        CREATE INDEX IF NOT EXISTS idx_memories_updated
            ON memories(updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_memories_expires
            ON memories(expires_at) WHERE expires_at IS NOT NULL;

        CREATE TABLE IF NOT EXISTS memory_history (
            id          INTEGER PRIMARY KEY,
            memory_id   INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            version     INTEGER NOT NULL,
            value       TEXT,
            tags        TEXT,
            importance  REAL,
            confidence  REAL,
            updated_at  REAL NOT NULL,
            updated_by  TEXT NOT NULL,
            op          TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_history_memory
            ON memory_history(memory_id, version DESC);

        CREATE TABLE IF NOT EXISTS embeddings (
            id          INTEGER PRIMARY KEY,
            memory_id   INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            model       TEXT NOT NULL,
            dim         INTEGER NOT NULL,
            created_at  REAL NOT NULL,
            UNIQUE(memory_id, model)
        );

        -- One row per memory that needs (re-)embedding. Migration 004 collapses
        -- legacy duplicates and indexes memory_id; the bound itself is enforced
        -- in manager._enqueue_embedding, NOT by a constraint (#597).
        CREATE TABLE IF NOT EXISTS embeddings_pending (
            id          INTEGER PRIMARY KEY,
            memory_id   INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            attempts    INTEGER NOT NULL DEFAULT 0,
            last_error  TEXT,
            enqueued_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS relations (
            id          INTEGER PRIMARY KEY,
            src_id      INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            dst_id      INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            kind        TEXT NOT NULL,
            weight      REAL NOT NULL DEFAULT 1.0,
            created_at  REAL NOT NULL,
            created_by  TEXT NOT NULL,
            UNIQUE(src_id, dst_id, kind)
        );
        CREATE INDEX IF NOT EXISTS idx_relations_src
            ON relations(src_id, kind);
        CREATE INDEX IF NOT EXISTS idx_relations_dst
            ON relations(dst_id, kind);

        CREATE TABLE IF NOT EXISTS memory_refs (
            id          INTEGER PRIMARY KEY,
            memory_id   INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            ref_kind    TEXT NOT NULL,
            ref_id      TEXT NOT NULL,
            access_kind TEXT NOT NULL,
            at          REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_refs_memory
            ON memory_refs(memory_id, at DESC);
        CREATE INDEX IF NOT EXISTS idx_refs_ref
            ON memory_refs(ref_kind, ref_id, at DESC);

        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            namespace, key, value, tags,
            content='memories', content_rowid='id',
            tokenize='unicode61'
        );

        -- Keep FTS in sync. We only index non-deleted rows; deletes from FTS
        -- happen explicitly in MemoryManager when a row is soft-deleted.
        CREATE TRIGGER IF NOT EXISTS memories_fts_insert
        AFTER INSERT ON memories
        WHEN new.deleted_at IS NULL
        BEGIN
            INSERT INTO memories_fts(rowid, namespace, key, value, tags)
            VALUES (new.id, new.namespace, new.key, new.value, new.tags);
        END;

        CREATE TRIGGER IF NOT EXISTS memories_fts_update
        AFTER UPDATE OF value, tags, namespace, key, deleted_at ON memories
        BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, namespace, key, value, tags)
            VALUES ('delete', old.id, old.namespace, old.key, old.value, old.tags);
            INSERT INTO memories_fts(rowid, namespace, key, value, tags)
            SELECT new.id, new.namespace, new.key, new.value, new.tags
            WHERE new.deleted_at IS NULL;
        END;

        CREATE TRIGGER IF NOT EXISTS memories_fts_delete
        AFTER DELETE ON memories
        BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, namespace, key, value, tags)
            VALUES ('delete', old.id, old.namespace, old.key, old.value, old.tags);
        END;
    """)
    # vec_memories is created lazily inside the same connection because it
    # requires the sqlite-vec extension to be loaded — see _try_create_vec.
    _try_create_vec(conn)


def _try_create_vec(conn: sqlite3.Connection) -> bool:
    """Attempt to create the vec_memories virtual table. Returns True on success.

    No-op if the extension isn't available (Phase 1 default state). Phase 2
    sets up the extension by default and re-runs this on boot.
    """
    try:
        conn.enable_load_extension(True)
        loaded = False
        for cand in _VEC_EXTENSION_CANDIDATES:
            try:
                conn.load_extension(cand)
                loaded = True
                break
            except sqlite3.OperationalError:
                continue
        if not loaded:
            return False
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS vec_memories USING vec0("
            "  embedding_id INTEGER PRIMARY KEY,"
            f"  vec FLOAT[{VEC_DIM}]"
            ")"
        )
        return True
    except (sqlite3.OperationalError, AttributeError):
        return False
    finally:
        try:
            conn.enable_load_extension(False)
        except (sqlite3.OperationalError, AttributeError):
            pass


# ───────────────────────────────────────────────────────────────────────────
# Vector (sqlite-vec) helpers — Phase 2 (#90)
#
# All best-effort: every function degrades to "vectors unavailable" when the
# sqlite-vec extension or the vec_memories table isn't present, which is the
# Phase-1 / no-extension state (e.g. CI, where vec0.so isn't installed).
# ───────────────────────────────────────────────────────────────────────────

def serialize_f32(vec: Sequence[float]) -> bytes:
    """Pack a vector as the little-endian float32 blob sqlite-vec expects."""
    return struct.pack('<%df' % len(vec), *(float(x) for x in vec))


def ensure_vec_table(db_path: str = DB_PATH) -> bool:
    """Create vec_memories if the extension is available. Idempotent.

    Needed on an *upgraded* DB: migration 001 already ran (schema_version=1)
    on the Phase-1 deploy without the extension, so the virtual table may be
    absent even though the migration is marked applied. The worker calls this
    on startup so activation needs no schema bump.
    """
    conn = _connect(db_path, load_vec=True)
    try:
        return _try_create_vec(conn)
    finally:
        conn.close()


def vectors_available(conn: sqlite3.Connection) -> bool:
    """True when vec_memories is queryable on this connection."""
    try:
        conn.execute('SELECT embedding_id FROM vec_memories LIMIT 0')
        return True
    except sqlite3.OperationalError:
        return False


def upsert_vector(conn: sqlite3.Connection, embedding_id: int,
                  vec: Sequence[float]) -> None:
    """Replace the stored vector for an embedding row. vec0 has no UPSERT, so
    delete-then-insert."""
    blob = serialize_f32(vec)
    conn.execute('DELETE FROM vec_memories WHERE embedding_id=?', (embedding_id,))
    conn.execute(
        'INSERT INTO vec_memories(embedding_id, vec) VALUES (?, ?)',
        (embedding_id, blob),
    )


def delete_vector(conn: sqlite3.Connection, embedding_id: int) -> None:
    try:
        conn.execute('DELETE FROM vec_memories WHERE embedding_id=?', (embedding_id,))
    except sqlite3.OperationalError:
        pass


def knn(conn: sqlite3.Connection, vec: Sequence[float], k: int
        ) -> List[Tuple[int, float]]:
    """K-nearest-neighbour search. Returns [(embedding_id, distance), …]
    nearest-first, or [] when vectors are unavailable."""
    try:
        rows = conn.execute(
            'SELECT embedding_id, distance FROM vec_memories '
            'WHERE vec MATCH ? AND k = ? ORDER BY distance',
            (serialize_f32(vec), int(k)),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(int(r['embedding_id']), float(r['distance'])) for r in rows]


# ───────────────────────────────────────────────────────────────────────────
# High-level connection helper
# ───────────────────────────────────────────────────────────────────────────

class MemoryStore:
    """Thin wrapper that opens a connection, runs initialize() lazily, and
    exposes a transactional context manager.

    Instances are cheap to create — use one per request / MCP tool call.
    """

    def __init__(self, db_path: str = DB_PATH, *, load_vec: bool = False):
        self.db_path = db_path
        self.load_vec = load_vec
        initialize(db_path)

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = _connect(self.db_path, load_vec=self.load_vec)
        try:
            yield c
        finally:
            c.close()

    @contextmanager
    def tx(self, *, retries: int = 3) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE with bounded retry on SQLITE_BUSY.

        Use for any write path. Readers can use `conn()` directly.
        """
        last_err: Optional[sqlite3.OperationalError] = None
        for attempt in range(retries + 1):
            c = _connect(self.db_path, load_vec=self.load_vec)
            try:
                c.execute('BEGIN IMMEDIATE')
                try:
                    yield c
                    c.execute('COMMIT')
                    return
                except Exception:
                    try:
                        c.execute('ROLLBACK')
                    except sqlite3.OperationalError:
                        pass
                    raise
            except sqlite3.OperationalError as e:
                last_err = e
                if 'database is locked' in str(e) and attempt < retries:
                    time.sleep(0.05 * (2 ** attempt))
                    continue
                raise
            finally:
                c.close()
        if last_err is not None:
            raise last_err
