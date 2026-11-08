"""`waiting_reason`: why a `waiting-for-input` Build is parked.

The status alone cannot tell a Build that needs a person from one whose turn
ended, so task.json and the API say which while it waits. The field is
additive: the status string, the event payload and every notification stay
exactly as they were.

tmux is simulated via subprocess.run stubs and TASKS_DIR is a tempdir (see
task_state_machine_test.Base), so nothing here reaches live workspace state.

Run with:    python3 -m unittest tests.task_waiting_reason_test
(from charts/workspace/)
"""

import hashlib
import itertools
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import hypervisor_session  # noqa: E402
import mcp_agent_orchestrator as orch  # noqa: E402
import mcp_dashboard  # noqa: E402
import server  # noqa: E402
import task_status  # noqa: E402
from task_state_machine_test import Base  # noqa: E402

CTM = server.ClaudeTaskManager
WAITING = 'waiting-for-input'
TERMINAL = ('completed', 'error', 'killed', 'terminated')

PROSE = 'All done, the tests pass. Want me to open a PR?\n> '
QUESTION = (
    'Do you want to proceed?\n'
    '❯ 1. Yes\n'
    "  2. Yes, and don't ask again\n"
    '  3. No, and tell Claude what to do differently\n'
)
TRUST_SCREEN = (
    '  Do you trust the files in this folder?\n'
    '  /home/dev/some/repo\n'
    '  ❯ 1. Yes, proceed\n'
    '    2. No, exit\n'
)


def digest(screen):
    return hashlib.sha1(
        server.strip_ansi(screen).encode('utf-8', 'replace')).hexdigest()


def pane(screen, detail_screen=None):
    """subprocess.run stub: the session is alive and shows `screen`. A capture
    with -J (the detail and Mission Control read) shows `detail_screen`."""
    def run(*args, **kwargs):
        argv = args[0] if args else kwargs.get('args', [])
        if argv[:2] == ['tmux', 'capture-pane']:
            wide = '-J' in argv and detail_screen is not None
            return mock.Mock(returncode=0, stdout=detail_screen if wide else screen,
                             stderr='')
        return mock.Mock(returncode=0, stdout='', stderr='')
    return run


class WaitingBase(Base):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(CTM, 'boot_id', return_value='boot-1')
        patcher.start()
        self.addCleanup(patcher.stop)

    def _running(self, screen, tid='t', **meta):
        """A Build whose screen has been static for longer than the idle
        threshold, so the next poll moves it to waiting."""
        return self._task(
            tid, status='running', boot_id='boot-1', pane_hash=digest(screen),
            last_activity_at=time.time() - server.IDLE_WAITING_SECONDS - 5,
            **meta)

    def _waiting(self, screen, tid='t', **meta):
        """A Build already parked on `screen`."""
        return self._task(
            tid, status=WAITING, waiting_for_input=True, boot_id='boot-1',
            pane_hash=digest(screen), last_activity_at=time.time() - 3600,
            **meta)

    def _poll(self, screen, detail_screen=None):
        with mock.patch.object(server.subprocess, 'run',
                               side_effect=pane(screen, detail_screen)) as run:
            CTM.reconcile_running()
        return run


class DerivationTests(unittest.TestCase):
    """The precedence on its own: trust_dialog, undelivered, question, idle."""

    def test_each_reason(self):
        for meta, screen, want in (
                ({'stalled_at_trust_dialog': True}, PROSE, 'trust_dialog'),
                ({'prompt_delivered': False}, PROSE, 'undelivered'),
                ({'prompt_delivered': True}, QUESTION, 'question'),
                ({'prompt_delivered': True}, PROSE, 'idle')):
            with self.subTest(want=want):
                self.assertEqual(CTM._waiting_reason(meta, screen), want)

    def test_precedence_across_every_combination(self):
        for stalled, undelivered, question in itertools.product(
                (True, False), repeat=3):
            meta = {'prompt_delivered': not undelivered}
            if stalled:
                meta['stalled_at_trust_dialog'] = True
            want = ('trust_dialog' if stalled else
                    'undelivered' if undelivered else
                    'question' if question else 'idle')
            with self.subTest(stalled=stalled, undelivered=undelivered,
                              question=question):
                self.assertEqual(
                    CTM._waiting_reason(meta, QUESTION if question else PROSE),
                    want)

    def test_only_a_literal_false_is_undelivered(self):
        # Absent or None means the delivery thread has not reported yet.
        for delivered in ({}, {'prompt_delivered': None}):
            with self.subTest(delivered=delivered):
                self.assertEqual(CTM._waiting_reason(delivered, PROSE), 'idle')

    def test_a_question_in_plain_prose_reads_idle(self):
        # Why `idle` must never be worded as done: the heuristic cannot see it.
        self.assertEqual(CTM._waiting_reason({}, PROSE), 'idle')

    def test_a_parser_failure_does_not_break_the_reconcile(self):
        with mock.patch.object(server, 'parse_screen_prompt',
                               side_effect=ValueError('boom')):
            self.assertEqual(CTM._waiting_reason({}, QUESTION), 'idle')


