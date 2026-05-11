"""Auto-sync of Claude Code's native file-based memory into the SQLite store.

Claude Code (the CLI) writes its `auto-memory` system to flat markdown
files at:

    ~/.claude/projects/<project-id>/memory/*.md
    ~/.claude/memory/*.md                       (user-level, if used)

Each file has YAML-ish frontmatter (`name`, `description`, `type`) and a
markdown body. This module scans those files and upserts each one as a
memory entry in our SQLite store so they appear in the dashboard Memory
tab alongside dashboard-authored and MCP-authored entries.

Sync is one-way (files → SQLite). The native files remain canonical for
Claude's own consumption; the SQLite copy is purely for surfacing.

Conflict policy: imported entries live in a dedicated namespace prefix
(`claude.<project>.*`) and are tagged `auto-imported,claude-memory` so
they cannot collide with user-authored entries. Re-running is cheap: an
mtime fingerprint is stored on the row (in `tags`) and unchanged files
are skipped.
"""

from __future__ import annotations

import os
import re
import sys
import time
import threading
from typing import Dict, Iterable, List, Optional, Tuple

from .manager import MemoryManager, ValidationError, MemoryError as MemErr


# Scan roots (each candidate home dir's .claude tree). We scan multiple
# because the workspace pod runs services under varying users
# (root → dev → ubuntu depending on container init), so Claude's native
# memory may land in any of /home/dev/.claude or /home/ubuntu/.claude.
DEFAULT_SCAN_ROOTS = (
    '/home/dev/.claude',
    '/home/ubuntu/.claude',
    os.path.expanduser('~/.claude'),
)

# Skip files Claude uses for things other than auto-memory.
SKIP_BASENAMES = frozenset({'MEMORY.md', 'CLAUDE.md'})

# Tag every imported row with these so the UI can badge them and the user
# can filter them out.
IMPORT_TAGS = ('auto-imported', 'claude-memory')

# Map Claude's `type` field to our `kind`.
TYPE_TO_KIND = {
    'user': 'preference',
    'feedback': 'preference',
    'project': 'semantic',
    'reference': 'procedural',
}


_NS_KEY_SANITIZE = re.compile(r'[^a-zA-Z0-9._-]')
_FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n(.*)$', re.DOTALL)


def _sanitize_component(s: str, *, max_len: int = 96) -> str:
    """Make `s` safe to embed in a namespace or key component."""
    s = _NS_KEY_SANITIZE.sub('-', s).strip('-')
    if not s:
        s = 'unnamed'
    if len(s) > max_len:
        s = s[:max_len].rstrip('-') or 'unnamed'
    return s


def _project_id_from_path(file_path: str) -> str:
    """Extract a stable project-id slug from `.claude/projects/<id>/memory/*.md`.

    Falls back to 'user' if the file is under `.claude/memory/` (the
    user-level Claude memory directory).
    """
    parts = file_path.split(os.sep)
    try:
        i = parts.index('projects')
        if i + 1 < len(parts):
            return _sanitize_component(parts[i + 1])
    except ValueError:
        pass
    return 'user'


