"""Background skills scanner with in-memory cache.

Modeled on memory/sync.py's ClaudeMemorySyncer (TOCTOU-safe start,
status(), trigger_sync()) but with two differences:

* No SQLite — skills are few, files are the source of truth, so the
  merged records live in an in-memory snapshot swapped under a lock.
* It publishes an aggregate `skills.changed` event through an injected
  publisher (the server passes EventBroker.publish) whenever a pass
  actually changed the cache — the TaskReconciler publish pattern.

Change detection is a cheap mtime fingerprint per provider (stat-only,
no file reads); parsing only happens on a pass where some file's mtime,
membership, or count changed.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Callable, Dict, List, Optional

from .model import SkillRecord, merge, find
from .providers import PROVIDERS


def scan_once() -> List[SkillRecord]:
    """One full scan across all enabled providers → merged records."""
    per_file: List[SkillRecord] = []
    for key, provider in PROVIDERS.items():
        try:
            per_file.extend(provider.scan())
        except Exception as e:  # provider bugs must never kill the pass
            print(f'[skills] provider {key} scan failed: {e}', file=sys.stderr)
    return merge(per_file)


class SkillsSyncer:
    """Single-process background poller. Idempotent; safe to start once."""

    _started = False
    _thread: Optional[threading.Thread] = None
    _stop_event = threading.Event()
    _start_lock = threading.Lock()

    _cache_lock = threading.Lock()
    _cache: List[SkillRecord] = []
    _cache_version = 0
    _last_fingerprints: Dict[str, Dict[str, float]] = {}
    _last_run_at: float = 0.0

    # Injected by the server at start(); called as publish(type, data).
    _publish: Optional[Callable[[str, dict], None]] = None

    # ── public API ───────────────────────────────────────────────────────

    @classmethod
    def start(cls, *, interval_seconds: int = 30,
              publish: Optional[Callable[[str, dict], None]] = None) -> None:
        with cls._start_lock:
            if cls._started:
                return
            cls._started = True
        cls._publish = publish

        def _loop():
            # Eager first pass so the dashboard has data immediately.
            while not cls._stop_event.is_set():
                try:
                    cls._pass()
                except Exception as e:
                    print(f'[skills] background pass failed: {e}',
                          file=sys.stderr)
                cls._stop_event.wait(interval_seconds)

        t = threading.Thread(target=_loop, name='skills-sync', daemon=True)
        cls._thread = t
        t.start()

    @classmethod
    def snapshot(cls) -> List[SkillRecord]:
        """Copy of the merged records; handlers read from this."""
        with cls._cache_lock:
            return list(cls._cache)

    @classmethod
    def get(cls, name: str) -> List[SkillRecord]:
        """All variants of a logical skill (1 usually; 2+ when divergent)."""
        return find(cls.snapshot(), name)

    @classmethod
    def status(cls) -> Dict[str, object]:
        with cls._cache_lock:
            count = len(cls._cache)
            version = cls._cache_version
        return {
            'running': cls._started and (cls._thread is not None
                                         and cls._thread.is_alive()),
            'last_run_at': cls._last_run_at or None,
            'count': count,
            'version': version,
            'providers': {k: p.enabled for k, p in PROVIDERS.items()},
        }

    @classmethod
    def trigger_sync(cls) -> Dict[str, object]:
        """Synchronous forced rescan (used by /api/skills/_scan and
        ?refresh=1). Bypasses the mtime shortcut."""
        records = scan_once()
        changed = cls._swap(records)
        # Refresh fingerprints so the next background pass doesn't redo work.
        cls._refingerprint()
        cls._last_run_at = time.time()
        if changed:
            cls._emit()
        return {'scanned': len(records), 'changed': changed,
                'version': cls._cache_version}

    # ── internals ────────────────────────────────────────────────────────

    @classmethod
    def _pass(cls) -> None:
        fps = {k: p.roots_mtime_fingerprint() for k, p in PROVIDERS.items()}
        if fps == cls._last_fingerprints and cls._cache_version > 0:
            cls._last_run_at = time.time()
            return
        records = scan_once()
        changed = cls._swap(records)
        cls._last_fingerprints = fps
        cls._last_run_at = time.time()
        if changed:
            cls._emit()

    @classmethod
    def _refingerprint(cls) -> None:
        cls._last_fingerprints = {
            k: p.roots_mtime_fingerprint() for k, p in PROVIDERS.items()
        }

    @classmethod
    def _swap(cls, records: List[SkillRecord]) -> bool:
        """Install a new snapshot; returns True if content changed."""
        new_shape = [(r.name, r.fingerprint, tuple(sorted(r.systems)),
                      r.scope) for r in records]
        with cls._cache_lock:
            old_shape = [(r.name, r.fingerprint, tuple(sorted(r.systems)),
                          r.scope) for r in cls._cache]
            if new_shape == old_shape and cls._cache_version > 0:
                return False
            cls._cache = records
            cls._cache_version += 1
            return True

    @classmethod
    def _emit(cls) -> None:
        pub = cls._publish
        if pub is None:
            return
        try:
            with cls._cache_lock:
                count, version = len(cls._cache), cls._cache_version
            pub('skills.changed', {'count': count, 'version': version})
        except Exception as e:
            print(f'[skills] publish failed: {e}', file=sys.stderr)

    # ── test hooks ───────────────────────────────────────────────────────

    @classmethod
    def _reset_for_test(cls) -> None:
        cls._stop_event.set()
        if cls._thread is not None:
            cls._thread.join(timeout=2)
        cls._started = False
        cls._thread = None
        cls._stop_event = threading.Event()
        with cls._cache_lock:
            cls._cache = []
            cls._cache_version = 0
        cls._last_fingerprints = {}
        cls._last_run_at = 0.0
        cls._publish = None
