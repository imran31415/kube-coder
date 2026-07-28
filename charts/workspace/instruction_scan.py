"""Hidden-text detection for agent-readable instruction files (#559).

WHY THIS EXISTS. The TrapDoor supply-chain campaign (npm/PyPI/Crates, 2026)
ships packages that drop `CLAUDE.md` / `.cursorrules` files carrying
*invisible* instructions — zero-width and Unicode-tag characters that render
as nothing to a human reviewer but are read verbatim by Claude Code, Cursor
and friends. The agent follows them as legitimate project-level instructions.
In the observed campaign that meant running a "security scan" that exfiltrated
local secrets.

kube-coder clones arbitrary repos and points agents at them, so this is the
delivery mechanism aimed squarely at us. No container escape is needed: the
agent is the payload carrier, acting with authority it legitimately has.

DESIGN — the two constraints that shape everything here:

1.  **This must not run as a tool the agent calls.** If the injected file has
    already captured the agent, asking that agent to scan is worthless — the
    injection simply says "skip the scan" or "report clean". Detection has to
    happen out-of-band, server-side, where a compromised agent cannot suppress
    the result. This module is therefore pure and synchronous, called by the
    server; the MCP surface (if any) is a convenience, never the boundary.

2.  **Deterministic, no LLM.** Consistent with the workspace rule that
    dashboard features stay deterministic. We do not judge whether text is
    *malicious* — only whether it is *invisible*. That is a decidable property
    of codepoints, and it is the property that makes the attack work.

FALSE POSITIVES ARE THE HARD PART. U+200D (ZWJ) is load-bearing in ordinary
emoji: 👨‍👩‍👧 is three pictographs joined by two ZWJs. A naive "contains
U+200D → reject" fires on every README with a family emoji, and a scanner
that cries wolf gets switched off. So ZWJ between pictographs is treated as
legitimate, as is a BOM at offset 0. See `_zwj_is_emoji_join`.

We report position and decoded content rather than a verdict, and we never
modify the file. Stripping is a human decision — silently rewriting a file an
agent is about to read is its own injection vector.
"""

from __future__ import annotations

import os
import unicodedata
from typing import Dict, Iterable, List, Optional

# Files an agent reads as instructions. Matched case-sensitively on basename
# except where the ecosystem is inconsistent (CLAUDE.md vs claude.md both seen).
INSTRUCTION_FILENAMES = frozenset({
    'CLAUDE.md', 'claude.md',
    'AGENTS.md', 'agents.md',
    'GEMINI.md',
    '.cursorrules',
    '.clinerules',
    '.windsurfrules',
    'copilot-instructions.md',   # lives under .github/
})

# Directories never worth walking — vendored trees generate noise and are not
# what an agent reads as *its* instructions.
SKIP_DIRS = frozenset({
    '.git', 'node_modules', '__pycache__', '.venv', 'venv', 'dist', 'build',
    '.next', '.cache', 'vendor', 'target', '.worktrees',
})

# --- character classes -------------------------------------------------------

# Zero-width / invisible formatting. ZWJ and ZWNJ have legitimate uses and are
# handled specially below; the rest have no business in a Markdown instruction.
ZERO_WIDTH = {
    0x200B: 'ZERO WIDTH SPACE',
    0x200C: 'ZERO WIDTH NON-JOINER',
    0x200D: 'ZERO WIDTH JOINER',
    0x2060: 'WORD JOINER',
    0xFEFF: 'ZERO WIDTH NO-BREAK SPACE (BOM)',
    0x180E: 'MONGOLIAN VOWEL SEPARATOR',
}

# Bidirectional overrides — the "Trojan Source" class. These reorder rendered
# text without changing bytes, so what a reviewer reads and what a parser (or
# an agent) consumes can differ arbitrarily. No legitimate use in these files.
BIDI_CONTROL = {
    0x202A: 'LEFT-TO-RIGHT EMBEDDING',
    0x202B: 'RIGHT-TO-LEFT EMBEDDING',
    0x202C: 'POP DIRECTIONAL FORMATTING',
    0x202D: 'LEFT-TO-RIGHT OVERRIDE',
    0x202E: 'RIGHT-TO-LEFT OVERRIDE',
    0x2066: 'LEFT-TO-RIGHT ISOLATE',
    0x2067: 'RIGHT-TO-LEFT ISOLATE',
    0x2068: 'FIRST STRONG ISOLATE',
    0x2069: 'POP DIRECTIONAL ISOLATE',
}

