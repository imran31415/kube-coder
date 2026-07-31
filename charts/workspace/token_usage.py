"""Token accounting for agent work (#574, Phase 1 of the Agent Spend epic #573).

Two jobs, one vocabulary:

1. **Split the token classes.** Fresh input, cache read, cache write and output
   bill at wildly different rates (cache reads ~10x cheaper than fresh input,
   cache writes carry a premium, output ~5x input). The pre-#574 accounting
   summed the three input classes into one `input` figure — fine for counting,
   impossible to price. Everything here keeps them apart.

2. **Measure Builds.** `HypervisorSession` threads get usage from the Claude
   stream's terminal `result` event, but Builds run an interactive CLI in a tmux
   pane with no structured stream, so they have never reported a token. Claude
   Code writes a durable JSONL transcript per session, and those lines carry
   per-message `model` + `usage`; this module reads them.

This phase is measurement only. **No prices, no dollar amounts** — that is
Phase 2. `costUSD` / `total_cost_usd` are deliberately not read even though the
stream offers them.

## Robustness

Claude Code's JSONL/stream shape is not a contract. Every field here is treated
as optional and every value as untrusted: a missing, malformed or absent
transcript degrades to zero with a warning. Nothing in this module raises on bad
input — callers must still isolate it (ingestion must never affect the Build it
is measuring), but it should never come to that.

## Idempotency (the correctness risk)

Re-reading a transcript on every poll would re-add the same messages, and one
API response is written to the JSONL as *several* lines (one per content block)
that all repeat the same `usage` — so counting lines double- or triple-counts.
Two mechanisms, both verified by tests:

* **Dedupe by billing identity**, not by line: `message.id` (the `msg_…` API
  response id, present on 100% of usage-bearing lines in the corpus surveyed),
  falling back to `requestId`, then the record `uuid`, then the line's byte
  offset.
* **Per-file cumulative ledgers.** `ingest()` stores each file's own running
  usage plus a resume offset, and the public totals are *recomputed* as the sum
  over files. So a rescan from scratch converges to the same number instead of
  doubling it — the ledger is idempotent even if its resume state is lost.

The resume offset points at the START of the last message group seen (not the
end of file), and the last few keys are retained, so a group whose lines straddle
two polls is recognised rather than counted twice.
"""

import glob
import json
import os
import re
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ── schema ───────────────────────────────────────────────────────────────────
# Bumped when the on-disk shape of a usage dict changes. v1 (pre-#574) was
# {'input': <fresh+cache_read+cache_write>, 'output': n} with no marker; v2 keeps
# the four classes apart and carries this version. `migrate()` converts v1 → v2
# without inventing a class split it cannot know.
SCHEMA_VERSION = 2

#: The four priceable token classes, in a stable order.
CLASSES: Tuple[str, ...] = ('input', 'cache_read', 'cache_write', 'output')

#: Where a usage figure came from. Recorded on every ledger so a later phase
#: knows how complete it is: the stream's `modelUsage` includes side-calls the
#: transcript never records (e.g. the small haiku title/quota calls), while the
#: transcript covers Builds, which have no stream at all.
SOURCE_STREAM = 'claude_stream'
SOURCE_TRANSCRIPT = 'claude_transcript'

#: Coverage markers — a 0 must be distinguishable from "never measured".
COVERAGE_MEASURED = 'measured'            # instrumented; 0 means 0
COVERAGE_NOT_INSTRUMENTED = 'not_instrumented'  # reports nothing; 0 means unknown
COVERAGE_NO_SESSION = 'no_session_id'     # Claude, but no session id to read

#: Assistants whose spend this workspace can actually measure. Claude Code is
#: the only one: it has both a structured stream (threads) and a durable JSONL
#: transcript (Builds). Codex / Antigravity / Ante / OpenCode / LibreFang /
#: kc-harness expose neither, so they contribute 0 — which is why the coverage
#: marker exists rather than a bare zero.
INSTRUMENTED_ASSISTANTS = frozenset({'claude'})

