"""Unit tests for the task-launch prompt-delivery fixes (issue #94 blocker).

Covers ClaudeTaskManager._write_claude_trust (pre-accepting Claude's
folder-trust dialog so the auto-pasted initial prompt isn't swallowed),
_ensure_claude_trust's read-back-and-retry around it (issue #800) and the
pane-readiness helpers that replace the old blind fixed delay.

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


class WriteClaudeTrustTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, '.claude.json')

    def _read(self):
        with open(self.cfg) as f:
            return json.load(f)

    def test_seeds_fresh_config(self):
        wrote = CTM._write_claude_trust('/home/dev', config_path=self.cfg)
        self.assertTrue(wrote)
        cfg = self._read()
        self.assertIs(cfg['hasCompletedOnboarding'], True)
        self.assertIs(cfg['projects']['/home/dev']['hasTrustDialogAccepted'], True)

    def test_idempotent_no_rewrite(self):
        self.assertTrue(CTM._write_claude_trust('/home/dev', config_path=self.cfg))
        # Second call: everything already present → no write.
        self.assertFalse(CTM._write_claude_trust('/home/dev', config_path=self.cfg))

    def test_preserves_unrelated_keys(self):
        with open(self.cfg, 'w') as f:
            json.dump({
                'oauthAccount': {'userID': 'abc'},
                'mcpServers': {'memory': {'type': 'stdio'}},
                'hasCompletedOnboarding': True,
                'projects': {'/home/dev/other': {'hasTrustDialogAccepted': True,
                                                 'projectOnboardingSeenCount': 3}},
            }, f)
        wrote = CTM._write_claude_trust('/home/dev/new', config_path=self.cfg)
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
        self.assertTrue(CTM._write_claude_trust('/w', config_path=self.cfg))
        self.assertIs(self._read()['hasCompletedOnboarding'], True)

    def test_missing_file_is_created(self):
        self.assertFalse(os.path.exists(self.cfg))
        self.assertTrue(CTM._write_claude_trust('/home/dev', config_path=self.cfg))
        self.assertTrue(os.path.exists(self.cfg))

    def test_invalid_json_not_clobbered(self):
        with open(self.cfg, 'w') as f:
            f.write('{not valid json')
        self.assertFalse(CTM._write_claude_trust('/home/dev', config_path=self.cfg))
        # Original content left intact.
        with open(self.cfg) as f:
            self.assertEqual(f.read(), '{not valid json')

    def test_non_dict_config_not_clobbered(self):
        with open(self.cfg, 'w') as f:
            json.dump(['a', 'list'], f)
        self.assertFalse(CTM._write_claude_trust('/home/dev', config_path=self.cfg))
        self.assertEqual(self._read(), ['a', 'list'])

    def test_projects_not_dict_is_replaced(self):
        with open(self.cfg, 'w') as f:
            json.dump({'hasCompletedOnboarding': True, 'projects': 'oops'}, f)
        self.assertTrue(CTM._write_claude_trust('/w', config_path=self.cfg))
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
        self.assertTrue(CTM._write_claude_trust(workdir, config_path=self.cfg))

    def test_missing_file_seeds_rejected(self):
        self.assertFalse(os.path.exists(self.cfg))
        wrote = CTM._write_claude_trust('/w', config_path=self.cfg,
                                         reject_api_key=self.KEY)
        self.assertTrue(wrote)
        resp = self._read()['customApiKeyResponses']
        self.assertEqual(resp['rejected'], [self.TAIL])
        self.assertEqual(resp['approved'], [])

    def test_idempotent_no_rewrite(self):
        CTM._write_claude_trust('/w', config_path=self.cfg,
                                 reject_api_key=self.KEY)
        self.assertFalse(CTM._write_claude_trust('/w', config_path=self.cfg,
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
        self.assertFalse(CTM._write_claude_trust('/w', config_path=self.cfg,
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
        self.assertTrue(CTM._write_claude_trust('/w', config_path=self.cfg,
                                                 reject_api_key=self.KEY))
        resp = self._read()['customApiKeyResponses']
        self.assertEqual(resp['approved'], ['otherKeyTail000000AA'])
        self.assertEqual(resp['rejected'], ['oldRejectedTail00000', self.TAIL])
        self.assertEqual(resp['extra'], 'kept')

    def test_no_key_leaves_responses_absent(self):
        CTM._write_claude_trust('/w', config_path=self.cfg)
        self.assertNotIn('customApiKeyResponses', self._read())


class ClaudeConfigPathTests(unittest.TestCase):
    """`$KC_CLAUDE_CONFIG_PATH` — the override the Makefile's python-tests
    target sets so a test that launches a Build cannot answer Claude Code's
    dialogs in the developer's real config (#762)."""

    def test_unset_or_blank_is_the_users_config(self):
        home = os.path.expanduser('~/.claude.json')
        self.assertEqual(server._resolve_claude_config_path({}), home)
        for blank in ('', '   ', '\n'):
            self.assertEqual(server._resolve_claude_config_path(
                {'KC_CLAUDE_CONFIG_PATH': blank}), home)

    def test_set_is_used_trimmed(self):
        self.assertEqual(server._resolve_claude_config_path(
            {'KC_CLAUDE_CONFIG_PATH': ' /tmp/kc/claude.json \n'}),
            '/tmp/kc/claude.json')