class EntryTests(WaitingBase):
    def test_entering_waiting_records_each_reason(self):
        for name, screen, meta, want in (
                ('question', QUESTION, {'prompt_delivered': True}, 'question'),
                ('idle', PROSE, {'prompt_delivered': True}, 'idle'),
                ('undelivered', PROSE, {'prompt_delivered': False}, 'undelivered'),
                ('trust', TRUST_SCREEN, {'prompt_delivered': True}, 'trust_dialog')):
            with self.subTest(name=name):
                self._running(screen, tid=name, **meta)
                self._poll(screen)
                got = self._read(name)
                self.assertEqual(got['status'], WAITING)
                self.assertEqual(got['waiting_reason'], want)
                shutil.rmtree(self._dir(name))

    def test_trust_outranks_undelivered_on_entry(self):
        self._running(TRUST_SCREEN, prompt_delivered=False)
        self._poll(TRUST_SCREEN)
        self.assertEqual(self._read()['waiting_reason'], 'trust_dialog')

    def test_undelivered_outranks_a_question_on_entry(self):
        self._running(QUESTION, prompt_delivered=False)
        self._poll(QUESTION)
        self.assertEqual(self._read()['waiting_reason'], 'undelivered')

    def test_no_reason_before_the_build_goes_waiting(self):
        self._task('t', status='running', boot_id='boot-1',
                   pane_hash=digest(PROSE), last_activity_at=time.time())
        self._poll(PROSE)
        self.assertEqual(self._read()['status'], 'running')
        self.assertNotIn('waiting_reason', self._read())


class ClearingTests(WaitingBase):
    def test_cleared_when_the_screen_moves_again(self):
        self._waiting(PROSE, waiting_reason='idle')
        self._poll('the agent is working again')
        got = self._read()
        self.assertEqual(got['status'], 'running')
        self.assertNotIn('waiting_reason', got)

    def test_cleared_on_every_way_out_of_waiting(self):
        for to in ('running',) + TERMINAL:
            with self.subTest(to=to):
                d = self._task(to, status=WAITING, waiting_reason='question')
                CTM._transition(d, to)
                self.assertNotIn('waiting_reason', self._read(to))

    def test_cleared_when_the_session_is_gone(self):
        self._waiting(PROSE, waiting_reason='idle')
        with mock.patch.object(
                server.subprocess, 'run',
                return_value=mock.Mock(returncode=1, stdout='', stderr='')):
            CTM.reconcile_running()
        got = self._read()
        self.assertEqual(got['status'], 'completed')
        self.assertNotIn('waiting_reason', got)

    def test_a_refused_move_leaves_the_reason(self):
        d = self._task('t', status=WAITING, waiting_reason='question')
        CTM._transition(d, 'error', guard=lambda m: False)
        self.assertEqual(self._read()['waiting_reason'], 'question')


