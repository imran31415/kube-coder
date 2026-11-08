"""The sub-agent orchestrator obeys the server's status rules.

mcp_agent_orchestrator.py runs as its own process and writes the same task.json
files as the dashboard server. These pin that its writes use the shared
transition table under the same lock: a terminal status never changes, status
is written before a session is killed, and concurrent writers lose nothing.

tmux is simulated via subprocess.run stubs; TASKS_DIR is a tempdir.

Run:  python3 -m unittest tests.orchestrator_status_guard_test   (from charts/workspace/)
"""

import fcntl
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
import mcp_agent_orchestrator as orch  # noqa: E402
import server  # noqa: E402
import task_status  # noqa: E402

CTM = server.ClaudeTaskManager
WAITING = 'waiting-for-input'
TERMINAL = ('completed', 'error', 'killed', 'terminated')
LIVE = ('running', WAITING)

#: Run in a child process, so the lock is exercised across real processes.
_APPEND_CHILDREN = """
import sys
sys.path.insert(0, sys.argv[1])
import mcp_agent_orchestrator as orch
orch.TASKS_DIR = sys.argv[2]
for i in range(int(sys.argv[4])):
    orch._append_sub_task_id('parent', f'{sys.argv[3]}-{i}')
"""


class Tmux:
    """subprocess.run stub. Remembers the argv, and task.json at each kill."""

    def __init__(self, tasks_dir, dead=(), fail_launch=None):
        self.tasks_dir = tasks_dir
        self.dead = dead
        self.fail_launch = fail_launch
        self.calls = []
        self.status_at_kill = []

    def __call__(self, argv, *a, **kw):
        self.calls.append(list(argv))
        if argv[:2] == ['tmux', 'kill-session']:
            tid = argv[-1].split('sess-', 1)[-1]
            path = os.path.join(self.tasks_dir, tid, 'task.json')
            with open(path) as f:
                self.status_at_kill.append(json.load(f)['status'])
        if argv[:2] == ['tmux', 'new-session'] and self.fail_launch:
            self.fail_launch()
            return mock.Mock(returncode=1, stdout='', stderr='no server running')
        rc = 1 if len(argv) > 1 and argv[1] in self.dead else 0
        return mock.Mock(returncode=rc, stdout='', stderr='')

    def killed(self):
        return [c for c in self.calls if c[:2] == ['tmux', 'kill-session']]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kcorch-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patch = mock.patch.multiple(orch, TASKS_DIR=self.tmp, _BOOT_ID='boot-now')
        patch.start()
        self.addCleanup(patch.stop)

    def dir(self, tid='t'):
        return os.path.join(self.tmp, tid)

    def task(self, tid='t', **meta):
        os.makedirs(self.dir(tid), exist_ok=True)
        record = {'task_id': tid, 'tmux_session': f'sess-{tid}', 'boot_id': 'boot-now'}
        record.update(meta)
        with open(os.path.join(self.dir(tid), 'task.json'), 'w') as f:
            json.dump(record, f)
        return self.dir(tid)

    def read(self, tid='t'):
        with open(os.path.join(self.dir(tid), 'task.json')) as f:
            return json.load(f)

    def raw(self, tid='t'):
        with open(os.path.join(self.dir(tid), 'task.json'), 'rb') as f:
            return f.read()

    def tmux(self, **kw):
        stub = Tmux(self.tmp, **kw)
        patch = mock.patch.object(orch.subprocess, 'run', side_effect=stub)
        patch.start()
        self.addCleanup(patch.stop)
        return stub

    def kill(self, tid='t'):
        res = orch._tool_kill_agent({'task_id': tid})
        self.assertFalse(res.get('isError'), res)
        return json.loads(res['content'][0]['text'])


