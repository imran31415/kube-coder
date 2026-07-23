"""Unit tests for durable completion-hook delivery (issue #97).

Covers _deliver_hook (bounded retry + dead-letter, no-retry on permanent 4xx),
delivery-state recording on the task meta, and redeliver_hook.

urlopen is mocked; time.sleep is stubbed so backoff doesn't slow tests;
TASKS_DIR is a tempdir.

Run with:    python3 -m unittest tests.completion_hook_delivery_test
(from charts/workspace/)
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402

CTM = server.ClaudeTaskManager


class _SyncThread:
    """threading.Thread stand-in that runs target synchronously on start()."""
    def __init__(self, target=None, args=(), daemon=None, **kw):
        self._t, self._a = target, args

    def start(self):
        if self._t:
            self._t(*self._a)


class HookDeliveryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kchook-')
        self._orig = CTM.TASKS_DIR
        CTM.TASKS_DIR = self.tmp
        # No real backoff sleeps.
        p = mock.patch.object(server.time, 'sleep')
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._restore)

    def _restore(self):
        CTM.TASKS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _task(self, task_id='t1', **meta):
        d = os.path.join(self.tmp, task_id)
        os.makedirs(d, exist_ok=True)
        m = {'task_id': task_id}
        m.update(meta)
        with open(os.path.join(d, 'task.json'), 'w') as f:
            json.dump(m, f)
        return d

    def _delivery(self, task_id='t1'):
        with open(os.path.join(self.tmp, task_id, 'task.json')) as f:
            return json.load(f).get('hook_delivery')


class DeliverHookTests(HookDeliveryTestCase):
    def test_success_records_delivered(self):
        self._task('t1')
        cm = mock.MagicMock()
        cm.__enter__.return_value = mock.Mock(status=200)
        with mock.patch.object(CTM, '_hook_urlopen', return_value=cm):
            CTM._deliver_hook('t1', 'http://h/x', b'{}', {})
        d = self._delivery()
        self.assertEqual(d['state'], 'delivered')
        self.assertEqual(d['attempts'], 1)

    def test_retries_then_dead_letters(self):
        self._task('t1')
        with mock.patch.object(CTM, '_hook_urlopen',
                               side_effect=urllib.error.URLError('conn refused')) as uo:
            CTM._deliver_hook('t1', 'http://h/x', b'{}', {}, max_attempts=3)
        self.assertEqual(uo.call_count, 3)  # all attempts used
        d = self._delivery()
        self.assertEqual(d['state'], 'failed')
        self.assertEqual(d['attempts'], 3)
        self.assertIn('URLError', d['last_error'])

    def test_permanent_4xx_not_retried(self):
        self._task('t1')
        err = urllib.error.HTTPError('http://h/x', 400, 'Bad Request', {}, None)
        with mock.patch.object(CTM, '_hook_urlopen', side_effect=err) as uo:
            CTM._deliver_hook('t1', 'http://h/x', b'{}', {}, max_attempts=5)
        self.assertEqual(uo.call_count, 1)  # broke early — no wasted retries
        d = self._delivery()
        self.assertEqual(d['state'], 'failed')
        self.assertEqual(d['last_error'], 'HTTP 400')

    def test_429_is_retried(self):
        self._task('t1')
        err = urllib.error.HTTPError('http://h/x', 429, 'Too Many', {}, None)
        with mock.patch.object(CTM, '_hook_urlopen', side_effect=err) as uo:
            CTM._deliver_hook('t1', 'http://h/x', b'{}', {}, max_attempts=3)
        self.assertEqual(uo.call_count, 3)  # 429 is transient → retried


class RedeliverTests(HookDeliveryTestCase):
    def test_missing_task(self):
        ok, msg = CTM.redeliver_hook('ghost')
        self.assertFalse(ok)
        self.assertEqual(msg, 'task not found')

    def test_no_response_url(self):
        self._task('t1')  # no response_url
        ok, msg = CTM.redeliver_hook('t1')
        self.assertFalse(ok)
        self.assertIn('response_url', msg)

    def test_redeliver_runs_delivery(self):
        self._task('t1', response_url='http://h/x', status='completed')
        cm = mock.MagicMock()
        cm.__enter__.return_value = mock.Mock(status=200)
        with mock.patch.object(server.threading, 'Thread', _SyncThread), \
             mock.patch.object(CTM, 'get_task_output', return_value=''), \
             mock.patch.object(CTM, '_is_safe_response_url', return_value=True), \
             mock.patch.object(CTM, '_hook_urlopen', return_value=cm):
            ok, msg = CTM.redeliver_hook('t1')
        self.assertTrue(ok)
        self.assertEqual(self._delivery()['state'], 'delivered')


if __name__ == '__main__':
    unittest.main()
