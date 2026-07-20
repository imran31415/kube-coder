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


class ApiKeyRejectSeedTests(unittest.TestCase):
    """Pre-answering No to "Do you want to use this API key?" (issue #375).

    Claude Code records the answer as the key's last 20 characters under
    customApiKeyResponses.{approved,rejected} in ~/.claude.json.
    """

    KEY = 'sk-ant-api03-' + 'x' * 75 + '7D6hPvKKA0g-8bB7sgAA'
    TAIL = KEY[-20:]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, '.claude.json')

    def _read(self):
        with open(self.cfg) as f:
            return json.load(f)

    def _seed_trust(self, workdir='/w'):
        # Get the trust/onboarding flags in place so later writes are
        # attributable to the key-rejection seeding alone.
        self.assertTrue(CTM._ensure_claude_trust(workdir, config_path=self.cfg))

    def test_missing_file_seeds_rejected(self):
        self.assertFalse(os.path.exists(self.cfg))
        wrote = CTM._ensure_claude_trust('/w', config_path=self.cfg,
                                         reject_api_key=self.KEY)
        self.assertTrue(wrote)
        resp = self._read()['customApiKeyResponses']
        self.assertEqual(resp['rejected'], [self.TAIL])
        self.assertEqual(resp['approved'], [])

    def test_idempotent_no_rewrite(self):
        CTM._ensure_claude_trust('/w', config_path=self.cfg,
                                 reject_api_key=self.KEY)
        self.assertFalse(CTM._ensure_claude_trust('/w', config_path=self.cfg,
                                                  reject_api_key=self.KEY))
        self.assertEqual(self._read()['customApiKeyResponses']['rejected'],
                         [self.TAIL])

    def test_existing_approval_is_respected(self):
        # The user already answered Yes for this key — never override that.
        self._seed_trust()
        cfg = self._read()
        cfg['customApiKeyResponses'] = {'approved': [self.TAIL], 'rejected': []}
        with open(self.cfg, 'w') as f:
            json.dump(cfg, f)
        self.assertFalse(CTM._ensure_claude_trust('/w', config_path=self.cfg,
                                                  reject_api_key=self.KEY))
        resp = self._read()['customApiKeyResponses']
        self.assertEqual(resp['approved'], [self.TAIL])
        self.assertEqual(resp['rejected'], [])

    def test_existing_entries_preserved(self):
        self._seed_trust()
        cfg = self._read()
        cfg['customApiKeyResponses'] = {'approved': ['otherKeyTail000000AA'],
                                        'rejected': ['oldRejectedTail00000'],
                                        'extra': 'kept'}
        with open(self.cfg, 'w') as f:
            json.dump(cfg, f)
        self.assertTrue(CTM._ensure_claude_trust('/w', config_path=self.cfg,
                                                 reject_api_key=self.KEY))
        resp = self._read()['customApiKeyResponses']
        self.assertEqual(resp['approved'], ['otherKeyTail000000AA'])
        self.assertEqual(resp['rejected'], ['oldRejectedTail00000', self.TAIL])
        self.assertEqual(resp['extra'], 'kept')

    def test_no_key_leaves_responses_absent(self):
        CTM._ensure_claude_trust('/w', config_path=self.cfg)
        self.assertNotIn('customApiKeyResponses', self._read())


class ApiKeyToRejectTests(unittest.TestCase):
    """Gating for which env ANTHROPIC_API_KEY gets pre-rejected."""

    def _call(self, env_key='sk-ant-test', overlay=None, status=None):
        with mock.patch.dict(os.environ,
                             {'ANTHROPIC_API_KEY': env_key} if env_key else {},
                             clear=False), \
             mock.patch.object(server.ProviderKeysManager, 'env_overlay',
                               return_value=overlay or {}), \
             mock.patch.object(server.SubscriptionStatusManager, '_claude_status',
                               return_value=status or {'logged_in': False}):
            if not env_key:
                os.environ.pop('ANTHROPIC_API_KEY', None)
            return CTM._api_key_to_reject()

    def test_rejects_env_key_with_subscription(self):
        key = self._call(status={'logged_in': True, 'kind': 'subscription'})
        self.assertEqual(key, 'sk-ant-test')

    def test_no_env_key(self):
        self.assertIsNone(self._call(env_key=None,
                                     status={'logged_in': True,
                                             'kind': 'subscription'}))

    def test_pasted_settings_key_is_kept(self):
        # A key set in Settings is an explicit opt-in to API-key auth.
        self.assertIsNone(self._call(overlay={'ANTHROPIC_API_KEY': 'sk-ant-test'},
                                     status={'logged_in': True,
                                             'kind': 'subscription'}))

    def test_no_subscription_login_keeps_key(self):
        # Without a subscription login the env key may be the only auth.
        self.assertIsNone(self._call(status={'logged_in': False}))