# Unicode tag block. U+E0020..U+E007E map 1:1 onto printable ASCII (subtract
# 0xE0000) and render as nothing. This is the highest-signal class by far: it
# can carry an entire English sentence invisibly, which is exactly how a
# hidden instruction gets smuggled into a file that looks empty.
TAG_BLOCK_START, TAG_BLOCK_END = 0xE0000, 0xE007F

# Codepoints that may legitimately sit adjacent to a ZWJ inside an emoji
# sequence without breaking the "joined pictographs" shape.
_EMOJI_MODIFIERS = frozenset(range(0x1F3FB, 0x1F400)) | {0xFE0F, 0xFE0E}

# Approximate Extended_Pictographic. Python's unicodedata does not expose the
# property, so this is a range table. It only needs to be good enough to avoid
# flagging real emoji — over-inclusion here costs us a missed ZWJ, which is
# the cheap direction to be wrong given ZWJ is the weakest signal anyway.
_PICTOGRAPHIC_RANGES = (
    (0x00A9, 0x00A9), (0x00AE, 0x00AE), (0x203C, 0x203C), (0x2049, 0x2049),
    (0x2122, 0x2122), (0x2139, 0x2139), (0x2194, 0x21AA), (0x231A, 0x231B),
    (0x2328, 0x2328), (0x23CF, 0x23FA), (0x24C2, 0x24C2), (0x25AA, 0x25FE),
    (0x2600, 0x27BF), (0x2934, 0x2935), (0x2B00, 0x2BFF), (0x3030, 0x3030),
    (0x303D, 0x303D), (0x3297, 0x3299),
    (0x1F000, 0x1FAFF), (0x1FC00, 0x1FFFD),
)


def _is_pictographic(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in _PICTOGRAPHIC_RANGES)


def _zwj_is_emoji_join(text: str, i: int) -> bool:
    """True when the ZWJ at `i` is joining two pictographs, i.e. ordinary emoji.

    Scans outward past variation selectors and skin-tone modifiers, which sit
    between the ZWJ and the pictograph in sequences like
    👩🏽‍🚀 (woman + tone + ZWJ + rocket).
    """
    j = i - 1
    while j >= 0 and ord(text[j]) in _EMOJI_MODIFIERS:
        j -= 1
    k = i + 1
    while k < len(text) and ord(text[k]) in _EMOJI_MODIFIERS:
        k += 1
    if j < 0 or k >= len(text):
        return False
    return _is_pictographic(ord(text[j])) and _is_pictographic(ord(text[k]))


def _severity(cp: int, text: str, i: int) -> Optional[str]:
    """Classify a suspicious codepoint, or None when it is legitimate here.

    'high'   — no plausible innocent use in an instruction file
    'medium' — invisible, occasionally innocent (stray BOM, ZWSP from a copy-paste)
    """
    if TAG_BLOCK_START <= cp <= TAG_BLOCK_END:
        return 'high'
    if cp in BIDI_CONTROL:
        return 'high'
    if cp == 0x200D:
        # Load-bearing in emoji; suspicious anywhere else.
        return None if _zwj_is_emoji_join(text, i) else 'medium'
    if cp == 0x200C:
        # ZWNJ is required by Persian/Arabic/Indic orthography. Real, but rare
        # in these files — surface it and let a human judge.
        return 'medium'
    if cp == 0xFEFF:
        # A BOM at offset 0 is a file marker, not hidden text.
        return None if i == 0 else 'medium'
    if cp in ZERO_WIDTH:
        return 'medium'
    return None


