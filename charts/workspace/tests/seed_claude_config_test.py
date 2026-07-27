"""Unit tests for seed_claude_config.py's ~/.claude/settings.json seeding.

The `tui` key matters for issue #529: Claude Code's first-run "Try the new
fullscreen renderer?" dialog is gated solely on `tui` being undefined, and on a
fresh pod that dialog swallows the auto-pasted prompt of a dispatched build task
— the task then stalls forever instead of building. Seeding the key at boot
suppresses the dialog.

Run with:    python3 -m unittest tests.seed_claude_config_test
(from charts/workspace/)
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import seed_claude_config as scc  # noqa: E402


class SeedSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kc-seed-')
        self._settings_save = scc.SETTINGS_PATH
        self._config_save = scc.CONFIG_PATH
        scc.SETTINGS_PATH = os.path.join(self.tmp, '.claude', 'settings.json')
        scc.CONFIG_PATH = os.path.join(self.tmp, '.claude.json')

    def tearDown(self):
        scc.SETTINGS_PATH = self._settings_save
        scc.CONFIG_PATH = self._config_save
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, obj):
        os.makedirs(os.path.dirname(scc.SETTINGS_PATH), exist_ok=True)
        with open(scc.SETTINGS_PATH, 'w') as f:
            json.dump(obj, f)

    def _read(self):
        with open(scc.SETTINGS_PATH) as f:
            return json.load(f)

    def test_seeds_tui_on_a_fresh_pod(self):
        scc._seed_settings()
        self.assertEqual(self._read()['tui'], 'default')

    def test_seeded_tui_is_the_classic_renderer(self):
        # Dispatched tasks are captured out of tmux by get_task_output, and the
        # fullscreen renderer drives the alt-screen with cursor-addressing
        # escapes that garble that capture — so the suppressing value must be
        # the classic main-screen renderer, not "fullscreen".
        self.assertEqual(scc.DEFAULT_SETTINGS['tui'], 'default')

    def test_seeds_the_enforced_keys_on_a_fresh_pod(self):
        scc._seed_settings()
        settings = self._read()
        for key, value in scc.ENFORCED_SETTINGS.items():
            self.assertEqual(settings[key], value)

    def test_keeps_a_tui_choice_the_user_made(self):
        # Someone who accepted the upsell (or picked a renderer in /config) owns
        # that value; re-seeding it every boot would fight the user.
        self._write({'tui': 'fullscreen'})
        scc._seed_settings()
        self.assertEqual(self._read()['tui'], 'fullscreen')

    def test_repairs_a_drifted_enforced_key(self):
        self._write({'skipDangerousModePermissionPrompt': False, 'tui': 'default'})
        scc._seed_settings()
        self.assertIs(self._read()['skipDangerousModePermissionPrompt'], True)

    def test_preserves_unrelated_settings(self):
        self._write({'theme': 'dark', 'model': 'opus[1m]',
                     'permissions': {'allow': ['Bash(ls:*)']}})
        scc._seed_settings()
        settings = self._read()
        self.assertEqual(settings['theme'], 'dark')
        self.assertEqual(settings['model'], 'opus[1m]')
        self.assertEqual(settings['permissions'], {'allow': ['Bash(ls:*)']})
        self.assertEqual(settings['tui'], 'default')

    def test_idempotent_second_boot_does_not_rewrite(self):
        scc._seed_settings()
        before = os.stat(scc.SETTINGS_PATH).st_mtime_ns
        scc._seed_settings()
        self.assertEqual(os.stat(scc.SETTINGS_PATH).st_mtime_ns, before)

    def test_main_seeds_settings_alongside_the_mcp_config(self):
        self.assertEqual(scc.main(), 0)
        self.assertEqual(self._read()['tui'], 'default')
        with open(scc.CONFIG_PATH) as f:
            self.assertIn('memory', json.load(f)['mcpServers'])


if __name__ == '__main__':
    unittest.main()
