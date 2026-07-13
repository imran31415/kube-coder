"""Multi-harness skills subsystem.

Discovers agent "skills" (markdown capability definitions) from every
supported AI harness in the workspace — Claude Code, OpenCode,
Antigravity, … — normalizes them into a single `SkillRecord` shape, and
keeps an in-memory cache fresh via a background mtime-fingerprint scan.

Design: each harness's paths/format live in exactly one provider class
(`providers/`). The rest of the stack (syncer, API handlers, SPA,
mobile) only ever sees normalized records. Adding a harness = one new
provider file + one registry entry. No harness is privileged: a skill
that exists only in OpenCode's folders is first-class, and (in the sync
phase) skills flow any-to-any.
"""

from .model import SkillRecord, merge, SKILL_NAME_RE
from .providers import PROVIDERS, SkillProvider
from .sync import SkillsSyncer

__all__ = [
    'SkillRecord', 'merge', 'SKILL_NAME_RE',
    'PROVIDERS', 'SkillProvider',
    'SkillsSyncer',
]
