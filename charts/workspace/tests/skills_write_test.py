"""Unit tests for the skills WRITE path (PR2) — render + install.

Covers the canonical SKILL.md serializer (render round-trips through the
parser and preserves the content fingerprint), the atomic install
(temp-file + rename, containment, traversal rejection, disabled/
non-writable providers), and the end-to-end invariant that an installed
copy collapses with its source into a single cross-system row.

Run:  python3 -m unittest tests.skills_write_test   (from charts/workspace/)
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from skills.model import (  # noqa: E402
    SkillRecord, SkillSource, fingerprint, merge, render_skill_md,
)
from skills.parser import parse_frontmatter, parse_bool, parse_tool_list  # noqa: E402
from skills.providers import SkillProvider  # noqa: E402
from skills.providers.claude import ClaudeProvider  # noqa: E402
from skills.providers.opencode import OpenCodeProvider  # noqa: E402
from skills.providers.ante import AnteProvider  # noqa: E402
from skills.providers.antigravity import AntigravityProvider  # noqa: E402


def rec(name='remote-task', body='# Body\n\ndo the thing\n', system='claude',
        scope='user', desc='A skill', invocable=True, tools=('Bash', 'Read'),
        hint='[prompt]'):
    return SkillRecord(
        name=name, description=desc, body=body, scope=scope, systems=[system],
        user_invocable=invocable, allowed_tools=list(tools), argument_hint=hint,
        fingerprint=fingerprint(body), updated_at=1.0,
        sources=[SkillSource(system, f'/x/{name}/SKILL.md', scope, 1.0)])


# A test provider that installs into an explicit temp dir per scope.
class TmpProvider(SkillProvider):
    def __init__(self, key, root):
        self.key = key
        self._root = root

    def scan_roots(self):
        return [('user', self._root)]

    def _install_dir(self, scope):
        return self._root if scope == 'user' else None


# ───────────────────────────────────────────────────────────────────────────
# render_skill_md
# ───────────────────────────────────────────────────────────────────────────

class RenderTests(unittest.TestCase):
    def test_round_trips_through_parser(self):
        r = rec()
        meta, body = parse_frontmatter(render_skill_md(r))
        self.assertEqual(meta['name'], 'remote-task')
        self.assertEqual(meta['description'], 'A skill')
        self.assertTrue(parse_bool(meta['user-invocable']))
        self.assertEqual(parse_tool_list(meta['allowed-tools']), ['Bash', 'Read'])
        self.assertEqual(meta['argument-hint'], '[prompt]')
        self.assertIn('do the thing', body)

    def test_fingerprint_preserved(self):
        r = rec()
        _meta, body = parse_frontmatter(render_skill_md(r))
        self.assertEqual(fingerprint(body), r.fingerprint)

    def test_omits_empty_optionals(self):
        r = rec(desc='', invocable=False, tools=(), hint='')
        text = render_skill_md(r)
        self.assertNotIn('description:', text)
        self.assertNotIn('user-invocable', text)
        self.assertNotIn('allowed-tools', text)
        self.assertNotIn('argument-hint', text)
        self.assertIn('name: remote-task', text)

    def test_unicode_and_multiline_body_survive(self):
        body = '# Café ☕\n\nline one\n\n    indented\nend — dash\n'
        r = rec(body=body)
        _m, parsed = parse_frontmatter(render_skill_md(r))
        self.assertEqual(fingerprint(parsed), fingerprint(body))

    def test_empty_body(self):
        r = rec(body='')
        text = render_skill_md(r)
        _m, parsed = parse_frontmatter(text)
        self.assertEqual(parsed.strip(), '')


# ───────────────────────────────────────────────────────────────────────────
# install()
# ───────────────────────────────────────────────────────────────────────────

class InstallTests(unittest.TestCase):
    def setUp(self):
        self.dst = tempfile.mkdtemp(prefix='skl-install-')
        self.p = TmpProvider('opencode', self.dst)

    def test_installs_to_expected_path(self):
        path = self.p.install(rec(name='deploy'), 'user')
        self.assertEqual(path, os.path.join(self.dst, 'deploy', 'SKILL.md'))
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding='utf-8') as f:
            self.assertIn('name: deploy', f.read())

    def test_creates_missing_dirs(self):
        # dst exists but the <name>/ subdir does not yet.
        self.p.install(rec(name='fresh'), 'user')
        self.assertTrue(os.path.isdir(os.path.join(self.dst, 'fresh')))

    def test_overwrite_is_atomic_no_temp_left(self):
        self.p.install(rec(name='a', body='v1\n'), 'user')
        self.p.install(rec(name='a', body='v2 body\n'), 'user')
        d = os.path.join(self.dst, 'a')
        leftovers = [f for f in os.listdir(d) if f.endswith('.tmp')]
        self.assertEqual(leftovers, [], 'temp file not cleaned up')
        with open(os.path.join(d, 'SKILL.md'), encoding='utf-8') as f:
            self.assertIn('v2 body', f.read())

    def test_rejects_unsafe_name(self):
        for bad in ('../evil', 'UPPER', 'a/b', '.hidden', 'sp ace', ''):
            with self.assertRaises(ValueError):
                self.p.install(rec(name=bad), 'user')

    def test_rejects_nonwritable_scope(self):
        with self.assertRaises(ValueError):
            self.p.install(rec(), 'plugin')

    def test_disabled_provider_refuses(self):
        self.p.enabled = False
        with self.assertRaises(ValueError):
            self.p.install(rec(), 'user')

    def test_install_path_validates_name(self):
        with self.assertRaises(ValueError):
            self.p.install_path('../escape', 'user')
        self.assertTrue(
            self.p.install_path('ok-name', 'user').endswith(
                os.path.join('ok-name', 'SKILL.md')))

    def test_writable_reflects_scope_and_enabled(self):
        self.assertTrue(self.p.writable())
        self.p.enabled = False
        self.assertFalse(self.p.writable())


# ───────────────────────────────────────────────────────────────────────────
# End-to-end: install → scan → merge collapses to one cross-system row
# ───────────────────────────────────────────────────────────────────────────

class CollapseInvariantTests(unittest.TestCase):
    def test_synced_copy_collapses_with_source(self):
        src = rec(name='shared', body='# Shared\n\nsame content\n', system='claude')
        dst = tempfile.mkdtemp(prefix='oc-')
        oc = TmpProvider('opencode', dst)
        oc.install(src, 'user')
        scanned = oc.scan()
        self.assertEqual(len(scanned), 1)
        self.assertEqual(scanned[0].fingerprint, src.fingerprint)
        merged = merge([src, scanned[0]])
        self.assertEqual(len(merged), 1)
        self.assertEqual(sorted(merged[0].systems), ['claude', 'opencode'])

    def test_divergent_after_edit_splits_again(self):
        src = rec(name='shared', body='original\n', system='claude')
        dst = tempfile.mkdtemp(prefix='oc-')
        oc = TmpProvider('opencode', dst)
        oc.install(src, 'user')
        # Edit the installed copy so content diverges.
        p = os.path.join(dst, 'shared', 'SKILL.md')
        with open(p, 'a', encoding='utf-8') as f:
            f.write('\nEDITED locally\n')
        merged = merge([src] + oc.scan())
        self.assertEqual(len(merged), 2)


# ───────────────────────────────────────────────────────────────────────────
# Real providers resolve sane install dirs
# ───────────────────────────────────────────────────────────────────────────

class RealProviderInstallDirTests(unittest.TestCase):
    def test_claude_user_and_project_dirs(self):
        c = ClaudeProvider()
        self.assertTrue(c._install_dir('user').endswith(os.path.join('.claude', 'skills'))
                        or c._install_dir('user').endswith('skills'))
        self.assertIn('.claude', c._install_dir('project'))
        self.assertIsNone(c._install_dir('plugin'))  # plugins not writable

    def test_opencode_dirs(self):
        o = OpenCodeProvider()
        self.assertIn(os.path.join('opencode', 'skills'), o._install_dir('user'))
        self.assertIn(os.path.join('.opencode', 'skills'), o._install_dir('project'))

    def test_ante_dirs(self):
        a = AnteProvider()
        self.assertIn(os.path.join('.ante', 'skills'), a._install_dir('user'))

    def test_antigravity_not_writable(self):
        ag = AntigravityProvider()
        self.assertFalse(ag.writable())  # disabled stub


if __name__ == '__main__':
    unittest.main()
