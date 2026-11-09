"""The server finishes the job when another process settles a Build.

mcp_agent_orchestrator.py writes terminal statuses from its own process and runs
no side effects. These pin that the server runs what was missed exactly once,
that the hundreds of old terminal records on a real workspace are never
replayed, and that its reconciler agrees with the orchestrator about crashes
and leaves launching sub-agents alone.

tmux is simulated via subprocess.run stubs; TASKS_DIR is a tempdir; side effects
are mocks, except in FeedNoiseTests, which uses a throwaway Feed.

Run:  python3 -m unittest tests.task_external_settle_test   (from charts/workspace/)
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
import mcp_agent_orchestrator as orch  # noqa: E402
import push_notify  # noqa: E402
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
FM = server.FeedManager
WAITING = 'waiting-for-input'
TERMINAL = ('completed', 'error', 'killed', 'terminated')
GRACE = CTM._ORCHESTRATOR_LAUNCH_GRACE
BOOT = 'boot-now'


class Tmux:
    """subprocess.run stub. has-session answers from `alive`."""

    def __init__(self, alive=False):
        self.alive = alive
        self.alive_sessions = set()
        self.calls = []
        self.on_launch = None
        self.launch_rc = 0

    def __call__(self, *args, **kwargs):
        argv = args[0] if args else kwargs.get('args', [])
        self.calls.append(list(argv))
        if argv[:2] == ['tmux', 'has-session']:
            up = self.alive or argv[-1] in self.alive_sessions
            return mock.Mock(returncode=0 if up else 1, stdout='', stderr='')
        if argv[:2] == ['tmux', 'new-session']:
            if self.on_launch:
                self.on_launch()
            if self.launch_rc:
                return mock.Mock(returncode=self.launch_rc, stdout='',
                                 stderr='no server running')
            self.alive = True
        return mock.Mock(returncode=0, stdout='', stderr='')


class Harness(unittest.TestCase):
    #: Side effects replaced by mocks. FeedNoiseTests keeps the real Feed.
    MOCKED = (('event', server.EventBroker, 'publish'),
              ('feed_terminal', FM, 'emit_task_terminal'),
              ('feed_waiting', FM, 'emit_task_waiting'),
              ('snapshot', server.WorktreeManager, 'snapshot_async'),
              ('hook', CTM, '_fire_completion_hook'),
              ('ingest', CTM, 'ingest_usage'))

    def setUp(self):
        isolate_feed_and_push(self)
        isolate_provider_keys(self)
        silence_prompt_delivery(self)
        self.tmp = tempfile.mkdtemp(prefix='kcext-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tmux = Tmux()
        for patcher in (
                mock.patch.object(CTM, 'TASKS_DIR', self.tmp),
                mock.patch.object(CTM, 'boot_id', return_value=BOOT),
                mock.patch.multiple(orch, TASKS_DIR=self.tmp, _BOOT_ID=BOOT),
                mock.patch('subprocess.run', side_effect=self.tmux),
                mock.patch.object(orch, '_count_live_agent_sessions', return_value=0),
                # Running inside a Build must not make these spawns its children.
                mock.patch.dict(os.environ, {'KC_TASK_ID': '', 'KC_AGENT_DEPTH': '0'})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.effects = {}
        for name, target, attr in self.MOCKED:
            patcher = mock.patch.object(target, attr)
            self.effects[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def dir(self, tid='t'):
        return os.path.join(self.tmp, tid)

    def task(self, tid='t', **meta):
        os.makedirs(self.dir(tid), exist_ok=True)
        record = {'task_id': tid, 'tmux_session': f'sess-{tid}', 'boot_id': BOOT}
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

    def exit_code(self, value, tid='t'):
        with open(os.path.join(self.dir(tid), 'exit_code'), 'w') as f:
            f.write(value)

    def fired(self):
        """How many times each side effect (usage aside) ran."""
        return {k: v.call_count for k, v in self.effects.items()
                if v.call_count and k != 'ingest'}

    def published(self):
        return [c.args[1]['status'] for c in self.effects['event'].call_args_list
                if c.args[0] == 'task.status']

    def assert_each_effect_ran_once(self, status, hook=True, snapshot=True):
        expected = {'event': 1, 'feed_terminal': 1}
        if hook:
            expected['hook'] = 1
        if snapshot:
            expected['snapshot'] = 1
        self.assertEqual(self.fired(), expected)
        self.assertEqual(self.published(), [status])
        self.assertEqual(self.effects['feed_terminal'].call_args.args[1], status)


class NoReplayTests(Harness):
    """Item 2. Everything else here is worth nothing if an upgrade replays."""

    def old_records(self, count=240):
        """Terminal records as older images left them: every effect looks
        unfired, and some look exactly like orchestrator-settled ones."""
        tids = []
        for i in range(count):
            tid = f'old-{i}'
            self.task(
                tid,
                status=TERMINAL[i % len(TERMINAL)],
                created_at=1_000_000.0 + i,
                finished_at=None if i % 3 == 0 else 1_000_100.0 + i,
                killed_at=1_000_200.0 + i if i % 4 == 2 else None,
                response_url='https://hooks.example/done',
                worktree={'path': f'/nowhere/{i}', 'branch': f'kc/{i}'},
                parent_task_id='old-parent' if i % 2 else None,
                depth=1 if i % 2 else None,
                sub_task_ids=[])
            if i % 5 == 0:
                self.exit_code('1\n', tid)
            tids.append(tid)
        return tids

    def test_no_side_effects_for_old_records_on_any_read_path(self):
        tids = self.old_records()
        before = {t: self.raw(t) for t in tids}

        CTM.reconcile_running()
        CTM.list_tasks()
        for tid in tids[:12]:
            CTM.get_task(tid)
        CTM.reconcile_running()

        self.assertEqual(self.fired(), {}, 'an old record replayed')
        self.assertEqual({t: self.raw(t) for t in tids}, before,
                         'an old record was rewritten')

    def test_a_tick_over_old_records_is_only_file_reads(self):
        self.old_records(40)
        self.assertEqual(CTM.reconcile_running(), 0)
        self.assertEqual(self.tmux.calls, [], 'a tmux call per finished task')

    def test_only_the_stamp_qualifies(self):
        base = {'status': 'killed', 'killed_at': 1.0}
        stamped = {**base, 'settled_by': task_status.SETTLED_BY_ORCHESTRATOR}
        self.assertFalse(task_status.needs_settle_effects(base))
        self.assertFalse(task_status.needs_settle_effects({**base, 'settled_by': 'x'}))
        self.assertTrue(task_status.needs_settle_effects(stamped))
        self.assertFalse(task_status.needs_settle_effects(
            {**stamped, task_status.SETTLE_EFFECTS_AT: 2.0}))
        self.assertFalse(task_status.needs_settle_effects(
            {**stamped, 'status': 'running'}))


class ExternalSettleTests(Harness):
    def build(self, tid='t', **meta):
        return self.task(tid, status='running', created_at=time.time() - 300,
                         response_url='https://hooks.example/done',
                         worktree={'path': '/nowhere', 'branch': 'b'}, **meta)

    def kill(self, tid='t'):
        res = orch._tool_kill_agent({'task_id': tid})
        self.assertFalse(res.get('isError'), res)

    def test_a_kill_runs_every_effect_once(self):
        self.build()
        self.kill()
        self.assertEqual(self.fired(), {}, 'the orchestrator ran an effect')

        self.assertEqual(CTM.reconcile_running(), 1)
        self.assert_each_effect_ran_once('killed')

        on_disk = self.read()
        self.assertEqual(on_disk['status'], 'killed')
        self.assertIsNotNone(on_disk['hook_fired_at'])
        self.assertEqual(on_disk['finished_at'], on_disk['killed_at'])
        self.assertEqual(self.effects['event'].call_args.args[1]['finished_at'],
                         on_disk['killed_at'])

    def test_a_normal_finish_runs_every_effect_once(self):
        self.build()
        self.exit_code('0\n')
        orch._reconcile_status(self.read(), self.dir())
        self.assertEqual(self.read()['status'], 'completed')

        CTM.reconcile_running()
        self.assert_each_effect_ran_once('completed')

    def test_an_orchestrator_crash_verdict_is_kept(self):
        self.build()
        self.exit_code('3\n')
        orch._reconcile_status(self.read(), self.dir())

        CTM.reconcile_running()
        self.assert_each_effect_ran_once('error')
        self.assertEqual(self.read()['error'], 'agent exited with code 3')

    def test_a_failed_launch_runs_its_effects(self):
        self.tmux.launch_rc = 1
        res = orch._tool_spawn_agent(
            {'prompt': 'do it', 'assistant': 'ante', 'workdir': self.tmp})
        self.assertTrue(res.get('isError'))
        tid = next(e for e in os.listdir(self.tmp)
                   if os.path.isfile(os.path.join(self.dir(e), 'task.json')))

        CTM.reconcile_running()
        self.assertEqual(self.fired(), {'event': 1, 'feed_terminal': 1})
        self.assertEqual(self.published(), ['error'])
        self.assertIsNotNone(self.read(tid)['finished_at'])

    def test_usage_settles_on_the_tick_not_the_next_read(self):
        self.build()
        self.kill()
        CTM.reconcile_running()
        ingested = [c.args[0]['status'] for c in self.effects['ingest'].call_args_list]
        self.assertTrue(ingested)
        self.assertEqual(set(ingested), {'killed'})

    def test_a_task_without_a_hook_or_worktree_runs_the_rest(self):
        self.task(status='running', created_at=time.time() - 300)
        self.kill()
        CTM.reconcile_running()
        self.assert_each_effect_ran_once('killed', hook=False, snapshot=False)

    def test_an_already_fired_hook_is_not_sent_again(self):
        self.build(hook_fired_at=5.0)
        self.kill()
        CTM.reconcile_running()
        self.assertEqual(self.effects['hook'].call_count, 0)
        self.assertEqual(self.read()['hook_fired_at'], 5.0)

    def test_every_read_path_can_claim_and_only_one_does(self):
        for name, reader in (('get', lambda: CTM.get_task('t')),
                             ('list', CTM.list_tasks)):
            with self.subTest(reader=name):
                for m in self.effects.values():
                    m.reset_mock()
                self.build()
                self.kill()
                reader()
                reader()
                self.assert_each_effect_ran_once('killed')

    def test_concurrent_reconcilers_run_the_effects_once(self):
        self.build()
        self.kill()
        racers = 12
        barrier = threading.Barrier(racers)
        errors = []

        def race():
            try:
                with open(os.path.join(self.dir(), 'task.json')) as f:
                    meta = json.load(f)
                barrier.wait(10)
                CTM._reconcile_status(meta, self.dir())
            except Exception as e:      # surfaced by the assertion below
                errors.append(e)

        threads = [threading.Thread(target=race) for _ in range(racers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)

        self.assertEqual(errors, [])
        self.assert_each_effect_ran_once('killed')

    def test_a_restart_does_not_repeat_them(self):
        self.build()
        self.kill()
        CTM.reconcile_running()
        self.assert_each_effect_ran_once('killed')
        on_disk = self.raw()

        with mock.patch.object(CTM, '_launching', set()), \
                mock.patch.object(CTM, 'boot_id', return_value='boot-after-restart'):
            CTM.reconcile_running()
            CTM.list_tasks()

        self.assert_each_effect_ran_once('killed')
        self.assertEqual(self.raw(), on_disk)

    def test_a_crash_after_the_claim_loses_them_rather_than_repeating(self):
        self.build()
        self.kill()
        with mock.patch.object(CTM, '_after_transition'):
            CTM.reconcile_running()
        self.assertIsNotNone(self.read()[task_status.SETTLE_EFFECTS_AT])

        CTM.reconcile_running()
        self.assertEqual(self.fired(), {})

    def test_a_settled_record_stops_being_considered(self):
        self.build()
        self.kill()
        self.assertEqual(CTM.reconcile_running(), 1)
        self.assertEqual(CTM.reconcile_running(), 0)

    def test_claiming_needs_no_tmux(self):
        self.build()
        self.kill()
        self.tmux.calls.clear()
        CTM.reconcile_running()
        self.assertEqual(self.tmux.calls, [])

    def test_the_caller_sees_the_claimed_record(self):
        self.build()
        self.kill()
        meta = self.read()
        CTM._reconcile_status(meta, self.dir())
        self.assertEqual(meta, self.read())

    def test_a_stale_running_read_claims_as_soon_as_it_learns(self):
        self.build()
        stale = self.read()
        self.kill()

        CTM._reconcile_status(stale, self.dir())

        self.assert_each_effect_ran_once('killed')
        self.assertEqual(stale['status'], 'killed')

    def test_a_server_settled_build_does_not_run_them_twice(self):
        self.build()
        CTM.reconcile_running()
        self.assert_each_effect_ran_once('completed')
        self.assertNotIn('settled_by', self.read())

        CTM.reconcile_running()
        CTM.list_tasks()
        self.assert_each_effect_ran_once('completed')

    def test_a_server_kill_does_not_run_them_twice(self):
        self.build()
        CTM._transition(self.dir(), 'killed')
        self.assert_each_effect_ran_once('killed')

        CTM.reconcile_running()
        self.assert_each_effect_ran_once('killed')

    def test_an_orchestrator_that_loses_the_race_leaves_no_stamp(self):
        self.build()
        CTM.reconcile_running()
        self.kill()     # already `completed`: this kill writes nothing
        self.assertNotIn('settled_by', self.read())

        CTM.reconcile_running()
        self.assert_each_effect_ran_once('completed')

    def test_the_status_strings_are_unchanged(self):
        self.build()
        self.kill()
        CTM.reconcile_running()
        self.assertEqual(self.read()['status'], 'killed')
        self.assertEqual(task_status.ALL_STATUSES,
                         ('running', WAITING, 'completed', 'error', 'killed',
                          'terminated'))


class ExitCodeTests(Harness):
    """Item 4: same verdict whichever process notices first."""

    def settle(self, **meta):
        self.task(status='running', created_at=time.time() - 300, **meta)
        CTM._reconcile_status(self.read(), self.dir())
        return self.read()

    def test_a_crash_reads_error_with_the_orchestrators_text(self):
        self.task(status='running', created_at=time.time() - 300)
        self.exit_code('3\n')
        CTM._reconcile_status(self.read(), self.dir())
        on_disk = self.read()
        self.assertEqual(on_disk['status'], 'error')
        self.assertEqual(on_disk['error'], 'agent exited with code 3')
        self.assertEqual(on_disk['exit_code'], 3)
        self.assertEqual(self.published(), ['error'])

    def test_a_crash_beats_a_restart(self):
        self.task(status='running', created_at=time.time() - 300, boot_id='older')
        self.exit_code('1\n')
        CTM._reconcile_status(self.read(), self.dir())
        self.assertEqual(self.read()['status'], 'error')

    def test_a_clean_exit_beats_a_restart(self):
        self.task(status='running', created_at=time.time() - 300, boot_id='older')
        self.exit_code('0\n')
        CTM._reconcile_status(self.read(), self.dir())
        on_disk = self.read()
        self.assertEqual(on_disk['status'], 'completed')
        self.assertEqual(on_disk['exit_code'], 0)
        self.assertNotIn('error', on_disk)

    def test_no_exit_file_behaves_as_before(self):
        on_disk = self.settle()
        self.assertEqual(on_disk['status'], 'completed')
        self.assertNotIn('exit_code', on_disk)
        self.assertNotIn('error', on_disk)

    def test_no_exit_file_after_a_restart_is_still_terminated(self):
        on_disk = self.settle(boot_id='older')
        self.assertEqual(on_disk['status'], 'terminated')
        self.assertEqual(on_disk['termination_reason'], 'pod_restart')
        self.assertNotIn('exit_code', on_disk)
        self.assertNotIn('error', on_disk)

    def test_a_garbled_exit_file_counts_as_none(self):
        self.task(status='running', created_at=time.time() - 300)
        self.exit_code('')
        CTM._reconcile_status(self.read(), self.dir())
        on_disk = self.read()
        self.assertEqual(on_disk['status'], 'completed')
        self.assertNotIn('exit_code', on_disk)

    def test_server_and_orchestrator_reach_the_same_verdict(self):
        for code, boot in (('0\n', BOOT), ('0\n', 'older'), ('7\n', BOOT),
                           ('7\n', 'older'), (None, BOOT), (None, 'older')):
            with self.subTest(code=code, boot=boot):
                verdicts = []
                for settle in (CTM._reconcile_status, orch._reconcile_status):
                    self.task(status='running', created_at=time.time() - 300,
                              boot_id=boot)
                    path = os.path.join(self.dir(), 'exit_code')
                    if os.path.exists(path):
                        os.remove(path)
                    if code is not None:
                        self.exit_code(code)
                    settle(self.read(), self.dir())
                    m = self.read()
                    verdicts.append((m['status'], m.get('exit_code')))
                self.assertEqual(verdicts[0], verdicts[1])


class LaunchWindowTests(Harness):
    """Item 5: tmux hasn't started, and `completed` could never be undone."""

    def sub_agent(self, age=0.0, **meta):
        return self.task(status='running', depth=1, workdir=self.tmp,
                         created_at=time.time() - age, **meta)

    def test_a_tick_during_a_real_spawn_cannot_finish_it(self):
        seen = []

        def tick_before_tmux_answers():
            self.assertEqual(CTM.reconcile_running(), 1)
            tid = next(e for e in os.listdir(self.tmp)
                       if os.path.isfile(os.path.join(self.dir(e), 'task.json')))
            seen.append(self.read(tid)['status'])

        self.tmux.on_launch = tick_before_tmux_answers
        res = orch._tool_spawn_agent(
            {'prompt': 'do it', 'assistant': 'ante', 'workdir': self.tmp})

        self.assertFalse(res.get('isError'), res)
        self.assertEqual(seen, ['running'])
        tid = json.loads(res['content'][0]['text'])['task_id']
        self.assertEqual(self.read(tid)['status'], 'running')
        self.assertEqual(self.fired(), {})

    def test_a_deferred_tick_writes_nothing_and_fires_nothing(self):
        self.sub_agent()
        before = self.raw()
        CTM.reconcile_running()
        self.assertEqual(self.raw(), before)
        self.assertEqual(self.fired(), {})

    def test_a_dead_session_settles_once_the_window_passes(self):
        self.sub_agent(age=GRACE + 1)
        CTM.reconcile_running()
        self.assertEqual(self.read()['status'], 'completed')
        self.assertEqual(self.published(), ['completed'])

    def test_an_exit_code_settles_inside_the_window(self):
        self.sub_agent()
        self.exit_code('2\n')
        CTM.reconcile_running()
        self.assertEqual(self.read()['status'], 'error')

    def test_a_clean_exit_code_settles_inside_the_window(self):
        self.sub_agent()
        self.exit_code('0\n')
        CTM.reconcile_running()
        self.assertEqual(self.read()['status'], 'completed')

    def test_a_restart_settles_inside_the_window(self):
        self.sub_agent(boot_id='older')
        CTM.reconcile_running()
        self.assertEqual(self.read()['status'], 'terminated')

    def test_a_dashboard_build_is_not_held_back(self):
        # `_launching` covers these; a missing session means gone, as today.
        self.task(status='running', created_at=time.time())
        CTM.reconcile_running()
        self.assertEqual(self.read()['status'], 'completed')

    def test_a_managed_worktree_launch_gets_the_lock_timeout_too(self):
        total = GRACE + worktrees.lock_timeout()
        with mock.patch.object(worktrees, 'launch_takes_lock', return_value=True):
            self.sub_agent(age=total - 5)
            CTM.reconcile_running()
            self.assertEqual(self.read()['status'], 'running')

            self.sub_agent(age=total + 1)
            CTM.reconcile_running()
            self.assertEqual(self.read()['status'], 'completed')

    def test_launch_takes_lock_finds_a_manifest_above_the_workdir(self):
        root = os.path.join(self.tmp, 'wt')
        nested = os.path.join(root, 'charts', 'workspace')
        os.makedirs(nested)
        self.assertFalse(worktrees.launch_takes_lock(nested))
        with open(os.path.join(root, worktrees.MANIFEST), 'w') as f:
            f.write('{}')
        self.assertTrue(worktrees.launch_takes_lock(nested))

    def test_a_record_with_no_birth_time_is_not_held_back(self):
        self.task(status='running', depth=1, workdir=self.tmp)
        CTM.reconcile_running()
        self.assertEqual(self.read()['status'], 'completed')

    def test_a_live_session_is_reconciled_as_usual(self):
        self.tmux.alive = True
        self.sub_agent()
        CTM.reconcile_running()
        self.assertEqual(self.read()['status'], 'running')
        self.assertIn(['tmux', 'capture-pane', '-t', 'sess-t', '-p'], self.tmux.calls)