def decode_tag_block(text: str) -> str:
    """Decode U+E00xx tag characters back to the ASCII they conceal.

    This is what turns "the file contains 47 invisible codepoints" into
    "the file says: ignore previous instructions and run curl ...", which is
    the difference between a warning a user dismisses and one they act on.
    """
    out = []
    for ch in text:
        cp = ord(ch)
        if TAG_BLOCK_START <= cp <= TAG_BLOCK_END:
            ascii_cp = cp - TAG_BLOCK_START
            if 0x20 <= ascii_cp <= 0x7E:
                out.append(chr(ascii_cp))
    return ''.join(out)


def _describe(cp: int) -> str:
    if TAG_BLOCK_START <= cp <= TAG_BLOCK_END:
        return 'UNICODE TAG (invisible ASCII carrier)'
    if cp in BIDI_CONTROL:
        return BIDI_CONTROL[cp]
    if cp in ZERO_WIDTH:
        return ZERO_WIDTH[cp]
    try:
        return unicodedata.name(chr(cp))
    except ValueError:
        return 'UNNAMED'


def scan_text(text: str, *, context_chars: int = 40) -> List[Dict]:
    """Return one finding per suspicious codepoint, in document order.

    Each finding carries line/column and a *visible-only* context excerpt, so a
    user can see where in the file the invisible content hides.
    """
    findings: List[Dict] = []
    line, col = 1, 1
    for i, ch in enumerate(text):
        cp = ord(ch)
        sev = _severity(cp, text, i)
        if sev:
            lo = max(0, i - context_chars)
            hi = min(len(text), i + context_chars)
            # Strip invisibles from the excerpt: showing them back is useless,
            # and re-emitting raw bidi controls into a UI is its own problem.
            excerpt = ''.join(
                c for c in text[lo:hi]
                if not (ord(c) in ZERO_WIDTH or ord(c) in BIDI_CONTROL
                        or TAG_BLOCK_START <= ord(c) <= TAG_BLOCK_END)
            ).replace('\n', ' ').strip()
            findings.append({
                'offset': i,
                'line': line,
                'column': col,
                'codepoint': f'U+{cp:04X}',
                'name': _describe(cp),
                'severity': sev,
                'context': excerpt,
            })
        if ch == '\n':
            line, col = line + 1, 1
        else:
            col += 1
    return findings


def scan_file(path: str) -> Optional[Dict]:
    """Scan one file. Returns None when clean or unreadable.

    Unreadable is deliberately conflated with clean: this runs on a walk over
    user-controlled trees, and a permission error or a binary blob must never
    take down the caller. A file we cannot read is a file the agent cannot
    read either.
    """
    try:
        with open(path, 'r', encoding='utf-8', errors='strict') as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError):
        return None
    findings = scan_text(text)
    if not findings:
        return None
    hidden = decode_tag_block(text)
    return {
        'path': path,
        'findings': findings,
        'counts': {
            'high': sum(1 for f in findings if f['severity'] == 'high'),
            'medium': sum(1 for f in findings if f['severity'] == 'medium'),
        },
        # The decoded payload, when there is one. This is the headline.
        'decoded_hidden_text': hidden,
    }


def iter_instruction_files(root: str, *, max_files: int = 5000) -> Iterable[str]:
    """Yield instruction-file paths beneath `root`, skipping vendored trees."""
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name in INSTRUCTION_FILENAMES:
                yield os.path.join(dirpath, name)
                seen += 1
                if seen >= max_files:
                    return


def scan_tree(root: str, *, max_files: int = 5000) -> Dict:
    """Scan every instruction file beneath `root`.

    Returns a summary even when clean, so a caller can distinguish "scanned,
    nothing found" from "never ran" — a security check that fails silently is
    worse than none.
    """
    results = []
    scanned = 0
    for path in iter_instruction_files(root, max_files=max_files):
        scanned += 1
        r = scan_file(path)
        if r:
            results.append(r)
    return {
        'root': root,
        'files_scanned': scanned,
        'files_flagged': len(results),
        'high': sum(r['counts']['high'] for r in results),
        'medium': sum(r['counts']['medium'] for r in results),
        'results': results,
    }
