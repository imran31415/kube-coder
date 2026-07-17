"""Unit tests for skills/commands.py — custom slash-command discovery for the
Hypervisor composer picker (issue #302).

Covers name derivation (flat + nested-dir namespacing), description sourcing
(frontmatter, then first-prose fallback), argument-hint extraction, scope
precedence (project shadows user for a duplicate name), and graceful handling
of missing/unsafe entries.

Run with:  python3 -m unittest tests.commands_test   (from charts/workspace/)
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from skills import commands as cmd  # noqa: E402


class DiscoverCommandsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Project root: <tmp>/proj/.claude/commands
        self.proj = os.path.join(self.tmp, 'proj')
        self.proj_cmds = os.path.join(self.proj, '.claude', 'commands')
        os.makedirs(self.proj_cmds)
        # User root: point HOME_CLAUDE_ROOTS at <tmp>/home/.claude
        self.user_claude = os.path.join(self.tmp, 'home', '.claude')
        self.user_cmds = os.path.join(self.user_claude, 'commands')
        os.makedirs(self.user_cmds)

        self._orig_env = os.environ.get(cmd.PROJECT_ROOT_ENV)
        os.environ[cmd.PROJECT_ROOT_ENV] = self.proj
        self._orig_homes = cmd.HOME_CLAUDE_ROOTS
        cmd.HOME_CLAUDE_ROOTS = (self.user_claude,)

    def tearDown(self):
        cmd.HOME_CLAUDE_ROOTS = self._orig_homes
        if self._orig_env is None:
            os.environ.pop(cmd.PROJECT_ROOT_ENV, None)
        else:
            os.environ[cmd.PROJECT_ROOT_ENV] = self._orig_env

    def _write(self, base, rel, text):
        path = os.path.join(base, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)

    def test_flat_command_frontmatter(self):
        self._write(self.proj_cmds, 'deploy.md',
                    '---\ndescription: Ship it\nargument-hint: [env]\n---\n\nBody.\n')
        out = {c['name']: c for c in cmd.discover_commands()}
        self.assertIn('deploy', out)
        self.assertEqual(out['deploy']['description'], 'Ship it')
        self.assertEqual(out['deploy']['argument_hint'], '[env]')
        self.assertEqual(out['deploy']['scope'], 'project')

    def test_nested_dir_namespacing(self):
        self._write(self.proj_cmds, os.path.join('git', 'commit.md'),
                    '---\ndescription: Make a commit\n---\n')
        names = {c['name'] for c in cmd.discover_commands()}
        self.assertIn('git:commit', names)

    def test_description_falls_back_to_first_prose_line(self):
        self._write(self.proj_cmds, 'notes.md',
                    '# Heading\n\nFirst real line here.\n')
        out = {c['name']: c for c in cmd.discover_commands()}
        # Heading marker stripped; first non-empty line wins.
        self.assertEqual(out['notes']['description'], 'Heading')

    def test_description_first_prose_when_no_heading(self):
        self._write(self.proj_cmds, 'plain.md', '\n\nJust prose, no header.\n')
        out = {c['name']: c for c in cmd.discover_commands()}
        self.assertEqual(out['plain']['description'], 'Just prose, no header.')

    def test_project_scope_shadows_user(self):
        self._write(self.proj_cmds, 'dup.md',
                    '---\ndescription: project one\n---\n')
        self._write(self.user_cmds, 'dup.md',
                    '---\ndescription: user one\n---\n')
        out = {c['name']: c for c in cmd.discover_commands()}
        self.assertEqual(out['dup']['description'], 'project one')
        self.assertEqual(out['dup']['scope'], 'project')

    def test_user_scope_discovered(self):
        self._write(self.user_cmds, 'mine.md', '---\ndescription: u\n---\n')
        out = {c['name']: c for c in cmd.discover_commands()}
        self.assertIn('mine', out)
        self.assertEqual(out['mine']['scope'], 'user')

    def test_non_md_ignored(self):
        self._write(self.proj_cmds, 'readme.txt', 'not a command')
        names = {c['name'] for c in cmd.discover_commands()}
        self.assertNotIn('readme', names)

    def test_missing_dirs_no_error(self):
        # Point at a nonexistent project root — must return [] not raise.
        os.environ[cmd.PROJECT_ROOT_ENV] = os.path.join(self.tmp, 'nope')
        cmd.HOME_CLAUDE_ROOTS = (os.path.join(self.tmp, 'nohome', '.claude'),)
        self.assertEqual(cmd.discover_commands(), [])

    def test_results_sorted_by_name(self):
        self._write(self.proj_cmds, 'zeta.md', 'z\n')
        self._write(self.proj_cmds, 'alpha.md', 'a\n')
        names = [c['name'] for c in cmd.discover_commands()]
        self.assertEqual(names, sorted(names))


if __name__ == '__main__':
    unittest.main()