class TransitionTests(Base):
    def test_only_a_live_status_can_end(self):
        for source in LIVE + TERMINAL + ('unknown', None):
            for target in TERMINAL:
                with self.subTest(source=source, target=target):
                    self.task(status=source)
                    before = self.raw()
                    meta, moved = orch._transition(self.dir(), target)
                    if source in LIVE:
                        self.assertTrue(moved)
                        self.assertEqual(self.read()['status'], target)
                        self.assertEqual(meta['status'], target)
                    else:
                        self.assertFalse(moved)
                        self.assertEqual(self.raw(), before, 'a refused move wrote')

    def test_fields_are_set_only_when_it_moves(self):
        self.task(status='completed')
        orch._transition(self.dir(), 'error', fields={'error': 'late'})
        self.assertNotIn('error', self.read())

    def test_record_lands_even_when_the_move_is_refused(self):
        self.task(status='completed')
        _, moved = orch._transition(
            self.dir(), 'error', record=lambda m: m.update(note='kept'))
        self.assertFalse(moved)
        self.assertEqual(self.read()['note'], 'kept')
        self.assertEqual(self.read()['status'], 'completed')

    def test_ending_clears_the_waiting_fields(self):
        self.task(status=WAITING, waiting_for_input=True,
                  last_input_prompt='?', waiting_reason='question')
        orch._transition(self.dir(), 'completed')
        for field in ('waiting_for_input', 'last_input_prompt', 'waiting_reason'):
            self.assertNotIn(field, self.read())

    def test_a_gone_task_reports_none(self):
        self.assertEqual(orch._transition(self.dir('nope'), 'killed'), (None, False))

    def test_a_corrupt_record_reports_none_instead_of_raising(self):
        self.task()
        with open(os.path.join(self.dir(), 'task.json'), 'w') as f:
            f.write('{not json')
        self.assertEqual(orch._transition(self.dir(), 'killed'), (None, False))


class ReconcileTests(Base):
    def test_a_stale_read_cannot_rewrite_a_terminal_status(self):
        for final in TERMINAL:
            with self.subTest(final=final):
                self.task(status=final)
                before = self.raw()
                stale = {**self.read(), 'status': 'running'}
                self.tmux(dead=('has-session',))
                orch._reconcile_status(stale, self.dir())
                self.assertEqual(self.raw(), before)
                self.assertEqual(stale['status'], final, 'caller must see the truth')

    def test_a_dead_session_settles_with_its_exit_code(self):
        self.task(status='running')
        with open(os.path.join(self.dir(), 'exit_code'), 'w') as f:
            f.write('3\n')
        self.tmux(dead=('has-session',))
        meta = self.read()
        orch._reconcile_status(meta, self.dir())
        on_disk = self.read()
        self.assertEqual(on_disk['status'], 'error')
        self.assertEqual(on_disk['exit_code'], 3)
        self.assertEqual(on_disk['error'], 'agent exited with code 3')
        self.assertIsNotNone(on_disk['finished_at'])
        self.assertEqual(meta['status'], 'error')

    def test_a_wiped_session_is_terminated(self):
        self.task(status='running', boot_id='older-boot')
        self.tmux(dead=('has-session',))
        orch._reconcile_status(self.read(), self.dir())
        on_disk = self.read()
        self.assertEqual(on_disk['status'], 'terminated')
        self.assertEqual(on_disk['termination_reason'], 'pod_restart')

    def test_adopting_the_boot_cannot_undo_a_status_change(self):
        # Stale read, session still up: the old code wrote the whole stale
        # record back, which brought the killed task back to `running`.
        self.task(status='killed', boot_id='older-boot')
        stale = {**self.read(), 'status': 'running'}
        self.tmux()
        orch._reconcile_status(stale, self.dir())
        self.assertEqual(self.read()['status'], 'killed')
        self.assertEqual(stale['status'], 'killed')


class KillTests(Base):
    def test_status_is_written_before_the_session_dies(self):
        self.task(status='running')
        tmux = self.tmux()
        self.kill()
        self.assertEqual(tmux.status_at_kill, ['killed'])

    def test_a_kill_records_it_and_keeps_the_response_shape(self):
        self.task(status=WAITING, waiting_reason='idle')
        self.tmux()
        self.assertEqual(self.kill(), {'task_id': 't', 'status': 'killed'})
        on_disk = self.read()
        self.assertEqual(on_disk['status'], 'killed')
        self.assertIsNotNone(on_disk['killed_at'])
        self.assertNotIn('waiting_reason', on_disk)

    def test_killing_a_finished_agent_changes_nothing_and_reports_how_it_ended(self):
        for final in TERMINAL:
            with self.subTest(final=final):
                self.task(status=final, finished_at=1.0)
                before = self.raw()
                self.tmux()
                self.assertEqual(self.kill(), {'task_id': 't', 'status': final})
                self.assertEqual(self.raw(), before)

    def test_a_retry_still_cleans_up_the_session(self):
        # Status is written first, so a crash before the kill leaves a live
        # session behind a finished record. Only the retry can reach it.
        self.task(status='killed')
        tmux = self.tmux()
        self.kill()
        self.assertEqual(len(tmux.killed()), 1)

    def test_a_kill_then_a_stale_reconcile_stays_killed(self):
        self.task(status='running')
        stale = self.read()
        self.tmux(dead=('has-session',))
        self.kill()
        orch._reconcile_status(stale, self.dir())
        self.assertEqual(self.read()['status'], 'killed')

    def test_unknown_task_is_still_an_error(self):
        res = orch._tool_kill_agent({'task_id': 'nope'})
        self.assertTrue(res.get('isError'))


