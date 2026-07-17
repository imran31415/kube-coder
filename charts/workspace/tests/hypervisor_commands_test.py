"""Tests for the Hypervisor composer picker source (issue #302):
BrowserHandler._hypervisor_commands() and its exposure on
GET /api/hypervisor/config.

The picker merges custom slash commands (skills/commands.py discovery) with
*invocable, Claude-runnable* skills from the SkillsSyncer snapshot, deduped by
name. This exercises that merge against a temp commands dir + a hand-seeded
SkillsSyncer cache.

Run:  python3 -m unittest tests.hypervisor_commands_test  (from charts/workspace/)
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

# fcntl shim so the pure-logic import works off-Linux too (mirrors
# hypervisor_routes_test.py).
try:
    import fcntl  # noqa: F401
except ImportError:  # pragma: no cover
    import types
    _shim = types.ModuleType('fcntl')
    _shim.flock = lambda *a, **k: None
    _shim.LOCK_EX = _shim.LOCK_UN = _shim.LOCK_SH = _shim.LOCK_NB = 0
    sys.modules['fcntl'] = _shim

import server  # noqa: E402
from skills import commands as cmd  # noqa: E402
from skills.model import SkillRecord, SkillSource  # noqa: E402
from skills.sync import SkillsSyncer  # noqa: E402


def _skill(name, *, invocable, systems, desc='', hint=''):
    return SkillRecord(
        name=name, description=desc, body='b', scope='project',
        systems=list(systems), user_invocable=invocable, argument_hint=hint,
        sources=[SkillSource(system=systems[0], path=f'/x/{name}',
                             scope='project', updated_at=1.0)],
        fingerprint='fp', updated_at=1.0,
    )


class HypervisorCommandsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.proj_cmds = os.path.join(self.tmp, 'proj', '.claude', 'commands')
        os.makedirs(self.proj_cmds)
        self._orig_env = os.environ.get(cmd.PROJECT_ROOT_ENV)
        os.environ[cmd.PROJECT_ROOT_ENV] = os.path.join(self.tmp, 'proj')
        self._orig_homes = cmd.HOME_CLAUDE_ROOTS
        cmd.HOME_CLAUDE_ROOTS = (os.path.join(self.tmp, 'nohome', '.claude'),)
        # Seed the syncer cache directly (bypass the background thread).
        SkillsSyncer._cache = []
        SkillsSyncer._cache_version = 1

    def tearDown(self):
        cmd.HOME_CLAUDE_ROOTS = self._orig_homes
        if self._orig_env is None:
            os.environ.pop(cmd.PROJECT_ROOT_ENV, None)
        else:
            os.environ[cmd.PROJECT_ROOT_ENV] = self._orig_env
        SkillsSyncer._reset_for_test()

    def _write_cmd(self, rel, text):
        path = os.path.join(self.proj_cmds, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)

    def test_merges_commands_and_invocable_claude_skills(self):
        self._write_cmd('deploy.md', '---\ndescription: Ship\n---\n')
        SkillsSyncer._cache = [
            _skill('kc-issue', invocable=True, systems=['claude'], desc='Issue'),
            _skill('internal', invocable=False, systems=['claude']),  # not invocable
            _skill('ante-only', invocable=True, systems=['ante']),    # no claude
        ]
        out = {c['name']: c for c in server.BrowserHandler._hypervisor_commands()}
        self.assertEqual(out['deploy']['kind'], 'command')
        self.assertEqual(out['kc-issue']['kind'], 'skill')
        self.assertNotIn('internal', out)      # non-invocable dropped
        self.assertNotIn('ante-only', out)     # non-Claude dropped

    def test_command_shadows_same_named_skill(self):
        self._write_cmd('kc-issue.md', '---\ndescription: from command\n---\n')
        SkillsSyncer._cache = [
            _skill('kc-issue', invocable=True, systems=['claude'], desc='from skill'),
        ]
        out = [c for c in server.BrowserHandler._hypervisor_commands()
               if c['name'] == 'kc-issue']
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['kind'], 'command')
        self.assertEqual(out[0]['description'], 'from command')

    def test_sorted_by_name(self):
        self._write_cmd('zeta.md', 'z\n')
        SkillsSyncer._cache = [
            _skill('alpha', invocable=True, systems=['claude']),
        ]
        names = [c['name'] for c in server.BrowserHandler._hypervisor_commands()]
        self.assertEqual(names, sorted(names))

    def test_config_includes_commands_field(self):
        self._write_cmd('deploy.md', '---\ndescription: Ship\n---\n')
        h = mock.Mock(spec=server.BrowserHandler)
        h.check_claude_auth.return_value = True
        # Let the real static aggregator run instead of the auto-mocked stub.
        h._hypervisor_commands.side_effect = server.BrowserHandler._hypervisor_commands
        captured = {}
        h.send_json.side_effect = lambda obj, status=200: captured.update(obj=obj)
        server.BrowserHandler.handle_hypervisor_config(h)
        self.assertIn('commands', captured['obj'])
        names = {c['name'] for c in captured['obj']['commands']}
        self.assertIn('deploy', names)


if __name__ == '__main__':
    unittest.main()
