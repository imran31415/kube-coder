"""High-level memory operations shared by HTTP API and MCP tools.

Phase 1 implements: validation, upsert/get/list/delete, history, refs,
hybrid FTS-based search with importance+recency boost, graph
create/walk, stats, and a top-K retrieval used by auto-injection.

All public methods are classmethods so callers (server.py threads,
mcp_memory.py main loop) don't need to share a single instance — they
share the underlying SQLite file via store.py.
"""

from __future__ import annotations

import re
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional

from .store import MemoryStore, DB_PATH


# ───────────────────────────────────────────────────────────────────────────
# Exceptions
# ───────────────────────────────────────────────────────────────────────────

class MemoryError(Exception):
    """Base for memory-system errors. Always carries a stable error code."""

    code = 'memory_error'

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        if code is not None:
            self.code = code


class NotFound(MemoryError):
    code = 'not_found'


class Conflict(MemoryError):
    code = 'conflict'


class ValidationError(MemoryError):
    code = 'validation'


# ───────────────────────────────────────────────────────────────────────────
# Validation
# ───────────────────────────────────────────────────────────────────────────

_NS_KEY_RE = re.compile(r'^[a-zA-Z0-9._-]{1,128}$')
_KIND_VALUES = {'semantic', 'episodic', 'procedural', 'preference'}
_RELATION_KIND_RE = re.compile(r'^[a-zA-Z0-9._:-]{1,64}$')
_MAX_VALUE_BYTES = 256 * 1024  # 256 KiB
_MAX_TAGS_BYTES = 1024

# Per-memory history is pruned to this many revisions on each write.
HISTORY_CAP_PER_MEMORY = 100

# Per-memory ref-log is pruned to this many rows on each write to that memory.
REFS_CAP_PER_MEMORY = 200


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ValidationError(msg)


def _validate_ns_key(namespace: str, key: str) -> None:
    _require(isinstance(namespace, str) and _NS_KEY_RE.match(namespace),
             'namespace must match [a-zA-Z0-9._-]{1,128}')
    _require(isinstance(key, str) and _NS_KEY_RE.match(key),
             'key must match [a-zA-Z0-9._-]{1,128}')


def _validate_value(value: str) -> None:
    _require(isinstance(value, str), 'value must be a string')
    _require(len(value.encode('utf-8')) <= _MAX_VALUE_BYTES,
             f'value exceeds {_MAX_VALUE_BYTES} bytes')


def _validate_tags(tags: str) -> None:
    _require(isinstance(tags, str), 'tags must be a string')
    _require(len(tags.encode('utf-8')) <= _MAX_TAGS_BYTES,
             f'tags exceed {_MAX_TAGS_BYTES} bytes')


def _validate_kind(kind: str) -> None:
    _require(kind in _KIND_VALUES,
             f"kind must be one of {sorted(_KIND_VALUES)}")


def _clamp01(x: float, *, name: str) -> float:
    _require(isinstance(x, (int, float)), f'{name} must be a number')
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return float(x)


# ───────────────────────────────────────────────────────────────────────────
# Row helpers
# ───────────────────────────────────────────────────────────────────────────

def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    # Sort tags as a list for clients while preserving comma-separated wire
    # form in the DB (UI expects free-text). Don't drop empty tags entirely.
    if 'tags' in d and isinstance(d['tags'], str):
        d['tags_list'] = [t for t in (s.strip() for s in d['tags'].split(',')) if t]
    return d


def _normalize_tags(tags: Optional[str]) -> str:
    if not tags:
        return ''
    # Trim and deduplicate while preserving the original comma-separated form.
    parts = [t.strip() for t in tags.split(',')]
    seen = set()
    out = []
    for p in parts:
        if not p or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return ','.join(out)


# ───────────────────────────────────────────────────────────────────────────
# MemoryManager
# ───────────────────────────────────────────────────────────────────────────

