"""Read-only scanner over Claude Code's local session transcripts.

Surfaces two derived views for the dashboard:

  * routines  — every ScheduleWakeup / CronCreate / CronDelete tool use,
                rolled up by routine name + cadence, plus the global
                routineFiredWatermark from ~/.claude.json so the rail can
                show when Claude's Kairos scheduler last actually fired.
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
CLAUDE_CONFIG = os.path.expanduser('~/.claude.json')
PROJECTS_DIR = os.path.join(CLAUDE_HOME, 'projects')

ROUTINE_TOOLS = {'ScheduleWakeup', 'CronCreate', 'CronDelete', 'CronList'}
SUBAGENT_TOOLS = {'Agent', 'Task'}

# How recent a session must be to scan. Older transcripts are skipped so
# scans stay bounded as a workspace accumulates history. Tunable via env.
_MAX_AGE_SEC = int(os.environ.get('KC_TRANSCRIPT_MAX_AGE_SEC', str(7 * 24 * 3600)))

# Per-process cache: {path: (mtime, parsed_records)}. Records are the
# lightweight tool-use dicts we extract, not raw transcript lines.
_FILE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _iter_transcripts() -> Iterable[str]:
    """Yield .jsonl session transcripts under ~/.claude/projects/, newest first.

    Skips files older than _MAX_AGE_SEC. We sort by mtime descending so a
    bounded scan still surfaces the freshest activity.
    """
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


def _parse_transcript(path: str) -> dict[str, Any]:
    """Extract routine and subagent tool-uses from one transcript file.

    Returns a dict with two lists: ``routines`` and ``subagents``. Each
    entry is keyed by the originating tool_use_id so the caller can join
    with results later. We also track tool_result records keyed by the
    same id so subagent status (running vs done) can be computed.
    """
    routines: list[dict[str, Any]] = []
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
                        if name in ROUTINE_TOOLS:
                            routines.append({
                                'tool_use_id': tu_id,
                                'tool': name,
                                'timestamp': ts,
                                'session_id': session_id,
                                'project': project,
                                'input': inp,
                            })
                        elif name in SUBAGENT_TOOLS:
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

    # Attach results to subagents so we can mark them running/completed.
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

    return {'routines': routines, 'subagents': subagents}


def _scan_all() -> dict[str, list[dict[str, Any]]]:
    """Parse every recent transcript, using the mtime cache to skip work."""
    all_routines: list[dict[str, Any]] = []
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
        all_routines.extend(parsed['routines'])
        all_subagents.extend(parsed['subagents'])

    # Drop cache entries for transcripts we no longer see (file aged out
    # or was deleted) so the cache does not grow unboundedly.
    for stale in list(_FILE_CACHE.keys() - seen_paths):
        _FILE_CACHE.pop(stale, None)

    return {'routines': all_routines, 'subagents': all_subagents}


def _load_routine_watermark() -> dict[str, Any]:
    """Read ~/.claude.json for the global Kairos scheduler watermark.

    Returns ``{}`` if the config is missing or unreadable; the dashboard
    treats absence as "Kairos has never fired".
    """
    try:
        with open(CLAUDE_CONFIG, 'r', encoding='utf-8') as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return {}
    out: dict[str, Any] = {}
    if 'routineFiredWatermark' in cfg:
        out['routine_fired_watermark'] = cfg['routineFiredWatermark']
    features = cfg.get('cachedGrowthBookFeatures') or {}
    out['kairos_cron_enabled'] = bool(features.get('tengu_kairos_cron'))
    out['kairos_cron_durable'] = bool(features.get('tengu_kairos_cron_durable'))
    out['kairos_loop_dynamic'] = bool(features.get('tengu_kairos_loop_dynamic'))
    return out


def _routine_key(r: dict[str, Any]) -> str:
    """Stable identity for a routine across multiple firings.

    For ScheduleWakeup the prompt is the routine — same prompt = same
    routine. For Cron* tools we key off the cron id when present.
    """
    inp = r.get('input') or {}
    if r['tool'] in ('CronCreate', 'CronDelete', 'CronList'):
        cron_id = inp.get('id') or inp.get('cronId') or inp.get('name') or ''
        if cron_id:
            return f"cron:{cron_id}"
    prompt = (inp.get('prompt') or '').strip()
    if prompt:
        return f"prompt:{hash(prompt) & 0xffffffff:08x}"
    return f"unknown:{r['tool_use_id']}"


def list_routines() -> dict[str, Any]:
    """Return rolled-up routines plus the Kairos watermark.

    Each routine groups every ScheduleWakeup / Cron* tool use we saw with
    the same key (see _routine_key). ``fire_count`` tells the user how
    often Claude scheduled this same routine; ``last_fired_at`` is the
    most recent invocation we found in transcripts.
    """
    scan = _scan_all()
    rolled: dict[str, dict[str, Any]] = {}
    for r in scan['routines']:
        key = _routine_key(r)
        inp = r.get('input') or {}
        existing = rolled.get(key)
        if existing is None:
            rolled[key] = {
                'id': key,
                'tool': r['tool'],
                'first_seen_at': r['timestamp'],
                'last_fired_at': r['timestamp'],
                'fire_count': 1,
                'last_session_id': r['session_id'],
                'last_project': r['project'],
                'prompt': inp.get('prompt') or '',
                'delay_seconds': inp.get('delaySeconds'),
                'reason': inp.get('reason') or '',
                'cron_expression': inp.get('cron') or inp.get('schedule') or '',
                'cron_id': inp.get('id') or inp.get('cronId') or '',
            }
        else:
            existing['fire_count'] += 1
            # Timestamps in transcripts are ISO-8601 strings, so string
            # comparison is monotonic. Update bounds accordingly.
            if r['timestamp'] and (not existing['last_fired_at'] or r['timestamp'] > existing['last_fired_at']):
                existing['last_fired_at'] = r['timestamp']
                existing['last_session_id'] = r['session_id']
                existing['last_project'] = r['project']
                # Keep the most recent prompt/reason — those can evolve.
                if inp.get('prompt'):
                    existing['prompt'] = inp['prompt']
                if inp.get('reason'):
                    existing['reason'] = inp['reason']
            if r['timestamp'] and (not existing['first_seen_at'] or r['timestamp'] < existing['first_seen_at']):
                existing['first_seen_at'] = r['timestamp']

    routines = sorted(rolled.values(), key=lambda x: x['last_fired_at'] or '', reverse=True)
    return {
        'routines': routines,
        'count': len(routines),
        'watermark': _load_routine_watermark(),
        'window_days': _MAX_AGE_SEC // 86400,
    }


def list_subagents() -> dict[str, Any]:
    """Return all Agent tool uses, newest first.

    Each entry has ``status`` ∈ {running, completed, error}. A subagent
    with no matching tool_result in the transcript is reported as running
    — this is the dashboard's "is anything happening right now" signal.
    """
    scan = _scan_all()
    items = sorted(scan['subagents'], key=lambda x: x['timestamp'] or '', reverse=True)
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


def stats() -> dict[str, Any]:
    """Lightweight counts for the dashboard's tab badges."""
    routines = list_routines()
    subs = list_subagents()
    return {
        'routine_count': routines['count'],
        'subagent_count': subs['count'],
        'subagent_running': subs['running_count'],
        'watermark': routines['watermark'],
        'window_days': _MAX_AGE_SEC // 86400,
    }


if __name__ == '__main__':
    # Hand-test: print a JSON summary so operators can spot-check the
    # scanner without booting the full server. Useful inside a pod via
    # `python3 /tmp/browser/transcript_scanner.py | jq`.
    print(json.dumps({
        'routines': list_routines(),
        'subagents': list_subagents(),
    }, indent=2, default=str))
