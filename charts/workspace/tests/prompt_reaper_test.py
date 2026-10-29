"""Unit tests for the undelivered-prompt reaper (issue #813).

A Build whose first prompt never reached the agent idles at `waiting-for-input`
forever and, in numbers, pins every slot under KC_MAX_TASKS. The reaper
re-delivers a bounded number of times and then fails the Build so the slot is
freed -- deciding ONLY from task.json (`prompt_delivered`, `status`), never
from captured screen text.

tmux is simulated via a subprocess.run stub that records every argv; TASKS_DIR
is a tempdir; the completion hook, feed and delivery path are mocked.

Run with:    python3 -m unittest tests.prompt_reaper_test
(from charts/workspace/)
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(HERE)
sys.path.insert(0, WORKSPACE)
import server  # noqa: E402

CTM = server.ClaudeTaskManager

NOW = 10_000.0
IDLE = NOW - 10_000  # idle far longer than any grace


class _InlineThread:
    """threading.Thread stand-in that runs its target immediately."""

    def __init__(self, target=None, args=(), kwargs=None, **_):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class FakeTmux:
    """subprocess.run stub: `alive` controls has-session; every argv is kept."""

    def __init__(self, alive=True):
        self.alive = alive
        self.calls = []

    def __call__(self, cmd, **kwargs):
        argv = list(cmd)
        self.calls.append(argv)
        if argv[:2] == ['tmux', 'has-session']:
            return mock.Mock(returncode=0 if self.alive else 1,
                             stdout='', stderr='')
        return mock.Mock(returncode=0, stdout='', stderr='')

    def verbs(self):
        return [c[1] for c in self.calls if len(c) > 1]


class ReaperBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kcreap-')
        self._orig = CTM.TASKS_DIR
        CTM.TASKS_DIR = self.tmp
        self.addCleanup(self._restore)
        for name, value in (('PROMPT_REAP_GRACE', 120.0),
                            ('PROMPT_REAP_RETRIES', 3),
                            ('PROMPT_REAP_RETRY_GAP', 60.0)):
            p = mock.patch.object(CTM, name, value)
            p.start()
            self.addCleanup(p.stop)
        for target in ('_fire_completion_hook',):
            p = mock.patch.object(CTM, target)
            setattr(self, target.strip('_'), p.start())
            self.addCleanup(p.stop)
        p = mock.patch.object(server.FeedManager, 'emit_task_terminal')
        self.feed = p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(server.threading, 'Thread', _InlineThread)
        p.start()
        self.addCleanup(p.stop)
        CTM._redelivering.clear()

    def _restore(self):
        CTM.TASKS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _task(self, tid, **meta):
        d = os.path.join(self.tmp, tid)
        os.makedirs(d, exist_ok=True)
        m = {'task_id': tid, 'tmux_session': f'kube-coder-{tid}',
             'status': 'waiting-for-input', 'prompt_delivered': False,
             'last_activity_at': IDLE, 'assistant': 'claude'}
        m.update(meta)
        with open(os.path.join(d, 'task.json'), 'w') as f:
            json.dump(m, f)
        with open(os.path.join(d, 'prompt.txt'), 'w') as f:
            f.write('do the thing')
        return d

    def _read(self, tid):
        with open(os.path.join(self.tmp, tid, 'task.json')) as f:
            return json.load(f)

    def _reap(self, tmux=None, delivers=False, now=NOW):
        tmux = tmux or FakeTmux()
        with mock.patch.object(server.subprocess, 'run', side_effect=tmux), \
             mock.patch.object(CTM, '_deliver_launch_prompt',
                               return_value=(delivers, False)) as deliver:
            out = CTM.reap_undelivered(now=now)
        return out, tmux, deliver


class CandidateTests(ReaperBase):
    """What the reaper may touch: only the server's own recorded state."""

    def test_idle_with_undelivered_prompt_is_a_candidate(self):
        self.assertTrue(CTM._reap_candidate(
            {'status': 'waiting-for-input', 'prompt_delivered': False,
             'last_activity_at': IDLE}, NOW))

    def test_delivered_prompt_is_never_touched(self):
        # A Build that finished its work idles here too; that is NOT stranded.
        self.assertFalse(CTM._reap_candidate(
            {'status': 'waiting-for-input', 'prompt_delivered': True,
             'last_activity_at': IDLE}, NOW))

    def test_absent_prompt_delivered_means_still_in_flight(self):
        self.assertFalse(CTM._reap_candidate(
            {'status': 'waiting-for-input', 'last_activity_at': IDLE}, NOW))
        self.assertFalse(CTM._reap_candidate(
            {'status': 'waiting-for-input', 'prompt_delivered': None,
             'last_activity_at': IDLE}, NOW))

    def test_running_and_terminal_builds_are_not_candidates(self):
        for status in ('running', 'completed', 'error', 'killed', 'terminated'):
            self.assertFalse(CTM._reap_candidate(
                {'status': status, 'prompt_delivered': False,
                 'last_activity_at': IDLE}, NOW), status)

    def test_a_session_that_took_a_followup_is_being_driven(self):
        meta = {'status': 'waiting-for-input', 'prompt_delivered': False,
                'last_activity_at': IDLE,
                'followups': [{'prompt': 'carry on', 'sent_at': IDLE}]}
        self.assertFalse(CTM._reap_candidate(meta, NOW))
        meta['followups'] = []
        self.assertTrue(CTM._reap_candidate(meta, NOW))

    def test_grace_period_protects_a_fresh_idle(self):
        meta = {'status': 'waiting-for-input', 'prompt_delivered': False,
                'last_activity_at': NOW - 119}
        self.assertFalse(CTM._reap_candidate(meta, NOW))
        meta['last_activity_at'] = NOW - 120
        self.assertTrue(CTM._reap_candidate(meta, NOW))

    def test_decision_never_reads_the_screen(self):
        # The field note behind #813: an EMPTY composer shows greyed ghost
        # suggestion text that looks like static content. Whatever the pane
        # says, the reaper must not so much as capture it.
        self._task('t1')
        _, tmux, _ = self._reap()
        self.assertNotIn('capture-pane', tmux.verbs())
        self.assertNotIn('send-keys', tmux.verbs())


