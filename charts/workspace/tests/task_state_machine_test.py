"""The Build lifecycle as a state machine.

Every status change goes through ClaudeTaskManager._transition: one table of
what is legal, decided under the task lock, with the side effects run once
outside it. The tables below are spelled out by hand rather than read from
server.py, so a change to the legal moves has to change a test too.

tmux is simulated via subprocess.run stubs; TASKS_DIR is a tempdir; every side
effect is a mock, so nothing here reaches a real Feed, hook or worktree.

Run with:    python3 -m unittest tests.task_state_machine_test
(from charts/workspace/)
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402
import task_status  # noqa: E402
import worktrees  # noqa: E402

sys.path.insert(0, HERE)
from live_state import (  # noqa: E402
    isolate_feed_and_push,
    isolate_provider_keys,
    silence_prompt_delivery,
)

CTM = server.ClaudeTaskManager
RUNNING = 'running'
WAITING = 'waiting-for-input'
TERMINAL = ('completed', 'error', 'killed', 'terminated')
LIVE = (RUNNING, WAITING)
ALL = LIVE + TERMINAL

#: Exactly the moves the lifecycle allows. Everything else in ALL x ALL refuses.
ALLOWED = {
    (RUNNING, WAITING), (WAITING, RUNNING),
    *((RUNNING, t) for t in TERMINAL),
    *((WAITING, t) for t in TERMINAL),
}


def tmux(*dead_for, stderr=''):
    """subprocess.run stub: tmux verbs in `dead_for` exit 1, the rest exit 0."""
    def run(*args, **kwargs):
        argv = args[0] if args else kwargs.get('args', [])
        failed = len(argv) >= 2 and argv[0] == 'tmux' and argv[1] in dead_for
        return mock.Mock(returncode=1 if failed else 0, stdout='', stderr=stderr)
    return run


SESSION_GONE = tmux('has-session')
SESSION_ALIVE = tmux()


def kill_calls(run_mock):
    return [c for c in run_mock.call_args_list
            if c.args and c.args[0][:2] == ['tmux', 'kill-session']]


class Base(unittest.TestCase):
    def setUp(self):
        isolate_feed_and_push(self)
        isolate_provider_keys(self)
        silence_prompt_delivery(self)
        self.tmp = tempfile.mkdtemp(prefix='kcsm-')
        self._orig = CTM.TASKS_DIR
        CTM.TASKS_DIR = self.tmp
        self.addCleanup(self._restore)
        self.effects = {}
        for name, target, attr in (
                ('event', server.EventBroker, 'publish'),
                ('feed_terminal', server.FeedManager, 'emit_task_terminal'),
                ('feed_waiting', server.FeedManager, 'emit_task_waiting'),
                ('snapshot', server.WorktreeManager, 'snapshot_async'),
                ('hook', CTM, '_fire_completion_hook'),
                ('ingest', CTM, 'ingest_usage')):
            patcher = mock.patch.object(target, attr)
            self.effects[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def _restore(self):
        CTM.TASKS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _dir(self, tid):
        return os.path.join(self.tmp, tid)

    def _task(self, tid='t', **meta):
        os.makedirs(self._dir(tid), exist_ok=True)
        m = {'task_id': tid, 'tmux_session': f'kube-coder-{tid}'}
        m.update(meta)
        with open(os.path.join(self._dir(tid), 'task.json'), 'w') as f:
            json.dump(m, f)
        return self._dir(tid)

    def _read(self, tid='t'):
        with open(os.path.join(self._dir(tid), 'task.json')) as f:
            return json.load(f)

    def _raw(self, tid='t'):
        with open(os.path.join(self._dir(tid), 'task.json'), 'rb') as f:
            return f.read()

    def counts(self):
        return {k: v.call_count for k, v in self.effects.items()
                if v.call_count and k != 'ingest'}

    def statuses_published(self):
        return [c.args[1]['status'] for c in self.effects['event'].call_args_list
                if c.args[0] == 'task.status']


class TransitionTableTests(Base):
    """Every pair in ALL x ALL, through the one function that writes status."""

    def test_table_matches_the_lifecycle(self):
        seen = {(a, b) for a, table in task_status.TRANSITIONS.items() for b in table}
        self.assertEqual(seen, ALLOWED)

    def test_every_pair(self):
        for source in ALL:
            for target in ALL:
                with self.subTest(source=source, target=target):
                    self._check_pair(source, target)

    def _check_pair(self, source, target):
        tid = f'{source}-{target}'
        self._task(tid, status=source, finished_at=1.0 if source in TERMINAL else None)
        before = self._raw(tid)
        for m in self.effects.values():
            m.reset_mock()

        meta, moved = CTM._transition(self._dir(tid), target)

        if (source, target) in ALLOWED:
            self.assertTrue(moved)
            self.assertEqual(meta['status'], target)
            self.assertEqual(self._read(tid)['status'], target)
            self.assertEqual(self.statuses_published(), [target])
        else:
            self.assertFalse(moved)
            self.assertEqual(self._raw(tid), before, 'a refused move wrote')
            self.assertEqual(self.counts(), {}, 'a refused move had effects')

    def test_unknown_or_missing_status_cannot_move(self):
        for status in ('unknown', None):
            with self.subTest(status=status):
                self._task('u', status=status)
                _, moved = CTM._transition(self._dir('u'), 'killed')
                self.assertFalse(moved)
                self.assertEqual(self._read('u')['status'], status)

    def test_gone_task_reports_none(self):
        self.assertEqual(CTM._transition(self._dir('nope'), 'killed'), (None, False))

    def test_terminal_list_matches_the_worktree_sweep(self):
        # worktrees.py decides what may be removed from its own copy.
        self.assertEqual(set(task_status.TERMINAL_STATUSES),
                         set(worktrees.TERMINAL_STATUSES))

    def test_wire_values_are_exactly_these_strings(self):
        self.assertEqual(set(task_status.TERMINAL_STATUSES),
                         {'completed', 'error', 'killed', 'terminated'})
        self.assertEqual(set(task_status.TRANSITIONS), {'running', 'waiting-for-input'})


class TransitionEffectsTests(Base):
    def test_stamps_per_terminal_status(self):
        stamps = {'completed': {'finished_at'},
                  'error': {'finished_at'},
                  'killed': {'finished_at', 'killed_at'},
                  'terminated': {'finished_at', 'terminated_at'}}
        every = {'finished_at', 'killed_at', 'terminated_at'}
        for status, wanted in stamps.items():
            with self.subTest(status=status):
                self._task(status, status=RUNNING)
                CTM._transition(self._dir(status), status, at=42.0)
                meta = self._read(status)
                for field in wanted:
                    self.assertEqual(meta[field], 42.0)
                for field in every - wanted:
                    self.assertNotIn(field, meta)

    def test_leaving_waiting_clears_its_fields(self):
        for target in (RUNNING,) + TERMINAL:
            with self.subTest(target=target):
                self._task(target, status=WAITING, waiting_for_input=True,
                           last_input_prompt='ok?')
                CTM._transition(self._dir(target), target)
                meta = self._read(target)
                self.assertNotIn('waiting_for_input', meta)
                self.assertNotIn('last_input_prompt', meta)

    def test_entering_waiting_sets_the_flag(self):
        self._task(status=RUNNING)
        CTM._transition(self._dir('t'), WAITING)
        self.assertIs(self._read()['waiting_for_input'], True)

    def test_terminal_effects_run_once(self):
        for status in TERMINAL:
            with self.subTest(status=status):
                for m in self.effects.values():
                    m.reset_mock()
                self._task(status, status=RUNNING, worktree={'path': '/x'},
                           response_url='http://hook.example/cb')
                CTM._transition(self._dir(status), status)
                self.assertEqual(self.counts(), {
                    'event': 1, 'feed_terminal': 1, 'snapshot': 1, 'hook': 1})
                self.effects['feed_terminal'].assert_called_once()
                self.assertEqual(self.effects['feed_terminal'].call_args.args[1], status)
                event = self.effects['event'].call_args.args[1]
                self.assertEqual(event['finished_at'], self._read(status)['finished_at'])

    def test_no_snapshot_without_a_worktree(self):
        self._task(status=RUNNING)
        CTM._transition(self._dir('t'), 'completed')
        self.effects['snapshot'].assert_not_called()

    def test_waiting_effects(self):
        self._task(status=RUNNING, worktree={'path': '/x'},
                   response_url='http://hook.example/cb')
        CTM._transition(self._dir('t'), WAITING)
        self.assertEqual(self.counts(),
                         {'event': 1, 'feed_waiting': 1, 'snapshot': 1})
        self.assertNotIn('hook_fired_at', self._read())

    def test_back_to_running_only_publishes(self):
        self._task(status=WAITING, worktree={'path': '/x'})
        CTM._transition(self._dir('t'), RUNNING)
        self.assertEqual(self.counts(), {'event': 1})

    def test_hook_is_never_claimed_twice(self):
        self._task(status=RUNNING, response_url='http://hook.example/cb',
                   hook_fired_at=7.0)
        CTM._transition(self._dir('t'), 'completed')
        self.effects['hook'].assert_not_called()
        self.assertEqual(self._read()['hook_fired_at'], 7.0)

    def test_racing_writers_get_exactly_one_winner(self):
        self._task(status=RUNNING, response_url='http://hook.example/cb',
                   worktree={'path': '/x'})
        targets = ['completed', 'killed', 'error', 'terminated'] * 4
        barrier = threading.Barrier(len(targets))
        won = []

        def go(target):
            barrier.wait()
            if CTM._transition(self._dir('t'), target)[1]:
                won.append(target)

        threads = [threading.Thread(target=go, args=(t,)) for t in targets]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(won), 1)
        self.assertEqual(self._read()['status'], won[0])
        self.assertEqual(self.counts(), {
            'event': 1, 'feed_terminal': 1, 'snapshot': 1, 'hook': 1})

    def test_one_failing_effect_does_not_cost_the_others(self):
        self._task(status=RUNNING, worktree={'path': '/x'})
        self.effects['event'].side_effect = RuntimeError('broker down')
        _, moved = CTM._transition(self._dir('t'), 'completed')
        self.assertTrue(moved)
        self.effects['feed_terminal'].assert_called_once()
        self.effects['snapshot'].assert_called_once()

    def test_guard_refuses_without_writing(self):
        self._task(status=WAITING, prompt_delivered=True)
        before = self._raw()
        _, moved = CTM._transition(self._dir('t'), 'error',
                                   guard=lambda m: m['prompt_delivered'] is False)
        self.assertFalse(moved)
        self.assertEqual(self._raw(), before)

    def test_record_is_kept_when_the_status_cannot_move(self):
        self._task(status=RUNNING)
        _, moved = CTM._transition(self._dir('t'), RUNNING,
                                   record=lambda m: m.__setitem__('x', 1))
        self.assertFalse(moved)
        self.assertEqual(self._read()['x'], 1)
        self.assertEqual(self.counts(), {})

    def test_record_returning_false_abandons_the_write(self):
        self._task(status=WAITING)
        before = self._raw()
        _, moved = CTM._transition(self._dir('t'), RUNNING, record=lambda m: False)
        self.assertFalse(moved)
        self.assertEqual(self._raw(), before)


class KillTests(Base):
    def test_defect1_kill_has_the_effects_of_a_natural_finish(self):
        self._task(status=RUNNING, worktree={'path': '/x'},
                   response_url='http://hook.example/cb')
        with mock.patch.object(server.subprocess, 'run', side_effect=SESSION_ALIVE):
            CTM.delete_task('t')
        self.assertEqual(self.counts(), {
            'event': 1, 'feed_terminal': 1, 'snapshot': 1, 'hook': 1})
        self.assertEqual(self.effects['feed_terminal'].call_args.args[1], 'killed')
        self.assertEqual(self.statuses_published(), ['killed'])

    def test_kill_of_a_waiting_build(self):
        self._task(status=WAITING, waiting_for_input=True)
        with mock.patch.object(server.subprocess, 'run', side_effect=SESSION_ALIVE):
            CTM.delete_task('t')
        meta = self._read()
        self.assertEqual(meta['status'], 'killed')
        self.assertNotIn('waiting_for_input', meta)

    def test_kill_stamps_finished_at_equal_to_killed_at(self):
        self._task(status=RUNNING)
        with mock.patch.object(server.subprocess, 'run', side_effect=SESSION_ALIVE):
            updated = CTM.delete_task('t')
        self.assertEqual(updated['finished_at'], updated['killed_at'])

    def test_defect2_status_is_written_before_the_session_dies(self):
        self._task(status=RUNNING)
        seen = []

        def run(*args, **kwargs):
            if args[0][:2] == ['tmux', 'kill-session']:
                seen.append(self._read()['status'])
            return mock.Mock(returncode=0, stdout='', stderr='')

        with mock.patch.object(server.subprocess, 'run', side_effect=run):
            CTM.delete_task('t')
        self.assertEqual(seen, ['killed'])

    def test_defect2_reconciler_watching_the_session_die_cannot_win(self):
        # The reconciler runs at the instant the session disappears.
        task_dir = self._task(status=RUNNING)

        def run(*args, **kwargs):
            argv = args[0]
            if argv[:2] == ['tmux', 'kill-session']:
                CTM._reconcile_status(self._read(), task_dir)
            return mock.Mock(returncode=1 if argv[1] == 'has-session' else 0,
                             stdout='', stderr='')

        with mock.patch.object(server.subprocess, 'run', side_effect=run):
            CTM.delete_task('t')
        self.assertEqual(self._read()['status'], 'killed')
        self.assertEqual(self.statuses_published(), ['killed'])

    def test_defect3_deleting_a_finished_build_changes_nothing(self):
        for status in TERMINAL:
            with self.subTest(status=status):
                self._task(status, status=status, finished_at=100.0,
                           worktree={'path': '/x'},
                           response_url='http://hook.example/cb')
                before = self._raw(status)
                with mock.patch.object(server.subprocess, 'run',
                                       side_effect=SESSION_ALIVE):
                    returned = CTM.delete_task(status)
                self.assertEqual(returned['status'], status)
                self.assertEqual(self._raw(status), before)
                self.assertNotIn('killed_at', returned)
                self.assertEqual(self.counts(), {})

    def test_delete_retry_cleans_up_a_session_a_crash_left_behind(self):
        # Status goes first now, so a crash can leave killed + a live session.
        self._task(status='killed', killed_at=5.0)
        with mock.patch.object(server.subprocess, 'run',
                               side_effect=SESSION_ALIVE) as run:
            CTM.delete_task('t')
        self.assertEqual(len(kill_calls(run)), 1)

    def test_a_second_delete_repeats_nothing(self):
        self._task(status=RUNNING, response_url='http://hook.example/cb')
        with mock.patch.object(server.subprocess, 'run', side_effect=SESSION_ALIVE):
            first = CTM.delete_task('t')
            second = CTM.delete_task('t')
        self.assertEqual(first['killed_at'], second['killed_at'])
        self.assertEqual(self.effects['hook'].call_count, 1)
        self.assertEqual(self.statuses_published(), ['killed'])

    def test_missing_task_is_still_none(self):
        self.assertIsNone(CTM.delete_task('nope'))

    def test_spend_settles_on_the_next_read_not_mid_flush(self):
        # A kill signals the CLI, which may still be writing its transcript.
        task_dir = self._task(status=RUNNING)
        with mock.patch.object(server.subprocess, 'run', side_effect=SESSION_ALIVE):
            CTM.delete_task('t')
        self.effects['ingest'].assert_not_called()
        CTM._reconcile_status(self._read(), task_dir)
        self.effects['ingest'].assert_called_once()

    def test_killed_build_stays_on_the_mission_board(self):
        task_dir = self._task(status=RUNNING)
        with mock.patch.object(server.subprocess, 'run', side_effect=SESSION_ALIVE):
            CTM.delete_task('t')
        card = server._mc_task_card(self._read(), task_dir, time.time())
        self.assertEqual(card['state'], 'done')
        self.assertEqual(card['finished_at'], self._read()['killed_at'])

    def test_board_run_supersede_still_frees_the_idle_owner(self):
        self._task('owner', status=WAITING, board_id='b1', board_item_id='7')
        WM = server.WorktreeManager
        with mock.patch.object(WM, 'worktree_owner', return_value='owner'), \
             mock.patch.object(WM, 'liveness', return_value=lambda _: True), \
             mock.patch.object(server.BoardRunsManager, '_set_item') as set_item, \
             mock.patch.object(server.subprocess, 'run',
                               side_effect=SESSION_ALIVE) as run:
            server.BoardRunsManager._supersede_stale_owner(
                {'id': 'b1'}, {'id': 'r1'}, {'id': '7'}, None,
                {'worktree_slug': 's', 'workdir': '/w'})
        self.assertEqual(self._read('owner')['status'], 'killed')
        self.assertEqual(len(kill_calls(run)), 1)
        set_item.assert_called_once_with('r1', '7', superseded_task_id='owner')


class FollowupTests(Base):
    def _send(self, deliver=None, tid='t', **kwargs):
        with mock.patch.object(server.subprocess, 'run', side_effect=SESSION_ALIVE), \
             mock.patch.object(CTM, '_deliver_prompt',
                               side_effect=deliver or (lambda *a, **k: True)):
            return CTM.send_followup(tid, 'hi', **kwargs)

    def test_defect4_every_terminal_status_survives_a_racing_followup(self):
        for status in TERMINAL:
            with self.subTest(status=status):
                task_dir = self._task(status, status=WAITING, waiting_for_input=True)

                def ends_mid_paste(*a, **k):
                    CTM._transition(task_dir, status)
                    return True

                updated, err = self._send(ends_mid_paste, tid=status)
                self.assertIsNone(updated)
                self.assertEqual(err, 'Session is no longer running')
                self.assertEqual(self._read(status)['status'], status)
                self.assertNotIn('followups', self._read(status))
                with mock.patch.object(server.subprocess, 'run',
                                       side_effect=SESSION_GONE):
                    CTM._reconcile_status(self._read(status), task_dir)
                self.assertEqual(self._read(status)['status'], status)

    def test_board_resume_falls_through_when_killed_mid_paste(self):
        task_dir = self._task(status=WAITING)

        def killed_mid_paste(*a, **k):
            CTM._transition(task_dir, 'killed')
            return True

        with mock.patch.object(server.subprocess, 'run', side_effect=SESSION_ALIVE), \
             mock.patch.object(CTM, '_deliver_prompt', side_effect=killed_mid_paste), \
             mock.patch.object(server.time, 'sleep') as sleep:
            got = server.BoardRunsManager._resume_in_place(
                {'task_id': 't', 'note': 'please revise'})
        self.assertEqual(got, '')
        sleep.assert_not_called()   # a dead session is not worth a retry

    def test_finished_build_is_refused_before_anything_is_pasted(self):
        # Even with a session still standing, e.g. a kill landing right now.
        self._task(status='killed')
        with mock.patch.object(server.subprocess, 'run', side_effect=SESSION_ALIVE), \
             mock.patch.object(CTM, '_deliver_prompt') as deliver:
            updated, err = CTM.send_followup('t', 'hi')
        deliver.assert_not_called()
        self.assertIsNone(updated)
        self.assertEqual(err, 'Session is no longer running')

    def test_defect5_waiting_to_running_is_published_and_clears_waiting(self):
        self._task(status=WAITING, waiting_for_input=True, last_input_prompt='ok?')
        updated, err = self._send()
        self.assertIsNone(err)
        self.assertEqual(updated['status'], RUNNING)
        meta = self._read()
        self.assertNotIn('waiting_for_input', meta)
        self.assertNotIn('last_input_prompt', meta)
        self.assertEqual(self.statuses_published(), [RUNNING])
        self.assertEqual([f['prompt'] for f in meta['followups']], ['hi'])

    def test_followup_while_running_records_without_an_event(self):
        self._task(status=RUNNING)
        updated, err = self._send()
        self.assertIsNone(err)
        self.assertEqual(len(updated['followups']), 1)
        self.assertEqual(self.statuses_published(), [])

    def test_paste_only_changes_nothing(self):
        self._task(status=WAITING, waiting_for_input=True)
        updated, err = self._send(submit=False)
        self.assertIsNone(err)
        self.assertEqual(updated['status'], WAITING)
        self.assertNotIn('followups', self._read())
        self.assertEqual(self.statuses_published(), [])


class ReconcilerTests(Base):
    def test_session_gone_completes_and_settles_spend_once(self):
        task_dir = self._task(status=WAITING, waiting_for_input=True,
                              worktree={'path': '/x'},
                              response_url='http://hook.example/cb')
        meta = self._read()
        with mock.patch.object(server.subprocess, 'run', side_effect=SESSION_GONE):
            CTM._reconcile_status(meta, task_dir)
        self.assertEqual(meta['status'], 'completed')
        self.assertNotIn('waiting_for_input', meta)
        self.assertEqual(self.counts(), {
            'event': 1, 'feed_terminal': 1, 'snapshot': 1, 'hook': 1})
        # Once at the top of the pass, once to settle after the transition.
        self.assertEqual(self.effects['ingest'].call_count, 2)
        self.assertEqual(self.effects['ingest'].call_args.args[0]['status'], 'completed')

    def test_pod_restart_keeps_its_stamps(self):
        task_dir = self._task(status=RUNNING, boot_id='old')
        meta = self._read()
        with mock.patch.object(CTM, 'boot_id', return_value='new'), \
             mock.patch.object(server.subprocess, 'run', side_effect=SESSION_GONE):
            CTM._reconcile_status(meta, task_dir)
        self.assertEqual(meta['status'], 'terminated')
        self.assertEqual(meta['terminated_at'], meta['finished_at'])
        self.assertEqual(meta['termination_reason'], 'pod_restart')
        self.assertEqual(self._read()['termination_reason'], 'pod_restart')

    def test_stale_reader_repeats_no_effects_and_learns_the_truth(self):
        # Read as running, but another writer finished it before the lock.
        task_dir = self._task(status=RUNNING)
        stale = self._read()
        with mock.patch.object(server.subprocess, 'run', side_effect=SESSION_ALIVE):
            CTM.delete_task('t')
        for m in self.effects.values():
            m.reset_mock()
        with mock.patch.object(server.subprocess, 'run', side_effect=SESSION_GONE):
            CTM._reconcile_status(stale, task_dir)
        self.assertEqual(self.counts(), {})
        self.assertEqual(stale['status'], 'killed')
        self.assertEqual(self._read()['status'], 'killed')

    def _alive_screen(self):
        return mock.Mock(returncode=0, stdout='screen', stderr='')

    def test_screen_change_returns_a_waiting_build_to_running(self):
        task_dir = self._task(status=WAITING, waiting_for_input=True,
                              pane_hash='old', boot_id='')
        meta = self._read()
        with mock.patch.object(CTM, 'boot_id', return_value=''), \
             mock.patch.object(server.subprocess, 'run',
                               return_value=self._alive_screen()):
            CTM._reconcile_status(meta, task_dir)
        self.assertEqual(meta['status'], RUNNING)
        self.assertNotIn('waiting_for_input', self._read())
        self.assertNotEqual(self._read()['pane_hash'], 'old')
        self.assertEqual(self.statuses_published(), [RUNNING])

    def test_screen_change_while_running_only_records_activity(self):
        task_dir = self._task(status=RUNNING, pane_hash='old', boot_id='')
        with mock.patch.object(CTM, 'boot_id', return_value=''), \
             mock.patch.object(server.subprocess, 'run',
                               return_value=self._alive_screen()):
            CTM._reconcile_status(self._read(), task_dir)
        self.assertNotEqual(self._read()['pane_hash'], 'old')
        self.assertEqual(self.statuses_published(), [])

    def test_quiet_screen_parks_a_running_build(self):
        import hashlib
        digest = hashlib.sha1(b'screen').hexdigest()
        task_dir = self._task(status=RUNNING, pane_hash=digest, boot_id='',
                              last_activity_at=time.time() - 3600)
        meta = self._read()
        with mock.patch.object(CTM, 'boot_id', return_value=''), \
             mock.patch.object(server.subprocess, 'run',
                               return_value=self._alive_screen()):
            CTM._reconcile_status(meta, task_dir)
        self.assertEqual(meta['status'], WAITING)
        self.assertIs(meta['waiting_for_input'], True)
        self.assertEqual(self.counts(), {'event': 1, 'feed_waiting': 1})

    def test_a_launching_build_is_not_judged_by_its_missing_session(self):
        task_dir = self._task(status=RUNNING)
        CTM._launching.add('t')
        self.addCleanup(CTM._launching.discard, 't')
        with mock.patch.object(server.subprocess, 'run', side_effect=SESSION_GONE):
            CTM._reconcile_status(self._read(), task_dir)
        self.assertEqual(self._read()['status'], RUNNING)
        self.assertEqual(self.counts(), {})


class LaunchFailureTests(Base):
    FAILED = staticmethod(tmux('new-session', stderr='no server'))

    def _create(self, **kwargs):
        with mock.patch.object(server.subprocess, 'run', side_effect=self.FAILED), \
             mock.patch.object(CTM, 'at_capacity', return_value=(False, 0, 0)):
            return CTM.create_task('do it', assistant='opencode-openrouter',
                                   disable_memory_injection=True, **kwargs)

    def _terminal(self):
        with mock.patch.object(server.subprocess, 'run', side_effect=self.FAILED), \
             mock.patch.object(CTM, 'at_capacity', return_value=(False, 0, 0)):
            return CTM.create_terminal_task('/tmp')

    def _assert_failed_launch(self, meta):
        tid = meta['task_id']
        disk = self._read(tid)
        self.assertEqual(disk['status'], 'error')
        self.assertEqual(disk['error'], 'no server')
        self.assertEqual(meta['status'], 'error')
        self.assertEqual(meta['error'], 'no server')
        self.assertEqual(disk['finished_at'], meta['finished_at'])
        self.assertEqual(self.statuses_published(), ['error'])
        self.effects['feed_terminal'].assert_called_once()
        self.assertEqual(self.effects['feed_terminal'].call_args.args[1], 'error')
        self.assertFalse(os.path.exists(
            os.path.join(self._dir(tid), 'task.json.tmp')))

    def test_defect6_build(self):
        meta = self._create(response_url='http://hook.example/cb')
        self._assert_failed_launch(meta)
        self.effects['hook'].assert_called_once()
        self.assertIn('hook_fired_at', self._read(meta['task_id']))

    def test_defect6_terminal_task(self):
        self._assert_failed_launch(self._terminal())

    def test_defect6_write_goes_through_the_locked_update(self):
        with mock.patch.object(CTM, '_atomic_update_meta',
                               wraps=CTM._atomic_update_meta) as update:
            meta = self._terminal()
        self.assertEqual(self._read(meta['task_id'])['status'], 'error')
        update.assert_called()

    def test_defect6_mission_control_shows_a_failed_launch(self):
        meta = self._terminal()
        card = server._mc_task_card(self._read(meta['task_id']),
                                    self._dir(meta['task_id']), time.time())
        self.assertIsNotNone(card)
        self.assertEqual(card['state'], 'done')

    def test_reconciler_during_launch_cannot_pre_empt_the_error(self):
        # tmux is slow to refuse, and a dashboard poll lands meanwhile.
        def run(*args, **kwargs):
            argv = args[0]
            if argv[:2] == ['tmux', 'new-session']:
                tid = next(iter(os.listdir(self.tmp)))
                CTM._reconcile_status(self._read(tid), self._dir(tid))
                return mock.Mock(returncode=1, stdout='', stderr='no server')
            return mock.Mock(returncode=1 if argv[1] == 'has-session' else 0,
                             stdout='', stderr='')

        with mock.patch.object(server.subprocess, 'run', side_effect=run), \
             mock.patch.object(CTM, 'at_capacity', return_value=(False, 0, 0)):
            meta = CTM.create_terminal_task('/tmp')
        self.assertEqual(self._read(meta['task_id'])['status'], 'error')
        self.assertEqual(self.statuses_published(), ['error'])

    def test_launch_window_closes_even_when_the_launch_raises(self):
        with mock.patch.object(CTM, '_launch_task', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                CTM._spawn_task('t1')
        self.assertNotIn('t1', CTM._launching)

    def test_a_kill_during_launch_is_not_overwritten(self):
        # The launch fails after the user has already killed the Build.
        def run(*args, **kwargs):
            argv = args[0]
            if argv[:2] == ['tmux', 'new-session']:
                tid = next(iter(os.listdir(self.tmp)))
                CTM._transition(self._dir(tid), 'killed')
                return mock.Mock(returncode=1, stdout='', stderr='no server')
            return mock.Mock(returncode=0, stdout='', stderr='')

        with mock.patch.object(server.subprocess, 'run', side_effect=run), \
             mock.patch.object(CTM, 'at_capacity', return_value=(False, 0, 0)):
            meta = CTM.create_terminal_task('/tmp')
        self.assertEqual(meta['status'], 'killed')
        self.assertEqual(self._read(meta['task_id'])['status'], 'killed')

    def test_a_successful_launch_writes_no_status_change(self):
        with mock.patch.object(server.subprocess, 'run', side_effect=SESSION_ALIVE), \
             mock.patch.object(CTM, 'at_capacity', return_value=(False, 0, 0)):
            meta = CTM.create_terminal_task('/tmp')
        self.assertEqual(meta['status'], RUNNING)
        self.assertEqual(self.statuses_published(), [])
        self.assertNotIn(meta['task_id'], CTM._launching)


if __name__ == '__main__':
    unittest.main()
