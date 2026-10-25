"""Folder-trust survival for freshly launched Builds (issue #800).

Three Builds once sat on Claude Code's folder-trust modal for 12 hours while
reporting `waiting-for-input` — the state a task that FINISHED also reports.
Their workdirs weren't in `~/.claude.json` at all: the pre-launch seed had
been clobbered by one of 8–10 concurrent CLIs rewriting that file wholesale.

Covered here, in the order the defences fire:

  * `_trust_accepted` / `_ensure_claude_trust` — seed, read the file BACK,
    re-seed while the entry is missing (the clobber half).
  * `_pane_shows_trust_dialog` — dialog detection against real pane fixtures
    (the half that doesn't care what the config says).
  * `_clear_trust_dialog` — answer `1` + Enter, confirm it went away.
  * `_deliver_launch_prompt` — the prompt gets delivered AFTER the dialog
    clears, instead of being a one-shot paste the dialog eats.
  * `_note_trust_stall` — the wedge is visible in the task record.

Run with:    python3 -m unittest tests.trust_dialog_watchdog_test
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


# ── pane fixtures ───────────────────────────────────────────────────────────
# Claude Code 2.1.x, verbatim shape: the modal over a drawn composer.
TRUST_DIALOG = (
    "╭──────────────────────────────────────────────────────────╮\n"
    "│ Quick safety check: Is this a project you created or one  │\n"
    "│ you trust?                                                │\n"
    "│                                                           │\n"
    "│ /home/dev/.worktrees/kube-coder/issue-800                 │\n"
    "│                                                           │\n"
    "│ ❯ 1. Yes, I trust this folder                             │\n"
    "│   2. No, exit                                             │\n"
    "╰──────────────────────────────────────────────────────────╯\n"
)

# An older CLI's wording — the question line differs, the answer line too.
TRUST_DIALOG_LEGACY = (
    "  Do you trust the files in this folder?\n"
    "  ❯ 1. Yes, proceed\n"
    "    2. No, exit\n"
)

# The same dialog with SGR colour, as `capture-pane -e` / output.log hold it.
TRUST_DIALOG_ANSI = (
    "\x1b[1m Quick safety check: Is this \x1b[0ma project you created or "
    "one you trust?\x1b[0m\n \x1b[36m❯ 1. Yes, I trust this folder\x1b[0m\n"
)

COMPOSER_READY = (
    "● Ready when you are.\n\n"
    "╭──────────────────────────────────────────────╮\n"
    "│ >                                            │\n"
    "╰──────────────────────────────────────────────╯\n"
    "  ? for shortcuts\n"
)

# Agent prose that mentions trust — must NOT trip the detector.
INNOCENT_OUTPUT = (
    "● I'll check whether this folder is one we can trust before writing.\n"
    "  Reading charts/workspace/server.py (1 of 2 files)\n"
    "  ? for shortcuts\n"
)

# Claude Code 2.1.310 reordered the dialog: the selector STARTS on "No, exit"
# and the `1.`/`2.` numbering is gone, so the historical `1` + Enter answer
# confirms the WRONG option and exits the CLI. Captured verbatim from a live
# 2.1.310 session (issue #810's compatibility check).
TRUST_DIALOG_NO_FIRST = (
    " Quick safety check: Is this a project you created or one you trust?"
    " (Like your own code, a well-known open source project, or work from"
    " your team). If not, take a moment to review what's in this folder"
    " first.\n"
    " Claude Code'll be able to read, edit, and execute files here.\n"
    " Security guide\n"
    " ❯ No, exit\n"
    "   Yes, I trust this folder\n"
    " Enter to confirm · Esc to cancel\n"
)

# The same dialog one Down-arrow later: selector on the affirmative option.
TRUST_DIALOG_NO_FIRST_YES_SELECTED = TRUST_DIALOG_NO_FIRST.replace(
    " ❯ No, exit\n   Yes, I trust this folder\n",
    "   No, exit\n ❯ Yes, I trust this folder\n")


class PaneShowsTrustDialogTests(unittest.TestCase):
    def test_current_dialog(self):
        self.assertTrue(CTM._pane_shows_trust_dialog(TRUST_DIALOG))

    def test_legacy_wording(self):
        self.assertTrue(CTM._pane_shows_trust_dialog(TRUST_DIALOG_LEGACY))

    def test_colour_escapes_are_stripped_first(self):
        # The question is split by an SGR reset mid-sentence; the affirmative
        # option line is what carries the match.
        self.assertTrue(CTM._pane_shows_trust_dialog(TRUST_DIALOG_ANSI))

    def test_idle_composer_is_not_a_dialog(self):
        self.assertFalse(CTM._pane_shows_trust_dialog(COMPOSER_READY))

    def test_agent_prose_about_trust_is_not_a_dialog(self):
        self.assertFalse(CTM._pane_shows_trust_dialog(INNOCENT_OUTPUT))

    def test_none_and_empty(self):
        self.assertFalse(CTM._pane_shows_trust_dialog(None))
        self.assertFalse(CTM._pane_shows_trust_dialog(''))

    def test_no_first_dialog_is_detected(self):
        self.assertTrue(CTM._pane_shows_trust_dialog(TRUST_DIALOG_NO_FIRST))


class TrustDialogAnswerKeysTests(unittest.TestCase):
    """The answer must be read off the pane, not assumed (#810): 2.1.310 put
    "No, exit" first and dropped the numbering, so the historical `1` + Enter
    confirms the wrong option and exits the CLI it was meant to rescue."""

    def test_yes_first_numbered_dialog_gets_plain_enter(self):
        # 2.1.233 shape: selector already on the affirmative option.
        self.assertEqual(('Enter',),
                         CTM._trust_dialog_answer_keys(TRUST_DIALOG))

    def test_no_first_dialog_gets_an_arrow_not_enter(self):
        # A bare Enter here would confirm "No, exit" and kill the task.
        self.assertEqual(('Down',),
                         CTM._trust_dialog_answer_keys(TRUST_DIALOG_NO_FIRST))

    def test_after_the_arrow_enter_confirms(self):
        self.assertEqual(
            ('Enter',),
            CTM._trust_dialog_answer_keys(TRUST_DIALOG_NO_FIRST_YES_SELECTED))

    def test_legacy_numbered_dialog_still_enters_on_selected_yes(self):
        # "Yes, proceed" wording, selector on it: Enter confirms it directly.
        self.assertEqual(('Enter',),
                         CTM._trust_dialog_answer_keys(TRUST_DIALOG_LEGACY))

    def test_selectorless_prompt_falls_back_to_the_historical_answer(self):
        pane = ("  Do you trust the files in this folder?\n"
                "  1. Yes, proceed\n  2. No, exit\n")
        self.assertEqual(('1', 'Enter'),
                         CTM._trust_dialog_answer_keys(pane))

    def test_ansi_and_box_drawing_do_not_hide_the_selector(self):
        pane = ("│ \x1b[36m❯ No, exit\x1b[0m        │\n"
                "│   Yes, I trust this folder │\n")
        self.assertEqual(('Down',), CTM._trust_dialog_answer_keys(pane))

    def test_yes_above_selector_moves_up(self):
        pane = ("   Yes, I trust this folder\n ❯ No, exit\n")
        self.assertEqual(('Up',), CTM._trust_dialog_answer_keys(pane))


class TrustAcceptedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, '.claude.json')

    def _write(self, obj):
        with open(self.cfg, 'w') as f:
            json.dump(obj, f)

    def test_true_when_seeded(self):
        self._write({'projects': {'/w': {'hasTrustDialogAccepted': True}}})
        self.assertTrue(CTM._trust_accepted('/w', config_path=self.cfg))

    def test_false_when_workdir_absent(self):
        # Exactly the clobbered state of #800: a big config, 300 other
        # projects, and ours simply not in it.
        self._write({'projects': {'/other': {'hasTrustDialogAccepted': True}}})
        self.assertFalse(CTM._trust_accepted('/w', config_path=self.cfg))

    def test_false_when_entry_says_not_accepted(self):
        self._write({'projects': {'/w': {'hasTrustDialogAccepted': False}}})
        self.assertFalse(CTM._trust_accepted('/w', config_path=self.cfg))

    def test_false_on_missing_invalid_or_odd_config(self):
        self.assertFalse(CTM._trust_accepted('/w', config_path=self.cfg))
        with open(self.cfg, 'w') as f:
            f.write('{truncated')
        self.assertFalse(CTM._trust_accepted('/w', config_path=self.cfg))
        self._write(['not', 'a', 'dict'])
        self.assertFalse(CTM._trust_accepted('/w', config_path=self.cfg))
        self._write({'projects': 'nonsense'})
        self.assertFalse(CTM._trust_accepted('/w', config_path=self.cfg))


class EnsureClaudeTrustRetryTests(unittest.TestCase):
    """Seed → read back → re-seed. The verdict is the file, not the write."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, '.claude.json')
        self.sleep = mock.patch.object(server.time, 'sleep')
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def test_one_round_when_the_seed_sticks(self):
        with mock.patch.object(CTM, '_write_claude_trust',
                               wraps=CTM._write_claude_trust) as w:
            self.assertTrue(CTM._ensure_claude_trust('/w', config_path=self.cfg))
        self.assertEqual(w.call_count, 1)
        with open(self.cfg) as f:
            self.assertIs(json.load(f)['projects']['/w']['hasTrustDialogAccepted'],
                          True)

    def test_already_trusted_is_true_even_though_nothing_is_written(self):
        # The old return value conflated "no write needed" with "failed";
        # a steady-state launch must now report trusted.
        CTM._write_claude_trust('/w', config_path=self.cfg)
        self.assertTrue(CTM._ensure_claude_trust('/w', config_path=self.cfg))

    def test_reseeds_when_a_concurrent_cli_clobbers_the_entry(self):
        calls = {'n': 0}
        real = CTM._write_claude_trust

        def clobbering_write(workdir, **kw):
            """Seed, then have 'another CLI' write the file back without us."""
            wrote = real(workdir, **kw)
            calls['n'] += 1
            if calls['n'] == 1:
                with open(self.cfg, 'w') as f:
                    json.dump({'hasCompletedOnboarding': True,
                               'projects': {'/other': {}}}, f)
            return wrote

        with mock.patch.object(CTM, '_write_claude_trust', clobbering_write):
            self.assertTrue(CTM._ensure_claude_trust('/w', config_path=self.cfg))
        self.assertEqual(calls['n'], 2)          # re-seeded after the clobber
        self.assertTrue(CTM._trust_accepted('/w', config_path=self.cfg))

    def test_gives_up_after_tries_and_reports_false(self):
        # A config that never keeps our entry (every write clobbered).
        def never_sticks(workdir, **kw):
            with open(self.cfg, 'w') as f:
                json.dump({'projects': {}}, f)
            return True

        with mock.patch.object(CTM, '_write_claude_trust', never_sticks) as _:
            self.assertFalse(
                CTM._ensure_claude_trust('/w', config_path=self.cfg, tries=3))

    def test_unwritable_config_is_false_not_an_exception(self):
        self.assertFalse(
            CTM._ensure_claude_trust('/w', config_path='/proc/nope/.claude.json',
                                     tries=2))

    def test_api_key_rejection_still_reaches_the_writer(self):
        CTM._ensure_claude_trust('/w', config_path=self.cfg,
                                 reject_api_key='sk-ant-' + 'x' * 30)
        with open(self.cfg) as f:
            cfg = json.load(f)
        self.assertEqual(cfg['customApiKeyResponses']['rejected'], ['x' * 20])


class WaitForPaneReadyStopsAtTheDialogTests(unittest.TestCase):
    """The composer never paints while the modal is up — don't wait for it."""

    def test_dialog_ends_the_wait_immediately(self):
        with mock.patch.object(CTM, '_capture_pane', return_value=TRUST_DIALOG) as cap, \
             mock.patch.object(server.time, 'sleep'):
            self.assertFalse(
                CTM._wait_for_pane_ready('sess', floor=0, ceiling=45, interval=0,
                                         expect_composer=True))
        self.assertEqual(cap.call_count, 1)   # not ~75 polls over 45s

    def test_a_dialog_that_appears_mid_wait_also_ends_it(self):
        panes = iter(['starting', 'still starting', TRUST_DIALOG, COMPOSER_READY])
        clock = iter(range(0, 200))
        with mock.patch.object(CTM, '_capture_pane', side_effect=lambda s: next(panes)), \
             mock.patch.object(server.time, 'sleep'), \
             mock.patch.object(server.time, 'time', side_effect=lambda: next(clock)):
            self.assertFalse(
                CTM._wait_for_pane_ready('sess', floor=0, ceiling=45, interval=0,
                                         expect_composer=True))

    def test_a_normal_cold_start_is_unaffected(self):
        panes = iter(['starting', COMPOSER_READY])
        clock = iter(range(0, 200))
        with mock.patch.object(CTM, '_capture_pane', side_effect=lambda s: next(panes)), \
             mock.patch.object(server.time, 'sleep'), \
             mock.patch.object(server.time, 'time', side_effect=lambda: next(clock)):
            self.assertTrue(
                CTM._wait_for_pane_ready('sess', floor=0, ceiling=45, interval=0,
                                         expect_composer=True))


class ClearTrustDialogTests(unittest.TestCase):
    def test_no_dialog_sends_no_keys(self):
        with mock.patch.object(CTM, '_capture_pane', return_value=COMPOSER_READY), \
             mock.patch.object(server.subprocess, 'run') as run, \
             mock.patch.object(server.time, 'sleep'):
            self.assertEqual(CTM._clear_trust_dialog('sess'), (False, True))
        run.assert_not_called()

    def test_answers_the_dialog_and_confirms(self):
        panes = iter([TRUST_DIALOG, COMPOSER_READY])
        with mock.patch.object(CTM, '_capture_pane', side_effect=lambda s: next(panes)), \
             mock.patch.object(server.subprocess, 'run',
                               return_value=mock.Mock(returncode=0)) as run, \
             mock.patch.object(server.time, 'sleep'):
            self.assertEqual(CTM._clear_trust_dialog('sess'), (True, True))
        # Selector already on the affirmative option -> a single Enter (the
        # historical `1` + Enter would also have worked on this layout, but
        # Enter is the one answer that is correct on every selector layout).
        self.assertEqual(run.call_args_list[-1].args[0],
                         ['tmux', 'send-keys', '-t', 'sess', 'Enter'])
        self.assertEqual(len(run.call_args_list), 1)

    def test_answers_the_no_first_dialog_without_exiting(self):
        # 2.1.310 layout (#810): arrow to "Yes, I trust this folder" first,
        # Enter only once the selector is on it — never Enter on "No, exit".
        panes = iter([TRUST_DIALOG_NO_FIRST, TRUST_DIALOG_NO_FIRST_YES_SELECTED,
                      COMPOSER_READY])
        with mock.patch.object(CTM, '_capture_pane', side_effect=lambda s: next(panes)), \
             mock.patch.object(server.subprocess, 'run',
                               return_value=mock.Mock(returncode=0)) as run, \
             mock.patch.object(server.time, 'sleep'):
            self.assertEqual(CTM._clear_trust_dialog('sess'), (True, True))
        sent = [c.args[0][4:] for c in run.call_args_list]
        self.assertEqual(sent, [['Down'], ['Enter']])

    def test_retries_a_dialog_that_does_not_take_the_first_answer(self):
        panes = iter([TRUST_DIALOG, TRUST_DIALOG, COMPOSER_READY])
        with mock.patch.object(CTM, '_capture_pane', side_effect=lambda s: next(panes)), \
             mock.patch.object(server.subprocess, 'run',
                               return_value=mock.Mock(returncode=0)) as run, \
             mock.patch.object(server.time, 'sleep'):
            self.assertEqual(CTM._clear_trust_dialog('sess'), (True, True))
        self.assertEqual(len(run.call_args_list), 2)

    def test_reports_not_cleared_when_the_dialog_never_goes(self):
        with mock.patch.object(CTM, '_capture_pane', return_value=TRUST_DIALOG), \
             mock.patch.object(server.subprocess, 'run',
                               return_value=mock.Mock(returncode=0)) as run, \
             mock.patch.object(server.time, 'sleep'):
            self.assertEqual(CTM._clear_trust_dialog('sess', tries=2),
                             (True, False))
        self.assertEqual(len(run.call_args_list), 2)   # bounded, not forever

    def test_unobservable_pane_is_not_a_dialog(self):
        with mock.patch.object(CTM, '_capture_pane', return_value=None), \
             mock.patch.object(server.subprocess, 'run') as run, \
             mock.patch.object(server.time, 'sleep'):
            self.assertEqual(CTM._clear_trust_dialog('sess'), (False, True))
        run.assert_not_called()


class DeliverLaunchPromptTests(unittest.TestCase):
    """The regression itself: the prompt must survive the dialog."""

    def test_clean_launch_delivers_once_and_sees_no_dialog(self):
        with mock.patch.object(CTM, '_wait_for_pane_ready', return_value=True), \
             mock.patch.object(CTM, '_capture_pane', return_value=COMPOSER_READY), \
             mock.patch.object(CTM, '_deliver_prompt', return_value=True) as dp:
            self.assertEqual(
                CTM._deliver_launch_prompt('sess', '/f', 'buf'), (True, False))
        self.assertEqual(dp.call_count, 1)

    def test_dialog_is_answered_before_the_paste(self):
        order = []
        with mock.patch.object(CTM, '_wait_for_pane_ready', return_value=True), \
             mock.patch.object(CTM, '_clear_trust_dialog',
                               side_effect=lambda *a, **k: (order.append('clear'),
                                                            (True, True))[1]), \
             mock.patch.object(CTM, '_capture_pane', return_value=COMPOSER_READY), \
             mock.patch.object(CTM, '_deliver_prompt',
                               side_effect=lambda *a, **k: (order.append('paste'),
                                                            True)[1]):
            self.assertEqual(
                CTM._deliver_launch_prompt('sess', '/f', 'buf'), (True, True))
        self.assertEqual(order, ['clear', 'paste'])

    def test_a_paste_the_dialog_ate_is_re_delivered(self):
        """The exact #800 sequence.

        Round 1: the dialog is up, our answer doesn't register in time, the
        paste goes into the modal (which "advances" the screen, so the old
        one-shot path called that delivered). Round 2: the dialog is gone and
        the prompt actually lands.
        """
        panes = iter([TRUST_DIALOG, COMPOSER_READY])
        clears = iter([(True, False), (False, True)])
        with mock.patch.object(CTM, '_wait_for_pane_ready', return_value=True), \
             mock.patch.object(CTM, '_clear_trust_dialog',
                               side_effect=lambda *a, **k: next(clears)), \
             mock.patch.object(CTM, '_capture_pane', side_effect=lambda s: next(panes)), \
             mock.patch.object(CTM, '_deliver_prompt', return_value=True) as dp:
            self.assertEqual(
                CTM._deliver_launch_prompt('sess', '/f', 'buf'), (True, True))
        self.assertEqual(dp.call_count, 2)   # re-delivered, not left unsent

    def test_undeliverable_prompt_behind_a_stuck_dialog_reports_both(self):
        with mock.patch.object(CTM, '_wait_for_pane_ready', return_value=True), \
             mock.patch.object(CTM, '_clear_trust_dialog', return_value=(True, False)), \
             mock.patch.object(CTM, '_capture_pane', return_value=TRUST_DIALOG), \
             mock.patch.object(CTM, '_deliver_prompt', return_value=False) as dp:
            self.assertEqual(
                CTM._deliver_launch_prompt('sess', '/f', 'buf', rounds=2),
                (False, True))
        self.assertEqual(dp.call_count, 2)   # bounded by `rounds`

    def test_failure_with_no_dialog_is_not_retried_again(self):
        # _deliver_prompt already retried its own paste three times; a second
        # round would just repeat it for nothing.
        with mock.patch.object(CTM, '_wait_for_pane_ready', return_value=True), \
             mock.patch.object(CTM, '_capture_pane', return_value=COMPOSER_READY), \
             mock.patch.object(CTM, '_deliver_prompt', return_value=False) as dp:
            self.assertEqual(
                CTM._deliver_launch_prompt('sess', '/f', 'buf'), (False, False))
        self.assertEqual(dp.call_count, 1)

    def test_non_claude_assistants_skip_the_dialog_path_entirely(self):
        with mock.patch.object(CTM, '_wait_for_pane_ready', return_value=True), \
             mock.patch.object(CTM, '_clear_trust_dialog') as clear, \
             mock.patch.object(CTM, '_capture_pane', return_value=TRUST_DIALOG), \
             mock.patch.object(CTM, '_deliver_prompt', return_value=True) as dp:
            self.assertEqual(
                CTM._deliver_launch_prompt('sess', '/f', 'buf',
                                           expect_composer=False), (True, False))
        clear.assert_not_called()
        self.assertEqual(dp.call_count, 1)


class NoteTrustStallTests(unittest.TestCase):
    """`waiting-for-input` alone can't tell "wedged" from "finished"."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.task_dir = os.path.join(self.tmp, 'task-1')
        os.makedirs(self.task_dir)
        self.meta = {'task_id': 'task-1', 'status': 'waiting-for-input'}
        self._save(self.meta)

    def _save(self, meta):
        with open(os.path.join(self.task_dir, 'task.json'), 'w') as f:
            json.dump(meta, f)

    def _stored(self):
        with open(os.path.join(self.task_dir, 'task.json')) as f:
            return json.load(f)

    def test_dialog_sets_the_flag_in_memory_and_on_disk(self):
        CTM._note_trust_stall(self.meta, self.task_dir, TRUST_DIALOG)
        self.assertIs(self.meta['stalled_at_trust_dialog'], True)
        self.assertIs(self._stored()['stalled_at_trust_dialog'], True)

    def test_a_cleared_dialog_retracts_the_flag(self):
        self.meta['stalled_at_trust_dialog'] = True
        self._save(self.meta)
        CTM._note_trust_stall(self.meta, self.task_dir, COMPOSER_READY)
        self.assertNotIn('stalled_at_trust_dialog', self.meta)
        self.assertNotIn('stalled_at_trust_dialog', self._stored())

    def test_steady_screen_writes_nothing(self):
        with mock.patch.object(CTM, '_atomic_update_meta') as upd:
            CTM._note_trust_stall(self.meta, self.task_dir, COMPOSER_READY)
            self.assertFalse(upd.called)
            self.meta['stalled_at_trust_dialog'] = True
            CTM._note_trust_stall(self.meta, self.task_dir, TRUST_DIALOG)
            self.assertFalse(upd.called)

    def test_a_vanished_task_dir_is_survivable(self):
        CTM._note_trust_stall({'status': 'running'}, os.path.join(self.tmp, 'gone'),
                              TRUST_DIALOG)

    def test_reconcile_flags_a_live_task_parked_on_the_dialog(self):
        meta = {'task_id': 'task-1', 'status': 'running',
                'tmux_session': 'kube-coder-task-1'}
        self._save(meta)

        def fake_run(argv, *a, **kw):
            if 'has-session' in argv:
                return mock.Mock(returncode=0, stdout='', stderr='')
            return mock.Mock(returncode=0, stdout=TRUST_DIALOG, stderr='')

        with mock.patch.object(server.subprocess, 'run', side_effect=fake_run), \
             mock.patch.object(CTM, 'ingest_usage', lambda *a, **k: None):
            CTM._reconcile_status(meta, self.task_dir)
        self.assertIs(self._stored()['stalled_at_trust_dialog'], True)


if __name__ == '__main__':
    unittest.main()