class AutoModeSetupDismissTests(unittest.TestCase):
    """Pre-answering "Don't show again" to the auto-mode setup nudge (#762).

    Claude Code shows "Set up auto mode for your environment?" after a turn
    in an auto-mode session and records "Don't show again" GLOBALLY as
    autoModeEnvSetup.dismissed, next to its own `denials` / `dismissedAt`.
    """

    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.cfg = home.name + os.sep + '.claude.json'

    def _read(self):
        with open(self.cfg) as f:
            return json.load(f)

    def _seed_others(self, **extra):
        """Write a config where every OTHER answer is already in place for
        '/w', so a write is attributable to the auto-mode answer alone."""
        cfg = {'hasCompletedOnboarding': True,
               'projects': {'/w': {'hasTrustDialogAccepted': True}}, **extra}
        with open(self.cfg, 'w') as f:
            f.write(json.dumps(cfg))

    def test_missing_file_seeds_dismissed(self):
        self.assertTrue(CTM._write_claude_trust('/w', config_path=self.cfg))
        self.assertEqual(self._read()['autoModeEnvSetup'], {'dismissed': True})

    def test_cli_counters_preserved(self):
        self._seed_others(autoModeEnvSetup={'denials': 7, 'dismissedAt': 123})
        self.assertTrue(CTM._write_claude_trust('/w', config_path=self.cfg))
        self.assertEqual(self._read()['autoModeEnvSetup'],
                         {'denials': 7, 'dismissedAt': 123, 'dismissed': True})

    def test_already_dismissed_is_a_no_op(self):
        self._seed_others(autoModeEnvSetup={'dismissed': True, 'denials': 9})
        self.assertFalse(CTM._write_claude_trust('/w', config_path=self.cfg))
        self.assertEqual(self._read()['autoModeEnvSetup'],
                         {'dismissed': True, 'denials': 9})

    def test_only_this_answer_missing_triggers_write(self):
        self._seed_others()
        self.assertTrue(CTM._write_claude_trust('/w', config_path=self.cfg))
        self.assertIs(self._read()['autoModeEnvSetup']['dismissed'], True)

    def test_malformed_value_is_replaced(self):
        for bad in (None, 'x', [], 3):
            with self.subTest(bad=bad):
                self._seed_others(autoModeEnvSetup=bad)
                self.assertTrue(CTM._write_claude_trust('/w', config_path=self.cfg))
                self.assertEqual(self._read()['autoModeEnvSetup'], {'dismissed': True})

    def test_helper_reports_change_only_once(self):
        cfg = {}
        self.assertTrue(CTM._dismiss_auto_mode_setup(cfg))
        self.assertFalse(CTM._dismiss_auto_mode_setup(cfg))
        self.assertEqual(cfg, {'autoModeEnvSetup': {'dismissed': True}})

    def test_falsy_dismissed_is_set_true(self):
        # The CLI only ever writes `true` here, so `false` is not a user answer.
        self._seed_others(autoModeEnvSetup={'dismissed': False})
        self.assertTrue(CTM._write_claude_trust('/w', config_path=self.cfg))
        self.assertIs(self._read()['autoModeEnvSetup']['dismissed'], True)

    def test_new_workdir_after_dismissal_keeps_it_global(self):
        # The answer lives once at the top level; a second workdir only adds
        # its own trust entry and never a per-project copy of the answer.
        CTM._write_claude_trust('/a', config_path=self.cfg)
        self.assertTrue(CTM._write_claude_trust('/b', config_path=self.cfg))
        cfg = self._read()
        self.assertEqual(cfg['projects'], {'/a': {'hasTrustDialogAccepted': True},
                                           '/b': {'hasTrustDialogAccepted': True}})
        self.assertEqual(cfg['autoModeEnvSetup'], {'dismissed': True})

    def test_unrelated_keys_untouched(self):
        self._seed_others(
            oauthAccount={'userID': 'abc'},
            mcpServers={'memory': {'type': 'stdio'}},
            customApiKeyResponses={'approved': ['t' * 20], 'rejected': []},
            numStartups=42,
        )
        self.assertTrue(CTM._write_claude_trust('/w', config_path=self.cfg))
        cfg = self._read()
        self.assertEqual(cfg['oauthAccount'], {'userID': 'abc'})
        self.assertEqual(cfg['mcpServers'], {'memory': {'type': 'stdio'}})
        self.assertEqual(cfg['customApiKeyResponses'],
                         {'approved': ['t' * 20], 'rejected': []})
        self.assertEqual(cfg['numStartups'], 42)

    def test_lands_with_api_key_answer_in_one_write(self):
        key = 'synthetic-test-key-' + 'y' * 40 + 'AbCdEfGhIjKlMnOpQrSt'
        self.assertTrue(CTM._write_claude_trust('/w', config_path=self.cfg,
                                                 reject_api_key=key))
        cfg = self._read()
        self.assertEqual(cfg['customApiKeyResponses']['rejected'], [key[-20:]])
        self.assertIs(cfg['autoModeEnvSetup']['dismissed'], True)
        self.assertFalse(CTM._write_claude_trust('/w', config_path=self.cfg,
                                                  reject_api_key=key))


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


    def test_gives_up_when_the_session_is_GONE_not_slow(self):
        """A capture that fails is tmux saying "can't find session".

        The wait used to poll out the whole 45s ceiling anyway — ~75 shell-outs
        for a session that no longer exists, and the paste afterwards could not
        land either. In the test suite that turned into real `tmux capture-pane`
        calls firing long after the test had finished and unpatched
        `subprocess.run`, which is how a completely unrelated test came to
        capture a board worker's argv (#712 follow-up).
        """
        calls = []

        def capture(_session):
            calls.append(_session)
            return None

        clock = iter(range(0, 200))
        with mock.patch.object(CTM, '_capture_pane', side_effect=capture), \
             mock.patch.object(server.time, 'sleep'), \
             mock.patch.object(server.time, 'time', side_effect=lambda: next(clock)):
            self.assertFalse(
                CTM._wait_for_pane_ready('sess', floor=0, ceiling=45,
                                         interval=0, expect_composer=True))
        self.assertEqual(len(calls), CTM.PANE_GONE_STRIKES)

    def test_one_missed_capture_is_not_a_dead_session(self):
        # A single hiccup must not abandon a TUI that is merely slow.
        frames = iter([None, 'starting', None, COMPOSER_READY])
        clock = iter(range(0, 200))
        with mock.patch.object(CTM, '_capture_pane', side_effect=lambda s: next(frames)), \
             mock.patch.object(server.time, 'sleep'), \
             mock.patch.object(server.time, 'time', side_effect=lambda: next(clock)):
            self.assertTrue(
                CTM._wait_for_pane_ready('sess', floor=0, ceiling=45,
                                         interval=0, expect_composer=True))

    def test_default_ceiling_outlasts_a_slow_cold_start(self):
        """A cold Claude Code start was measured at 33s in a container (large
        binary + MCP spawn). The old 12s ceiling expired first, so the prompt
        was pasted into a TUI not yet accepting input: it RENDERED but Enter
        was ignored, and the task idled with its prompt sitting unsent.

        Passing ceiling=None must take the configurable default, and that
        default must comfortably exceed a slow start."""
        self.assertGreaterEqual(CTM.PANE_READY_TIMEOUT, 40)

        # composer appears at t=30s, past the old 12s ceiling
        frames = ['starting'] * 30 + [COMPOSER_READY]
        captures = iter(frames)
        clock = iter(range(0, 200))
        with mock.patch.object(CTM, '_capture_pane', side_effect=lambda s: next(captures)), \
             mock.patch.object(server.time, 'sleep'), \
             mock.patch.object(server.time, 'time', side_effect=lambda: next(clock)):
            self.assertTrue(
                CTM._wait_for_pane_ready('sess', floor=0, ceiling=None,
                                         interval=0, expect_composer=True))


