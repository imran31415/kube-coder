"""Tolerant SKILL.md-style frontmatter parsing.

Adapted from memory/sync.py's dependency-free k:v parser. Skills use the
same convention: a `---` fenced YAML-ish header followed by a markdown
body. We deliberately stay dependency-free and lossy-tolerant on read:
unknown keys are ignored (kept in the returned meta dict for callers
that care), files without frontmatter are treated as all-body, and
malformed lines are skipped rather than raised on.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

_FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n?(.*)$', re.DOTALL)

# Truthy strings accepted for boolean frontmatter values.
_TRUTHY = frozenset({'true', 'yes', '1', 'on'})


def parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """Split `text` into (meta, body). No frontmatter → ({}, text)."""
    m = _FRONTMATTER_RE.match(text or '')
    if not m:
        return {}, text or ''
    head, body = m.group(1), m.group(2)
    meta: Dict[str, str] = {}
    for line in head.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if ':' not in line:
            continue
        k, v = line.split(':', 1)
        meta[k.strip().lower()] = v.strip().strip('"').strip("'")
    return meta, body.lstrip('\n')


def parse_bool(value: str) -> bool:
    return (value or '').strip().lower() in _TRUTHY


def parse_tool_list(value: str) -> List[str]:
    """'Bash, Read, Grep' → ['Bash', 'Read', 'Grep']. Empty → []."""
    if not value:
        return []
    return [t.strip() for t in value.split(',') if t.strip()]
