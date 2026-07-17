"""Custom slash-command discovery for the Hypervisor composer picker.

Claude Code *custom slash commands* live as flat markdown files under
`.claude/commands/` — distinct from skills, which live as
`.claude/skills/<name>/SKILL.md` folders. A file `deploy.md` resolves as
`/deploy`; a nested `git/commit.md` resolves as `/git:commit`. The skills
registry (issue #187) never scans this layout, so the Hypervisor picker
needs its own discovery pass.

We only extract what the picker lists — `name`, a one-line `description`
(frontmatter `description`, else the first prose line), and an
`argument_hint`. Actual *resolution* is Claude's job: the composer passes
`/<name> …` verbatim to `claude -p`, which expands it (confirmed in print
mode; `claude --help`: "Skills still resolve via /skill-name.").

Scanning mirrors `skills/providers/claude.py`: multi-home user scope plus a
project scope at `<checkout>/.claude/commands`, so it works whether the pod
runs services under /home/dev, /home/ubuntu, or the current $HOME.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Dict, List

from .parser import parse_frontmatter

# Same multi-home trio as skills/providers/claude.py HOME_CLAUDE_ROOTS and
# memory/sync.py DEFAULT_SCAN_ROOTS: ~/.claude may live under a different user.
HOME_CLAUDE_ROOTS = (
    '/home/dev/.claude',
    '/home/ubuntu/.claude',
    os.path.expanduser('~/.claude'),
)

# Project-scoped commands: <checkout>/.claude/commands. Env override for tests.
PROJECT_ROOT_ENV = 'KC_SKILLS_PROJECT_ROOTS'
DEFAULT_PROJECT_ROOTS = ('/home/dev',)

# Command names allow nested-dir namespacing (`git:commit`) — a superset of the
# skills name gate (which forbids ':'). Still lowercased + traversal-safe: a
# name is only ever displayed and passed to Claude as `/<name>`, never used to
# build a filesystem path, but we keep it strict anyway.
COMMAND_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9:_-]*$')

# Depth guard so a stray deep tree under .claude/commands can't turn discovery
# into an unbounded walk (mirrors the depth discipline elsewhere in skills/).
_MAX_DEPTH = 4


def _project_roots() -> List[str]:
    env = os.environ.get(PROJECT_ROOT_ENV, '')
    if env:
        return [p for p in env.split(os.pathsep) if p]
    return list(DEFAULT_PROJECT_ROOTS)


def _scan_roots() -> List[tuple]:
    """[(scope, dir)] — every place Claude reads custom slash commands from.
    Project scope wins over user scope for a duplicate name."""
    roots: List[tuple] = []
    seen = set()

    def add(scope: str, path: str):
        rp = os.path.realpath(path)
        if rp in seen:
            return
        seen.add(rp)
        roots.append((scope, path))

    for base in _project_roots():
        add('project', os.path.join(base, '.claude', 'commands'))
    for home in HOME_CLAUDE_ROOTS:
        add('user', os.path.join(home, 'commands'))
    return roots


def _name_from(root: str, path: str) -> str:
    """Relative-path → command name: strip `.md`, dirs join with ':'."""
    rel = os.path.relpath(path, root)
    stem = rel[:-3] if rel.endswith('.md') else rel
    parts = [p for p in stem.split(os.sep) if p]
    return ':'.join(parts).lower()


def _first_prose_line(body: str) -> str:
    """First non-empty body line, heading marker stripped — a description
    fallback when frontmatter carries none."""
    for line in (body or '').splitlines():
        s = line.strip().lstrip('#').strip()
        if s:
            return s
    return ''


def _iter_command_files(root: str):
    """Yield every `*.md` under `root`, recursing up to _MAX_DEPTH."""
    base_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath.count(os.sep) - base_depth >= _MAX_DEPTH:
            dirnames[:] = []
        for fn in sorted(filenames):
            if fn.endswith('.md'):
                yield os.path.join(dirpath, fn)
        dirnames.sort()


def discover_commands() -> List[Dict[str, str]]:
    """All custom slash commands, deduped by name (project scope wins).

    Never raises: an unreadable file or root is logged and skipped so a bad
    command file can't take the composer config down with it.
    """
    out: List[Dict[str, str]] = []
    seen = set()
    for scope, root in _scan_roots():
        if not root or not os.path.isdir(root):
            continue
        for path in _iter_command_files(root):
            name = _name_from(root, path)
            if not COMMAND_NAME_RE.match(name) or name in seen:
                continue
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    raw = f.read()
            except OSError as e:
                print(f'[commands] skip {path}: {e}', file=sys.stderr)
                continue
            meta, body = parse_frontmatter(raw)
            desc = (meta.get('description') or '').strip() or _first_prose_line(body)
            seen.add(name)
            out.append({
                'name': name,
                'description': desc,
                'argument_hint': (meta.get('argument-hint') or '').strip(),
                'scope': scope,
            })
    out.sort(key=lambda c: c['name'])
    return out