class RetryTests(ReaperBase):
    def test_first_pass_redelivers_instead_of_failing(self):
        self._task('t1')
        out, tmux, deliver = self._reap(delivers=True)
        self.assertEqual(out, {'retried': 1, 'errored': 0})
        deliver.assert_called_once()
        session, prompt_file, buf = deliver.call_args.args
        self.assertEqual(session, 'kube-coder-t1')
        self.assertTrue(prompt_file.endswith(os.path.join('t1', 'prompt.txt')))
        self.assertTrue(deliver.call_args.kwargs['expect_composer'])
        self.assertNotIn('kill-session', tmux.verbs())

    def test_successful_redelivery_records_the_prompt_as_delivered(self):
        self._task('t1')
        self._reap(delivers=True)
        meta = self._read('t1')
        self.assertIs(meta['prompt_delivered'], True)
        self.assertEqual(meta['prompt_redelivery_attempts'], 1)
        self.assertIn('prompt_redelivered_at', meta)
        self.assertEqual(meta['status'], 'waiting-for-input')  # reconciler flips it

    def test_failed_redelivery_leaves_it_undelivered_and_counted(self):
        self._task('t1')
        self._reap(delivers=False)
        meta = self._read('t1')
        self.assertIs(meta['prompt_delivered'], False)
        self.assertEqual(meta['prompt_redelivery_attempts'], 1)

    def test_non_claude_assistants_do_not_wait_for_a_composer(self):
        self._task('t1', assistant='opencode')
        _, _, deliver = self._reap()
        self.assertFalse(deliver.call_args.kwargs['expect_composer'])

    def test_retries_are_spaced_by_the_gap(self):
        self._task('t1')
        self._reap(now=NOW)
        out, _, deliver = self._reap(now=NOW + 30)
        self.assertEqual(out['retried'], 0)
        deliver.assert_not_called()
        out, _, deliver = self._reap(now=NOW + 61)
        self.assertEqual(out['retried'], 1)
        self.assertEqual(self._read('t1')['prompt_redelivery_attempts'], 2)

    def test_in_flight_redelivery_is_not_started_twice(self):
        self._task('t1')
        CTM._redelivering.add('t1')
        out, _, deliver = self._reap()
        self.assertEqual(out, {'retried': 0, 'errored': 0})
        deliver.assert_not_called()

    def test_redelivery_claim_is_released_afterwards(self):
        self._task('t1')
        self._reap()
        self.assertNotIn('t1', CTM._redelivering)

    def test_a_crash_in_delivery_still_spends_the_attempt(self):
        self._task('t1')
        tmux = FakeTmux()
        with mock.patch.object(server.subprocess, 'run', side_effect=tmux), \
             mock.patch.object(CTM, '_deliver_launch_prompt',
                               side_effect=RuntimeError('boom')):
            out = CTM.reap_undelivered(now=NOW)
        self.assertEqual(out['retried'], 1)
        self.assertEqual(self._read('t1')['prompt_redelivery_attempts'], 1)
        self.assertNotIn('t1', CTM._redelivering)