class LaunchFailureTests(Base):
    def spawn(self):
        for p in (mock.patch.object(orch, '_count_live_agent_sessions', return_value=0),
                  mock.patch.object(orch.threading, 'Thread')):
            p.start()
            self.addCleanup(p.stop)
        return orch._tool_spawn_agent(
            {'prompt': 'do it', 'assistant': 'ante', 'workdir': self.tmp})

    def spawned_id(self):
        return next(e for e in os.listdir(self.tmp)
                    if os.path.isfile(os.path.join(self.dir(e), 'task.json')))

    def test_a_failed_launch_records_the_error(self):
        self.tmux(fail_launch=lambda: None)
        res = self.spawn()
        self.assertTrue(res.get('isError'))
        on_disk = self.read(self.spawned_id())
        self.assertEqual(on_disk['status'], 'error')
        self.assertEqual(on_disk['error'], 'no server running')

    def test_a_failed_launch_cannot_rewrite_a_finished_record(self):
        def server_got_there_first():
            CTM._atomic_update_meta(self.dir(self.spawned_id()),
                                    lambda m: m.update(status='completed'))

        self.tmux(fail_launch=server_got_there_first)
        res = self.spawn()
        self.assertTrue(res.get('isError'), 'the caller still hears it failed')
        on_disk = self.read(self.spawned_id())
        self.assertEqual(on_disk['status'], 'completed')
        self.assertNotIn('error', on_disk)


class SharedLockTests(Base):
    def test_a_transition_waits_for_the_servers_lock(self):
        self.task(status='running')
        done = threading.Event()
        with open(os.path.join(self.dir(), '.meta.lock'), 'a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)

            def end():
                orch._transition(self.dir(), 'killed')
                done.set()

            worker = threading.Thread(target=end)
            worker.start()
            self.assertFalse(done.wait(0.2), 'wrote without the task lock')
            self.assertEqual(self.read()['status'], 'running')
            fcntl.flock(held, fcntl.LOCK_UN)
        worker.join(10)
        self.assertTrue(done.is_set())
        self.assertEqual(self.read()['status'], 'killed')

    def test_server_and_orchestrator_writes_lose_nothing(self):
        # Every path that rewrites an existing record has to take the lock,
        # including the lineage append, which never touches status.
        self.task('parent', status='running', sub_task_ids=[], followups=[])
        count = 40
        children = [subprocess.Popen(
            [sys.executable, '-c', _APPEND_CHILDREN, WORKSPACE, self.tmp,
             name, str(count)]) for name in ('a', 'b')]

        def followup(i):
            return lambda m: m['followups'].append(i)

        for i in range(count):
            CTM._atomic_update_meta(self.dir('parent'), followup(i))
        for child in children:
            self.assertEqual(child.wait(60), 0)

        record = self.read('parent')
        self.assertEqual(len(record['sub_task_ids']), count * 2)
        self.assertEqual(sorted(record['followups']), list(range(count)))
        self.assertEqual(record['status'], 'running')

    def test_the_server_uses_the_shared_lock(self):
        self.task(status='running')
        with mock.patch.object(task_status, 'atomic_update_meta',
                               return_value={'status': 'x'}) as shared:
            self.assertEqual(CTM._atomic_update_meta(self.dir(), lambda m: None),
                             {'status': 'x'})
        shared.assert_called_once()


if __name__ == '__main__':
    unittest.main()
