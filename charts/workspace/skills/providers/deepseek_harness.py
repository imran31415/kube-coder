"""DeepSeek Harness (`dsh`) skill provider (issue #639).

Unlike the Ante provider — which had to infer a Claude-compatible layout
and says so — the harness documents its skill discovery precisely
(`@deepseek-ai/dsh-skill-filesystem`), so this provider follows the real
contract:

    project:  <projectRoot>/.dsh/skills
    project:  <projectRoot>/.agents/skills
    user:     $DSH_HOME/skills          (DSH_HOME defaults to ~/.dsh)
    user:     $DSH_AGENTS_HOME/skills   (defaults to ~/.agents)

Two things differ from every other provider here and are handled below:

1. **A skill may be a flat `<name>.md`,** not only a `<name>/SKILL.md`
   bundle. Both live at the TOP level of a root; the harness explicitly
   does not discover nested `**/SKILL.md`, and neither do we.

2. **`$DSH_HOME/skills` has a reserved `.system` child** which the
   harness skips. It holds the harness's own built-ins, not the user's,
   so translating them back out would be noise at best.

The `.agents/skills` roots are the cross-harness convention the harness
also reads; they are scanned but never written to, because a shared root
is not ours to install into — `.dsh/skills` is.

`~/.dsh` exists on every pod (start.sh points it at the PVC), and
scanning a skills dir that isn't there yet is a no-op. Set
KC_SKILLS_DEEPSEEK_HARNESS=0 to turn the provider off.
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

# The harness's own env overrides for the two user roots. Both fall back to
# the documented `~/<dir>` default, which is what a stock pod uses.
DSH_HOME_ENV = 'DSH_HOME'
AGENTS_HOME_ENV = 'DSH_AGENTS_HOME'

# Skipped inside $DSH_HOME/skills — the harness's built-ins, not the user's.
RESERVED_USER_CHILD = '.system'


class DeepseekHarnessProvider(SkillProvider):
    # Matches the 'deepseek-harness' key in ClaudeTaskManager.ASSISTANTS so
    # the dashboard can correlate skills with the runnable assistant.
    key = 'deepseek-harness'
    enabled = os.environ.get('KC_SKILLS_DEEPSEEK_HARNESS', '1') != '0'

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
            add('user', self._dsh_home(home))
            add('user', self._agents_home(home))
        for base in self._project_roots():
            if not os.path.isdir(base):
                continue
            add('project', os.path.join(base, '.dsh', 'skills'))
            add('project', os.path.join(base, '.agents', 'skills'))
            try:
                for entry in sorted(os.listdir(base)):
                    add('project', os.path.join(base, entry, '.dsh', 'skills'))
                    add('project', os.path.join(base, entry, '.agents', 'skills'))
            except OSError:
                continue
        return roots

    # ── root resolution ─────────────────────────────────────────────────

    @staticmethod
    def _dsh_home(home: str) -> str:
        """`$DSH_HOME/skills`, else `<home>/.dsh/skills`. A blank or
        whitespace-only DSH_HOME counts as unset — the harness's own rule,
        so a stray empty var never resolves the root to the cwd."""
        env = (os.environ.get(DSH_HOME_ENV) or '').strip()
        base = env or os.path.join(home, '.dsh')
        return os.path.join(base, 'skills')

    @staticmethod
    def _agents_home(home: str) -> str:
        env = (os.environ.get(AGENTS_HOME_ENV) or '').strip()
        base = env or os.path.join(home, '.agents')
        return os.path.join(base, 'skills')

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

    # ── read path ───────────────────────────────────────────────────────

    def _iter_skill_files(self, root: str) -> Iterable[str]:
        """`<root>/<name>/SKILL.md` AND `<root>/<name>.md`.

        The flat form is the divergence from every other provider here. The
        harness discovers both at the top level of a root and deliberately
        does not recurse, so neither do we.
        """
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            return
        for entry in entries:
            if entry == RESERVED_USER_CHILD:
                continue
            path = os.path.join(root, entry)
            if os.path.isdir(path):
                bundle = os.path.join(path, 'SKILL.md')
                if os.path.isfile(bundle):
                    yield bundle
            elif entry.lower().endswith('.md') and os.path.isfile(path):
                # `<name>.md` — _load_one falls back to the file stem for the
                # name when frontmatter omits it, which is the right answer
                # here (the parent dir is the root, not the skill).
                yield path

    # ── write path ──────────────────────────────────────────────────────
    #
    # Bundles only, and only into `.dsh/skills`. The `.agents/skills` roots
    # are a shared cross-harness convention we read but must not own, and
    # the bundle form is the one that round-trips with every other provider
    # here (a flat `<name>.md` has nowhere to put attachments).

    def _install_dir(self, scope):
        homes = list(self._homes())
        if scope == 'user':
            env = (os.environ.get(DSH_HOME_ENV) or '').strip()
            if env:
                return os.path.join(env, 'skills')
            home = next((h for h in homes if os.path.isdir(h)), homes[-1])
            return os.path.join(home, '.dsh', 'skills')
        if scope == 'project':
            roots = list(self._project_roots())
            if not roots:
                return None
            base = next((r for r in roots if os.path.isdir(r)), roots[0])
            return os.path.join(base, '.dsh', 'skills')
        return None