class FailTests(ReaperBase):
    def _exhausted(self, tid='t1', **meta):
        return self._task(tid, prompt_redelivery_attempts=3,
                          prompt_redelivery_at=NOW - 1000, **meta)

    def test_exhausted_retries_fail_the_build_and_free_its_slot(self):
        self._exhausted()
        out, tmux, deliver = self._reap()
        self.assertEqual(out, {'retried': 0, 'errored': 1})
        deliver.assert_not_called()
        meta = self._read('t1')
        self.assertEqual(meta['status'], 'error')
        self.assertIn('never delivered', meta['error'])
        self.assertIn('3 re-delivery', meta['error'])
        self.assertIn('finished_at', meta)
        self.assertIn('prompt_undelivered_reaped_at', meta)
        self.assertNotIn('waiting_for_input', meta)
        kills = [c for c in tmux.calls if c[1] == 'kill-session']
        self.assertEqual(kills, [['tmux', 'kill-session', '-t', 'kube-coder-t1']])

    def test_it_takes_the_full_budget_to_get_there(self):
        # End to end across passes: three failed re-deliveries, then the fail.
        self._task('t1')
        for i in range(3):
            out, _, _ = self._reap(now=NOW + 100 * i)
            self.assertEqual(out, {'retried': 1, 'errored': 0})
        self.assertEqual(self._read('t1')['status'], 'waiting-for-input')
        out, tmux, _ = self._reap(now=NOW + 400)
        self.assertEqual(out, {'retried': 0, 'errored': 1})
        self.assertEqual(self._read('t1')['status'], 'error')
        self.assertIn('kill-session', tmux.verbs())

    def test_failing_fires_the_completion_hook_once_and_the_feed(self):
        self._exhausted(response_url='http://hook.example/cb')
        self._reap()
        self.fire_completion_hook.assert_called_once()
        self.assertEqual(self.fire_completion_hook.call_args.args[0]['status'],
                         'error')
        self.assertIn('hook_fired_at', self._read('t1'))
        self.feed.assert_called_once()
        self.assertEqual(self.feed.call_args.args[1], 'error')
        # A second pass finds a terminal Build and does nothing.
        out, _, _ = self._reap()
        self.assertEqual(out, {'retried': 0, 'errored': 0})
        self.fire_completion_hook.assert_called_once()

    def test_no_hook_without_a_response_url(self):
        self._exhausted()
        self._reap()
        self.fire_completion_hook.assert_not_called()

    def test_a_build_that_recovered_meanwhile_is_not_failed(self):
        # Re-checked inside the lock: the screen changed and the reconciler
        # flipped it to running between the scan and the write.
        d = self._exhausted()
        real = CTM._atomic_update_meta

        def racing(task_dir, mutate):
            with open(os.path.join(d, 'task.json')) as f:
                m = json.load(f)
            m['status'] = 'running'
            with open(os.path.join(d, 'task.json'), 'w') as f:
                json.dump(m, f)
            return real(task_dir, mutate)

        tmux = FakeTmux()
        with mock.patch.object(server.subprocess, 'run', side_effect=tmux), \
             mock.patch.object(CTM, '_atomic_update_meta', side_effect=racing):
            out = CTM.reap_undelivered(now=NOW)
        self.assertEqual(out['errored'], 0)
        self.assertEqual(self._read('t1')['status'], 'running')
        self.assertNotIn('kill-session', tmux.verbs())
        self.feed.assert_not_called()


