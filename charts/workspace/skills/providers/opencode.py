"""OpenCode skill provider.

OpenCode (opencode-ai) advertises compatibility with Claude-style skill
folders; it also has an older "custom commands" convention of flat
markdown files. We scan, in order:

    user:     ~/.config/opencode/skills/<name>/SKILL.md   (Claude-compatible)
    project:  <checkout>/.opencode/skills/<name>/SKILL.md
    user:     ~/.config/opencode/command/*.md             (legacy commands:
              file stem → name, frontmatter `description` honored)

All path knowledge lives in this one file — if a runtime `ls` of the
pod shows OpenCode uses a different layout, this is the single
correction point.
"""

from __future__ import annotations

import os
from typing import Iterable, List, Tuple

from . import SkillProvider
from ..model import SkillRecord, SkillSource, SKILL_NAME_RE, fingerprint
from ..parser import parse_frontmatter, parse_bool, parse_tool_list

HOME_CANDIDATES = (
    '/home/dev',
    '/home/ubuntu',
    os.path.expanduser('~'),
)

PROJECT_ROOT_ENV = 'KC_SKILLS_PROJECT_ROOTS'
DEFAULT_PROJECT_ROOTS = ('/home/dev',)


class OpenCodeProvider(SkillProvider):
    key = 'opencode'

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
            add('user', os.path.join(home, '.config', 'opencode', 'skills'))
        for base in self._project_roots():
            if not os.path.isdir(base):
                continue
            add('project', os.path.join(base, '.opencode', 'skills'))
            try:
                for entry in sorted(os.listdir(base)):
                    add('project', os.path.join(base, entry, '.opencode', 'skills'))
            except OSError:
                continue
        return roots

    def scan(self) -> List[SkillRecord]:
        # Claude-compatible skills dirs first (base implementation)…
        records = super().scan()
        # …then legacy flat command files.
        seen_names = {r.name for r in records}
        for home in self._homes():
            cmd_dir = os.path.join(home, '.config', 'opencode', 'command')
            if not os.path.isdir(cmd_dir):
                continue
            try:
                entries = sorted(os.listdir(cmd_dir))
            except OSError:
                continue
            for entry in entries:
                if not entry.endswith('.md'):
                    continue
                rec = self._load_command(os.path.join(cmd_dir, entry))
                if rec is not None and rec.name not in seen_names:
                    seen_names.add(rec.name)
                    records.append(rec)
        return records

    def _load_command(self, path: str):
        """Legacy command file: <stem>.md with optional frontmatter."""
        try:
            st = os.stat(path)
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                raw = f.read()
        except OSError:
            return None
        meta, body = parse_frontmatter(raw)
        name = (meta.get('name') or
                os.path.splitext(os.path.basename(path))[0]).strip().lower()
        if not SKILL_NAME_RE.match(name):
            return None
        return SkillRecord(
            name=name,
            description=(meta.get('description') or '').strip(),
            body=body,
            scope='user',
            systems=[self.key],
            user_invocable=parse_bool(meta.get('user-invocable', 'true')),
            allowed_tools=parse_tool_list(meta.get('allowed-tools', '')),
            argument_hint=(meta.get('argument-hint') or '').strip(),
            sources=[SkillSource(system=self.key, path=path, scope='user',
                                 updated_at=st.st_mtime)],
            fingerprint=fingerprint(body),
            updated_at=st.st_mtime,
        )

    def roots_mtime_fingerprint(self):
        fp = super().roots_mtime_fingerprint()
        # Include legacy command files so edits there trigger a rescan.
        for home in self._homes():
            cmd_dir = os.path.join(home, '.config', 'opencode', 'command')
            if not os.path.isdir(cmd_dir):
                continue
            try:
                entries = os.listdir(cmd_dir)
            except OSError:
                continue
            for entry in entries:
                if not entry.endswith('.md'):
                    continue
                p = os.path.join(cmd_dir, entry)
                try:
                    fp[os.path.realpath(p)] = os.stat(p).st_mtime
                except OSError:
                    continue
        return fp

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