# ── Scripted-pane fixtures for the delivery path (issue #805) ───────────────
# Claude Code 2.1.x shapes, as `tmux capture-pane -p` hands them over.

#: The folder-trust modal, drawn over (and covering) the composer (#800).
TRUST_DIALOG_SCREEN = (
    "╭──────────────────────────────────────────────────────────╮\n"
    "│ Quick safety check: Is this a project you created or one  │\n"
    "│ you trust?                                                │\n"
    "│                                                           │\n"
    "│ ❯ 1. Yes, I trust this folder                             │\n"
    "│   2. No, exit                                             │\n"
    "╰──────────────────────────────────────────────────────────╯\n"
    "  ? for shortcuts\n"
)


def composer_screen(box_lines, transcript=()):
    """Render a Claude Code screen: transcript, then the input box, then the
    shortcuts footer. `box_lines` is what sits INSIDE the box."""
    out = list(transcript)
    out.append('╭──────────────────────────────────────────────────────────╮')
    out.extend(f'│ {line:<56} │' for line in box_lines)
    out.append('╰──────────────────────────────────────────────────────────╯')
    out.append('  ? for shortcuts')
    return '\n'.join(out) + '\n'


class FakeSession:
    """A scripted tmux session, driven by a virtual clock.

    Models the one thing the 0.4s check got wrong: a paste becomes VISIBLE in
    the composer `paste_delay` virtual seconds after `paste-buffer` returns,
    not immediately. Patch `subprocess.run` at it and the real delivery code
    runs end to end — `_capture_pane` included.

    - `drops`      — this many pastes vanish entirely (the #288 failure).
    - `swallow`    — this many Enters are absorbed (bracketed-paste eats one).
    - `preloaded`  — copies already sitting in the box (a pre-fix stacked box).
    - `placeholder`— render pastes as `[Pasted text #N +14 lines]`, which is
                     what Claude shows for a long or multi-line prompt.
    - `unclearable`— C-u / Escape do nothing (the box refuses to empty).
    - `dialog`     — start parked on the folder-trust modal.
    """

    PROMPT = 'implement the thing and open a PR'

    def __init__(self, paste_delay=0.0, drops=0, swallow=0, preloaded=0,
                 placeholder=False, unclearable=False, dialog=False):
        self.clock = 0.0
        self.paste_delay = paste_delay
        self.drops = drops
        self.swallow = swallow
        self.placeholder = placeholder
        self.unclearable = unclearable
        self.dialog = dialog
        self.visible = preloaded
        self.pending = []
        self.submitted = []
        self.pastes = 0
        self.enters = 0
        self.keys = []

    # -- virtual clock --------------------------------------------------
    def sleep(self, secs):
        # A zero sleep still has to advance time or a bounded poll loop would
        # never reach its deadline.
        self.clock += float(secs) if secs else 0.01

    def time(self):
        return self.clock

    def _tick(self):
        due = [t for t in self.pending if t <= self.clock]
        self.pending = [t for t in self.pending if t > self.clock]
        self.visible += len(due)

    # -- the pane -------------------------------------------------------
    def render(self):
        self._tick()
        if self.dialog:
            return TRUST_DIALOG_SCREEN
        # Every submitted copy stays on screen in the transcript above the
        # box. A whole-pane search would read that as "still unsent".
        transcript = []
        for _ in self.submitted:
            transcript.append(f'> {self.PROMPT}')
            transcript.append('● Working on it…')
        if not self.visible:
            return composer_screen(['>'], transcript)
        if self.placeholder:
            body = [f'[Pasted text #{i + 1} +14 lines]'
                    for i in range(self.visible)]
        else:
            body = [self.PROMPT] * self.visible
        body[0] = '> ' + body[0]
        return composer_screen(body, transcript)

    # -- tmux -----------------------------------------------------------
    def run(self, cmd, **kwargs):
        self._tick()
        argv = list(cmd)
        verb = argv[1] if len(argv) > 1 else ''
        if verb == 'capture-pane':
            return mock.Mock(returncode=0, stdout=self.render(), stderr='')
        if verb == 'paste-buffer':
            self.pastes += 1
            if self.drops > 0:
                self.drops -= 1
            else:
                self.pending.append(self.clock + self.paste_delay)
        elif verb == 'send-keys':
            keys = argv[4:]
            self.keys.append(tuple(keys))
            for key in keys:
                self._key(key)
        return mock.Mock(returncode=0, stdout='', stderr='')

    def _key(self, key):
        if key == 'Enter':
            self.enters += 1
            if self.dialog:
                self.dialog = False
                return
            if self.swallow > 0:
                self.swallow -= 1
                return
            self._tick()
            if self.visible:
                # Enter sends the WHOLE box as one message, however many
                # copies of the prompt are in it.
                self.submitted.append(self.visible)
                self.visible = 0
        elif key in ('C-u', 'Escape') and not self.unclearable:
            # Keys reach the TUI in order, so a clear also discards a paste
            # that was still queued behind it.
            self.visible = 0
            self.pending = []


