"""Unit tests for the idle-session reaper and resuming a parked Build (#928).

A Build that finished its turn parks on `waiting-for-input` with its REPL still
open, for hours. The reaper closes such a session after a TTL and marks the
Build `parked` -- keeping task.json and the transcript -- so it can be resumed
with `claude --resume`. It decides from task.json alone (status, waiting
reason, timestamps); git and tmux's attached-client count are safety gates
that can only hold a session open, never close one.

tmux is simulated via a subprocess.run stub that records every argv; TASKS_DIR
is a tempdir; git runs for real against throwaway repositories.

Run with:    python3 -m unittest tests.idle_session_reaper_test
(from charts/workspace/)
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(HERE)
sys.path.insert(0, WORKSPACE)
import server  # noqa: E402
import worktrees  # noqa: E402

CTM = server.ClaudeTaskManager
_REAL_RUN = subprocess.run

NOW = 100_000.0
TTL = 1800.0
QTTL = 21600.0
SID = '0b9f6f53-5d0e-4b8c-9a39-2a8f3c1d7e11'


class _InlineThread:
    """threading.Thread stand-in that runs its target immediately."""

    def __init__(self, target=None, args=(), kwargs=None, **_):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class FakeTmux:
    """subprocess.run stub for tmux; anything else (git) runs for real.

    `alive` drives has-session, `attached` the session_attached count, and
    `launch_ok` whether new-session succeeds. Only the owning thread's calls
    are recorded (a daemon thread from another test may land here).
    """

    def __init__(self, alive=True, attached=0, launch_ok=True, sessions=''):
        self.alive, self.attached, self.launch_ok = alive, attached, launch_ok
        self.sessions = sessions
        self.calls = []
        self._owner = threading.get_ident()

    def __call__(self, cmd, **kwargs):
        argv = list(cmd)
        if argv[:1] != ['tmux']:
            return _REAL_RUN(cmd, **kwargs)
        if threading.get_ident() == self._owner:
            self.calls.append(argv)
        verb = argv[1] if len(argv) > 1 else ''
        if verb == 'has-session':
            return mock.Mock(returncode=0 if self.alive else 1, stdout='', stderr='')
        if verb == 'display-message':
            if self.attached is None:
                return mock.Mock(returncode=1, stdout='', stderr='no server')
            return mock.Mock(returncode=0, stdout=f'{self.attached}\n', stderr='')
        if verb == 'new-session':
            return mock.Mock(returncode=0 if self.launch_ok else 1, stdout='',
                             stderr='' if self.launch_ok else 'duplicate session')
        if verb == 'list-sessions':
            return mock.Mock(returncode=0, stdout=self.sessions, stderr='')
        return mock.Mock(returncode=0, stdout='', stderr='')

    def verbs(self):
        return [c[1] for c in self.calls if len(c) > 1]

    def call(self, verb):
        return next((c for c in self.calls if c[1:2] == [verb]), None)


def _git(*args, cwd):
    _REAL_RUN(['git', *args], cwd=cwd, check=True, capture_output=True,
              env={**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@x',
                   'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@x'})


class ReaperBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kcidle-')
        self.tasks = os.path.join(self.tmp, 'tasks')
        os.makedirs(self.tasks)
        self.plain = os.path.join(self.tmp, 'plain')   # not a git repo
        os.makedirs(self.plain)
        patches = [
            mock.patch.object(CTM, 'TASKS_DIR', self.tasks),
            mock.patch.object(CTM, 'IDLE_SESSION_TTL', TTL),
            mock.patch.object(CTM, 'IDLE_QUESTION_TTL', QTTL),
            mock.patch.object(CTM, 'MAX_TASKS', 12),
            mock.patch.object(CTM, '_claude_supports_resume', return_value=True),
            mock.patch.object(CTM, '_ensure_claude_trust', return_value=True),
            mock.patch.object(CTM, '_api_key_to_reject', return_value=None),
            mock.patch.object(CTM, '_fire_completion_hook'),
            mock.patch.object(server.FeedManager, 'emit_task_terminal'),
            mock.patch.object(server.threading, 'Thread', _InlineThread),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(server.FeedManager, 'emit_task_parked')
        self.feed = p.start()
        self.addCleanup(p.stop)
        CTM._resuming.clear()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _task(self, tid, **meta):
        d = os.path.join(self.tasks, tid)
        os.makedirs(d, exist_ok=True)
        m = {'task_id': tid, 'tmux_session': f'kube-coder-{tid}',
             'status': 'waiting-for-input', 'waiting_reason': 'idle',
             'waiting_for_input': True, 'prompt_delivered': True,
             'assistant': 'claude', 'claude_session_id': SID,
             'workdir': self.plain, 'created_at': NOW - 50_000,
             'last_activity_at': NOW - TTL - 1, 'prompt': 'research the thing'}
        m.update(meta)
        with open(os.path.join(d, 'task.json'), 'w') as f:
            json.dump(m, f)
        with open(os.path.join(d, 'output.log'), 'w') as f:
            f.write('the transcript so far\n')
        return d

    def _read(self, tid):
        with open(os.path.join(self.tasks, tid, 'task.json')) as f:
            return json.load(f)

    def _reap(self, tmux=None, now=NOW):
        tmux = tmux or FakeTmux()
        with mock.patch.object(server.subprocess, 'run', side_effect=tmux):
            out = CTM.park_idle(now=now)
        return out, tmux


class CandidateTests(ReaperBase):
    """What may be parked: decided from task.json alone."""

    def _meta(self, **kw):
        m = {'status': 'waiting-for-input', 'waiting_reason': 'idle',
             'assistant': 'claude', 'claude_session_id': SID,
             'last_activity_at': NOW - TTL}
        m.update(kw)
        return m

    def test_idle_past_its_ttl_is_a_candidate(self):
        self.assertTrue(CTM._park_candidate(self._meta(), NOW))
        self.assertFalse(CTM._park_candidate(
            self._meta(last_activity_at=NOW - TTL + 1), NOW))

    def test_a_question_gets_the_longer_ttl(self):
        q = self._meta(waiting_reason='question', last_activity_at=NOW - TTL * 2)
        self.assertFalse(CTM._park_candidate(q, NOW))
        q['last_activity_at'] = NOW - QTTL
        self.assertTrue(CTM._park_candidate(q, NOW))

    def test_a_zero_ttl_turns_that_half_off(self):
        with mock.patch.object(CTM, 'IDLE_SESSION_TTL', 0):
            self.assertFalse(CTM._park_candidate(
                self._meta(last_activity_at=0), NOW))
            self.assertTrue(CTM._park_candidate(
                self._meta(waiting_reason='question', last_activity_at=0), NOW))
        with mock.patch.object(CTM, 'IDLE_QUESTION_TTL', 0):
            self.assertFalse(CTM._park_candidate(
                self._meta(waiting_reason='question', last_activity_at=0), NOW))

    def test_trust_dialog_and_undelivered_are_the_prompt_reapers(self):
        for reason in ('trust_dialog', 'undelivered', None):
            self.assertFalse(CTM._park_candidate(
                self._meta(waiting_reason=reason, last_activity_at=0), NOW), reason)

    def test_only_a_waiting_build_parks(self):
        for status in ('running', 'completed', 'error', 'killed',
                       'terminated', 'parked'):
            self.assertFalse(CTM._park_candidate(
                self._meta(status=status, last_activity_at=0), NOW), status)

    def test_a_build_that_could_not_be_resumed_is_never_closed(self):
        old = {'last_activity_at': 0}
        self.assertFalse(CTM._park_candidate(self._meta(assistant='codex', **old), NOW))
        self.assertFalse(CTM._park_candidate(self._meta(claude_session_id='', **old), NOW))
        self.assertFalse(CTM._park_candidate(self._meta(claude_session_id='nope', **old), NOW))
        self.assertFalse(CTM._park_candidate(self._meta(kind='terminal', **old), NOW))
        # A Build that was itself resumed carries its session there instead.
        self.assertTrue(CTM._park_candidate(
            self._meta(claude_session_id='', resumed_session_id=SID, **old), NOW))

    def test_board_bound_builds_are_left_to_the_board(self):
        self.assertFalse(CTM._park_candidate(
            self._meta(board_id='b1', last_activity_at=0), NOW))

    def test_a_recent_followup_or_resume_resets_the_clock(self):
        m = self._meta(last_activity_at=0,
                       followups=[{'prompt': 'more', 'sent_at': NOW - 10}])
        self.assertFalse(CTM._park_candidate(m, NOW))
        m = self._meta(last_activity_at=0, resumed_at=NOW - 10)
        self.assertFalse(CTM._park_candidate(m, NOW))


class ParkTests(ReaperBase):
    def test_parks_closes_the_session_and_keeps_the_record(self):
        self._task('t1')
        out, tmux = self._reap()
        self.assertEqual(out['parked'], 1)
        meta = self._read('t1')
        self.assertEqual(meta['status'], 'parked')
        self.assertEqual(meta['parked_at'], NOW)
        self.assertEqual(meta['parked_reason'], 'idle')
        self.assertEqual(meta['claude_session_id'], SID)  # resumable
        # Not a result and not a failure.
        self.assertNotIn('finished_at', meta)
        self.assertNotIn('killed_at', meta)
        self.assertIn('kill-session', tmux.verbs())
        with open(os.path.join(self.tasks, 't1', 'output.log')) as f:
            self.assertIn('transcript', f.read())
        self.feed.assert_called_once()

    def test_a_question_parks_with_its_reason(self):
        self._task('q1', waiting_reason='question', last_activity_at=NOW - QTTL)
        out, _ = self._reap()
        self.assertEqual(out['parked'], 1)
        self.assertEqual(self._read('q1')['parked_reason'], 'question')

    def test_under_ttl_nothing_happens(self):
        self._task('t1', last_activity_at=NOW - 60)
        out, tmux = self._reap()
        self.assertEqual(out['parked'], 0)
        self.assertEqual(self._read('t1')['status'], 'waiting-for-input')
        self.assertNotIn('kill-session', tmux.verbs())

    def test_an_attached_session_is_never_closed(self):
        self._task('t1')
        out, tmux = self._reap(FakeTmux(attached=1))
        self.assertEqual(out, {'parked': 0, 'kept': {'attached': 1}})
        self.assertEqual(self._read('t1')['status'], 'waiting-for-input')
        self.assertNotIn('kill-session', tmux.verbs())

    def test_attached_unknown_counts_as_attached(self):
        self._task('t1')
        out, _ = self._reap(FakeTmux(attached=None))
        self.assertEqual(out['kept'], {'attached': 1})

    def test_a_gone_session_is_the_reconcilers(self):
        self._task('t1')
        out, tmux = self._reap(FakeTmux(alive=False))
        self.assertEqual(out['parked'], 0)
        self.assertEqual(self._read('t1')['status'], 'waiting-for-input')

    def test_no_resume_support_closes_nothing(self):
        self._task('t1')
        with mock.patch.object(CTM, '_claude_supports_resume', return_value=False):
            out, tmux = self._reap()
        self.assertEqual(out['kept'], {'resume_unsupported': 1})
        self.assertNotIn('kill-session', tmux.verbs())

    def test_both_ttls_zero_is_a_noop(self):
        self._task('t1', last_activity_at=0)
        with mock.patch.object(CTM, 'IDLE_SESSION_TTL', 0), \
                mock.patch.object(CTM, 'IDLE_QUESTION_TTL', 0):
            out, tmux = self._reap()
        self.assertEqual(out['parked'], 0)
        self.assertEqual(tmux.calls, [])

    def test_decision_never_reads_the_screen(self):
        self._task('t1')
        _, tmux = self._reap()
        self.assertNotIn('capture-pane', tmux.verbs())

    def test_a_build_that_moved_on_is_not_parked(self):
        d = self._task('t1')
        stale = self._read('t1')
        # Activity lands between the reaper's read and its write.
        server.task_status.atomic_update_meta(
            d, lambda m: m.__setitem__('last_activity_at', NOW - 1))
        with mock.patch.object(server.subprocess, 'run', side_effect=FakeTmux()):
            self.assertFalse(CTM._park('t1', d, stale, NOW))
        self.assertEqual(self._read('t1')['status'], 'waiting-for-input')

    def test_the_reconciler_leaves_a_parked_build_alone(self):
        d = self._task('t1')
        self._reap()
        meta = self._read('t1')
        with mock.patch.object(server.subprocess, 'run',
                               side_effect=FakeTmux(alive=False)) as run:
            CTM._reconcile_status(meta, d)
        self.assertEqual(self._read('t1')['status'], 'parked')
        self.assertFalse(any(c.args[0][:2] == ['tmux', 'has-session']
                             for c in run.call_args_list))

    def test_the_list_says_when_and_why_it_was_parked(self):
        self._task('t1')
        self._reap()
        with mock.patch.object(server.subprocess, 'run',
                               side_effect=FakeTmux(alive=False)), \
                mock.patch.object(CTM, 'ingest_usage'):
            row = next(r for r in CTM.list_tasks() if r['task_id'] == 't1')
        self.assertEqual(row['status'], 'parked')
        self.assertEqual((row['parked_at'], row['parked_reason']), (NOW, 'idle'))
        self.assertNotIn('waiting_reason', row)

    def test_a_parked_build_holds_no_slot(self):
        self._task('t1')
        self._reap()
        # Its session is gone, so it is not among the sessions counted; and
        # an open but idle session would not have counted either.
        with mock.patch.object(server.subprocess, 'run',
                               side_effect=FakeTmux(sessions='')):
            self.assertEqual(CTM.count_tasks(), {'busy': 0, 'idle': 0})


class UnsavedWorkTests(ReaperBase):
    """Never close an agent whose folder holds work git could lose."""

    def _repo(self):
        remote = os.path.join(self.tmp, 'remote.git')
        _git('init', '-q', '--bare', remote, cwd=self.tmp)
        repo = os.path.join(self.tmp, 'repo')
        _git('clone', '-q', remote, repo, cwd=self.tmp)
        with open(os.path.join(repo, 'a.txt'), 'w') as f:
            f.write('a\n')
        _git('add', 'a.txt', cwd=repo)
        _git('commit', '-q', '-m', 'a', cwd=repo)
        _git('push', '-q', 'origin', 'HEAD', cwd=repo)
        return repo

    def test_unsaved_work_verdicts(self):
        repo = self._repo()
        self.assertEqual(worktrees.unsaved_work(repo), '')
        self.assertEqual(worktrees.unsaved_work(self.plain), '')
        self.assertEqual(worktrees.unsaved_work(os.path.join(self.tmp, 'gone')), '')
        with open(os.path.join(repo, 'new.txt'), 'w') as f:
            f.write('untracked\n')
        self.assertEqual(worktrees.unsaved_work(repo), 'dirty')
        _git('add', 'new.txt', cwd=repo)
        _git('commit', '-q', '-m', 'local only', cwd=repo)
        self.assertEqual(worktrees.unsaved_work(repo), 'unpushed')

    def test_a_dirty_worktree_is_kept(self):
        repo = self._repo()
        with open(os.path.join(repo, 'a.txt'), 'a') as f:
            f.write('edited\n')
        self._task('t1', workdir=repo)
        out, tmux = self._reap()
        self.assertEqual(out['kept'], {'dirty': 1})
        self.assertEqual(self._read('t1')['status'], 'waiting-for-input')
        self.assertNotIn('kill-session', tmux.verbs())

    def test_an_unpushed_commit_is_kept(self):
        repo = self._repo()
        with open(os.path.join(repo, 'b.txt'), 'w') as f:
            f.write('b\n')
        _git('add', 'b.txt', cwd=repo)
        _git('commit', '-q', '-m', 'b', cwd=repo)
        self._task('t1', workdir=repo)
        out, _ = self._reap()
        self.assertEqual(out['kept'], {'unpushed': 1})

    def test_a_clean_pushed_repo_parks(self):
        self._task('t1', workdir=self._repo())
        out, _ = self._reap()
        self.assertEqual(out['parked'], 1)


class ResumeTests(ReaperBase):
    def _parked(self, tid='t1', **meta):
        self._task(tid, **meta)
        self._reap()
        self.assertEqual(self._read(tid)['status'], 'parked')

    def _resume(self, tid='t1', prompt=None, tmux=None, delivered=True):
        tmux = tmux or FakeTmux()
        with mock.patch.object(server.subprocess, 'run', side_effect=tmux), \
                mock.patch.object(CTM, '_deliver_launch_prompt',
                                  return_value=(delivered, False)) as deliver:
            meta, err = CTM.resume_task(tid, prompt=prompt)
        return meta, err, tmux, deliver

    def test_resume_reopens_the_same_session_in_place(self):
        self._parked(auto_approve=True)
        meta, err, tmux, deliver = self._resume()
        self.assertIsNone(err)
        self.assertEqual(meta['status'], 'running')
        launch = tmux.call('new-session')
        self.assertIsNotNone(launch)
        self.assertIn('kube-coder-t1', launch)
        self.assertIn('KC_TASK_ID=t1', launch)
        shell = launch[-1]
        self.assertIn(f'--resume {SID}', shell)
        self.assertIn(self.plain, shell)
        self.assertIn('--dangerously-skip-permissions', shell)
        stored = self._read('t1')
        self.assertEqual(stored['status'], 'running')
        self.assertNotIn('parked_at', stored)
        self.assertEqual(stored['resumes'][0]['parked_reason'], 'idle')
        self.assertEqual(stored['claude_session_id'], SID)
        deliver.assert_not_called()

    def test_resume_with_a_prompt_delivers_it_and_records_a_followup(self):
        self._parked()
        meta, err, _, deliver = self._resume(prompt='carry on')
        self.assertIsNone(err)
        deliver.assert_called_once()
        stored = self._read('t1')
        self.assertEqual(stored['followups'][-1]['prompt'], 'carry on')
        self.assertTrue(stored['resume_prompt_delivered'])

    def test_a_failed_launch_leaves_it_parked(self):
        self._parked()
        meta, err, _, _ = self._resume(tmux=FakeTmux(launch_ok=False))
        self.assertIsNone(meta)
        self.assertIn('duplicate session', err)
        self.assertEqual(self._read('t1')['status'], 'parked')

    def test_resume_respects_the_busy_cap(self):
        self._parked()
        with mock.patch.object(CTM, 'MAX_TASKS', 1):
            busy = FakeTmux(sessions='kube-coder-other\n')
            meta, err, tmux, _ = self._resume(tmux=busy)
        self.assertEqual(err, 'at_capacity')
        self.assertEqual(meta['status'], 'rejected')
        self.assertIsNone(tmux.call('new-session'))
        self.assertEqual(self._read('t1')['status'], 'parked')

    def test_only_a_parked_build_resumes(self):
        self._task('t1')
        _, err, tmux, _ = self._resume()
        self.assertEqual(err, 'not_parked')
        self.assertIsNone(tmux.call('new-session'))
        _, err, _, _ = self._resume('missing')
        self.assertEqual(err, 'Task not found')
        _, err, _, _ = self._resume('../etc')
        self.assertEqual(err, 'Task not found')

    def test_a_deleted_parked_build_can_be_killed(self):
        self._parked()
        with mock.patch.object(server.subprocess, 'run', side_effect=FakeTmux()):
            meta = CTM.delete_task('t1')
        self.assertEqual(meta['status'], 'killed')

    def test_a_message_to_a_parked_build_resumes_it(self):
        self._parked()
        tmux = FakeTmux()
        with mock.patch.object(server.subprocess, 'run', side_effect=tmux), \
                mock.patch.object(CTM, '_deliver_launch_prompt',
                                  return_value=(True, False)):
            meta, err = CTM.send_followup('t1', 'and then?')
        self.assertIsNone(err)
        self.assertEqual(meta['status'], 'running')
        self.assertIsNotNone(tmux.call('new-session'))
        self.assertEqual(self._read('t1')['followups'][-1]['prompt'], 'and then?')

    def test_a_paste_to_a_parked_build_is_refused(self):
        self._parked()
        with mock.patch.object(server.subprocess, 'run', side_effect=FakeTmux()):
            meta, err = CTM.send_followup('t1', 'draft', submit=False)
        self.assertIsNone(meta)
        self.assertIn('parked', err)
        self.assertEqual(self._read('t1')['status'], 'parked')


if __name__ == '__main__':
    unittest.main()