class StalenessTests(WaitingBase):
    """The reason's inputs move while the status stays waiting."""

    def assert_silent(self):
        self.assertEqual(self.statuses_published(), [])
        self.effects['feed_waiting'].assert_not_called()

    def test_trust_stall_noted_after_entry(self):
        self._waiting(TRUST_SCREEN, waiting_reason='idle')
        self._poll(TRUST_SCREEN)
        got = self._read()
        self.assertEqual(got['status'], WAITING)
        self.assertTrue(got['stalled_at_trust_dialog'])
        self.assertEqual(got['waiting_reason'], 'trust_dialog')
        self.assert_silent()

    def test_trust_stall_retracted_falls_through(self):
        self._waiting(PROSE, waiting_reason='trust_dialog',
                      stalled_at_trust_dialog=True, prompt_delivered=True)
        self._poll(PROSE)
        got = self._read()
        self.assertEqual(got['status'], WAITING)
        self.assertNotIn('stalled_at_trust_dialog', got)
        self.assertEqual(got['waiting_reason'], 'idle')
        self.assert_silent()

    def test_delivery_failure_recorded_after_entry(self):
        self._waiting(PROSE, waiting_reason='idle', prompt_delivered=False)
        self._poll(PROSE)
        self.assertEqual(self._read()['waiting_reason'], 'undelivered')
        self.assert_silent()

    def test_redelivery_recorded_after_entry(self):
        self._waiting(QUESTION, waiting_reason='undelivered', prompt_delivered=True)
        self._poll(QUESTION)
        self.assertEqual(self._read()['waiting_reason'], 'question')
        self.assert_silent()

    def test_build_parked_before_the_field_existed_is_backfilled(self):
        self._waiting(QUESTION, prompt_delivered=True)
        self._poll(QUESTION)
        self.assertEqual(self._read()['waiting_reason'], 'question')
        self.assert_silent()

    def test_a_steady_poll_writes_nothing(self):
        self._waiting(PROSE, waiting_reason='idle', prompt_delivered=True)
        before = self._raw()
        self._poll(PROSE)
        self.assertEqual(self._raw(), before)

    def test_a_poll_adds_no_tmux_call(self):
        self._waiting(PROSE, waiting_reason='trust_dialog',
                      stalled_at_trust_dialog=True, prompt_delivered=True)
        run = self._poll(PROSE)
        verbs = [c.args[0][1] for c in run.call_args_list]
        self.assertEqual(sorted(verbs), ['capture-pane', 'has-session'])

    def test_refresh_does_not_write_once_the_build_has_left_waiting(self):
        # Another writer moved it after this poll read task.json.
        d = self._task('t', status='running')
        CTM._refresh_waiting_reason(
            {'status': WAITING, 'waiting_reason': 'idle'}, d, QUESTION)
        self.assertNotIn('waiting_reason', self._read())


class ApiTests(WaitingBase):
    def _row(self, tid='t'):
        return next(r for r in CTM.list_tasks() if r['task_id'] == tid)

    def test_list_row_carries_the_reason_while_waiting(self):
        self._waiting(PROSE, waiting_reason='idle', prompt_delivered=True)
        with mock.patch.object(server.subprocess, 'run', side_effect=pane(PROSE)):
            self.assertEqual(self._row()['waiting_reason'], 'idle')

    def test_list_row_has_no_field_when_not_waiting(self):
        self._task('t', status='completed', tmux_session='s')
        self.assertNotIn('waiting_reason', self._row())

    def test_detail_carries_the_reason(self):
        self._waiting(PROSE, waiting_reason='idle', prompt_delivered=True)
        with mock.patch.object(server.subprocess, 'run', side_effect=pane(PROSE)):
            self.assertEqual(CTM.get_task('t')['waiting_reason'], 'idle')

    def test_detail_never_calls_a_detected_prompt_idle(self):
        # The wider capture sees a prompt the visible screen does not.
        self._waiting(PROSE, waiting_reason='idle', prompt_delivered=True)
        with mock.patch.object(server.subprocess, 'run',
                               side_effect=pane(PROSE, QUESTION)):
            got = CTM.get_task('t')
        self.assertIsNotNone(got['pending_prompt'])
        self.assertEqual(got['waiting_reason'], 'question')
        self.assertEqual(self._read()['waiting_reason'], 'idle')

    def test_detail_keeps_a_higher_reason_over_a_detected_prompt(self):
        self._waiting(QUESTION, waiting_reason='undelivered', prompt_delivered=False)
        with mock.patch.object(server.subprocess, 'run', side_effect=pane(QUESTION)):
            self.assertEqual(CTM.get_task('t')['waiting_reason'], 'undelivered')