class ComposerTextTests(unittest.TestCase):
    """Reading the input box REGION, not the whole screen (#805)."""

    def test_empty_box_is_empty_string_not_none(self):
        self.assertEqual(CTM._pane_composer_text(composer_screen(['>'])), '')

    def test_box_contents_are_returned_without_chrome(self):
        pane = composer_screen(['> do the thing'])
        self.assertEqual(CTM._pane_composer_text(pane), 'do the thing')

    def test_transcript_above_the_box_is_not_box_content(self):
        pane = composer_screen(['>'], transcript=['> do the thing', '● Working'])
        self.assertEqual(CTM._pane_composer_text(pane), '')

    def test_bare_affordance_without_a_box(self):
        self.assertEqual(CTM._pane_composer_text('❯ typed text\n? for shortcuts'),
                         'typed text')

    def test_no_input_box_at_all_is_unknown(self):
        self.assertIsNone(CTM._pane_composer_text(STARTUP_QUIET))
        self.assertIsNone(CTM._pane_composer_text(None))
        self.assertIsNone(CTM._pane_composer_text(''))

    def test_wrapped_paste_still_matches(self):
        needle = CTM._match_text('open a PR when the tests pass')
        pane = composer_screen(['> open a PR when the', 'tests pass'])
        self.assertEqual(CTM._paste_copies(pane, needle), 1)

    def test_counts_stacked_copies(self):
        needle = CTM._match_text('do the thing')
        pane = composer_screen(['> do the thing', 'do the thing', 'do the thing'])
        self.assertEqual(CTM._paste_copies(pane, needle), 3)

    def test_counts_paste_placeholders(self):
        # The real triple-paste evidence from #805: Claude shows a placeholder
        # for a long prompt, so the prompt's words are nowhere on screen.
        pane = composer_screen(['> [Pasted text #1 +14 lines]'
                                '[Pasted text #2 +7 lines]'])
        self.assertEqual(CTM._paste_copies(pane, 'nowhere-in-this-pane'), 2)

    def test_submitted_box_reads_as_zero_copies(self):
        needle = CTM._match_text('do the thing')
        pane = composer_screen(['>'], transcript=['> do the thing'])
        self.assertEqual(CTM._paste_copies(pane, needle), 0)

    def test_unreadable_box_is_minus_one(self):
        self.assertEqual(CTM._paste_copies(None, 'x'), -1)
        self.assertEqual(CTM._paste_copies(STARTUP_QUIET, 'x'), -1)

    def test_hint_text_without_a_needle_is_unknown_not_empty(self):
        # Claude draws greyed suggestion text in an empty box; without a
        # fingerprint that is not evidence either way.
        pane = composer_screen(['> Try "fix the typecheck errors"'])
        self.assertEqual(CTM._paste_copies(pane, None), -1)
        self.assertEqual(CTM._paste_copies(composer_screen(['>']), None), 0)


class PasteNeedleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'prompt.txt')

    def _write(self, text):
        with open(self.path, 'w') as f:
            f.write(text)
        return self.path

    def test_tail_is_the_fingerprint(self):
        needle = CTM._paste_needle(self._write('a' * 10 + 'TAILEND'),
                                   chars=7)
        self.assertEqual(needle, 'tailend')

    def test_whitespace_is_dropped_so_wrapping_cannot_break_it(self):
        self.assertEqual(CTM._paste_needle(self._write('Do\n The  Thing\n')),
                         'dothething')

    def test_unreadable_or_trivial_file_has_no_fingerprint(self):
        self.assertIsNone(CTM._paste_needle(os.path.join(self.tmp.name, 'nope')))
        self.assertIsNone(CTM._paste_needle(self._write('\n \n')))


class ClearComposerTests(unittest.TestCase):
    def _drive(self, sess):
        with mock.patch.object(server.subprocess, 'run', side_effect=sess.run), \
             mock.patch.object(server.time, 'sleep', side_effect=sess.sleep), \
             mock.patch.object(server.time, 'time', side_effect=sess.time):
            return CTM._clear_composer('sess', CTM._match_text(sess.PROMPT))

    def test_empty_box_costs_no_keys(self):
        sess = FakeSession()
        self.assertTrue(self._drive(sess))
        self.assertEqual(sess.keys, [])

    def test_stacked_box_is_emptied(self):
        sess = FakeSession(preloaded=3)
        self.assertTrue(self._drive(sess))
        self.assertEqual(sess.visible, 0)
        self.assertEqual(sess.keys[0], ('C-u',))

    def test_box_that_refuses_to_empty_reports_false(self):
        sess = FakeSession(preloaded=1, unclearable=True)
        self.assertFalse(self._drive(sess))
        self.assertEqual(len(sess.keys), len(CTM.COMPOSER_CLEAR_KEYS))