class FeedNoiseTests(Harness):
    """A fan-out is one row and no notifications, however it was settled."""

    MOCKED = (('event', server.EventBroker, 'publish'),
              ('snapshot', server.WorktreeManager, 'snapshot_async'),
              ('hook', CTM, '_fire_completion_hook'),
              ('ingest', CTM, 'ingest_usage'))

    def setUp(self):
        super().setUp()
        push_notify.PushTokenStore.register(
            'ExponentPushToken[fanout]', 'ios', 'api:owner')
        self.whatsapp = mock.Mock()
        self.dispatched = []
        self.pushes = 0
        real = push_notify.dispatch

        def spy(item, **kw):
            self.dispatched.append(item)
            sent = real(item, **kw)
            self.pushes += bool(sent)
            return sent

        for patcher in (mock.patch.object(push_notify, 'dispatch', side_effect=spy),
                        mock.patch.object(server, 'GATEWAY_ENABLED', True),
                        mock.patch.object(server, 'get_gateway', lambda: None),
                        mock.patch.object(server, '_GATEWAY_REVIEW', self.whatsapp)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.task('parent', status='running', prompt='Refactor the parser\nand more',
                  sub_task_ids=[])
        self.tmux.alive_sessions.add('sess-parent')

    def child(self, i):
        return self.task(f'c{i}', status='running', parent_task_id='parent',
                         depth=1, created_at=time.time() - 300,
                         workdir=self.tmp, prompt=f'child {i}')

    def rows(self):
        return FM.list(limit=50)

    def test_a_five_agent_fan_out_is_one_row_and_no_notifications(self):
        for i in range(5):
            self.child(i)
        orch._tool_kill_agent({'task_id': 'c0'})
        self.exit_code('1\n', 'c1')
        orch._reconcile_status(self.read('c1'), self.dir('c1'))
        self.exit_code('0\n', 'c2')
        orch._reconcile_status(self.read('c2'), self.dir('c2'))
        # c3 and c4 are settled by the server itself, as a tick finds them.

        CTM.reconcile_running()

        rows = self.rows()
        self.assertEqual(len(rows), 1, [r['title'] for r in rows])
        self.assertEqual(rows[0]['title'],
                         'Sub-agents of Refactor the parser: 5 of 5 done, '
                         '2 failed or stopped')
        self.assertFalse(rows[0]['waiting'])
        self.assertEqual(rows[0]['links'],
                         [{'label': 'Open parent task', 'ref': 'task:parent'}])
        self.assertTrue(self.dispatched)
        self.assertEqual(self.pushes, 0, 'a fan-out pushed to a phone')
        self.assertFalse(any(push_notify.should_push(i) for i in self.dispatched))
        self.whatsapp.on_feed_item.assert_not_called()

    def test_a_row_only_counts_settled_children(self):
        for i in range(3):
            self.child(i)
        orch._tool_kill_agent({'task_id': 'c0'})
        CTM._run_missed_settle_effects(self.read('c0'), self.dir('c0'))
        self.assertEqual(self.rows()[0]['title'],
                         'Sub-agents of Refactor the parser: 1 of 3 done, '
                         '1 failed or stopped')

    def test_a_sub_agent_going_quiet_is_no_row_and_no_push(self):
        self.child(0)
        CTM._transition(self.dir('c0'), WAITING, screen='')
        self.assertEqual(self.read('c0')['status'], WAITING)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.pushes, 0)

    def test_a_top_level_build_keeps_its_row_and_push(self):
        self.task('solo', status='running', prompt='solo job')
        CTM._transition(self.dir('solo'), WAITING, screen='')
        self.assertEqual([r['title'] for r in self.rows()],
                         ['Task waiting on you: solo job'])
        self.assertEqual(self.pushes, 1)

    def test_a_top_level_build_killed_elsewhere_keeps_its_own_row(self):
        self.task('solo', status='running', prompt='solo job',
                  created_at=time.time() - 300)
        orch._tool_kill_agent({'task_id': 'solo'})
        CTM.reconcile_running()
        self.assertEqual([r['title'] for r in self.rows()],
                         ['Task was stopped: solo job'])

    def test_a_board_build_keeps_its_review_row(self):
        link = {'label': 'Review KC-1', 'ref': 'board:b:1'}
        self.child(0)
        with mock.patch.object(FM, '_board_review_link', return_value=link):
            CTM._transition(self.dir('c0'), 'completed')
        self.assertEqual(self.rows()[0]['links'][0], link)
        self.assertEqual(self.rows()[0]['title'], 'Task finished: child 0')

    def test_a_missing_parent_record_still_makes_a_row(self):
        self.task('c', status='running', parent_task_id='gone', depth=1,
                  created_at=time.time() - 300, workdir=self.tmp)
        CTM._transition(self.dir('c'), 'completed')
        self.assertEqual(self.rows()[0]['title'],
                         'Sub-agents of gone: 1 of 1 done')

    def test_a_worktree_child_still_queues_its_publish_probe(self):
        publisher = mock.Mock()
        self.task('c', status='running', parent_task_id='parent', depth=1,
                  created_at=time.time() - 300, workdir=self.tmp,
                  worktree={'path': '/nowhere', 'branch': 'b'})
        with mock.patch.object(server, '_PUBLISHER', publisher):
            CTM._transition(self.dir('c'), 'completed')
        publisher.queue_probe.assert_called_once_with('c')


if __name__ == '__main__':
    unittest.main()