#: Cap on retained warning strings per ledger — bounded so a persistently
#: broken transcript can't grow task.json without limit.
MAX_WARNINGS = 5

#: Message keys retained per file to catch a message group whose lines straddle
#: two polls. A group is a handful of lines; 8 is generous.
_KEY_RING = 8

#: Cap on files tracked in one ledger (a Build's main transcript plus its
#: subagent transcripts). Beyond this, extra files are ignored with a warning
#: rather than growing the ingest state without bound.
MAX_TRACKED_FILES = 256


# ── tolerant coercion ────────────────────────────────────────────────────────

def _int(v: Any) -> int:
    """Any JSON value → a non-negative int, never raising.

    Transcript fields are not a contract: a class may be absent (None), a string,
    a float, or something structural. Anything uninterpretable is 0, and negative
    values are clamped — a negative token count is nonsense and would silently
    reduce a total.
    """
    if v is None or isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return max(0, v)
    if isinstance(v, float):
        return max(0, int(v)) if v == v and v not in (float('inf'), float('-inf')) else 0
    if isinstance(v, str):
        try:
            return max(0, int(float(v.strip())))
        except (ValueError, TypeError):
            return 0
    return 0


def _model_name(v: Any) -> str:
    """A usable model id, or ''. `<synthetic>` is Claude Code's marker for a
    locally-generated message (an API-error notice); it is not a model anyone
    can price, and those records carry all-zero usage anyway."""
    if not isinstance(v, str):
        return ''
    v = v.strip()
    return '' if (not v or v.startswith('<')) else v


# ── coverage ─────────────────────────────────────────────────────────────────

def assistant_coverage(assistant: Optional[str]) -> str:
    """COVERAGE_MEASURED when this assistant's spend can be observed at all,
    COVERAGE_NOT_INSTRUMENTED otherwise. Callers narrow a measured assistant to
    COVERAGE_NO_SESSION when the specific record lacks a session id to read."""
    return (COVERAGE_MEASURED if (assistant or '') in INSTRUMENTED_ASSISTANTS
            else COVERAGE_NOT_INSTRUMENTED)


def is_instrumented(assistant: Optional[str]) -> bool:
    return (assistant or '') in INSTRUMENTED_ASSISTANTS


# ── usage dicts ──────────────────────────────────────────────────────────────

def zero_classes() -> Dict[str, int]:
    return {c: 0 for c in CLASSES}


def empty_usage(source: str = '', coverage: str = '') -> Dict[str, Any]:
    """A fresh v2 usage dict.

    `records` counts the usage figures folded in — API responses on the
    transcript path, turns on the stream path (the stream reports a turn total,
    not per-message). `source` disambiguates which.
    """
    u: Dict[str, Any] = {'schema': SCHEMA_VERSION}
    u.update(zero_classes())
    u['records'] = 0
    u['by_model'] = {}
    if source:
        u['source'] = source
    if coverage:
        u['coverage'] = coverage
    return u


def classes_total(u: Optional[Dict[str, Any]]) -> int:
    """Every token in a usage dict, priceable classes plus the un-splittable v1
    residue. This is the *count* — the figure the pre-#574 `tokens.total` meant —
    so it stays comparable across the migration."""
    if not isinstance(u, dict):
        return 0
    return priceable_total(u) + _int(u.get('legacy_input_combined'))


def priceable_total(u: Optional[Dict[str, Any]]) -> int:
    """Tokens whose class is known, and which Phase 2 can therefore price."""
    if not isinstance(u, dict):
        return 0
    return sum(_int(u.get(c)) for c in CLASSES)


