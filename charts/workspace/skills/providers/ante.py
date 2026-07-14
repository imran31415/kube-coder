"""Ante (Antigma Labs) skill provider.

Ante's config home is `~/.ante/` — confirmed in-pod: start.sh seeds
`~/.ante/settings.json` with the shared MCP servers (see the "seeding
Ante MCP config" stage). We scan the Claude-compatible skills layout
under that home, plus a project-scoped `.ante/skills/` mirroring the
OpenCode provider's project convention:

    user:     ~/.ante/skills/<name>/SKILL.md
    project:  <checkout>/.ante/skills/<name>/SKILL.md

Enabled by default: `~/.ante/` exists on every pod, and scanning a
skills dir that isn't there yet is a no-op (missing roots are skipped).
Set KC_SKILLS_ANTE=0 to turn the provider off. If a runtime `ls
~/.ante` shows Ante adopts a different skills layout, `scan_roots()`
below is the single correction point — nothing else changes.
"""

from __future__ import annotations

import os
from typing import Iterable, List, Tuple

from . import SkillProvider

HOME_CANDIDATES = (
    '/home/dev',
    '/home/ubuntu',
    os.path.expanduser('~'),
)

PROJECT_ROOT_ENV = 'KC_SKILLS_PROJECT_ROOTS'
DEFAULT_PROJECT_ROOTS = ('/home/dev',)


class AnteProvider(SkillProvider):
    # Matches the 'ante' key in ClaudeTaskManager.ASSISTANTS so the
    # dashboard can correlate skills with the runnable assistant.
    key = 'ante'
    enabled = os.environ.get('KC_SKILLS_ANTE', '1') != '0'

    def scan_roots(self) -> List[Tuple[str, str]]:
        roots: List[Tuple[str, str]] = []
        seen = set()

        def add(scope: str, path: str):
            rp = os.path.realpath(path)
            if rp in seen:
                return
            seen.add(rp)
            roots.append((scope, path))

        for home in self._homes():
            add('user', os.path.join(home, '.ante', 'skills'))
        for base in self._project_roots():
            if not os.path.isdir(base):
                continue
            add('project', os.path.join(base, '.ante', 'skills'))
            try:
                for entry in sorted(os.listdir(base)):
                    add('project', os.path.join(base, entry, '.ante', 'skills'))
            except OSError:
                continue
        return roots

    @staticmethod
    def _homes() -> Iterable[str]:
        out, seen = [], set()
        for h in HOME_CANDIDATES:
            rp = os.path.realpath(h)
            if rp not in seen:
                seen.add(rp)
                out.append(h)
        return out

    @staticmethod
    def _project_roots() -> Iterable[str]:
        env = os.environ.get(PROJECT_ROOT_ENV, '')
        if env:
            return [p for p in env.split(os.pathsep) if p]
        return DEFAULT_PROJECT_ROOTS

    # ── write path ──────────────────────────────────────────────────────

    def _install_dir(self, scope):
        homes = list(self._homes())
        if scope == 'user':
            home = next((h for h in homes if os.path.isdir(h)), homes[-1])
            return os.path.join(home, '.ante', 'skills')
        if scope == 'project':
            roots = list(self._project_roots())
            if not roots:
                return None
            base = next((r for r in roots if os.path.isdir(r)), roots[0])
            return os.path.join(base, '.ante', 'skills')
        return None
