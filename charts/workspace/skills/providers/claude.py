"""Claude Code skill provider.

Native layout (also the interchange format other providers translate
to/from — an open markdown + frontmatter convention, no Claude software
required to read or write it):

    project:  <workspace>/.claude/skills/<name>/SKILL.md
    user:     <home>/.claude/skills/<name>/SKILL.md
    plugin:   <home>/.claude/plugins/marketplaces/*/plugins/*/skills/<name>/SKILL.md

Multi-home scanning matches memory/sync.py's DEFAULT_SCAN_ROOTS: the
pod runs services under varying users, so ~/.claude may live in
/home/dev, /home/ubuntu, or the current $HOME.
"""

from __future__ import annotations

import glob
import os
from typing import Iterable, List, Tuple

from . import SkillProvider

# Same multi-home trio as memory/sync.py DEFAULT_SCAN_ROOTS.
HOME_CLAUDE_ROOTS = (
    '/home/dev/.claude',
    '/home/ubuntu/.claude',
    os.path.expanduser('~/.claude'),
)

# Candidate workspace checkouts for project-scoped skills. The primary
# repo checkout lives under /home/dev in the pod; env override for tests
# and non-standard layouts.
PROJECT_ROOT_ENV = 'KC_SKILLS_PROJECT_ROOTS'
DEFAULT_PROJECT_ROOTS = ('/home/dev',)


class ClaudeProvider(SkillProvider):
    key = 'claude'

    def scan_roots(self) -> List[Tuple[str, str]]:
        roots: List[Tuple[str, str]] = []
        seen = set()

        def add(scope: str, path: str):
            rp = os.path.realpath(path)
            if rp in seen:
                return
            seen.add(rp)
            roots.append((scope, path))

        # Project scope: <checkout>/.claude/skills for every repo directly
        # under each project root (depth-limited: no full tree walk).
        for base in self._project_roots():
            if not os.path.isdir(base):
                continue
            add('project', os.path.join(base, '.claude', 'skills'))
            try:
                for entry in sorted(os.listdir(base)):
                    add('project', os.path.join(base, entry, '.claude', 'skills'))
            except OSError:
                continue

        # User scope.
        for home in HOME_CLAUDE_ROOTS:
            add('user', os.path.join(home, 'skills'))

        # Plugin scope: marketplaces glob (bounded depth).
        for home in HOME_CLAUDE_ROOTS:
            pattern = os.path.join(home, 'plugins', 'marketplaces', '*',
                                   'plugins', '*', 'skills')
            for d in sorted(glob.glob(pattern)):
                add('plugin', d)

        return roots

    @staticmethod
    def _project_roots() -> Iterable[str]:
        env = os.environ.get(PROJECT_ROOT_ENV, '')
        if env:
            return [p for p in env.split(os.pathsep) if p]
        return DEFAULT_PROJECT_ROOTS

    # ── write path ──────────────────────────────────────────────────────

    def _install_dir(self, scope):
        """Canonical dir to write a `scope` skill into. Plugin skills are
        marketplace-managed, so they are not a writable target."""
        if scope == 'user':
            homes = list(HOME_CLAUDE_ROOTS)
            home = next((h for h in homes if os.path.isdir(h)), homes[-1])
            return os.path.join(home, 'skills')
        if scope == 'project':
            roots = list(self._project_roots())
            if not roots:
                return None
            base = next((r for r in roots if os.path.isdir(r)), roots[0])
            return os.path.join(base, '.claude', 'skills')
        return None
