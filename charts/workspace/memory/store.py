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

SCHEMA_VERSION = 1

# Width of the vec_memories FLOAT[N] column. Embedding providers must emit
# (or reduce to) this many dimensions — see memory/embeddings.py. A provider
# whose dim differs would have every insert rejected by vec0, so the worker
# warns loudly at startup instead of silently poison-dropping the queue.
VEC_DIM = 1024

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
        finally:
            conn.close()
        _INITIALIZED = True


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