class MemoryManager:
    """High-level operations. Stateless; all classmethods."""

    _store: Optional[MemoryStore] = None

    @classmethod
    def store(cls) -> MemoryStore:
        if cls._store is None:
            cls._store = MemoryStore(DB_PATH)
        return cls._store

    # ── Writes ────────────────────────────────────────────────────────────

    @classmethod
    def upsert(
        cls,
        *,
        namespace: str,
        key: str,
        value: str,
        kind: str = 'semantic',
        tags: str = '',
        importance: float = 0.5,
        confidence: float = 1.0,
        source: str = '',
        expires_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        _validate_ns_key(namespace, key)
        _validate_value(value)
        _validate_tags(tags)
        _validate_kind(kind)
        importance = _clamp01(importance, name='importance')
        confidence = _clamp01(confidence, name='confidence')
        tags = _normalize_tags(tags)
        now = time.time()

        with cls.store().tx() as c:
            existing = c.execute(
                'SELECT id, version FROM memories WHERE namespace=? AND key=?',
                (namespace, key),
            ).fetchone()

            if existing is None:
                cur = c.execute(
                    'INSERT INTO memories ('
                    '  namespace, key, value, kind, tags, importance, confidence,'
                    '  source, created_at, updated_at, version, expires_at'
                    ') VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (namespace, key, value, kind, tags, importance, confidence,
                     source or '', now, now, 1, expires_at),
                )
                mem_id = cur.lastrowid
                version = 1
                op = 'create'
            else:
                mem_id = existing['id']
                version = int(existing['version']) + 1
                c.execute(
                    'UPDATE memories SET'
                    '  value=?, kind=?, tags=?, importance=?, confidence=?,'
                    '  source=?, updated_at=?, version=?, expires_at=?,'
                    '  deleted_at=NULL'
                    ' WHERE id=?',
                    (value, kind, tags, importance, confidence,
                     source or '', now, version, expires_at, mem_id),
                )
                op = 'update'

            c.execute(
                'INSERT INTO memory_history ('
                '  memory_id, version, value, tags, importance, confidence,'
                '  updated_at, updated_by, op'
                ') VALUES (?,?,?,?,?,?,?,?,?)',
                (mem_id, version, value, tags, importance, confidence,
                 now, source or 'unknown', op),
            )

            # Enqueue an embedding refresh; harmless in Phase 1 (worker absent).
            c.execute(
                'INSERT INTO embeddings_pending (memory_id, enqueued_at) VALUES (?, ?)',
                (mem_id, now),
            )

            cls._prune_history(c, mem_id)
            row = c.execute('SELECT * FROM memories WHERE id=?', (mem_id,)).fetchone()

        return _row_to_dict(row)

    @classmethod
    def update_partial(
        cls,
        *,
        namespace: str,
        key: str,
        value: Optional[str] = None,
        tags: Optional[str] = None,
        kind: Optional[str] = None,
        importance: Optional[float] = None,
        confidence: Optional[float] = None,
        expires_at: Optional[float] = None,
        source: str = '',
    ) -> Dict[str, Any]:
        _validate_ns_key(namespace, key)
        if value is not None:
            _validate_value(value)
        if tags is not None:
            _validate_tags(tags)
            tags = _normalize_tags(tags)
        if kind is not None:
            _validate_kind(kind)
        if importance is not None:
            importance = _clamp01(importance, name='importance')
        if confidence is not None:
            confidence = _clamp01(confidence, name='confidence')
        now = time.time()

        with cls.store().tx() as c:
            row = c.execute(
                'SELECT * FROM memories WHERE namespace=? AND key=? AND deleted_at IS NULL',
                (namespace, key),
            ).fetchone()
            if row is None:
                raise NotFound(f'no memory at {namespace}/{key}')

            new_value = value if value is not None else row['value']
            new_kind = kind if kind is not None else row['kind']
            new_tags = tags if tags is not None else row['tags']
            new_imp = importance if importance is not None else row['importance']
            new_conf = confidence if confidence is not None else row['confidence']
            new_exp = expires_at if expires_at is not None else row['expires_at']
            version = int(row['version']) + 1

            c.execute(
                'UPDATE memories SET'
                '  value=?, kind=?, tags=?, importance=?, confidence=?,'
                '  source=?, updated_at=?, version=?, expires_at=?'
                ' WHERE id=?',
                (new_value, new_kind, new_tags, new_imp, new_conf,
                 source or row['source'], now, version, new_exp, row['id']),
            )
            c.execute(
                'INSERT INTO memory_history ('
                '  memory_id, version, value, tags, importance, confidence,'
                '  updated_at, updated_by, op'
                ') VALUES (?,?,?,?,?,?,?,?,?)',
                (row['id'], version, new_value, new_tags, new_imp, new_conf,
                 now, source or 'unknown', 'update'),
            )
            if value is not None or tags is not None:
                c.execute(
                    'INSERT INTO embeddings_pending (memory_id, enqueued_at) VALUES (?, ?)',
                    (row['id'], now),
                )
            cls._prune_history(c, row['id'])
            updated = c.execute('SELECT * FROM memories WHERE id=?', (row['id'],)).fetchone()
        return _row_to_dict(updated)

    @classmethod
    def soft_delete(cls, *, namespace: str, key: str, source: str = '') -> Dict[str, Any]:
        _validate_ns_key(namespace, key)
        now = time.time()
        with cls.store().tx() as c:
            row = c.execute(
                'SELECT * FROM memories WHERE namespace=? AND key=? AND deleted_at IS NULL',
                (namespace, key),
            ).fetchone()
            if row is None:
                raise NotFound(f'no memory at {namespace}/{key}')
            version = int(row['version']) + 1
            c.execute(
                'UPDATE memories SET deleted_at=?, updated_at=?, version=? WHERE id=?',
                (now, now, version, row['id']),
            )
            c.execute(
                'INSERT INTO memory_history ('
                '  memory_id, version, value, tags, importance, confidence,'
                '  updated_at, updated_by, op'
                ') VALUES (?,?,?,?,?,?,?,?,?)',
                (row['id'], version, row['value'], row['tags'],
                 row['importance'], row['confidence'],
                 now, source or 'unknown', 'delete'),
            )
            updated = c.execute('SELECT * FROM memories WHERE id=?', (row['id'],)).fetchone()
        return _row_to_dict(updated)

    @classmethod
    def _prune_history(cls, c: sqlite3.Connection, memory_id: int) -> None:
        c.execute(
            'DELETE FROM memory_history WHERE memory_id=? AND id NOT IN ('
            '  SELECT id FROM memory_history WHERE memory_id=? '
            '  ORDER BY version DESC LIMIT ?'
            ')',
            (memory_id, memory_id, HISTORY_CAP_PER_MEMORY),
        )

    # ── Reads ─────────────────────────────────────────────────────────────

    @classmethod
    def get(cls, *, namespace: str, key: str, include_deleted: bool = False
            ) -> Optional[Dict[str, Any]]:
        _validate_ns_key(namespace, key)
        with cls.store().conn() as c:
            q = 'SELECT * FROM memories WHERE namespace=? AND key=?'
            params: List[Any] = [namespace, key]
            if not include_deleted:
                # Filter soft-deleted AND TTL-expired in the same gate so
                # callers that want the canonical "live" view get it.
                q += ' AND deleted_at IS NULL AND (expires_at IS NULL OR expires_at > ?)'
                params.append(time.time())
            row = c.execute(q, params).fetchone()
        return _row_to_dict(row) if row else None

    @classmethod
    def list(
        cls,
        *,
        namespace: Optional[str] = None,
        kind: Optional[str] = None,
        q: Optional[str] = None,
        limit: int = 500,
        include_deleted: bool = False,
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 100000))
        if q:
            return cls.search(q=q, namespaces=[namespace] if namespace else None,
                              kinds=[kind] if kind else None, limit=limit)
        clauses = []
        params: List[Any] = []
        if not include_deleted:
            clauses.append('deleted_at IS NULL')
            clauses.append('(expires_at IS NULL OR expires_at > ?)')
            params.append(time.time())
        if namespace:
            clauses.append('namespace=?')
            params.append(namespace)
        if kind:
            clauses.append('kind=?')
            params.append(kind)
        where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
        params.append(limit)
        with cls.store().conn() as c:
            rows = c.execute(
                f'SELECT * FROM memories {where} ORDER BY updated_at DESC LIMIT ?',
                params,
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    @classmethod
    def history(cls, *, namespace: str, key: str, limit: int = 100
                ) -> List[Dict[str, Any]]:
        _validate_ns_key(namespace, key)
        limit = max(1, min(int(limit), HISTORY_CAP_PER_MEMORY))
        with cls.store().conn() as c:
            row = c.execute(
                'SELECT id FROM memories WHERE namespace=? AND key=?',
                (namespace, key),
            ).fetchone()
            if row is None:
                return []
            rows = c.execute(
                'SELECT * FROM memory_history WHERE memory_id=? '
                'ORDER BY version DESC LIMIT ?',
                (row['id'], limit),
            ).fetchall()
        return [dict(r) for r in rows]

    @classmethod
    def refs(cls, *, namespace: str, key: str, limit: int = 100
             ) -> List[Dict[str, Any]]:
        _validate_ns_key(namespace, key)
        limit = max(1, min(int(limit), 500))
        with cls.store().conn() as c:
            row = c.execute(
                'SELECT id FROM memories WHERE namespace=? AND key=?',
                (namespace, key),
            ).fetchone()
            if row is None:
                return []
            rows = c.execute(
                'SELECT * FROM memory_refs WHERE memory_id=? '
                'ORDER BY at DESC LIMIT ?',
                (row['id'], limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Search (Phase 1: FTS + scoring) ───────────────────────────────────

    @classmethod
    def search(
        cls,
        *,
        q: str,
        kinds: Optional[Iterable[str]] = None,
        namespaces: Optional[Iterable[str]] = None,
        limit: int = 25,
    ) -> List[Dict[str, Any]]:
        """Hybrid keyword search ranked by FTS rank + importance + recency.

        Phase 1 is FTS5-only. Phase 2 adds vector results and fuses via
        reciprocal-rank. The signature is stable across phases.
        """
        limit = max(1, min(int(limit), 200))
        if not q or not q.strip():
            return []
        fts_q = _build_fts_query(q)
        now = time.time()
        clauses = ['m.deleted_at IS NULL',
                   '(m.expires_at IS NULL OR m.expires_at > ?)']
        kinds = [k for k in (kinds or []) if k]
        namespaces = [n for n in (namespaces or []) if n]
        if namespaces:
            clauses.append('m.namespace IN (' + ','.join('?' * len(namespaces)) + ')')
        if kinds:
            clauses.append('m.kind IN (' + ','.join('?' * len(kinds)) + ')')
        where = ' AND '.join(clauses)

        # Param order matches the SQL: MATCH ?, expires-at-now, namespaces, kinds, LIMIT.
        params: List[Any] = [fts_q, now, *namespaces, *kinds, limit * 4]

        sql = f"""
            SELECT m.*, bm25(memories_fts) AS fts_rank
            FROM memories_fts
            JOIN memories m ON m.id = memories_fts.rowid
            WHERE memories_fts MATCH ? AND {where}
            ORDER BY fts_rank LIMIT ?
        """
        with cls.store().conn() as c:
            try:
                rows = c.execute(sql, params).fetchall()
            except sqlite3.OperationalError as e:
                # Malformed FTS query (e.g. stray operator) — degrade to LIKE.
                if 'malformed MATCH' in str(e) or 'fts5' in str(e).lower():
                    rows = cls._search_like(c, q, namespaces, kinds, limit * 4)
                else:
                    raise

        scored = [(cls._rerank_score(dict(r), now), dict(r)) for r in rows]
        scored.sort(key=lambda t: -t[0])
        out = []
        for score, r in scored[:limit]:
            r['_score'] = round(score, 4)
            out.append(_row_to_dict_from_dict(r))
        return out

    @staticmethod
    def _search_like(c, q, namespaces, kinds, limit):
        params: List[Any] = [time.time()]
        clauses = ['deleted_at IS NULL',
                   '(expires_at IS NULL OR expires_at > ?)']
        like = f'%{q}%'
        clauses.append('(value LIKE ? OR key LIKE ? OR tags LIKE ?)')
        params.extend([like, like, like])
        if namespaces:
            clauses.append('namespace IN (' + ','.join('?' * len(namespaces)) + ')')
            params.extend(namespaces)
        if kinds:
            clauses.append('kind IN (' + ','.join('?' * len(kinds)) + ')')
            params.extend(kinds)
        params.append(limit)
        return c.execute(
            f"SELECT *, 0.0 AS fts_rank FROM memories WHERE {' AND '.join(clauses)} "
            f"ORDER BY updated_at DESC LIMIT ?",
            params,
        ).fetchall()

    @staticmethod
    def _rerank_score(row: Dict[str, Any], now: float) -> float:
        # bm25 returns negative scores; invert so larger=better, clamp range.
        raw = row.get('fts_rank') or 0.0
        fts = 1.0 / (1.0 + abs(raw)) if raw != 0.0 else 0.4
        importance = float(row.get('importance') or 0.5)
        updated_at = float(row.get('updated_at') or now)
        age_days = max(0.0, (now - updated_at) / 86400.0)
        recency = 1.0 / (1.0 + age_days / 14.0)  # half-decay in ~2 weeks
        last_acc = row.get('last_accessed_at')
        if last_acc:
            acc_days = max(0.0, (now - float(last_acc)) / 86400.0)
            recency = max(recency, 1.0 / (1.0 + acc_days / 14.0))
        # Weighted sum (phase-1 mix tuned conservatively).
        return 0.45 * fts + 0.30 * importance + 0.25 * recency

    # ── Auto-injection helper ─────────────────────────────────────────────

    @classmethod
    def top_for_prompt(
        cls,
        prompt: str,
        *,
        k: int = 8,
        min_score: float = 0.30,
        max_chars: int = 4096,
        exclude_secret_tag: bool = True,
    ) -> List[Dict[str, Any]]:
        """Pick the top-K memories relevant to a free-form prompt.

        Phase 1: keyword-extracted FTS query, re-ranked with importance +
        recency, secret-tagged entries optionally excluded. Phase 2 will
        union this with vector top-K via reciprocal-rank fusion.
        """
        terms = _extract_terms(prompt)
        if not terms:
            # Empty / stopword-only prompt — fall back to most-important
            # recently-updated preferences/procedurals.
            with cls.store().conn() as c:
                rows = c.execute(
                    "SELECT * FROM memories WHERE deleted_at IS NULL "
                    " AND (expires_at IS NULL OR expires_at > ?) "
                    " AND kind IN ('preference','procedural') "
                    " ORDER BY importance DESC, updated_at DESC LIMIT ?",
                    (time.time(), k * 2),
                ).fetchall()
            results = [_row_to_dict(r) for r in rows]
        else:
            q = ' OR '.join(terms)
            results = cls.search(q=q, limit=k * 3)

        out: List[Dict[str, Any]] = []
        budget = max_chars
        for r in results:
            if exclude_secret_tag and 'secret' in (r.get('tags_list') or []):
                continue
            if r.get('_score', 1.0) < min_score and terms:
                continue
            line_chars = len(r.get('value') or '') + len(r.get('namespace') or '') + 32
            if line_chars > budget:
                continue
            budget -= line_chars
            out.append(r)
            if len(out) >= k:
                break
        return out

    @staticmethod
    def format_injection_block(memories: List[Dict[str, Any]]) -> str:
        if not memories:
            return ''
        lines = []
        for m in memories:
            tags = m.get('tags') or ''
            tag_part = f' (tags: {tags})' if tags else ''
            lines.append(
                f"- [{m['namespace']}.{m['key']}] {m['value']}{tag_part}"
            )
        return (
            "<workspace_memories>\n"
            "The user has previously remembered the following. Treat as "
            "authoritative prior context; do not re-ask.\n"
            + '\n'.join(lines)
            + "\n</workspace_memories>\n\n"
        )

    # ── Relations (Phase 1: create + walk; tools wired in Phase 3) ───────

    @classmethod
    def link(
        cls,
        *,
        src_namespace: str,
        src_key: str,
        dst_namespace: str,
        dst_key: str,
        kind: str = 'related-to',
        weight: float = 1.0,
        created_by: str = '',
    ) -> Dict[str, Any]:
        _validate_ns_key(src_namespace, src_key)
        _validate_ns_key(dst_namespace, dst_key)
        _require(bool(_RELATION_KIND_RE.match(kind)),
                 'relation kind must match [a-zA-Z0-9._:-]{1,64}')
        weight = _clamp01(weight, name='weight')
        now = time.time()
        with cls.store().tx() as c:
            src = c.execute(
                'SELECT id FROM memories WHERE namespace=? AND key=? AND deleted_at IS NULL',
                (src_namespace, src_key)).fetchone()
            dst = c.execute(
                'SELECT id FROM memories WHERE namespace=? AND key=? AND deleted_at IS NULL',
                (dst_namespace, dst_key)).fetchone()
            if not src:
                raise NotFound(f'src {src_namespace}/{src_key}')
            if not dst:
                raise NotFound(f'dst {dst_namespace}/{dst_key}')
            try:
                cur = c.execute(
                    'INSERT INTO relations (src_id, dst_id, kind, weight, '
                    '  created_at, created_by) VALUES (?,?,?,?,?,?)',
                    (src['id'], dst['id'], kind, weight, now, created_by or 'unknown'),
                )
                rel_id = cur.lastrowid
            except sqlite3.IntegrityError:
                raise Conflict(f'relation already exists') from None
            row = c.execute('SELECT * FROM relations WHERE id=?', (rel_id,)).fetchone()
        return dict(row)

    @classmethod
    def neighbors(
        cls,
        *,
        namespace: str,
        key: str,
        depth: int = 1,
        kinds: Optional[Iterable[str]] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        _validate_ns_key(namespace, key)
        depth = max(1, min(int(depth), 4))
        limit = max(1, min(int(limit), 500))
        kinds_list = [k for k in (kinds or []) if k]
        now = time.time()
        with cls.store().conn() as c:
            root = c.execute(
                'SELECT id FROM memories WHERE namespace=? AND key=? '
                'AND deleted_at IS NULL '
                'AND (expires_at IS NULL OR expires_at > ?)',
                (namespace, key, now)).fetchone()
            if not root:
                return []
            kind_filter = (
                f' AND r.kind IN ({",".join("?"*len(kinds_list))})'
                if kinds_list else ''
            )
            sql = f"""
                WITH RECURSIVE walk(memory_id, depth) AS (
                    SELECT ?, 0
                    UNION ALL
                    SELECT r.dst_id, walk.depth+1
                    FROM relations r JOIN walk ON r.src_id = walk.memory_id
                    WHERE walk.depth < ? {kind_filter}
                )
                SELECT DISTINCT m.*, walk.depth
                FROM walk
                JOIN memories m ON m.id = walk.memory_id
                WHERE walk.depth > 0 AND m.deleted_at IS NULL
                  AND (m.expires_at IS NULL OR m.expires_at > ?)
                ORDER BY walk.depth, m.updated_at DESC
                LIMIT ?
            """
            ordered: List[Any] = [root['id'], depth]
            if kinds_list:
                ordered.extend(kinds_list)
            ordered.extend([now, limit])
            rows = c.execute(sql, ordered).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ── Ref logging + access stats ────────────────────────────────────────

    @classmethod
    def log_ref(cls, *, namespace: str, key: str, ref_kind: str, ref_id: str,
                access_kind: str) -> None:
        """Record a read/write access. Best-effort; never raises."""
        if access_kind not in ('read', 'write'):
            return
        try:
            with cls.store().tx() as c:
                row = c.execute(
                    'SELECT id FROM memories WHERE namespace=? AND key=?',
                    (namespace, key),
                ).fetchone()
                if not row:
                    return
                now = time.time()
                c.execute(
                    'INSERT INTO memory_refs (memory_id, ref_kind, ref_id, '
                    '  access_kind, at) VALUES (?,?,?,?,?)',
                    (row['id'], ref_kind, ref_id, access_kind, now),
                )
                if access_kind == 'read':
                    c.execute(
                        'UPDATE memories SET last_accessed_at=?, '
                        '  access_count=access_count+1 WHERE id=?',
                        (now, row['id']),
                    )
                # Prune ref-log per memory.
                c.execute(
                    'DELETE FROM memory_refs WHERE memory_id=? AND id NOT IN ('
                    '  SELECT id FROM memory_refs WHERE memory_id=? '
                    '  ORDER BY at DESC LIMIT ?'
                    ')',
                    (row['id'], row['id'], REFS_CAP_PER_MEMORY),
                )
        except sqlite3.Error:
            pass

    # ── Stats ─────────────────────────────────────────────────────────────

    @classmethod
    def stats(cls) -> Dict[str, Any]:
        with cls.store().conn() as c:
            total = c.execute(
                'SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL'
            ).fetchone()[0]
            by_kind = {
                r['kind']: r['n'] for r in c.execute(
                    'SELECT kind, COUNT(*) AS n FROM memories '
                    ' WHERE deleted_at IS NULL GROUP BY kind'
                ).fetchall()
            }
            by_namespace = [
                {'namespace': r['namespace'], 'n': r['n']}
                for r in c.execute(
                    'SELECT namespace, COUNT(*) AS n FROM memories '
                    ' WHERE deleted_at IS NULL '
                    ' GROUP BY namespace ORDER BY n DESC LIMIT 50'
                ).fetchall()
            ]
            relations = c.execute('SELECT COUNT(*) FROM relations').fetchone()[0]
            embeddings = c.execute('SELECT COUNT(*) FROM embeddings').fetchone()[0]
            pending = c.execute(
                'SELECT COUNT(*) FROM embeddings_pending'
            ).fetchone()[0]
            history = c.execute('SELECT COUNT(*) FROM memory_history').fetchone()[0]
            refs = c.execute('SELECT COUNT(*) FROM memory_refs').fetchone()[0]
        db_size = 0
        try:
            db_size = os.path.getsize(DB_PATH)
        except OSError:
            pass
        return {
            'total': total,
            'by_kind': by_kind,
            'by_namespace': by_namespace,
            'relations': relations,
            'embeddings': embeddings,
            'embeddings_pending': pending,
            'history_rows': history,
            'ref_rows': refs,
            'db_size_bytes': db_size,
            'schema_version': 1,
        }


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────

import os  # placed at bottom to keep public imports clean

_STOPWORDS = frozenset({
    'a','an','and','any','are','as','at','be','by','do','for','from','have',
    'i','if','in','is','it','me','my','of','on','or','out','that','the','this',
    'to','was','we','were','what','when','where','who','will','with','you','your',
    'yours','yourself','have','had','has','did','does','can','could','should',
    'would','about','some','they','them','their','there','these','those','than',
    'then','also','just','please','tell','say','said','remember','recall','know',
    'do','dont','don\'t','not','no','yes','only','very','really','thing','things',
})


def _extract_terms(s: str, max_terms: int = 8) -> List[str]:
    if not s:
        return []
    toks = re.findall(r"[A-Za-z][A-Za-z0-9_-]{1,}", s.lower())
    seen = []
    seen_set = set()
    for t in toks:
        if t in _STOPWORDS or len(t) < 3:
            continue
        if t in seen_set:
            continue
        seen.append(t)
        seen_set.add(t)
        if len(seen) >= max_terms:
            break
    return seen


def _build_fts_query(q: str) -> str:
    """Build a forgiving FTS5 MATCH query from free-form text.

    Splits on whitespace, drops FTS-special chars, OR-joins so any term hit
    surfaces a candidate (re-rank tightens precision).
    """
    cleaned = re.sub(r'[\"\'\(\)\*\^]', ' ', q)
    parts = [p for p in re.split(r'\s+', cleaned.strip()) if p]
    if not parts:
        return ''
    # Quote each term to disable operator parsing, then OR them.
    quoted = [f'"{p}"' for p in parts[:16]]
    return ' OR '.join(quoted)


def _row_to_dict_from_dict(d: Dict[str, Any]) -> Dict[str, Any]:
    """Like _row_to_dict but for already-dicted rows (e.g. from re-ranking)."""
    out = dict(d)
    if 'tags' in out and isinstance(out['tags'], str):
        out['tags_list'] = [t for t in (s.strip() for s in out['tags'].split(',')) if t]
    return out