class ClientCompatibilityTests(WaitingBase):
    """A client that ignores `waiting_reason` must not notice this feature."""

    def test_status_vocabulary_is_unchanged(self):
        self.assertEqual(
            server.PrometheusMetricsCollector.TASK_STATUSES,
            ('running', WAITING, 'completed', 'error', 'killed', 'terminated'))
        self.assertIn(WAITING, hypervisor_session.WATCH_TASK_FIRE_STATUSES)
        self.assertIn(WAITING, task_status.QUIESCENT_STATUSES)

    def test_every_reason_keeps_the_same_status_and_flag(self):
        for screen, meta in ((QUESTION, {}), (PROSE, {}),
                             (TRUST_SCREEN, {}), (PROSE, {'prompt_delivered': False})):
            with self.subTest(screen=screen[:12], meta=meta):
                self._running(screen, **meta)
                self._poll(screen)
                got = self._read()
                self.assertEqual(got['status'], WAITING)
                self.assertIs(got['waiting_for_input'], True)
                shutil.rmtree(self._dir('t'))

    def test_every_reason_notifies_exactly_as_before(self):
        for screen, meta in ((QUESTION, {}), (PROSE, {}), (TRUST_SCREEN, {}),
                             (PROSE, {'prompt_delivered': False})):
            with self.subTest(screen=screen[:12], meta=meta):
                self.effects['feed_waiting'].reset_mock()
                self._running(screen, **meta)
                self._poll(screen)
                self.effects['feed_waiting'].assert_called_once()
                shutil.rmtree(self._dir('t'))

    def test_the_event_payload_is_unchanged(self):
        self._running(PROSE)
        self._poll(PROSE)
        self.effects['event'].assert_called_once_with(
            'task.status', {'task_id': 't', 'status': WAITING})

    def test_a_list_row_differs_only_by_the_new_key(self):
        self._waiting(PROSE, waiting_reason='idle', prompt_delivered=True)
        with mock.patch.object(server.subprocess, 'run', side_effect=pane(PROSE)):
            row = next(iter(CTM.list_tasks()))
        self._task('t', status=WAITING, waiting_for_input=True, boot_id='boot-1',
                   pane_hash=digest(PROSE), last_activity_at=row['last_activity_at'],
                   prompt_delivered=True)
        with mock.patch.object(CTM, '_refresh_waiting_reason'), \
             mock.patch.object(server.subprocess, 'run', side_effect=pane(PROSE)):
            legacy = next(iter(CTM.list_tasks()))
        self.assertEqual({k: v for k, v in row.items() if k != 'waiting_reason'},
                         legacy)


class MissionControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kcwr-mc-')
        for target, attr, value in (
                (CTM, 'TASKS_DIR', self.tmp),
                (server, '_HYPERVISOR_AVAILABLE', False)):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        recon = mock.patch.object(CTM, '_reconcile_status',
                                  side_effect=lambda meta, task_dir: None)
        recon.start()
        self.addCleanup(recon.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _task(self, tid, **meta):
        d = os.path.join(self.tmp, tid)
        os.makedirs(d)
        m = {'task_id': tid, 'status': WAITING, 'tmux_session': tid,
             'created_at': time.time() - 600, 'last_activity_at': time.time() - 60}
        m.update(meta)
        with open(os.path.join(d, 'task.json'), 'w') as f:
            json.dump(m, f)

    def _queue(self, screen=''):
        with mock.patch.object(server.subprocess, 'run', side_effect=pane(screen)):
            return server.missioncontrol_queue()

    def test_cards_carry_the_reason(self):
        for reason in ('question', 'trust_dialog', 'undelivered', 'idle'):
            self._task(reason, waiting_reason=reason)
        for card in self._queue()['cards']:
            self.assertEqual(card['waiting_reason'], card['ref_id'])

    def test_needs_you_counts_only_the_first_group(self):
        for reason in ('question', 'trust_dialog', 'undelivered', 'idle'):
            self._task(reason, waiting_reason=reason)
        pulse = self._queue()['pulse']
        self.assertEqual(pulse['waiting'], 4)
        self.assertEqual(pulse['needs_you'], 3)

    def test_an_unknown_reason_counts_as_needing_you(self):
        self._task('legacy')
        pulse = self._queue()['pulse']
        self.assertEqual((pulse['waiting'], pulse['needs_you']), (1, 1))

    def test_idle_cards_sort_after_needs_you_within_waiting(self):
        now = time.time()
        self._task('idle-new', waiting_reason='idle', last_activity_at=now - 1)
        self._task('asks-old', waiting_reason='question', last_activity_at=now - 500)
        self.assertEqual([c['ref_id'] for c in self._queue()['cards']],
                         ['asks-old', 'idle-new'])

    def test_quick_reply_buttons_never_sit_on_an_idle_card(self):
        self._task('t', waiting_reason='idle')
        card = self._queue(QUESTION)['cards'][0]
        self.assertIsNotNone(card['waiting_prompt'])
        self.assertEqual(card['waiting_reason'], 'question')

    def test_non_waiting_cards_have_no_reason(self):
        self._task('r', status='running')
        self.assertIsNone(self._queue()['cards'][0]['waiting_reason'])

    def test_idle_timeline_does_not_claim_your_input_is_needed(self):
        card = {'state': 'waiting', 'waiting_reason': 'idle', 'waiting_since': 1}
        text = [e['text'] for e in server._mc_task_timeline(
            {'created_at': 0, 'prompt': 'x'}, card, [])]
        self.assertIn('Turn ended, no question detected', text)
        self.assertNotIn('Waiting on your input', text)


class AgentToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kcwr-orch-')
        patcher = mock.patch.object(orch, 'TASKS_DIR', self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _task(self, tid='t', **meta):
        d = os.path.join(self.tmp, tid)
        os.makedirs(d)
        m = {'task_id': tid, 'status': WAITING, 'tmux_session': tid,
             'waiting_reason': 'question', 'parent_task_id': 'P'}
        m.update(meta)
        with open(os.path.join(d, 'task.json'), 'w') as f:
            json.dump(m, f)
        return d

    def _stored(self, tid='t'):
        with open(os.path.join(self.tmp, tid, 'task.json')) as f:
            return json.load(f)

    @staticmethod
    def _body(result):
        return json.loads(result['content'][0]['text'])

    def _alive(self):
        return mock.patch.object(
            orch.subprocess, 'run',
            return_value=mock.Mock(returncode=0, stdout='', stderr=''))

    def test_get_agent_status(self):
        self._task()
        with self._alive():
            body = self._body(orch._tool_get_agent_status({'task_id': 't'}))
        self.assertEqual(body['status'], WAITING)
        self.assertEqual(body['waiting_reason'], 'question')

    def test_list_subagents(self):
        self._task()
        with self._alive():
            body = self._body(orch._tool_list_subagents({'parent_task_id': 'P'}))
        self.assertEqual(body['sub_tasks'][0]['waiting_reason'], 'question')

    def test_wait_for_agents_reports_why_a_child_is_pending(self):
        self._task()
        with self._alive():
            body = self._body(orch._tool_wait_for_agents(
                {'task_ids': ['t'], 'timeout': 0, 'poll_interval': 0}))
        self.assertEqual(body['results'][0]['waiting_reason'], 'question')

    def test_orchestrator_reconcile_clears_it(self):
        d = self._task()
        meta = self._stored()
        with mock.patch.object(
                orch.subprocess, 'run',
                return_value=mock.Mock(returncode=1, stdout='', stderr='')):
            orch._reconcile_status(meta, d)
        self.assertEqual(self._stored()['status'], 'completed')
        self.assertNotIn('waiting_reason', self._stored())

    def test_orchestrator_kill_clears_it(self):
        self._task()
        with self._alive():
            orch._tool_kill_agent({'task_id': 't'})
        self.assertEqual(self._stored()['status'], 'killed')
        self.assertNotIn('waiting_reason', self._stored())

    def test_dashboard_tools_pass_it_through(self):
        row = {'task_id': 't', 'status': WAITING, 'waiting_reason': 'idle'}
        with mock.patch.object(mcp_dashboard, '_api', return_value=(200, row)):
            text = mcp_dashboard._t_get_task({'task_id': 't'})['content'][0]['text']
        self.assertIn('"waiting_reason": "idle"', text)


if __name__ == '__main__':
    unittest.main()
