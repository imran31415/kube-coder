"""Unit tests for the skills/ package — multi-harness skill discovery.

Covers the tolerant frontmatter parser, the content-fingerprint identity
function, the merge/dedupe algorithm (cross-system collapse, divergence,
within-system scope shadowing), the name-safety gate, and provider scans
over temp-dir fixtures for the Claude layout (project/user/plugin) and
OpenCode's legacy command files.

Run with:    python3 -m unittest tests.skills_test
(from charts/workspace/)
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from skills.model import (  # noqa: E402
    SkillRecord, SkillSource, SKILL_NAME_RE, fingerprint, merge, find,
)
from skills.parser import (  # noqa: E402
    parse_frontmatter, parse_bool, parse_tool_list,
)
from skills.providers import PROVIDERS, SkillProvider  # noqa: E402
from skills.providers.ante import AnteProvider  # noqa: E402
from skills.providers.opencode import OpenCodeProvider  # noqa: E402


SKILL_MD = """---
name: remote-task
description: Launch a task on a remote workspace
user-invocable: true
allowed-tools: Bash, Read, Grep
argument-hint: [prompt or "status"]
---

# Remote Task Skill

Body text here.
"""


# ───────────────────────────────────────────────────────────────────────────
# Parser
# ───────────────────────────────────────────────────────────────────────────

class ParseFrontmatterTests(unittest.TestCase):
    def test_full_frontmatter(self):
        meta, body = parse_frontmatter(SKILL_MD)
        self.assertEqual(meta['name'], 'remote-task')
        self.assertEqual(meta['description'],
                         'Launch a task on a remote workspace')
        self.assertEqual(meta['user-invocable'], 'true')
        self.assertIn('# Remote Task Skill', body)
        self.assertNotIn('---', body.split('\n')[0])

    def test_no_frontmatter_is_all_body(self):
        meta, body = parse_frontmatter('just markdown, no fence')
        self.assertEqual(meta, {})
        self.assertEqual(body, 'just markdown, no fence')

    def test_empty_input(self):
        meta, body = parse_frontmatter('')
        self.assertEqual(meta, {})
        self.assertEqual(body, '')

    def test_quoted_values_unquoted(self):
        meta, _ = parse_frontmatter('---\nname: "quoted"\n---\nbody')
        self.assertEqual(meta['name'], 'quoted')

    def test_keys_lowercased(self):
        meta, _ = parse_frontmatter('---\nName: x\nDESCRIPTION: y\n---\nbody')
        self.assertEqual(meta['name'], 'x')
        self.assertEqual(meta['description'], 'y')

    def test_malformed_lines_skipped(self):
        meta, _ = parse_frontmatter('---\nno-colon-line\nname: ok\n---\nbody')
        self.assertEqual(meta, {'name': 'ok'})

    def test_bool_parsing(self):
        for t in ('true', 'True', 'yes', '1', 'on'):
            self.assertTrue(parse_bool(t), t)
        for f in ('false', 'no', '0', '', 'banana'):
            self.assertFalse(parse_bool(f), f)

    def test_tool_list(self):
        self.assertEqual(parse_tool_list('Bash, Read, Grep'),
                         ['Bash', 'Read', 'Grep'])
        self.assertEqual(parse_tool_list(''), [])
        self.assertEqual(parse_tool_list('  Bash ,, Read '), ['Bash', 'Read'])


# ───────────────────────────────────────────────────────────────────────────
# Fingerprint (identity function)
# ───────────────────────────────────────────────────────────────────────────

class FingerprintTests(unittest.TestCase):
    def test_whitespace_normalized(self):
        self.assertEqual(fingerprint('hello\nworld  \n\n'),
                         fingerprint('\n\nhello\nworld'))

    def test_content_change_changes_fingerprint(self):
        self.assertNotEqual(fingerprint('hello'), fingerprint('hello!'))

    def test_stable_length(self):
        self.assertEqual(len(fingerprint('anything')), 16)

    def test_empty_body(self):
        self.assertEqual(fingerprint(''), fingerprint('\n\n'))


# ───────────────────────────────────────────────────────────────────────────
# Merge / dedupe
# ───────────────────────────────────────────────────────────────────────────

def _rec(name, body, system, scope='user', mtime=1.0, description=''):
    return SkillRecord(
        name=name, description=description, body=body, scope=scope,
        systems=[system],
        sources=[SkillSource(system=system, path=f'/x/{system}/{scope}/{name}',
                             scope=scope, updated_at=mtime)],
        fingerprint=fingerprint(body), updated_at=mtime,
    )


class MergeTests(unittest.TestCase):
    def test_same_content_across_systems_collapses(self):
        out = merge([_rec('a', 'same body', 'claude'),
                     _rec('a', 'same body', 'opencode')])
        self.assertEqual(len(out), 1)
        self.assertEqual(sorted(out[0].systems), ['claude', 'opencode'])
        self.assertEqual(len(out[0].sources), 2)

    def test_divergent_content_stays_split(self):
        out = merge([_rec('a', 'one', 'claude'),
                     _rec('a', 'two', 'opencode')])
        self.assertEqual(len(out), 2)
        self.assertEqual(len(find(out, 'a')), 2)

    def test_project_shadows_user_within_system(self):
        out = merge([_rec('a', 'proj body', 'claude', scope='project'),
                     _rec('a', 'user body', 'claude', scope='user')])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].scope, 'project')
        self.assertIn('proj body', out[0].body)
        shadowed = [s for s in out[0].sources if s.shadowed]
        self.assertEqual(len(shadowed), 1)
        self.assertEqual(shadowed[0].scope, 'user')

    def test_user_shadows_plugin(self):
        out = merge([_rec('a', 'plug', 'claude', scope='plugin'),
                     _rec('a', 'usr', 'claude', scope='user')])
        self.assertEqual(out[0].scope, 'user')

    def test_shadowing_order_independent(self):
        a = merge([_rec('a', 'p', 'claude', 'project'),
                   _rec('a', 'u', 'claude', 'user')])
        b = merge([_rec('a', 'u', 'claude', 'user'),
                   _rec('a', 'p', 'claude', 'project')])
        self.assertEqual(a[0].scope, b[0].scope)
        self.assertEqual(a[0].fingerprint, b[0].fingerprint)

    def test_updated_at_is_max_over_merged(self):
        out = merge([_rec('a', 'same', 'claude', mtime=5.0),
                     _rec('a', 'same', 'opencode', mtime=9.0)])
        self.assertEqual(out[0].updated_at, 9.0)

    def test_description_filled_from_newcomer_when_blank(self):
        out = merge([_rec('a', 'same', 'claude', description=''),
                     _rec('a', 'same', 'opencode', description='hi')])
        self.assertEqual(out[0].description, 'hi')

    def test_different_names_never_merge(self):
        out = merge([_rec('a', 'same', 'claude'),
                     _rec('b', 'same', 'claude')])
        self.assertEqual(len(out), 2)

    def test_sorted_output_deterministic(self):
        out = merge([_rec('z', '1', 'claude'), _rec('a', '2', 'claude')])
        self.assertEqual([r.name for r in out], ['a', 'z'])


class NameSafetyTests(unittest.TestCase):
    def test_valid_names(self):
        for n in ('a', 'remote-task', 'x1', '9lives', 'a-b-c'):
            self.assertTrue(SKILL_NAME_RE.match(n), n)

    def test_rejected_names(self):
        for n in ('../evil', 'UPPER', 'sp ace', '.hidden', '-lead',
                  'a/b', 'a\\b', 'a..b/..', ''):
            self.assertFalse(SKILL_NAME_RE.match(n), n)


# ───────────────────────────────────────────────────────────────────────────
# Provider scans over tmpdir fixtures
# ───────────────────────────────────────────────────────────────────────────

class _TmpProvider(SkillProvider):
    """Test provider bound to explicit (scope, dir) roots."""
    key = 'testtool'

    def __init__(self, roots):
        self._roots = roots

    def scan_roots(self):
        return self._roots


def _write_skill(root, name, text):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, 'SKILL.md')
    with open(p, 'w', encoding='utf-8') as f:
        f.write(text)
    return p


class ProviderScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='skills-test-')

    def test_scan_standard_layout(self):
        _write_skill(self.tmp, 'remote-task', SKILL_MD)
        recs = _TmpProvider([('user', self.tmp)]).scan()
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r.name, 'remote-task')
        self.assertEqual(r.systems, ['testtool'])
        self.assertEqual(r.scope, 'user')
        self.assertTrue(r.user_invocable)
        self.assertEqual(r.allowed_tools, ['Bash', 'Read', 'Grep'])
        self.assertIn('Body text here.', r.body)
        self.assertTrue(r.fingerprint)

    def test_name_falls_back_to_folder(self):
        _write_skill(self.tmp, 'folder-name', 'no frontmatter body')
        recs = _TmpProvider([('user', self.tmp)]).scan()
        self.assertEqual(recs[0].name, 'folder-name')

    def test_unsafe_folder_name_skipped(self):
        _write_skill(self.tmp, 'Bad Name', 'body')
        recs = _TmpProvider([('user', self.tmp)]).scan()
        self.assertEqual(recs, [])

    def test_missing_root_is_fine(self):
        recs = _TmpProvider([('user', os.path.join(self.tmp, 'nope'))]).scan()
        self.assertEqual(recs, [])

    def test_disabled_provider_returns_nothing(self):
        _write_skill(self.tmp, 'x', SKILL_MD)
        p = _TmpProvider([('user', self.tmp)])
        p.enabled = False
        self.assertEqual(p.scan(), [])

    def test_duplicate_realpath_deduped(self):
        _write_skill(self.tmp, 'a', SKILL_MD)
        recs = _TmpProvider([('user', self.tmp), ('user', self.tmp)]).scan()
        self.assertEqual(len(recs), 1)

    def test_mtime_fingerprint_reflects_files(self):
        _write_skill(self.tmp, 'a', SKILL_MD)
        p = _TmpProvider([('user', self.tmp)])
        fp1 = p.roots_mtime_fingerprint()
        self.assertEqual(len(fp1), 1)
        _write_skill(self.tmp, 'b', SKILL_MD)
        fp2 = p.roots_mtime_fingerprint()
        self.assertEqual(len(fp2), 2)


class OpenCodeCommandTests(unittest.TestCase):
    """Legacy ~/.config/opencode/command/*.md flat files."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='oc-test-')
        self.cmd_dir = os.path.join(self.tmp, '.config', 'opencode', 'command')
        os.makedirs(self.cmd_dir)
        self.provider = OpenCodeProvider()
        # Bind home discovery to the fixture.
        self.provider._homes = lambda: [self.tmp]  # type: ignore
        self.provider._project_roots = lambda: []  # type: ignore

    def test_command_file_mapped_to_skill(self):
        with open(os.path.join(self.cmd_dir, 'deploy-prod.md'), 'w') as f:
            f.write('---\ndescription: Deploy to prod\n---\nDo the deploy.')
        recs = self.provider.scan()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].name, 'deploy-prod')
        self.assertEqual(recs[0].description, 'Deploy to prod')
        self.assertEqual(recs[0].systems, ['opencode'])

    def test_skills_dir_takes_precedence_over_command(self):
        skills_dir = os.path.join(self.tmp, '.config', 'opencode', 'skills')
        _write_skill(skills_dir, 'dup', '---\nname: dup\n---\nskills-dir copy')
        with open(os.path.join(self.cmd_dir, 'dup.md'), 'w') as f:
            f.write('command copy')
        recs = self.provider.scan()
        names = [r.name for r in recs]
        self.assertEqual(names.count('dup'), 1)
        self.assertIn('skills-dir copy', recs[names.index('dup')].body)


class ProviderRegistryTests(unittest.TestCase):
    """The registry is the extension point — every harness key present."""

    def test_all_harnesses_registered(self):
        for key in ('claude', 'opencode', 'ante', 'antigravity'):
            self.assertIn(key, PROVIDERS)
            self.assertEqual(PROVIDERS[key].key, key)

    def test_ante_enabled_by_default(self):
        # ~/.ante exists on every pod (start.sh seeds settings.json), and
        # scanning a missing skills dir is a no-op — safe to ship on.
        self.assertTrue(PROVIDERS['ante'].enabled)


class AnteProviderTests(unittest.TestCase):
    """Ante scans the Claude-compatible layout under ~/.ante/skills."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ante-test-')
        self.skills_dir = os.path.join(self.tmp, '.ante', 'skills')
        self.provider = AnteProvider()
        self.provider._homes = lambda: [self.tmp]  # type: ignore
        self.provider._project_roots = lambda: []  # type: ignore

    def test_scan_roots_point_at_ante_home(self):
        roots = self.provider.scan_roots()
        self.assertEqual(roots, [('user', self.skills_dir)])

    def test_scan_standard_layout(self):
        _write_skill(self.skills_dir, 'runbook', SKILL_MD)
        recs = self.provider.scan()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].systems, ['ante'])
        self.assertEqual(recs[0].name, 'remote-task')  # frontmatter name wins

    def test_missing_skills_dir_is_noop(self):
        self.assertEqual(self.provider.scan(), [])

    def test_project_scope_scanned(self):
        proj = tempfile.mkdtemp(prefix='ante-proj-')
        repo_skills = os.path.join(proj, 'myrepo', '.ante', 'skills')
        _write_skill(repo_skills, 'deploy', '---\nname: deploy\n---\nbody')
        self.provider._project_roots = lambda: [proj]  # type: ignore
        recs = self.provider.scan()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].scope, 'project')

    def test_cross_system_collapse_with_claude_copy(self):
        # The same SKILL.md in Ante's dir and a "claude" dir → one row.
        _write_skill(self.skills_dir, 'remote-task', SKILL_MD)
        claude_dir = tempfile.mkdtemp(prefix='claude-side-')
        _write_skill(claude_dir, 'remote-task', SKILL_MD)

        class C(_TmpProvider):
            key = 'claude'

        out = merge(self.provider.scan() + C([('user', claude_dir)]).scan())
        self.assertEqual(len(out), 1)
        self.assertEqual(sorted(out[0].systems), ['ante', 'claude'])


class EndToEndMergeTests(unittest.TestCase):
    """Two providers over real files → one collapsed logical skill."""

    def test_cross_system_collapse_from_disk(self):
        t1 = tempfile.mkdtemp(prefix='sysA-')
        t2 = tempfile.mkdtemp(prefix='sysB-')
        _write_skill(t1, 'shared', SKILL_MD)
        _write_skill(t2, 'shared', SKILL_MD)

        class A(_TmpProvider):
            key = 'claude'

        class B(_TmpProvider):
            key = 'opencode'

        recs = A([('user', t1)]).scan() + B([('user', t2)]).scan()
        out = merge(recs)
        self.assertEqual(len(out), 1)
        self.assertEqual(sorted(out[0].systems), ['claude', 'opencode'])

        # Now diverge one copy → two variants.
        _write_skill(t2, 'shared', SKILL_MD + '\nEDITED')
        recs = A([('user', t1)]).scan() + B([('user', t2)]).scan()
        out = merge(recs)
        self.assertEqual(len(out), 2)


if __name__ == '__main__':
    unittest.main()
