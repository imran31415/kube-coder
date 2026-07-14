"""Tests for the POST /api/skills/{name}/sync handler (PR2).

Exercises the handler's decision logic directly (mock BrowserHandler,
same style as RequestBodyCapTests) with `server.SKILL_PROVIDERS` and
`server.SkillsSyncer` monkeypatched to test providers bound to temp
dirs. Covers: happy single/multi-target install, 409-on-divergence and
force override, unknown/disabled/non-writable targets, missing source,
ambiguous divergent source, bad body, and auth.

Readonly (403) is enforced by do_POST's `_readonly_block()` chokepoint
BEFORE this handler runs, so it is verified separately at that layer
(see ReadonlyChokepointTest below).

Run:  python3 -m unittest tests.skills_sync_endpoint_test
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

# server.py imports fcntl (Unix-only) at module load. Provide a no-op shim
# so this pure-logic handler test also runs on non-Unix dev machines; on
# Linux/CI the real fcntl is already importable and this branch is skipped.
try:
    import fcntl  # noqa: F401
except ImportError:  # pragma: no cover - platform shim
    import types
    _shim = types.ModuleType('fcntl')
    _shim.flock = lambda *a, **k: None
    _shim.LOCK_EX = _shim.LOCK_UN = _shim.LOCK_SH = _shim.LOCK_NB = 0
    sys.modules['fcntl'] = _shim

import server  # noqa: E402
from skills.model import SkillRecord, SkillSource, fingerprint  # noqa: E402
from skills.providers import SkillProvider  # noqa: E402


def variant(name, body, system, scope='user'):
    return SkillRecord(
        name=name, description='d', body=body, scope=scope, systems=[system],
        user_invocable=True, allowed_tools=['Bash'], argument_hint='',
        fingerprint=fingerprint(body), updated_at=1.0,
        sources=[SkillSource(system, f'/x/{system}/{name}/SKILL.md', scope, 1.0)])


class TmpProvider(SkillProvider):
    def __init__(self, key, root, enabled=True):
        self.key = key
        self._root = root
        self.enabled = enabled

    def scan_roots(self):
        return [('user', self._root)]

    def _install_dir(self, scope):
        return self._root if scope == 'user' else None


class FakeSyncer:
    """Stand-in for SkillsSyncer with a fixed variant set."""
    def __init__(self, variants):
        self._variants = variants
        self.trigger_calls = 0

    def get(self, name):
        return [v for v in self._variants if v.name == name]

    def trigger_sync(self):
        self.trigger_calls += 1
        return {'scanned': len(self._variants)}


class SyncHandlerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp_claude = tempfile.mkdtemp(prefix='claude-')
        self.tmp_oc = tempfile.mkdtemp(prefix='oc-')
        self.tmp_ante = tempfile.mkdtemp(prefix='ante-')
        self.providers = {
            'claude': TmpProvider('claude', self.tmp_claude),
            'opencode': TmpProvider('opencode', self.tmp_oc),
            'ante': TmpProvider('ante', self.tmp_ante),
            'antigravity': TmpProvider('antigravity', tempfile.mkdtemp(), enabled=False),
        }
        self._save_providers = server.SKILL_PROVIDERS
        self._save_syncer = server.SkillsSyncer
        server.SKILL_PROVIDERS = self.providers

    def tearDown(self):
        server.SKILL_PROVIDERS = self._save_providers
        server.SkillsSyncer = self._save_syncer

    def _handler(self, body):
        h = mock.Mock(spec=server.BrowserHandler)
        h.check_claude_auth.return_value = True
        h._skills_unavailable.return_value = False
        h.read_json_body.return_value = body
        self.responses = []
        h.send_json.side_effect = lambda obj, status=200: self.responses.append((obj, status))
        return h

    def call(self, name, body, variants):
        server.SkillsSyncer = FakeSyncer(variants)
        h = self._handler(body)
        server.BrowserHandler.handle_skills_sync(h, name)
        self.assertTrue(self.responses, 'handler sent no response')
        return self.responses[-1]  # (obj, status)


class HappyPathTests(SyncHandlerTestBase):
    def test_single_target_installs(self):
        v = [variant('remote-task', 'same body\n', 'claude')]
        obj, status = self.call('remote-task',
            {'source_system': 'claude', 'targets': [{'system': 'opencode'}]}, v)
        self.assertEqual(status, 200, obj)
        self.assertEqual(len(obj['installed']), 1)
        self.assertEqual(obj['installed'][0]['system'], 'opencode')
        self.assertTrue(os.path.isfile(
            os.path.join(self.tmp_oc, 'remote-task', 'SKILL.md')))
        self.assertEqual(obj['failed'], [])

    def test_multi_target_installs_all(self):
        v = [variant('shared', 'body\n', 'claude')]
        obj, status = self.call('shared',
            {'source_system': 'claude',
             'targets': [{'system': 'opencode'}, {'system': 'ante'}]}, v)
        self.assertEqual(status, 200, obj)
        self.assertEqual(len(obj['installed']), 2)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp_oc, 'shared', 'SKILL.md')))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp_ante, 'shared', 'SKILL.md')))

    def test_triggers_rescan(self):
        v = [variant('x', 'b\n', 'claude')]
        server.SkillsSyncer = FakeSyncer(v)
        h = self._handler({'source_system': 'claude', 'targets': [{'system': 'opencode'}]})
        server.BrowserHandler.handle_skills_sync(h, 'x')
        self.assertEqual(server.SkillsSyncer.trigger_calls, 1)

    def test_any_to_any_opencode_to_claude(self):
        v = [variant('cross', 'body\n', 'opencode')]
        obj, status = self.call('cross',
            {'source_system': 'opencode', 'targets': [{'system': 'claude'}]}, v)
        self.assertEqual(status, 200, obj)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp_claude, 'cross', 'SKILL.md')))


class ConflictTests(SyncHandlerTestBase):
    def test_divergent_target_409_no_force(self):
        v = [variant('dup', 'claude version\n', 'claude'),
             variant('dup', 'opencode DIFFERENT\n', 'opencode')]
        obj, status = self.call('dup',
            {'source_system': 'claude', 'targets': [{'system': 'opencode'}]}, v)
        self.assertEqual(status, 409, obj)
        self.assertEqual(obj['code'], 'conflict')
        self.assertEqual(obj['conflicts'][0]['system'], 'opencode')
        # nothing written
        self.assertFalse(os.path.exists(os.path.join(self.tmp_oc, 'dup', 'SKILL.md')))

    def test_force_overwrites_divergent(self):
        v = [variant('dup', 'claude version\n', 'claude'),
             variant('dup', 'opencode DIFFERENT\n', 'opencode')]
        obj, status = self.call('dup',
            {'source_system': 'claude', 'force': True,
             'targets': [{'system': 'opencode'}]}, v)
        self.assertEqual(status, 200, obj)
        p = os.path.join(self.tmp_oc, 'dup', 'SKILL.md')
        self.assertTrue(os.path.isfile(p))
        with open(p, encoding='utf-8') as f:
            self.assertIn('claude version', f.read())

    def test_identical_target_not_a_conflict(self):
        # Same fingerprint already present in target → no conflict, re-install ok.
        v = [variant('same', 'body\n', 'claude'),
             variant('same', 'body\n', 'opencode')]
        obj, status = self.call('same',
            {'source_system': 'claude', 'targets': [{'system': 'opencode'}]}, v)
        self.assertEqual(status, 200, obj)


class ValidationTests(SyncHandlerTestBase):
    def test_unknown_target_400(self):
        v = [variant('x', 'b\n', 'claude')]
        obj, status = self.call('x',
            {'source_system': 'claude', 'targets': [{'system': 'nope'}]}, v)
        self.assertEqual(status, 400)
        self.assertEqual(obj['code'], 'bad_target')

    def test_disabled_target_400(self):
        v = [variant('x', 'b\n', 'claude')]
        obj, status = self.call('x',
            {'source_system': 'claude', 'targets': [{'system': 'antigravity'}]}, v)
        self.assertEqual(status, 400)
        self.assertEqual(obj['code'], 'target_disabled')

    def test_nonwritable_scope_400(self):
        v = [variant('x', 'b\n', 'claude')]
        obj, status = self.call('x',
            {'source_system': 'claude',
             'targets': [{'system': 'opencode', 'scope': 'plugin'}]}, v)
        self.assertEqual(status, 400)
        self.assertEqual(obj['code'], 'bad_target')

    def test_not_found_404(self):
        obj, status = self.call('ghost',
            {'source_system': 'claude', 'targets': [{'system': 'opencode'}]}, [])
        self.assertEqual(status, 404)

    def test_ambiguous_divergent_source_400(self):
        v = [variant('dup', 'a\n', 'claude'), variant('dup', 'b\n', 'opencode')]
        obj, status = self.call('dup', {'targets': [{'system': 'ante'}]}, v)
        self.assertEqual(status, 400)
        self.assertEqual(obj['code'], 'ambiguous_source')

    def test_source_system_disambiguates(self):
        v = [variant('dup', 'aaa\n', 'claude'), variant('dup', 'bbb\n', 'opencode')]
        obj, status = self.call('dup',
            {'source_system': 'opencode', 'targets': [{'system': 'ante'}]}, v)
        self.assertEqual(status, 200, obj)
        with open(os.path.join(self.tmp_ante, 'dup', 'SKILL.md'), encoding='utf-8') as f:
            self.assertIn('bbb', f.read())  # opencode's body was the source

    def test_missing_targets_400(self):
        v = [variant('x', 'b\n', 'claude')]
        obj, status = self.call('x', {'source_system': 'claude'}, v)
        self.assertEqual(status, 400)

    def test_empty_targets_list_400(self):
        v = [variant('x', 'b\n', 'claude')]
        obj, status = self.call('x', {'source_system': 'claude', 'targets': []}, v)
        self.assertEqual(status, 400)

    def test_bad_name_400(self):
        obj, status = self.call('Bad_Name',
            {'source_system': 'claude', 'targets': [{'system': 'opencode'}]}, [])
        self.assertEqual(status, 400)
        self.assertEqual(obj['code'], 'bad_name')

    def test_bad_json_body_400(self):
        server.SkillsSyncer = FakeSyncer([])
        h = self._handler(None)
        h.read_json_body.side_effect = ValueError('bad')
        server.BrowserHandler.handle_skills_sync(h, 'x')
        self.assertEqual(self.responses[-1][1], 400)

    def test_auth_401(self):
        server.SkillsSyncer = FakeSyncer([])
        h = self._handler({})
        h.check_claude_auth.return_value = False
        server.BrowserHandler.handle_skills_sync(h, 'x')
        self.assertEqual(self.responses[-1][1], 401)


class ReadonlyChokepointTest(unittest.TestCase):
    """The sync route lives in do_POST, which calls _readonly_block() first;
    verify that chokepoint returns True (→ 403 sent) when READONLY_MODE."""
    def test_readonly_block_active(self):
        orig = server.READONLY_MODE
        try:
            server.READONLY_MODE = True
            h = mock.Mock(spec=server.BrowserHandler)
            sent = []
            h.send_json.side_effect = lambda obj, status=200: sent.append((obj, status))
            # Some builds read module global directly; ensure the method reports blocked.
            blocked = server.BrowserHandler._readonly_block(h)
            self.assertTrue(blocked)
        finally:
            server.READONLY_MODE = orig


if __name__ == '__main__':
    unittest.main()