class DeliverPromptTests(unittest.TestCase):
    """Delivery against a scripted pane.

    #288 (a DROPPED paste must be pasted again) and #805 (a LATE paste must
    not be) pull in opposite directions, so each case here pins which one the
    screen is actually showing.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prompt_file = os.path.join(self.tmp.name, 'prompt.txt')
        with open(self.prompt_file, 'w') as f:
            f.write(FakeSession.PROMPT)

    def _deliver(self, sess, **kw):
        with mock.patch.object(server.subprocess, 'run', side_effect=sess.run), \
             mock.patch.object(server.time, 'sleep', side_effect=sess.sleep), \
             mock.patch.object(server.time, 'time', side_effect=sess.time):
            return CTM._deliver_prompt('sess', self.prompt_file, 'buf', **kw)

    def _launch(self, sess, **kw):
        with mock.patch.object(server.subprocess, 'run', side_effect=sess.run), \
             mock.patch.object(server.time, 'sleep', side_effect=sess.sleep), \
             mock.patch.object(server.time, 'time', side_effect=sess.time):
            return CTM._deliver_launch_prompt('sess', self.prompt_file, 'buf', **kw)

    def test_fast_start_pastes_once_and_submits(self):
        sess = FakeSession(paste_delay=0.0)
        self.assertTrue(self._deliver(sess))
        self.assertEqual(sess.pastes, 1)
        self.assertEqual(sess.submitted, [1])

    def test_slow_start_paste_is_late_not_lost(self):
        # The #805 case: the paste shows up 8s after `paste-buffer` returns,
        # long past the old 0.4s check. One paste, one copy, one Enter.
        sess = FakeSession(paste_delay=8.0)
        self.assertTrue(self._deliver(sess))
        self.assertEqual(sess.pastes, 1)
        self.assertEqual(sess.submitted, [1])
        self.assertEqual(sess.enters, 1)

    def test_slow_start_with_a_paste_placeholder(self):
        # A long prompt renders as `[Pasted text #1 +14 lines]`, so delivery
        # cannot look for the prompt's own words.
        sess = FakeSession(paste_delay=12.0, placeholder=True)
        self.assertTrue(self._deliver(sess))
        self.assertEqual(sess.pastes, 1)
        self.assertEqual(sess.submitted, [1])

    def test_dropped_paste_is_pasted_again(self):
        # #288 must not regress: the box is still empty at the deadline, so
        # this paste really was lost.
        sess = FakeSession(drops=1)
        self.assertTrue(self._deliver(sess, retries=3))
        self.assertEqual(sess.pastes, 2)
        self.assertEqual(sess.submitted, [1])  # exactly one copy sent

    def test_paste_that_never_appears_fails_without_submitting(self):
        sess = FakeSession(drops=99)
        self.assertFalse(self._deliver(sess, retries=3))
        self.assertEqual(sess.pastes, 3)
        self.assertEqual(sess.submitted, [])
        self.assertEqual(sess.enters, 0)
        # Bounded patience: the appear budget is spent once across attempts,
        # not once per attempt, so delivery stays inside the Board run's
        # quiet-settle window.
        self.assertLess(sess.clock, CTM.PASTE_APPEAR_TIMEOUT + 20)

    def test_swallowed_enter_gets_one_nudge(self):
        sess = FakeSession(paste_delay=1.0, swallow=1)
        self.assertTrue(self._deliver(sess))
        self.assertEqual(sess.enters, 2)
        self.assertEqual(sess.submitted, [1])
        self.assertEqual(sess.pastes, 1)

    def test_unsubmittable_box_is_reported_not_re_pasted(self):
        # Enter never registers. The prompt IS in the box; pasting again would
        # only stack a second copy of a prompt a human can still send.
        sess = FakeSession(swallow=99)
        self.assertFalse(self._deliver(sess))
        self.assertEqual(sess.pastes, 1)
        self.assertEqual(sess.visible, 1)
        self.assertEqual(sess.submitted, [])

    def test_recovers_a_box_that_already_holds_copies(self):
        # The pre-fix wreckage: three copies queued in the box. Delivery must
        # empty it first and send exactly one.
        sess = FakeSession(preloaded=3)
        self.assertTrue(self._deliver(sess))
        self.assertEqual(sess.submitted, [1])
        self.assertIn(('C-u',), sess.keys)

    def test_refuses_to_paste_onto_a_box_it_cannot_empty(self):
        sess = FakeSession(preloaded=1, unclearable=True)
        self.assertFalse(self._deliver(sess))
        self.assertEqual(sess.pastes, 0)
        self.assertEqual(sess.submitted, [])

    def test_paste_without_submit_skips_enter(self):
        sess = FakeSession(paste_delay=2.0)
        self.assertTrue(self._deliver(sess, submit=False))
        self.assertEqual(sess.enters, 0)
        self.assertEqual(sess.visible, 1)

    def test_trust_dialog_is_handed_back_rather_than_waited_out(self):
        # No composer is coming while the modal is up, so delivery must not
        # spend its whole appear budget on it (#800).
        sess = FakeSession(dialog=True)
        self.assertFalse(self._deliver(sess))
        self.assertEqual(sess.pastes, 1)
        self.assertLess(sess.clock, CTM.PASTE_APPEAR_TIMEOUT)

    def test_dialog_then_slow_start_delivers_once(self):
        # Both failures at once: the trust modal is up AND the paste that
        # follows it is late. One answered dialog, one paste, one submit.
        sess = FakeSession(paste_delay=6.0, dialog=True)
        delivered, saw_dialog = self._launch(sess)
        self.assertTrue(delivered)
        self.assertTrue(saw_dialog)
        self.assertEqual(sess.pastes, 1)
        self.assertEqual(sess.submitted, [1])

    def test_unobservable_session_is_not_pasted_into_twice(self):
        # Captures that keep failing cannot prove anything; re-pasting into a
        # session we cannot see is how text gets duplicated.
        def dead(cmd, **kw):
            return mock.Mock(returncode=1, stdout='', stderr='no server')

        with mock.patch.object(server.subprocess, 'run', side_effect=dead) as run, \
             mock.patch.object(server.time, 'sleep'), \
             mock.patch.object(server.time, 'time', side_effect=iter(range(500))):
            self.assertTrue(CTM._deliver_prompt('sess', self.prompt_file, 'buf'))
        pastes = [c for c in run.call_args_list if 'paste-buffer' in c.args[0]]
        self.assertEqual(len(pastes), 1)


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