# A realistic idle Claude Code composer: the shortcuts footer plus the box
# input affordance. This is the "really ready for input" screen — distinct
# from a momentary quiet gap between startup notices.
COMPOSER_READY = (
    "● Ready when you are.\n\n"
    "╭──────────────────────────────────────────────╮\n"
    "│ >                                            │\n"
    "╰──────────────────────────────────────────────╯\n"
    "  ? for shortcuts\n"
)

# A mid-startup screen: notices painted, but no interactive composer yet.
# Two of these 0.6s apart would falsely look "settled" (issue #288).
STARTUP_QUIET = (
    "✻ Welcome to Claude Code\n\n"
    "  Meet Fable 5 — now available\n"
    "  Auto-update failed: no write permission to npm prefix\n"
)


class PaneInputReadyTests(unittest.TestCase):
    def test_composer_screen_is_ready(self):
        self.assertTrue(CTM._pane_input_ready(COMPOSER_READY))

    def test_startup_notice_is_not_ready(self):
        self.assertFalse(CTM._pane_input_ready(STARTUP_QUIET))

    def test_footer_without_composer_is_not_ready(self):
        # Footer text alone (no input affordance) shouldn't count.
        self.assertFalse(CTM._pane_input_ready('? for shortcuts'))

    def test_none_and_empty(self):
        self.assertFalse(CTM._pane_input_ready(None))
        self.assertFalse(CTM._pane_input_ready(''))


class ScreenAdvancedTests(unittest.TestCase):
    def test_change_detected(self):
        self.assertTrue(CTM._screen_advanced('a', 'b'))

    def test_no_change(self):
        self.assertFalse(CTM._screen_advanced('same', 'same'))

    def test_unobservable_assumed_advanced(self):
        # A failed capture can't be verified — assume progress rather than
        # re-paste into a session we can't see (would duplicate text).
        self.assertTrue(CTM._screen_advanced(None, 'x'))
        self.assertTrue(CTM._screen_advanced('x', None))


class WaitForPaneReadyTests(unittest.TestCase):
    def test_returns_true_on_composer_affordance(self):
        # Even while the screen is still "changing" (never two identical
        # frames), the composer affordance ends the wait immediately.
        captures = iter([STARTUP_QUIET, 'banner-2', COMPOSER_READY])
        clock = iter(range(0, 100))
        with mock.patch.object(CTM, '_capture_pane', side_effect=lambda s: next(captures)), \
             mock.patch.object(server.time, 'sleep'), \
             mock.patch.object(server.time, 'time', side_effect=lambda: next(clock)):
            self.assertTrue(
                CTM._wait_for_pane_ready('sess', floor=0, ceiling=10, interval=0))

    def test_expect_composer_ignores_false_settle(self):
        # A repeated startup-notice frame (a false "settle") must NOT end the
        # wait when a composer is expected — only the real affordance does.
        captures = iter([STARTUP_QUIET, STARTUP_QUIET, STARTUP_QUIET, COMPOSER_READY])
        clock = iter(range(0, 100))
        with mock.patch.object(CTM, '_capture_pane', side_effect=lambda s: next(captures)), \
             mock.patch.object(server.time, 'sleep'), \
             mock.patch.object(server.time, 'time', side_effect=lambda: next(clock)):
            self.assertTrue(
                CTM._wait_for_pane_ready('sess', floor=0, ceiling=10, interval=0,
                                         expect_composer=True))

    def test_returns_when_screen_settles(self):
        captures = iter(['drawing...', 'banner', 'ready', 'ready'])
        with mock.patch.object(CTM, '_capture_pane', side_effect=lambda s: next(captures)), \
             mock.patch.object(server.time, 'sleep'):
            # Non-composer UI: should fall back to settling ('ready'=='ready').
            self.assertFalse(
                CTM._wait_for_pane_ready('sess', floor=0, ceiling=10, interval=0))

    def test_gives_up_at_ceiling_when_never_settles(self):
        # Always-changing screen; bounded fake clock forces the deadline.
        clock = iter([0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13])
        changing = (f'frame-{i}' for i in range(100))
        with mock.patch.object(CTM, '_capture_pane', side_effect=lambda s: next(changing)), \
             mock.patch.object(server.time, 'sleep'), \
             mock.patch.object(server.time, 'time', side_effect=lambda: next(clock)):
            self.assertFalse(
                CTM._wait_for_pane_ready('sess', floor=0, ceiling=5, interval=0))


