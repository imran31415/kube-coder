"""Background embedding worker — Phase 2 semantic memory (#90).

Every memory write enqueues an `embeddings_pending` row (see
`MemoryManager.upsert` / `update_partial`). This worker is the consumer that
the Phase-1 schema was always waiting for: a single daemon thread that drains
the queue in batches, turns each memory's text into a vector via the
configured provider (`memory/embeddings.py`), and writes both an `embeddings`
bookkeeping row and the vector itself into the `vec_memories` vec0 table that
`MemoryManager.search()` fuses with FTS.

It is modeled directly on `memory.sync.ClaudeMemorySyncer`: idempotent
`start()`, a stop event, and a `status()` snapshot for the dashboard.

The whole feature no-ops gracefully:
  * no provider/key configured  → `start()` returns False, no thread spawned.
  * sqlite-vec extension absent  → `start()` returns False (nothing to write to).
A provider error during a cycle doesn't drop work: the pending rows stay,
their `attempts`/`last_error` are bumped, and the next cycle retries with
backoff. A row that fails `MAX_ATTEMPTS` times is dropped so one poison memory
can't wedge the queue forever.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any, Dict, List, Optional

from . import store as _store
from .store import MemoryStore, DB_PATH

# Drop a pending row after this many failed embed attempts (poison-pill guard).
MAX_ATTEMPTS = 5
# Max memories embedded per provider call / cycle. Bounds latency + memory.
DEFAULT_BATCH = 32


def _load_pending_batch(c, batch: int) -> List[Dict[str, Any]]:
    """Read up to `batch` distinct memories with queued embeddings.

    Collapses the (possibly many) pending rows per memory into one work item
    carrying every pending row id so the whole backlog for that memory drains
    in one shot. Soft-deleted memories surface too (deleted_at set) so we can
    drop their stale queue entries without embedding them.
    """
    rows = c.execute(
        'SELECT p.id AS pid, p.memory_id, p.attempts, '
        '       m.value, m.deleted_at '
        'FROM embeddings_pending p '
        'JOIN memories m ON m.id = p.memory_id '
        'ORDER BY p.id'
    ).fetchall()
    items: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        mid = int(r['memory_id'])
        it = items.get(mid)
        if it is None:
            # New distinct memory: respect the batch cap. We keep scanning
            # past the cap only to fold in more pending rows for memories
            # already selected (so a memory's backlog drains atomically).
            if len(items) >= batch:
                continue
            it = {
                'memory_id': mid,
                'value': r['value'],
                'deleted': r['deleted_at'] is not None,
                'attempts': 0,
                'pids': [],
            }
            items[mid] = it
        it['pids'].append(int(r['pid']))
        it['attempts'] = max(it['attempts'], int(r['attempts'] or 0))
    return list(items.values())


class EmbeddingWorker:
    """Single-process background embedder. Idempotent; safe to start once."""

    _started = False
    _thread: Optional[threading.Thread] = None
    _stop_event = threading.Event()
    _start_lock = threading.Lock()
    _last_run_at: float = 0.0
    _last_result: Dict[str, Any] = {}
    _provider_name: Optional[str] = None

    @classmethod
    def start(cls, *, interval_seconds: int = 30,
              provider=None, store: Optional[MemoryStore] = None,
              batch: int = DEFAULT_BATCH) -> bool:
        """Spawn the worker if (and only if) embeddings are fully available.

        Returns True when the thread was started, False when the feature is
        disabled (no provider) or vectors are unavailable (no extension).
        Callers use the return value purely for logging.
        """
        if provider is None:
            try:
                from .embeddings import get_provider
                provider = get_provider()
            except Exception as e:  # pragma: no cover - defensive
                print(f'[memory.embed] provider init failed: {e}', file=sys.stderr)
                provider = None
        if provider is None:
            return False

        # Ensure the vec0 table exists on this (possibly upgraded) DB; if the
        # extension isn't loadable there's nowhere to write vectors.
        if not _store.ensure_vec_table(DB_PATH if store is None else store.db_path):
            print('[memory.embed] sqlite-vec unavailable; worker not started',
                  file=sys.stderr)
            return False

        # vec_memories is FLOAT[VEC_DIM]; a provider that emits a different
        # width would have every insert rejected. Warn loudly rather than let
        # the queue silently poison-drop.
        pdim = getattr(provider, 'dim', _store.VEC_DIM)
        if pdim != _store.VEC_DIM:
            print(f'[memory.embed] WARNING: provider dim {pdim} != vec table '
                  f'dim {_store.VEC_DIM}; embeddings will be rejected. Set '
                  f'KC_EMBED_DIM={_store.VEC_DIM} or choose a matching model.',
                  file=sys.stderr)

        with cls._start_lock:
            if cls._started:
                return True
            cls._started = True

        store = store or MemoryStore(DB_PATH, load_vec=True)
        cls._provider_name = getattr(provider, 'name', None)

        def _loop():
            while not cls._stop_event.is_set():
                try:
                    res = cls.run_once(provider, store, batch=batch)
                    cls._last_run_at = time.time()
                    cls._last_result = res
                    if res.get('embedded') or res.get('failed') or res.get('dropped'):
                        print(
                            f"[memory.embed] embedded={res['embedded']} "
                            f"failed={res['failed']} dropped={res['dropped']} "
                            f"remaining={res['remaining']}"
                        )
                except Exception as e:  # pragma: no cover - defensive
                    print(f'[memory.embed] cycle failed: {e}', file=sys.stderr)
                cls._stop_event.wait(interval_seconds)

        t = threading.Thread(target=_loop, name='memory-embed-worker', daemon=True)
        cls._thread = t
        t.start()
        return True

    @classmethod
    def run_once(cls, provider, store: MemoryStore, *, batch: int = DEFAULT_BATCH
                 ) -> Dict[str, Any]:
        """Drain one batch. Returns counters. Never raises on provider errors —
        those are recorded on the pending rows for the next cycle.
        """
        embedded = failed = dropped = 0

        with store.conn() as c:
            items = _load_pending_batch(c, batch)

        if not items:
            return cls._counters(store, embedded, failed, dropped)

        # Drop queue entries for soft-deleted memories without embedding them.
        live = []
        drop_pids: List[int] = []
        for it in items:
            if it['deleted']:
                drop_pids.extend(it['pids'])
                dropped += 1
            else:
                live.append(it)

        embeddings: List[List[float]] = []
        embed_failed = False
        if live:
            try:
                embeddings = provider.embed([it['value'] for it in live])
            except Exception as e:
                embed_failed = True
                err = str(e)[:500]

        with store.tx() as c:
            if drop_pids:
                cls._delete_pending(c, drop_pids)

            if live and embed_failed:
                # Whole-batch failure: bump attempts, keep rows, drop the ones
                # that have exhausted their retries.
                for it in live:
                    if it['attempts'] + 1 >= MAX_ATTEMPTS:
                        cls._delete_pending(c, it['pids'])
                        dropped += 1
                    else:
                        cls._bump_attempts(c, it['pids'], err)
                        failed += 1
            elif live:
                model = getattr(provider, 'model', 'unknown')
                dim = getattr(provider, 'dim', len(embeddings[0]) if embeddings else 0)
                for it, vec in zip(live, embeddings):
                    try:
                        emb_id = cls._record_embedding(c, it['memory_id'], model, dim)
                        _store.upsert_vector(c, emb_id, vec)
                        cls._delete_pending(c, it['pids'])
                        embedded += 1
                    except Exception as e:  # vec write failed for this row
                        if it['attempts'] + 1 >= MAX_ATTEMPTS:
                            cls._delete_pending(c, it['pids'])
                            dropped += 1
                        else:
                            cls._bump_attempts(c, it['pids'], str(e)[:500])
                            failed += 1

        return cls._counters(store, embedded, failed, dropped)

    # ── SQL helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _record_embedding(c, memory_id: int, model: str, dim: int) -> int:
        """Upsert the embeddings bookkeeping row, returning its id.

        UNIQUE(memory_id, model) means one row per memory+model; we reuse the
        same embedding_id across re-embeds so vec_memories stays 1:1."""
        c.execute(
            'INSERT INTO embeddings (memory_id, model, dim, created_at) '
            'VALUES (?,?,?,?) '
            'ON CONFLICT(memory_id, model) DO UPDATE SET '
            '  dim=excluded.dim, created_at=excluded.created_at',
            (memory_id, model, int(dim), time.time()),
        )
        row = c.execute(
            'SELECT id FROM embeddings WHERE memory_id=? AND model=?',
            (memory_id, model),
        ).fetchone()
        return int(row['id'])

    @staticmethod
    def _delete_pending(c, pids: List[int]) -> None:
        if not pids:
            return
        c.execute(
            'DELETE FROM embeddings_pending WHERE id IN (%s)'
            % ','.join('?' * len(pids)),
            pids,
        )

    @staticmethod
    def _bump_attempts(c, pids: List[int], err: str) -> None:
        if not pids:
            return
        c.execute(
            'UPDATE embeddings_pending SET attempts=attempts+1, last_error=? '
            'WHERE id IN (%s)' % ','.join('?' * len(pids)),
            [err, *pids],
        )

    @classmethod
    def _counters(cls, store: MemoryStore, embedded: int, failed: int,
                  dropped: int) -> Dict[str, Any]:
        with store.conn() as c:
            remaining = c.execute(
                'SELECT COUNT(*) FROM embeddings_pending'
            ).fetchone()[0]
        return {
            'embedded': embedded,
            'failed': failed,
            'dropped': dropped,
            'remaining': remaining,
        }

    @classmethod
    def status(cls) -> Dict[str, Any]:
        return {
            'running': cls._started and (
                cls._thread is not None and cls._thread.is_alive()),
            'provider': cls._provider_name,
            'last_run_at': cls._last_run_at or None,
            'last_result': cls._last_result or {},
        }

    @classmethod
    def trigger(cls, provider=None, store: Optional[MemoryStore] = None
                ) -> Dict[str, Any]:
        """Manual one-shot drain (used by tests / an admin endpoint)."""
        if provider is None:
            from .embeddings import get_provider
            provider = get_provider()
        if provider is None:
            return {'embedded': 0, 'failed': 0, 'dropped': 0, 'remaining': 0,
                    'disabled': True}
        store = store or MemoryStore(DB_PATH, load_vec=True)
        res = cls.run_once(provider, store)
        cls._last_run_at = time.time()
        cls._last_result = res
        return res