def _parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """Tiny dependency-free YAML-ish parser. Handles k: v lines only."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    head, body = m.group(1), m.group(2)
    meta: Dict[str, str] = {}
    for line in head.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if ':' not in line:
            continue
        k, v = line.split(':', 1)
        meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, body.lstrip()


def _mtime_tag(mtime: float) -> str:
    """Encode the file mtime as a tag so we can detect unchanged files."""
    return f'mtime:{int(mtime)}'


def _has_mtime_tag(tags: str, mtime: float) -> bool:
    target = _mtime_tag(mtime)
    for t in tags.split(','):
        if t.strip() == target:
            return True
    return False


def _build_tags(mtime: float, extra: Optional[Iterable[str]] = None) -> str:
    parts = list(IMPORT_TAGS) + [_mtime_tag(mtime)]
    if extra:
        for t in extra:
            t = (t or '').strip()
            if t and t not in parts:
                parts.append(t)
    return ','.join(parts)


def _iter_memory_files(roots: Iterable[str]) -> Iterable[str]:
    seen = set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        # Per-project memory: ~/.claude/projects/<id>/memory/*.md
        projects_dir = os.path.join(root, 'projects')
        if os.path.isdir(projects_dir):
            for entry in os.listdir(projects_dir):
                mem_dir = os.path.join(projects_dir, entry, 'memory')
                if not os.path.isdir(mem_dir):
                    continue
                for name in os.listdir(mem_dir):
                    if not name.endswith('.md'):
                        continue
                    if name in SKIP_BASENAMES:
                        continue
                    p = os.path.join(mem_dir, name)
                    rp = os.path.realpath(p)
                    if rp in seen:
                        continue
                    seen.add(rp)
                    yield p
        # User-level memory: ~/.claude/memory/*.md
        user_mem_dir = os.path.join(root, 'memory')
        if os.path.isdir(user_mem_dir):
            for name in os.listdir(user_mem_dir):
                if not name.endswith('.md'):
                    continue
                if name in SKIP_BASENAMES:
                    continue
                p = os.path.join(user_mem_dir, name)
                rp = os.path.realpath(p)
                if rp in seen:
                    continue
                seen.add(rp)
                yield p


def _sync_one(file_path: str) -> Tuple[str, str, bool]:
    """Upsert a single memory file. Returns (namespace, key, changed)."""
    try:
        st = os.stat(file_path)
    except OSError:
        return ('', '', False)
    mtime = st.st_mtime

    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            raw = f.read()
    except OSError:
        return ('', '', False)

    meta, body = _parse_frontmatter(raw)
    project = _project_id_from_path(file_path)
    namespace = f'claude.{project}'
    key = _sanitize_component(os.path.basename(file_path)[:-3])  # strip .md

    # Look up existing row; skip if unchanged.
    existing = MemoryManager.get(namespace=namespace, key=key)
    if existing and _has_mtime_tag(existing.get('tags', ''), mtime):
        return (namespace, key, False)

    kind = TYPE_TO_KIND.get(meta.get('type', '').lower(), 'semantic')
    # Preserve human-friendly title (if `name` was set) in the body.
    title = meta.get('name', '').strip()
    description = meta.get('description', '').strip()
    value_parts = []
    if title:
        value_parts.append(f'**{title}**')
    if description:
        value_parts.append(description)
    if body.strip():
        value_parts.append(body.strip())
    value = '\n\n'.join(value_parts) if value_parts else raw

    # Truncate to the manager's 256 KiB ceiling; very long memos are unusual
    # but we want to fail soft, not raise.
    encoded = value.encode('utf-8')
    if len(encoded) > 200_000:
        value = encoded[:200_000].decode('utf-8', errors='replace') + '\n…(truncated)'

    try:
        MemoryManager.upsert(
            namespace=namespace,
            key=key,
            value=value,
            kind=kind,
            tags=_build_tags(mtime),
            importance=0.6,
            source=f'claude-auto:{file_path}',
        )
        return (namespace, key, True)
    except (ValidationError, MemErr) as e:
        print(f'[memory.sync] upsert {namespace}/{key} failed: {e}',
              file=sys.stderr)
        return (namespace, key, False)


def sync_once(roots: Iterable[str] = DEFAULT_SCAN_ROOTS) -> Dict[str, int]:
    """One pass over the filesystem. Returns counters for logging/stats."""
    scanned = 0
    changed = 0
    seen_keys: List[Tuple[str, str]] = []
    for p in _iter_memory_files(roots):
        scanned += 1
        ns, key, did = _sync_one(p)
        if ns and key:
            seen_keys.append((ns, key))
            if did:
                changed += 1

    # Soft-delete entries whose source file disappeared (only entries with
    # the claude-memory tag are touched; user-authored entries are untouched).
    seen_set = {(n, k) for n, k in seen_keys}
    pruned = 0
    try:
        # Scan the full set of claude.* namespaces.
        candidates = MemoryManager.list(limit=2000)
        for row in candidates:
            tags = row.get('tags', '')
            if 'claude-memory' not in tags:
                continue
            if (row['namespace'], row['key']) not in seen_set:
                MemoryManager.soft_delete(
                    namespace=row['namespace'], key=row['key'],
                    source='claude-auto:removed',
                )
                pruned += 1
    except Exception as e:
        print(f'[memory.sync] prune phase failed: {e}', file=sys.stderr)

    return {'scanned': scanned, 'changed': changed, 'pruned': pruned}


# ───────────────────────────────────────────────────────────────────────────
# Background thread
# ───────────────────────────────────────────────────────────────────────────

class ClaudeMemorySyncer:
    """Single-process background poller. Idempotent; safe to start once."""

    _started = False
    _thread: Optional[threading.Thread] = None
    _stop_event = threading.Event()
    _last_run_at: float = 0.0
    _last_result: Dict[str, int] = {}

    @classmethod
    def start(cls, *, interval_seconds: int = 60,
              roots: Iterable[str] = DEFAULT_SCAN_ROOTS) -> None:
        if cls._started:
            return
        cls._started = True

        def _loop():
            # One eager pass on startup so the dashboard shows existing
            # files immediately; subsequent passes catch new writes.
            while not cls._stop_event.is_set():
                try:
                    res = sync_once(roots)
                    cls._last_run_at = time.time()
                    cls._last_result = res
                    if res.get('changed') or res.get('pruned'):
                        print(
                            f"[memory.sync] scanned={res['scanned']} "
                            f"changed={res['changed']} pruned={res['pruned']}"
                        )
                except Exception as e:
                    print(f'[memory.sync] background pass failed: {e}',
                          file=sys.stderr)
                cls._stop_event.wait(interval_seconds)

        t = threading.Thread(target=_loop, name='claude-memory-sync', daemon=True)
        cls._thread = t
        t.start()

    @classmethod
    def status(cls) -> Dict[str, object]:
        return {
            'running': cls._started and (cls._thread is not None and cls._thread.is_alive()),
            'last_run_at': cls._last_run_at or None,
            'last_result': cls._last_result or {},
        }

    @classmethod
    def trigger_sync(cls, roots: Iterable[str] = DEFAULT_SCAN_ROOTS) -> Dict[str, int]:
        """Manual one-shot trigger (used by /api/memory/_sync_claude)."""
        res = sync_once(roots)
        cls._last_run_at = time.time()
        cls._last_result = res
        return res