class SkipTests(ReaperBase):
    def test_a_gone_session_is_left_to_the_reconciler(self):
        self._task('t1')
        self._task('t2', prompt_redelivery_attempts=3,
                   prompt_redelivery_at=NOW - 1000)
        out, tmux, deliver = self._reap(tmux=FakeTmux(alive=False))
        self.assertEqual(out, {'retried': 0, 'errored': 0})
        deliver.assert_not_called()
        self.assertNotIn('kill-session', tmux.verbs())
        self.assertEqual(self._read('t2')['status'], 'waiting-for-input')

    def test_healthy_builds_are_untouched(self):
        self._task('done', prompt_delivered=True)
        self._task('busy', status='running')
        self._task('fresh', last_activity_at=NOW - 5)
        self._task('legacy')
        legacy = os.path.join(self.tmp, 'legacy', 'task.json')
        with open(legacy) as f:
            m = json.load(f)
        del m['prompt_delivered']
        with open(legacy, 'w') as f:
            json.dump(m, f)
        out, tmux, deliver = self._reap()
        self.assertEqual(out, {'retried': 0, 'errored': 0})
        deliver.assert_not_called()
        self.assertEqual(tmux.calls, [])

    def test_unreadable_or_malformed_task_dirs_are_skipped(self):
        os.makedirs(os.path.join(self.tmp, 'empty'))
        bad = os.path.join(self.tmp, 'bad')
        os.makedirs(bad)
        with open(os.path.join(bad, 'task.json'), 'w') as f:
            f.write('{not json')
        self._task('t1')
        out, _, _ = self._reap()
        self.assertEqual(out['retried'], 1)

    def test_missing_tasks_dir_is_zero(self):
        shutil.rmtree(self.tmp)
        with mock.patch.object(CTM, 'ensure_tasks_dir'):
            self.assertEqual(CTM.reap_undelivered(now=NOW),
                             {'retried': 0, 'errored': 0})


class ConfigTests(unittest.TestCase):
    """The reaper is config-gated and every knob survives a bad value."""

    def _eval(self, expr, **env):
        e = {k: v for k, v in os.environ.items() if not k.startswith('KC_PROMPT')}
        e.update(env)
        r = subprocess.run(
            [sys.executable, '-c', f'import server; print({expr})'],
            cwd=WORKSPACE, env=e, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip().splitlines()[-1]

    def test_on_by_default(self):
        self.assertEqual(
            self._eval('server.ClaudeTaskManager.PROMPT_REAPER_ENABLED'), 'True')

    def test_switched_off(self):
        for off in ('0', 'false', 'off', 'no'):
            self.assertEqual(
                self._eval('server.ClaudeTaskManager.PROMPT_REAPER_ENABLED',
                           KC_PROMPT_REAPER=off), 'False', off)

    def test_knobs_are_overridable_and_garbage_falls_back(self):
        self.assertEqual(
            self._eval('server.ClaudeTaskManager.PROMPT_REAP_RETRIES',
                       KC_PROMPT_REAP_RETRIES='5'), '5')
        self.assertEqual(
            self._eval('server.ClaudeTaskManager.PROMPT_REAP_GRACE',
                       KC_PROMPT_REAP_GRACE_S='nonsense'), '120.0')
        self.assertEqual(
            self._eval('server.ClaudeTaskManager.PROMPT_REAP_RETRIES',
                       KC_PROMPT_REAP_RETRIES='-4'), '0')


class PromptReaperLoopTests(unittest.TestCase):
    def setUp(self):
        self._saved = (PR._started, PR._thread, PR._last_run_at, PR._last_result)
        PR._started, PR._thread = False, None
        PR._stop_event.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        PR._stop_event.set()
        if PR._thread is not None:
            PR._thread.join(timeout=5)
        PR._stop_event.clear()
        PR._started, PR._thread, PR._last_run_at, PR._last_result = self._saved

    def test_start_runs_a_pass_and_is_idempotent(self):
        with mock.patch.object(CTM, 'reap_undelivered',
                               return_value={'retried': 0, 'errored': 0}) as reap:
            PR.start(interval_seconds=3600)
            first = PR._thread
            PR.start(interval_seconds=3600)
            self.assertIs(PR._thread, first)
            for _ in range(100):
                if PR._last_run_at:
                    break
                PR._stop_event.wait(0.05)
        self.assertEqual(reap.call_count, 1)
        self.assertTrue(PR.status()['running'])
        self.assertEqual(PR.status()['last_result'], {'retried': 0, 'errored': 0})

    def test_a_failing_pass_does_not_kill_the_loop(self):
        calls = []

        def boom():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('boom')
            return {'retried': 0, 'errored': 0}

        with mock.patch.object(CTM, 'reap_undelivered', side_effect=boom):
            PR.start(interval_seconds=0.05)
            for _ in range(100):
                if len(calls) >= 2:
                    break
                PR._stop_event.wait(0.05)
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(PR.status()['running'])


PR = server.PromptReaper

if __name__ == '__main__':
    unittest.main()
