"""Read-only scanner over Claude Code's local session transcripts.

Surfaces one derived view for the dashboard:

  * subagents — every Agent tool use, matched against its tool_result
                (or marked running if no result has landed yet), with
                description, subagent_type, parent session, and duration.

There is no SQLite or write path here. Transcripts ARE the source of truth
— scanning them every ~10s is cheap and avoids a parallel store that can
drift. Results are cached per file by mtime so repeated polls do not
re-parse unchanged transcripts.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Iterable

CLAUDE_HOME = os.path.expanduser('~/.claude')
PROJECTS_DIR = os.path.join(CLAUDE_HOME, 'projects')

SUBAGENT_TOOLS = {'Agent', 'Task'}

_MAX_AGE_SEC = int(os.environ.get('KC_TRANSCRIPT_MAX_AGE_SEC', str(7 * 24 * 3600)))

_FILE_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _iter_transcripts() -> Iterable[str]:
    """Yield .jsonl session transcripts under ~/.claude/projects/, newest first."""
    if not os.path.isdir(PROJECTS_DIR):
        return
    cutoff = time.time() - _MAX_AGE_SEC
    candidates: list[tuple[float, str]] = []
    for proj in os.listdir(PROJECTS_DIR):
        pdir = os.path.join(PROJECTS_DIR, proj)
        if not os.path.isdir(pdir):
            continue
        try:
            entries = os.listdir(pdir)
        except OSError:
            continue
        for name in entries:
            if not name.endswith('.jsonl'):
                continue
            full = os.path.join(pdir, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            candidates.append((st.st_mtime, full))
    candidates.sort(reverse=True)
    for _, path in candidates:
        yield path


def _parse_transcript(path: str) -> list[dict[str, Any]]:
    """Extract subagent tool-uses from one transcript file."""
    subagents: list[dict[str, Any]] = []
    results: dict[str, dict[str, Any]] = {}
    session_id = os.path.basename(path).removesuffix('.jsonl')
    project = os.path.basename(os.path.dirname(path))

    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                msg = rec.get('message') or {}
                content = msg.get('content')
                ts = rec.get('timestamp')
                if not isinstance(content, list):
                    continue
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    btype = blk.get('type')
                    if btype == 'tool_use':
                        name = blk.get('name') or ''
                        inp = blk.get('input') or {}
                        tu_id = blk.get('id') or ''
                        if name in SUBAGENT_TOOLS:
                            subagents.append({
                                'tool_use_id': tu_id,
                                'tool': name,
                                'timestamp': ts,
                                'session_id': session_id,
                                'project': project,
                                'description': inp.get('description') or '',
                                'subagent_type': inp.get('subagent_type') or 'general-purpose',
                                'prompt': inp.get('prompt') or '',
                            })
                    elif btype == 'tool_result':
                        tu_id = blk.get('tool_use_id') or ''
                        if tu_id:
                            results[tu_id] = {
                                'timestamp': ts,
                                'is_error': bool(blk.get('is_error')),
                            }
    except OSError:
        pass

    for sa in subagents:
        res = results.get(sa['tool_use_id'])
        if res is None:
            sa['status'] = 'running'
            sa['ended_at'] = None
            sa['is_error'] = False
        else:
            sa['status'] = 'error' if res['is_error'] else 'completed'
            sa['ended_at'] = res['timestamp']
            sa['is_error'] = res['is_error']

    return subagents


def _scan_all() -> list[dict[str, Any]]:
    """Parse every recent transcript, using the mtime cache to skip work."""
    all_subagents: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    for path in _iter_transcripts():
        seen_paths.add(path)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        cached = _FILE_CACHE.get(path)
        if cached is None or cached[0] != mtime:
            parsed = _parse_transcript(path)
            _FILE_CACHE[path] = (mtime, parsed)
        else:
            parsed = cached[1]
        all_subagents.extend(parsed)

    for stale in list(_FILE_CACHE.keys() - seen_paths):
        _FILE_CACHE.pop(stale, None)

    return all_subagents


def list_subagents() -> dict[str, Any]:
    """Return all Agent tool uses, newest first."""
    items = sorted(_scan_all(), key=lambda x: x['timestamp'] or '', reverse=True)
    running = sum(1 for s in items if s['status'] == 'running')
    completed = sum(1 for s in items if s['status'] == 'completed')
    errored = sum(1 for s in items if s['status'] == 'error')
    return {
        'subagents': items,
        'count': len(items),
        'running_count': running,
        'completed_count': completed,
        'error_count': errored,
        'window_days': _MAX_AGE_SEC // 86400,
    }


if __name__ == '__main__':
    print(json.dumps(list_subagents(), indent=2, default=str))
