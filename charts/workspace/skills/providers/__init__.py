"""Skill providers — one class per AI harness.

A provider encapsulates EVERYTHING harness-specific: where skills live
on disk (`scan_roots`), how the native file format maps to the
normalized `SkillRecord` (`scan`), and — in the sync phase — how a
canonical record is rendered back into the harness's native format and
installed (`render`/`install`).

The registry below is the single extension point: adding support for a
new harness means writing one provider file and adding one entry here.
Provider keys match the assistant registry in server.py
(ClaudeTaskManager.ASSISTANTS) so the dashboard can correlate skills
with runnable assistants.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from typing import Dict, Iterable, List, Optional, Tuple

from ..model import (
    SkillRecord, SkillSource, SKILL_NAME_RE, fingerprint, render_skill_md,
)
from ..parser import parse_frontmatter, parse_bool, parse_tool_list


class SkillProvider:
    """Base class. Subclasses set `key` and implement `scan_roots()`.

    The default `scan()` handles the common "directory of
    <name>/SKILL.md folders" layout shared by Claude-compatible
    harnesses; providers with other native formats override `scan()`.
    """

    key: str = ''
    enabled: bool = True

    # ── read path ───────────────────────────────────────────────────────

    def scan_roots(self) -> List[Tuple[str, str]]:
        """[(scope, directory)] — every location this harness reads
        skills from. Scope is 'project' | 'user' | 'plugin'."""
        raise NotImplementedError

    def scan(self) -> List[SkillRecord]:
        """Walk roots and return per-file records (systems=[self.key]).
        Never raises: unreadable/malformed files are logged and skipped."""
        if not self.enabled:
            return []
        records: List[SkillRecord] = []
        seen_paths = set()
        for scope, root in self.scan_roots():
            if not root or not os.path.isdir(root):
                continue
            for path in self._iter_skill_files(root):
                rp = os.path.realpath(path)
                if rp in seen_paths:
                    continue
                seen_paths.add(rp)
                rec = self._load_one(path, scope)
                if rec is not None:
                    records.append(rec)
        return records

    def _iter_skill_files(self, root: str) -> Iterable[str]:
        """Default layout: <root>/<name>/SKILL.md. Depth-limited, no
        recursion — mirrors the discipline of memory/sync.py's walk."""
        try:
            entries = os.listdir(root)
        except OSError:
            return
        for entry in sorted(entries):
            p = os.path.join(root, entry, 'SKILL.md')
            if os.path.isfile(p):
                yield p

    def _load_one(self, path: str, scope: str):
        try:
            st = os.stat(path)
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                raw = f.read()
        except OSError as e:
            print(f'[skills] {self.key}: skip {path}: {e}', file=sys.stderr)
            return None
        meta, body = parse_frontmatter(raw)
        # Name: frontmatter wins, else the containing folder / file stem.
        name = (meta.get('name') or '').strip()
        if not name:
            parent = os.path.basename(os.path.dirname(path))
            name = parent if parent else os.path.splitext(os.path.basename(path))[0]
        name = name.lower()
        if not SKILL_NAME_RE.match(name):
            print(f'[skills] {self.key}: skip {path}: unsafe name {name!r}',
                  file=sys.stderr)
            return None
        return SkillRecord(
            name=name,
            description=(meta.get('description') or '').strip(),
            body=body,
            scope=scope,
            systems=[self.key],
            user_invocable=parse_bool(meta.get('user-invocable', '')),
            allowed_tools=parse_tool_list(meta.get('allowed-tools', '')),
            argument_hint=(meta.get('argument-hint') or '').strip(),
            sources=[SkillSource(system=self.key, path=path, scope=scope,
                                 updated_at=st.st_mtime)],
            fingerprint=fingerprint(body),
            updated_at=st.st_mtime,
        )

    # ── fingerprint of roots (cheap change detection) ───────────────────

    def roots_mtime_fingerprint(self) -> Dict[str, float]:
        """path→mtime over every skill file. Cheap: stat only, no reads.
        The syncer compares consecutive fingerprints to skip parsing."""
        fp: Dict[str, float] = {}
        if not self.enabled:
            return fp
        for _scope, root in self.scan_roots():
            if not root or not os.path.isdir(root):
                continue
            for path in self._iter_skill_files(root):
                try:
                    fp[os.path.realpath(path)] = os.stat(path).st_mtime
                except OSError:
                    continue
        return fp

    # ── write path (sync engine, PR 2) ──────────────────────────────────
    #
    # A writable provider only needs to implement `_install_dir(scope)` —
    # the ONE canonical directory this harness reads a given scope's skills
    # from. `render()`/`install_path()`/`install()` are generic and shared:
    # all currently-supported harnesses use the Claude-compatible SKILL.md
    # layout. A provider whose native write format differs overrides
    # `render()` alone; nothing else changes.

    def _install_dir(self, scope: str) -> Optional[str]:
        """Canonical directory to WRITE `scope` skills into (not the full
        multi-home scan list — one destination). Return None for scopes
        this harness can't be written to (e.g. plugin). Writable providers
        override; the default is read-only (no destination)."""
        return None

    def install_path(self, name: str, scope: str = 'user') -> str:
        """Destination path a translated skill would be written to.
        Validates the name up front (path-traversal guard)."""
        if not SKILL_NAME_RE.match(name or ''):
            raise ValueError(f'unsafe skill name: {name!r}')
        base = self._install_dir(scope)
        if not base:
            raise ValueError(
                f'{self.key}: scope {scope!r} is not writable')
        return os.path.join(base, name, 'SKILL.md')

    def render(self, record: SkillRecord) -> str:
        """Canonical record → this harness's native file text. Default is
        the interchange SKILL.md; override only for a divergent format."""
        return render_skill_md(record)

    def writable(self) -> bool:
        return self.enabled and self._install_dir('user') is not None

    def install(self, record: SkillRecord, scope: str = 'user') -> str:
        """render() + atomic write into this harness's dir. Returns the
        written path. Guards: name regex, realpath containment, temp-file
        + os.replace so a crash never leaves a half-written skill."""
        if not self.enabled:
            raise ValueError(f'{self.key}: provider is disabled')
        name = (record.name or '')
        if not SKILL_NAME_RE.match(name):
            raise ValueError(f'unsafe skill name: {name!r}')
        base = self._install_dir(scope)
        if not base:
            raise ValueError(f'{self.key}: scope {scope!r} is not writable')
        dest = os.path.join(base, name, 'SKILL.md')
        # Defense in depth: the resolved destination must stay inside the
        # resolved install dir even though `name` is already regex-clean.
        real_base = os.path.realpath(base)
        real_dest = os.path.realpath(dest)
        if real_dest != os.path.join(real_base, name, 'SKILL.md') and \
                not real_dest.startswith(real_base + os.sep):
            raise ValueError('destination escapes install root')
        text = self.render(record)
        dest_dir = os.path.dirname(dest)
        os.makedirs(dest_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest_dir, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(text)
            os.replace(tmp, dest)  # atomic on same filesystem
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return dest


def _now() -> float:
    return time.time()


# Registry — populated at import bottom to avoid circular imports.
from .claude import ClaudeProvider          # noqa: E402
from .opencode import OpenCodeProvider      # noqa: E402
from .ante import AnteProvider              # noqa: E402
from .antigravity import AntigravityProvider  # noqa: E402

PROVIDERS: Dict[str, SkillProvider] = {
    p.key: p for p in (ClaudeProvider(), OpenCodeProvider(), AnteProvider(),
                       AntigravityProvider())
}
