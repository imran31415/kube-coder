"""Write-time summarization for memory values (#359).

WHY THIS EXISTS
    Injection used to hard-cut every entry at 280 characters, mid-sentence,
    mid-word — and drop entries entirely once the block budget ran out. That is
    truncation, not shortening: the reader gets a fragment that may end in the
    middle of a clause and carries no signal about what was removed.

    Instead we compute a short, self-contained summary once at WRITE time and
    inject that. The original `value` is never modified — recall, search, the
    UI and export all keep returning the user's full text (see
    _migration_003). Summarizing on write also means the cost is paid once per
    write rather than on every prompt.

WHY EXTRACTIVE BY DEFAULT
    The only provider-shaped dependency in this subsystem (embeddings) defaults
    to `none`, so an LLM-only summarizer would be a no-op on most deployments —
    and a write that can fail because a remote model is down is a bad trade for
    a *memory* store. The default is therefore deterministic, offline, and
    allocation-light: it never fails, never blocks, and needs no credential.

    `set_summarizer()` leaves a seam for an LLM-backed implementation; callers
    that install one are responsible for its latency/failure behaviour (upsert
    already treats summarization as best-effort and falls back to no summary).
"""

from __future__ import annotations

import re
from typing import Callable, Optional

# Values at or under this length are already short enough to inject verbatim —
# summarizing them would only add noise. Comfortably above the injection
# per-entry budget so a "short" memory never needs trimming downstream.
SUMMARIZE_THRESHOLD = 240

# Target length for a generated summary. Kept under the injection per-entry
# budget so a summary never itself needs trimming.
DEFAULT_TARGET = 220

# Sentence boundary: ., ! or ? followed by whitespace. Deliberately simple —
# this runs on every write, and over-splitting on "e.g." merely yields a
# slightly shorter first sentence, never a crash or data loss.
_SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+')
_WS = re.compile(r'\s+')


def _normalize(text: str) -> str:
    """Collapse whitespace/newlines — injection renders one entry per line."""
    return _WS.sub(' ', (text or '')).strip()


def truncate_on_boundary(text: str, limit: int) -> str:
    """Shorten `text` to at most `limit` chars, breaking on a word boundary and
    marking the elision with '…'.

    This is the graceful floor used when even a summary must be shortened. It
    never splits a word in half, which is exactly what the old fixed 280-char
    slice did.
    """
    text = _normalize(text)
    if limit <= 0:
        return ''
    if len(text) <= limit:
        return text
    # Reserve one char for the ellipsis.
    cut = text[:max(0, limit - 1)]
    space = cut.rfind(' ')
    # Only honour the word boundary if it doesn't gut the string (a single very
    # long token has no usable boundary).
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(' ,;:.') + '…'


def extractive_summary(value: str, target: int = DEFAULT_TARGET) -> str:
    """Condense `value` to ~`target` chars by keeping whole leading sentences.

    Memories are written lead-first ("X is Y because Z"), so the opening
    sentences carry the fact and later ones carry elaboration. We accumulate
    whole sentences while they fit, which yields a summary that always ends on
    a real sentence boundary. If even the first sentence overflows, fall back to
    a word-boundary trim so the result is still clean.
    """
    text = _normalize(value)
    if not text:
        return ''
    if len(text) <= target:
        return text

    out = ''
    for sentence in _SENTENCE_SPLIT.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        candidate = f'{out} {sentence}'.strip() if out else sentence
        if len(candidate) > target:
            break
        out = candidate

    if not out:
        # First sentence alone overflows — degrade to a clean word-boundary cut.
        return truncate_on_boundary(text, target)
    # Signal that content was elided, so the reader knows to consult the full
    # entry (via memory_recall) rather than assuming this is everything.
    return out + ' …'


# ── Pluggable seam ──────────────────────────────────────────────────────────
# A summarizer takes (value, target) and returns the summary text. Swap in an
# LLM-backed one via set_summarizer(); callers treat failures as "no summary".
Summarizer = Callable[[str, int], str]
_summarizer: Summarizer = extractive_summary


def set_summarizer(fn: Optional[Summarizer]) -> None:
    """Install a custom summarizer (None restores the extractive default)."""
    global _summarizer
    _summarizer = fn or extractive_summary


def summarize(value: str, target: int = DEFAULT_TARGET) -> Optional[str]:
    """The write-path entry point.

    Returns None when no summary is warranted (empty, already short, or the
    summarizer produced nothing useful) — the reader then falls back to `value`,
    so None is always a safe outcome. Never raises: a summarization failure must
    not fail a memory write.
    """
    text = _normalize(value)
    if not text or len(text) <= SUMMARIZE_THRESHOLD:
        return None
    try:
        summary = _normalize(_summarizer(text, target))
    except Exception:
        return None
    # A "summary" that didn't actually shorten anything is not worth storing.
    if not summary or len(summary) >= len(text):
        return None
    return summary
