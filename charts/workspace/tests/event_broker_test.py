"""Unit tests for the SSE EventBroker and its task event emit points (#93).

Covers the in-process pub/sub (subscribe/publish/unsubscribe, fan-out,
no-subscriber safety, slow-consumer drop-oldest) and the integration where
reconciling a finished task publishes a `task.status` event.

Run with:    python3 -m unittest tests.event_broker_test
(from charts/workspace/)
"""

import json
import os
import queue
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402

EB = server.EventBroker
CTM = server.ClaudeTaskManager


def _tmux_dead(*args, **kwargs):
    argv = args[0] if args else kwargs.get('args', [])
    if len(argv) >= 2 and argv[0] == 'tmux' and argv[1] == 'has-session':
        return mock.Mock(returncode=1, stdout='', stderr='no session')
    return mock.Mock(returncode=0, stdout='', stderr='')


class BrokerTestCase(unittest.TestCase):
    def setUp(self):
        # Isolate the class-level subscriber set per test.
        self._orig = set(EB._subscribers)
        EB._subscribers = set()
        self.addCleanup(lambda: setattr(EB, '_subscribers', self._orig))


class BrokerBasicsTests(BrokerTestCase):
    def test_subscriber_receives_published_event(self):
        q = EB.subscribe()
        EB.publish('task.status', {'task_id': 't1', 'status': 'completed'})
        evt = q.get_nowait()
        self.assertEqual(evt['type'], 'task.status')
        self.assertEqual(evt['data']['task_id'], 't1')
        self.assertIn('ts', evt)

    def test_fan_out_to_all_subscribers(self):
        q1, q2 = EB.subscribe(), EB.subscribe()
        self.assertEqual(EB.subscriber_count(), 2)
        EB.publish('task.created', {'task_id': 'x'})
        self.assertEqual(q1.get_nowait()['data']['task_id'], 'x')
        self.assertEqual(q2.get_nowait()['data']['task_id'], 'x')

    def test_unsubscribe_stops_delivery(self):
        q = EB.subscribe()
        EB.unsubscribe(q)
        self.assertEqual(EB.subscriber_count(), 0)
        EB.publish('task.status', {'task_id': 't'})
        with self.assertRaises(queue.Empty):
            q.get_nowait()

    def test_publish_with_no_subscribers_is_safe(self):
        # Must not raise.
        evt = EB.publish('task.status', {'task_id': 't'})
        self.assertEqual(evt['type'], 'task.status')

    def test_publish_defaults_empty_data(self):
        q = EB.subscribe()
        EB.publish('ready')
        self.assertEqual(q.get_nowait()['data'], {})

    def test_slow_consumer_drops_oldest(self):
        with mock.patch.object(EB, 'QUEUE_MAX', 3):
            q = EB.subscribe()
            for i in range(5):
                EB.publish('task.status', {'n': i})
            drained = []
            try:
                while True:
                    drained.append(q.get_nowait()['data']['n'])
            except queue.Empty:
                pass
        # Bounded to QUEUE_MAX, oldest dropped, newest retained.
        self.assertEqual(len(drained), 3)
        self.assertEqual(drained[-1], 4)
        self.assertNotIn(0, drained)


class ReconcileEmitTests(BrokerTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix='kcevt-')
        self._orig_dir = CTM.TASKS_DIR
        CTM.TASKS_DIR = self.tmp
        self.addCleanup(self._restore)

    def _restore(self):
        CTM.TASKS_DIR = self._orig_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _task(self, tid, **meta):
        d = os.path.join(self.tmp, tid)
        os.makedirs(d, exist_ok=True)
        m = {'task_id': tid}
        m.update(meta)
        with open(os.path.join(d, 'task.json'), 'w') as f:
            json.dump(m, f)

    def _drain(self, q):
        out = []
        try:
            while True:
                out.append(q.get_nowait())
        except queue.Empty:
            pass
        return out

    def test_completed_transition_publishes_task_status(self):
        q = EB.subscribe()
        self._task('t1', status='running', tmux_session='kube-coder-t1')
        with mock.patch.object(server.subprocess, 'run', side_effect=_tmux_dead):
            CTM.reconcile_running()
        events = self._drain(q)
        status_events = [e for e in events if e['type'] == 'task.status']
        self.assertEqual(len(status_events), 1)
        self.assertEqual(status_events[0]['data']['status'], 'completed')
        self.assertEqual(status_events[0]['data']['task_id'], 't1')

    def test_no_event_when_nothing_changes(self):
        q = EB.subscribe()
        self._task('done', status='completed', tmux_session='x')
        with mock.patch.object(server.subprocess, 'run', side_effect=_tmux_dead):
            CTM.reconcile_running()
        self.assertEqual(self._drain(q), [])  # terminal task → no transition → no event


if __name__ == '__main__':
    unittest.main()
