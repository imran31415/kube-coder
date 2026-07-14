"""Normalized skill model + identity/dedupe logic.

A `SkillRecord` is the tool-agnostic shape every provider emits. Skill
identity across harnesses is `(name, fingerprint)` where the
fingerprint is a whitespace-normalized hash of the markdown body — pure
content math, no tool involved. Same name + same fingerprint in two
systems collapses to one record ("same skill, synced"); same name with
different fingerprints stays as separate records the UI badges as
divergent.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Scope precedence within a single system (lower index wins).
SCOPE_ORDER = ('project', 'user', 'plugin')

# Safe skill names: lowercase alnum + hyphens, must start alnum. This is
# the gate used before any filesystem path is built from a name (path
# traversal guard) — mirrors the posture proposed for the Facts feature.
SKILL_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9-]*$')


@dataclass
class SkillSource:
    """One concrete file a skill was discovered in."""
    system: str          # harness key, e.g. 'claude', 'opencode'
    path: str
    scope: str           # 'project' | 'user' | 'plugin'
    updated_at: float
    shadowed: bool = False  # true when a higher-precedence scope wins

    def to_dict(self) -> Dict[str, object]:
        return {
            'system': self.system,
            'path': self.path,
            'scope': self.scope,
            'updated_at': self.updated_at,
            'shadowed': self.shadowed,
        }


@dataclass
class SkillRecord:
    """Tool-agnostic normalized skill. Providers emit one per file with
    systems=[<their key>]; `merge()` collapses across systems."""
    name: str
    description: str = ''
    body: str = ''
    scope: str = 'user'
    systems: List[str] = field(default_factory=list)
    user_invocable: bool = False
    allowed_tools: List[str] = field(default_factory=list)
    argument_hint: str = ''
    sources: List[SkillSource] = field(default_factory=list)
    fingerprint: str = ''
    updated_at: float = 0.0

    def to_dict(self) -> Dict[str, object]:
        return {
            'name': self.name,
            'description': self.description,
            'body': self.body,
            'scope': self.scope,
            'systems': sorted(self.systems),
            'user_invocable': self.user_invocable,
            'allowed_tools': self.allowed_tools,
            'argument_hint': self.argument_hint,
            'sources': [s.to_dict() for s in self.sources],
            'fingerprint': self.fingerprint,
            'updated_at': self.updated_at,
        }


def fingerprint(body: str) -> str:
    """Content hash of the markdown body (frontmatter already stripped).

    Normalization: per-line trailing whitespace trimmed, leading/trailing
    blank lines dropped — so a byte-identical sync round-trip and trivial
    whitespace drift still match, while real edits do not.
    """
    lines = [ln.rstrip() for ln in (body or '').splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    normalized = '\n'.join(lines)
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]


def render_skill_md(record: 'SkillRecord') -> str:
    """Serialize a record back into canonical SKILL.md text.

    This is the interchange format every Claude-compatible harness reads
    (Claude Code, OpenCode, Ante). The body is written VERBATIM so the
    installed copy hashes to the same `fingerprint()` as the source and
    therefore collapses into one row on the next scan. Only known
    frontmatter keys are emitted; optional keys are omitted when empty so
    a re-scan parses back to an equivalent record (absent user-invocable
    parses as False, empty allowed-tools as []).
    """
    lines = ['---', f'name: {record.name}']
    desc = (record.description or '').strip()
    if desc:
        lines.append(f'description: {desc}')
    if record.user_invocable:
        lines.append('user-invocable: true')
    if record.allowed_tools:
        lines.append('allowed-tools: ' + ', '.join(record.allowed_tools))
    hint = (record.argument_hint or '').strip()
    if hint:
        lines.append(f'argument-hint: {hint}')
    lines.append('---')
    body = (record.body or '').strip('\n')
    text = '\n'.join(lines) + '\n'
    if body:
        text += '\n' + body + '\n'
    return text


def _scope_rank(scope: str) -> int:
    try:
        return SCOPE_ORDER.index(scope)
    except ValueError:
        return len(SCOPE_ORDER)


def merge(records: List[SkillRecord]) -> List[SkillRecord]:
    """Collapse per-file provider records into logical skills.

    1. Within one system, same name at multiple scopes: highest-precedence
       scope (project > user > plugin) wins; shadowed copies are kept in
       `sources` with shadowed=True but don't contribute content.
    2. Across systems, group key = (name, fingerprint): identical content
       under the same name merges into one record whose `systems` is the
       union. Different content under the same name stays separate
       (divergent variants, surfaced to the UI as such).
    """
    # Pass 1: within-system shadowing. Key: (system, name).
    winners: Dict[tuple, SkillRecord] = {}
    shadowed_sources: Dict[tuple, List[SkillSource]] = {}
    for rec in records:
        if not rec.systems or not rec.sources:
            continue
        system = rec.systems[0]
        k = (system, rec.name)
        cur = winners.get(k)
        if cur is None:
            winners[k] = rec
            continue
        if _scope_rank(rec.scope) < _scope_rank(cur.scope):
            # rec wins; demote current winner's sources to shadowed.
            for s in cur.sources:
                s.shadowed = True
            shadowed_sources.setdefault(k, []).extend(cur.sources)
            winners[k] = rec
        else:
            for s in rec.sources:
                s.shadowed = True
            shadowed_sources.setdefault(k, []).extend(rec.sources)

    # Pass 2: cross-system grouping by (name, fingerprint).
    grouped: Dict[tuple, SkillRecord] = {}
    for (system, name), rec in winners.items():
        fp = rec.fingerprint or fingerprint(rec.body)
        rec.fingerprint = fp
        gk = (name, fp)
        extra = shadowed_sources.get((system, name), [])
        tgt = grouped.get(gk)
        if tgt is None:
            rec.sources = list(rec.sources) + extra
            rec.systems = [system]
            grouped[gk] = rec
        else:
            if system not in tgt.systems:
                tgt.systems.append(system)
            tgt.sources.extend(rec.sources)
            tgt.sources.extend(extra)
            tgt.updated_at = max(tgt.updated_at, rec.updated_at)
            # Prefer the richest description/metadata already present;
            # fill blanks from the newcomer.
            if not tgt.description:
                tgt.description = rec.description
            if not tgt.argument_hint:
                tgt.argument_hint = rec.argument_hint
            if not tgt.allowed_tools:
                tgt.allowed_tools = rec.allowed_tools

    out = list(grouped.values())
    out.sort(key=lambda r: (r.name, r.fingerprint))
    return out


def find(records: List[SkillRecord], name: str) -> List[SkillRecord]:
    """All variants of a logical skill name (1 normally; 2+ if divergent)."""
    return [r for r in records if r.name == name]
