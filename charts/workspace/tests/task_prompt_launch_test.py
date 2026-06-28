"""Unit tests for the task-launch prompt-delivery fixes (issue #94 blocker).

Covers ClaudeTaskManager._ensure_claude_trust (pre-accepting Claude's
folder-trust dialog so the auto-pasted initial prompt isn't swallowed) and
the pane-readiness helpers that replace the old blind fixed delay.

Run with:    python3 -m unittest tests.task_prompt_launch_test
(from charts/workspace/)
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402

CTM = server.ClaudeTaskManager


class EnsureClaudeTrustTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, '.claude.json')

    def _read(self):
        with open(self.cfg) as f:
            return json.load(f)

    def test_seeds_fresh_config(self):
        wrote = CTM._ensure_claude_trust('/home/dev', config_path=self.cfg)
        self.assertTrue(wrote)
        cfg = self._read()
        self.assertIs(cfg['hasCompletedOnboarding'], True)
        self.assertIs(cfg['projects']['/home/dev']['hasTrustDialogAccepted'], True)

    def test_idempotent_no_rewrite(self):
        self.assertTrue(CTM._ensure_claude_trust('/home/dev', config_path=self.cfg))
        # Second call: everything already present → no write.
        self.assertFalse(CTM._ensure_claude_trust('/home/dev', config_path=self.cfg))

    def test_preserves_unrelated_keys(self):
        with open(self.cfg, 'w') as f:
            json.dump({
                'oauthAccount': {'userID': 'abc'},
                'mcpServers': {'memory': {'type': 'stdio'}},
                'hasCompletedOnboarding': True,
                'projects': {'/home/dev/other': {'hasTrustDialogAccepted': True,
                                                 'projectOnboardingSeenCount': 3}},
            }, f)
        wrote = CTM._ensure_claude_trust('/home/dev/new', config_path=self.cfg)
        self.assertTrue(wrote)
        cfg = self._read()
        # Untouched existing keys.
        self.assertEqual(cfg['oauthAccount'], {'userID': 'abc'})
        self.assertEqual(cfg['mcpServers'], {'memory': {'type': 'stdio'}})
        self.assertEqual(cfg['projects']['/home/dev/other']['projectOnboardingSeenCount'], 3)
        # New project trusted.
        self.assertIs(cfg['projects']['/home/dev/new']['hasTrustDialogAccepted'], True)

    def test_only_onboarding_missing_triggers_write(self):
        with open(self.cfg, 'w') as f:
            json.dump({'projects': {'/w': {'hasTrustDialogAccepted': True}}}, f)
        self.assertTrue(CTM._ensure_claude_trust('/w', config_path=self.cfg))
        self.assertIs(self._read()['hasCompletedOnboarding'], True)

    def test_missing_file_is_created(self):
        self.assertFalse(os.path.exists(self.cfg))
        self.assertTrue(CTM._ensure_claude_trust('/home/dev', config_path=self.cfg))
        self.assertTrue(os.path.exists(self.cfg))

    def test_invalid_json_not_clobbered(self):
        with open(self.cfg, 'w') as f:
            f.write('{not valid json')
        self.assertFalse(CTM._ensure_claude_trust('/home/dev', config_path=self.cfg))
        # Original content left intact.
        with open(self.cfg) as f:
            self.assertEqual(f.read(), '{not valid json')

    def test_non_dict_config_not_clobbered(self):
        with open(self.cfg, 'w') as f:
            json.dump(['a', 'list'], f)
        self.assertFalse(CTM._ensure_claude_trust('/home/dev', config_path=self.cfg))
        self.assertEqual(self._read(), ['a', 'list'])

    def test_projects_not_dict_is_replaced(self):
        with open(self.cfg, 'w') as f:
            json.dump({'hasCompletedOnboarding': True, 'projects': 'oops'}, f)
        self.assertTrue(CTM._ensure_claude_trust('/w', config_path=self.cfg))
        self.assertIs(self._read()['projects']['/w']['hasTrustDialogAccepted'], True)


class WaitForPaneReadyTests(unittest.TestCase):
    def test_returns_when_screen_settles(self):
        captures = iter(['drawing...', 'banner', 'ready', 'ready'])
        with mock.patch.object(CTM, '_capture_pane', side_effect=lambda s: next(captures)), \
             mock.patch.object(server.time, 'sleep'):
            # Should return as soon as two consecutive captures match ('ready').
            CTM._wait_for_pane_ready('sess', floor=0, ceiling=10, interval=0)

    def test_gives_up_at_ceiling_when_never_settles(self):
        # Always-changing screen; bounded fake clock forces the deadline.
        clock = iter([0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13])
        changing = (f'frame-{i}' for i in range(100))
        with mock.patch.object(CTM, '_capture_pane', side_effect=lambda s: next(changing)), \
             mock.patch.object(server.time, 'sleep'), \
             mock.patch.object(server.time, 'time', side_effect=lambda: next(clock)):
            CTM._wait_for_pane_ready('sess', floor=0, ceiling=5, interval=0)


class CapturePaneTests(unittest.TestCase):
    def test_returns_stdout_on_success(self):
        with mock.patch.object(server.subprocess, 'run',
                               return_value=mock.Mock(returncode=0, stdout='hi')):
            self.assertEqual(CTM._capture_pane('sess'), 'hi')

    def test_returns_none_on_failure(self):
        with mock.patch.object(server.subprocess, 'run',
                               return_value=mock.Mock(returncode=1, stdout='')):
            self.assertIsNone(CTM._capture_pane('sess'))


if __name__ == '__main__':
    unittest.main()