def migrate(u: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Bring a persisted usage dict up to SCHEMA_VERSION.

    v1 → v2 is deliberately lossy-but-honest: v1's `input` was
    fresh+cache_read+cache_write collapsed, and that split cannot be recovered.
    Rather than silently re-labelling it as fresh input (which would overstate
    cost by ~10x once priced), it moves to `legacy_input_combined` — still
    counted by `classes_total`, so no total jumps, but never mistaken for a
    priceable class. v1's `output` meant exactly what v2's does, so it carries
    over unchanged.
    """
    if not isinstance(u, dict):
        return empty_usage()
    if _int(u.get('schema')) >= SCHEMA_VERSION:
        out = empty_usage()
        out.update({k: v for k, v in u.items()})
        for c in CLASSES:
            out[c] = _int(u.get(c))
        out['records'] = _int(u.get('records'))
        out['legacy_input_combined'] = _int(u.get('legacy_input_combined'))
        out['by_model'] = {
            str(m): _sanitize_model_entry(e)
            for m, e in (u.get('by_model') or {}).items()
            if isinstance(e, dict)
        } if isinstance(u.get('by_model'), dict) else {}
        out['schema'] = SCHEMA_VERSION
        return out
    out = empty_usage(source=str(u.get('source') or ''),
                      coverage=str(u.get('coverage') or ''))
    out['output'] = _int(u.get('output'))
    out['legacy_input_combined'] = _int(u.get('input'))
    out['records'] = _int(u.get('turns') or u.get('records'))
    out['migrated_from_schema'] = 1
    return out


def _sanitize_model_entry(e: Dict[str, Any]) -> Dict[str, int]:
    out = {c: _int(e.get(c)) for c in CLASSES}
    out['records'] = _int(e.get('records'))
    return out


def add_usage(dst: Dict[str, Any], src: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Accumulate `src` into `dst` in place (classes, records, by_model)."""
    if not isinstance(dst, dict):
        dst = empty_usage()
    if not isinstance(src, dict):
        return dst
    dst.setdefault('schema', SCHEMA_VERSION)
    for c in CLASSES:
        dst[c] = _int(dst.get(c)) + _int(src.get(c))
    dst['records'] = _int(dst.get('records')) + _int(src.get('records'))
    if _int(src.get('legacy_input_combined')):
        dst['legacy_input_combined'] = (_int(dst.get('legacy_input_combined'))
                                       + _int(src.get('legacy_input_combined')))
    by = dst.setdefault('by_model', {})
    if not isinstance(by, dict):
        by = dst['by_model'] = {}
    for model, entry in (src.get('by_model') or {}).items():
        if not isinstance(entry, dict):
            continue
        tgt = by.setdefault(str(model), {**zero_classes(), 'records': 0})
        for c in CLASSES:
            tgt[c] = _int(tgt.get(c)) + _int(entry.get(c))
        tgt['records'] = _int(tgt.get('records')) + _int(entry.get('records'))
    return dst


def public_block(u: Optional[Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
    """An API-ready view of a ledger: the four priceable classes kept apart, the
    un-splittable v1 residue, both totals, and the per-model breakdown.

    `total` is every token counted (priceable + residue) so it stays comparable
    with the pre-#574 figure; `priceable_total` is the subset Phase 2 can put a
    number on. Extra keys (e.g. `sessions`, `tasks`) are merged in verbatim.
    """
    u = u if isinstance(u, dict) else {}
    out: Dict[str, Any] = {c: _int(u.get(c)) for c in CLASSES}
    out['legacy_input_combined'] = _int(u.get('legacy_input_combined'))
    out['priceable_total'] = priceable_total(u)
    out['total'] = classes_total(u)
    out['records'] = _int(u.get('records'))
    out['by_model'] = {
        str(m): _sanitize_model_entry(e)
        for m, e in (u.get('by_model') or {}).items() if isinstance(e, dict)
    }
    out.update(extra)
    return out


def coverage_summary(measured: int = 0, not_instrumented: int = 0,
                     no_session_id: int = 0, **extra: Any) -> Dict[str, Any]:
    """The honest-partial-coverage marker. Phase 2 renders this as
    "Claude measured · N assistants not instrumented" instead of a confidently
    wrong total; a 0 from an uninstrumented assistant is not a measurement."""
    out: Dict[str, Any] = {
        'measured_assistants': sorted(INSTRUMENTED_ASSISTANTS),
        'measured': measured,
        'not_instrumented': not_instrumented,
        'no_session_id': no_session_id,
    }
    out.update(extra)
    return out


def _warn(bucket: Dict[str, Any], code: str, detail: str = '') -> None:
    """Record a bounded, de-duplicated warning and mirror it to stderr."""
    msg = f'{code}: {detail}' if detail else code
    ws = bucket.setdefault('warnings', [])
    if not isinstance(ws, list):
        ws = bucket['warnings'] = []
    if msg in ws:
        return
    ws.append(msg)
    del ws[:-MAX_WARNINGS]
    print(f'[token-usage] {msg}', file=sys.stderr)


# ── the stream path (Hypervisor threads) ─────────────────────────────────────

#: `modelUsage` entry keys → our class names. Verified against a live
#: `claude -p --output-format stream-json` result event.
_MODEL_USAGE_KEYS = {
    'input': 'inputTokens',
    'cache_read': 'cacheReadInputTokens',
    'cache_write': 'cacheCreationInputTokens',
    'output': 'outputTokens',
}

#: Transcript / top-level `usage` keys → our class names.
_USAGE_KEYS = {
    'input': 'input_tokens',
    'cache_read': 'cache_read_input_tokens',
    'cache_write': 'cache_creation_input_tokens',
    'output': 'output_tokens',
}


def classes_from_usage(u: Any, keymap: Dict[str, str]) -> Dict[str, int]:
    if not isinstance(u, dict):
        return zero_classes()
    return {c: _int(u.get(k)) for c, k in keymap.items()}


def usage_from_stream_result(obj: Any,
                             fallback_model: str = '') -> Optional[Dict[str, Any]]:
    """A turn's usage from the Claude stream's terminal `result` event, or None
    when the event carries nothing usable.

    Prefers `modelUsage` — a per-model breakdown, which is both what pricing
    needs and *more complete* than the top-level `usage`: on a verified live run
    the top-level figure covered only the primary model and omitted a 521-token
    side-call to haiku that `modelUsage` listed. Falls back to the top-level
    `usage`, attributed to `fallback_model` (the last model seen on an assistant
    event) so the turn is still priceable.

    Deliberately ignores `costUSD` / `total_cost_usd`: pricing is Phase 2.
    """
    if not isinstance(obj, dict):
        return None
    out = empty_usage(source=SOURCE_STREAM, coverage=COVERAGE_MEASURED)
    mu = obj.get('modelUsage')
    counted = False
    if isinstance(mu, dict):
        for model, entry in mu.items():
            if not isinstance(entry, dict):
                continue
            cls = classes_from_usage(entry, _MODEL_USAGE_KEYS)
            if not any(cls.values()):
                continue
            name = _model_name(model) or fallback_model or ''
            add_usage(out, {**cls, 'records': 1,
                            'by_model': {name: {**cls, 'records': 1}}})
            counted = True
    if counted:
        # One turn folded in, however many models served it.
        out['records'] = 1
        return out
    cls = classes_from_usage(obj.get('usage'), _USAGE_KEYS)
    if not isinstance(obj.get('usage'), dict):
        return None
    name = _model_name(fallback_model)
    add_usage(out, {**cls, 'records': 1,
                    'by_model': {name: {**cls, 'records': 1}}})
    return out


# ── the transcript path (Builds) ─────────────────────────────────────────────

def _record_key(obj: Dict[str, Any], msg: Dict[str, Any], offset: int) -> str:
    """Billing identity of a transcript record.

    `message.id` is the API response id (`msg_…`): one API call, one id — and one
    call's usage is repeated on every JSONL line it produced, so this is the only
    key that counts it once. Falls back through `requestId` and the record
    `uuid`, then to the line's byte offset (a stable identity inside an
    append-only file) so a keyless record is still deduped.
    """
    for v in (msg.get('id'), obj.get('requestId'), obj.get('uuid')):
        if isinstance(v, str) and v.strip():
            return v.strip()
    return f'@{offset}'


def _scan_file(path: str, state: Dict[str, Any],
               bucket: Dict[str, Any]) -> Dict[str, Any]:
    """Recompute one transcript's cumulative usage, resuming where we left off.

    Returns the file's new state: `{'usage', 'offset', 'inode', 'size', 'keys'}`.
    `usage` is that file's total (not a delta), so the caller's totals are a pure
    function of the files — a rescan from scratch converges instead of doubling.

    Resume rules:
    * Same inode and the file only grew → resume from `offset` (the end of the
      last COMPLETE line read) and skip records whose key is in the retained
      ring. One API response's lines are written contiguously (verified across
      every transcript on this pod: zero interleaved groups), so a group whose
      lines straddle two polls is always within the last few keys and is
      recognised rather than counted twice.
    * Inode changed, or the file shrank → it is a different file (or was
      rewritten). Recount from 0 and discard the stale figure rather than trust
      an offset into unknown bytes.
    """
    prev = state if isinstance(state, dict) else {}
    try:
        st = os.stat(path)
    except OSError as e:
        _warn(bucket, 'transcript_unreadable', f'{os.path.basename(path)}: {e}')
        return {'usage': migrate(prev.get('usage')) if prev.get('usage') else empty_usage(
            source=SOURCE_TRANSCRIPT), 'offset': _int(prev.get('offset')),
            'inode': _int(prev.get('inode')), 'size': _int(prev.get('size')),
            'keys': list(prev.get('keys') or [])}

    inode, size = _int(st.st_ino), _int(st.st_size)
    resume = (_int(prev.get('inode')) == inode
              and size >= _int(prev.get('size'))
              and _int(prev.get('offset')) <= size)
    if resume:
        usage = migrate(prev.get('usage'))
        usage.setdefault('source', SOURCE_TRANSCRIPT)
        offset = _int(prev.get('offset'))
        keys = [k for k in (prev.get('keys') or []) if isinstance(k, str)]
    else:
        if prev:
            _warn(bucket, 'transcript_rewritten', os.path.basename(path))
        usage = empty_usage(source=SOURCE_TRANSCRIPT)
        offset, keys = 0, []

    seen = set(keys)
    # Key of the message group being read. Carried over from the ring so the
    # continuation of a group that straddled the previous poll is recognised.
    group_key = keys[-1] if keys else None
    consumed = offset  # bytes up to and including the last COMPLETE line
    try:
        f = open(path, 'rb')
    except OSError as e:
        _warn(bucket, 'transcript_unreadable', f'{os.path.basename(path)}: {e}')
        return {'usage': usage, 'offset': offset, 'inode': inode, 'size': size,
                'keys': keys}
    with f:
        try:
            f.seek(offset)
        except OSError:
            f.seek(0)
            offset = consumed = 0
        while True:
            line_at = f.tell()
            raw = f.readline()
            if not raw:
                break
            if not raw.endswith(b'\n'):
                # A half-written trailing line: leave it unconsumed so the next
                # poll reads it whole instead of discarding it as malformed.
                break
            consumed = f.tell()
            s = raw.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except (json.JSONDecodeError, UnicodeDecodeError):
                _warn(bucket, 'transcript_bad_line', os.path.basename(path))
                continue
            if not isinstance(obj, dict):
                _warn(bucket, 'transcript_bad_line', os.path.basename(path))
                continue
            msg = obj.get('message')
            if not isinstance(msg, dict):
                continue
            cls = classes_from_usage(msg.get('usage'), _USAGE_KEYS)
            if not isinstance(msg.get('usage'), dict) or not any(cls.values()):
                # No usage, or an all-zero record (Claude Code's `<synthetic>`
                # API-error notices). Nothing to count and nothing to dedupe.
                continue
            key = _record_key(obj, msg, line_at)
            # `seen` below is the authority on whether a record has been counted.
            # This group check and the `group_key` carry-over above are fast
            # paths to the same decision (a group's first line is always added to
            # `seen`, and the carried-over key is always in it), so mutating
            # either alone provably cannot change the output — verified by
            # pairwise mutation testing. Keep them, but don't mistake them for
            # the dedupe: that is `seen`, and the resume offset is what keeps
            # correctness from depending on the ring's size.
            if key == group_key:
                continue  # same API response, another content-block line
            group_key = key
            if key in seen:
                continue
            seen.add(key)
            keys.append(key)
            del keys[:-_KEY_RING]
            model = _model_name(msg.get('model'))
            add_usage(usage, {**cls, 'records': 1,
                              'by_model': {model: {**cls, 'records': 1}}})
    # Resume at the end of the last complete line; a trailing partial line stays
    # unconsumed so the next poll reads it whole.
    return {'usage': usage, 'offset': consumed,
            'inode': inode, 'size': size, 'keys': keys}


def subagent_transcripts(project_dir: str, session_id: str) -> List[str]:
    """A session's subagent transcripts (the main one comes from the caller's
    existing `locate_claude_session_log` resolver).

    Claude Code writes `<project>/<session>.jsonl` for the main thread and
    `<project>/<session>/subagents/agent-*.jsonl` for each subagent it spawns.
    Subagent spend is real spend and appears ONLY in those side files — the main
    transcript carries no sidechain usage lines at all (verified: 0 of 21,281
    usage-bearing top-level records were sidechain) — so a Build that fans out to
    subagents under-reports badly without them.
    """
    if not project_dir or not session_id:
        return []
    sub = os.path.join(project_dir, session_id, 'subagents')
    try:
        found = sorted(glob.glob(os.path.join(glob.escape(sub), '*.jsonl')))
    except (OSError, re.error):
        return []
    return [p for p in found if os.path.isfile(p)]


def ingest(paths: Iterable[str],
           ingest_state: Optional[Dict[str, Any]] = None,
           coverage: str = COVERAGE_MEASURED,
           ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Scan transcripts and return `(usage, ingest_state)`.

    `usage` is the ledger — the sum over files, recomputed every call, so calling
    `ingest` twice on an unchanged transcript returns the same totals. Pass the
    returned `ingest_state` back in next time to resume rather than re-read.

    Never raises. An unreadable file, a malformed line, a rewritten transcript
    and an empty path list all degrade to zero-with-a-warning.
    """
    prev = ingest_state if isinstance(ingest_state, dict) else {}
    prev_files = prev.get('files') if isinstance(prev.get('files'), dict) else {}
    total = empty_usage(source=SOURCE_TRANSCRIPT, coverage=coverage)
    files: Dict[str, Any] = {}
    paths = list(paths or [])
    if len(paths) > MAX_TRACKED_FILES:
        _warn(total, 'too_many_transcripts',
              f'{len(paths)} files, tracking first {MAX_TRACKED_FILES}')
        paths = paths[:MAX_TRACKED_FILES]
    for p in paths:
        try:
            fs = _scan_file(p, prev_files.get(p) or {}, total)
        except Exception as e:  # a reader bug must never cost the caller its data
            _warn(total, 'transcript_scan_failed', f'{type(e).__name__}: {e}')
            fs = prev_files.get(p) or {}
        files[p] = fs
        add_usage(total, fs.get('usage'))
    if not paths:
        _warn(total, 'transcript_absent')
    total['files'] = len(files)
    total['updated_at'] = int(time.time())
    return total, {'files': files, 'scanned_at': int(time.time())}