class DeliverPromptTests(unittest.TestCase):
    """The core issue #288 fix: verify the paste landed and re-PASTE (not just
    re-Enter) when it didn't."""

    def _ok(self, *a, **k):
        return mock.Mock(returncode=0, stdout='', stderr='')

    def test_delivers_on_first_try(self):
        captures = iter(['empty-composer', 'text-pasted', 'assistant-working'])
        with mock.patch.object(server.subprocess, 'run', side_effect=self._ok) as run, \
             mock.patch.object(CTM, '_capture_pane', side_effect=lambda s: next(captures)), \
             mock.patch.object(server.time, 'sleep'):
            self.assertTrue(CTM._deliver_prompt('sess', '/f', 'buf'))
        # Exactly one paste-buffer (no retry needed).
        pastes = [c for c in run.call_args_list if 'paste-buffer' in c.args[0]]
        self.assertEqual(len(pastes), 1)

    def test_retries_paste_when_dropped(self):
        # Attempt 1: composer unchanged after paste (dropped). Attempt 2 lands.
        captures = iter([
            'empty', 'empty',                 # attempt 1: before, pasted (dropped)
            'empty', 'text-pasted', 'working'  # attempt 2: before, pasted, after
        ])
        with mock.patch.object(server.subprocess, 'run', side_effect=self._ok) as run, \
             mock.patch.object(CTM, '_capture_pane', side_effect=lambda s: next(captures)), \
             mock.patch.object(server.time, 'sleep'):
            self.assertTrue(CTM._deliver_prompt('sess', '/f', 'buf', retries=3))
        pastes = [c for c in run.call_args_list if 'paste-buffer' in c.args[0]]
        self.assertEqual(len(pastes), 2)  # re-PASTE, not just re-Enter

    def test_returns_false_when_all_pastes_dropped(self):
        with mock.patch.object(server.subprocess, 'run', side_effect=self._ok), \
             mock.patch.object(CTM, '_capture_pane', side_effect=lambda s: 'empty'), \
             mock.patch.object(server.time, 'sleep'):
            self.assertFalse(CTM._deliver_prompt('sess', '/f', 'buf', retries=2))

    def test_nudges_enter_when_submit_not_registered(self):
        # Paste lands, but first Enter is absorbed (screen unchanged); the
        # nudge submits it.
        captures = iter(['empty', 'text-pasted', 'text-pasted', 'working'])
        with mock.patch.object(server.subprocess, 'run', side_effect=self._ok) as run, \
             mock.patch.object(CTM, '_capture_pane', side_effect=lambda s: next(captures)), \
             mock.patch.object(server.time, 'sleep'):
            self.assertTrue(CTM._deliver_prompt('sess', '/f', 'buf'))
        enters = [c for c in run.call_args_list
                  if 'send-keys' in c.args[0] and 'Enter' in c.args[0]]
        self.assertEqual(len(enters), 2)  # initial + one nudge

    def test_paste_without_submit_skips_enter(self):
        captures = iter(['empty', 'text-pasted'])
        with mock.patch.object(server.subprocess, 'run', side_effect=self._ok) as run, \
             mock.patch.object(CTM, '_capture_pane', side_effect=lambda s: next(captures)), \
             mock.patch.object(server.time, 'sleep'):
            self.assertTrue(CTM._deliver_prompt('sess', '/f', 'buf', submit=False))
        enters = [c for c in run.call_args_list
                  if 'send-keys' in c.args[0] and 'Enter' in c.args[0]]
        self.assertEqual(len(enters), 0)


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
