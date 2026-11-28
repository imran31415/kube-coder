"""Follow-up delivery is at-most-once and observable (#927).

`send_task_message` gave up after 30 s while the server's paste+submit ran on,
and a retry (or a human's Enter) then left the prompt doubled in the Build's
input box. These tests pin the contract that replaced it:

- a delivery is recorded on the task (`deliveries[]`) with the state the
  input box was left in, and can be read back by id;
- one delivery runs per Build at a time, and a repeat of the same
  idempotency key never pastes again;
- a client that times out mid-delivery and retries gets the first delivery's
  outcome, not a second paste.

The tmux side is faked one level up (`_deliver_prompt_outcome`), except where
a test drives the real paste path against tests.task_prompt_launch_test's
scripted pane. A real ThreadingHTTPServer carries the HTTP tests, and the MCP
tool is pointed at it, so the client timeout is a real socket timeout.

Run:  python3 -m unittest tests.followup_delivery_test   (from charts/workspace/)
"""

import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import mcp_dashboard as md  # noqa: E402
import server  # noqa: E402
from tests.task_prompt_launch_test import FakeSession  # noqa: E402

CTM = server.ClaudeTaskManager


def _alive_tmux(cmd, **kw):
    """`tmux has-session` succeeds; nothing else reaches tmux in these tests."""
    return mock.Mock(returncode=0, stdout='', stderr='')


class _Deliverer:
    """Stands in for `_deliver_prompt_outcome`: counts pastes, and can hold a
    delivery open until the test releases it (the slow TUI of #927)."""

    def __init__(self, outcome='delivered', block=False):
        self.outcome = outcome
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        if not block:
            self.release.set()

    def __call__(self, session_name, prompt_file, buf_name, submit=True, **kw):
        self.calls += 1
        self.started.set()
        self.release.wait(10)
        return {'state': self.outcome, 'reason': f'fake {self.outcome}'}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kc927-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tasks = os.path.join(self.tmp, 'tasks')
        os.makedirs(self.tasks)
        for p in (mock.patch.object(CTM, 'TASKS_DIR', self.tasks),
                  mock.patch.object(CTM, '_fire_completion_hook'),
                  mock.patch.object(server.EventBroker, 'publish'),
                  mock.patch.object(server.FeedManager, 'emit_task_waiting'),
                  mock.patch.object(server.FeedManager, 'emit_task_terminal')):
            p.start()
            self.addCleanup(p.stop)
        CTM._active_deliveries.clear()
        self.addCleanup(CTM._active_deliveries.clear)

    def _task(self, tid='t1', **meta):
        d = os.path.join(self.tasks, tid)
        os.makedirs(d, exist_ok=True)
        m = {'task_id': tid, 'tmux_session': f'kube-coder-{tid}',
             'status': 'waiting-for-input', 'waiting_for_input': True,
             'assistant': 'claude', 'created_at': time.time() - 60}
        m.update(meta)
        with open(os.path.join(d, 'task.json'), 'w') as f:
            json.dump(m, f)
        return d

    def _read(self, tid='t1'):
        with open(os.path.join(self.tasks, tid, 'task.json')) as f:
            return json.load(f)

    def _deliverer(self, **kw):
        fake = _Deliverer(**kw)
        p = mock.patch.object(CTM, '_deliver_prompt_outcome', side_effect=fake)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(fake.release.set)
        return fake

    def _settle(self, tid='t1', timeout=5):
        deadline = time.time() + timeout
        while CTM._active_deliveries.get(tid) and time.time() < deadline:
            time.sleep(0.01)


class DeliveryRecordTests(_Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(server.subprocess, 'run', side_effect=_alive_tmux)
        p.start()
        self.addCleanup(p.stop)

    def test_a_delivered_followup_is_recorded_and_readable(self):
        self._task()
        self._deliverer()
        res = CTM.followup('t1', 'and then?')
        self.assertEqual(res['kind'], 'sent')
        d = res['delivery']
        self.assertEqual(d['state'], CTM.DELIVERED)
        stored = self._read()
        self.assertEqual(stored['deliveries'][-1]['state'], CTM.DELIVERED)
        self.assertEqual(stored['followups'][-1]['delivery_id'], d['id'])
        self.assertEqual(stored['status'], 'running')
        got, status = CTM.get_delivery('t1', d['id'])
        self.assertEqual(got['state'], CTM.DELIVERED)
        self.assertNotIn('t1', CTM._active_deliveries)

    def test_a_failure_says_what_the_box_holds(self):
        self._task()
        self._deliverer(outcome=CTM.LEFT_UNSENT)
        res = CTM.followup('t1', 'and then?')
        self.assertEqual(res['kind'], 'failed')
        self.assertEqual(res['delivery']['state'], CTM.LEFT_UNSENT)
        self.assertIn('UNSENT', res['error'])
        # Nothing was sent, so nothing is recorded as a followup.
        self.assertFalse(self._read().get('followups'))
        # The old tuple API carries the same sentence.
        meta, err = CTM.send_followup('t1', 'and then?')
        self.assertIsNone(meta)
        self.assertIn('UNSENT', err)

    def test_a_second_message_while_one_is_mid_delivery_is_not_pasted(self):
        self._task()
        fake = self._deliverer(block=True)
        first = CTM.followup('t1', 'one', key='k1', wait=False)
        self.assertEqual(first['kind'], 'accepted')
        self.assertTrue(fake.started.wait(5))
        other = CTM.followup('t1', 'two')
        self.assertEqual(other['kind'], 'busy')
        self.assertIn(first['delivery']['id'], other['error'])
        fake.release.set()
        self._settle()
        self.assertEqual(fake.calls, 1)

    def test_a_repeated_key_never_pastes_twice(self):
        self._task()
        fake = self._deliverer(block=True)
        first = CTM.followup('t1', 'one', key='k1', wait=False)
        self.assertTrue(fake.started.wait(5))
        again = CTM.followup('t1', 'one', key='k1', wait=False)
        self.assertEqual(again['kind'], 'duplicate')
        self.assertEqual(again['delivery']['id'], first['delivery']['id'])
        self.assertEqual(again['delivery']['state'], CTM.PENDING)
        fake.release.set()
        self._settle()
        late = CTM.followup('t1', 'one', key='k1')
        self.assertEqual(late['kind'], 'duplicate')
        self.assertEqual(late['delivery']['state'], CTM.DELIVERED)
        self.assertEqual(fake.calls, 1)
        self.assertEqual(len(self._read()['followups']), 1)

    def test_a_key_that_was_not_delivered_may_be_sent_again(self):
        self._task()
        fake = self._deliverer(outcome=CTM.NOT_DELIVERED)
        self.assertEqual(CTM.followup('t1', 'one', key='k1')['kind'], 'failed')
        fake.outcome = CTM.DELIVERED
        self.assertEqual(CTM.followup('t1', 'one', key='k1')['kind'], 'sent')
        self.assertEqual(fake.calls, 2)

    def test_a_delivered_key_expires_after_the_window(self):
        self._task()
        fake = self._deliverer()
        CTM.followup('t1', 'carry on', key='k1')
        with mock.patch.object(CTM, 'FOLLOWUP_DEDUPE_WINDOW', 0):
            self.assertEqual(CTM.followup('t1', 'carry on', key='k1')['kind'],
                             'sent')
        self.assertEqual(fake.calls, 2)

    def test_a_delivery_the_server_lost_is_interrupted_and_not_resent(self):
        # Pending on disk, but no delivery in flight in this process: the
        # server restarted mid-paste. Nobody can say whether it was sent.
        self._task(deliveries=[{'id': 'dlv-0000aa', 'state': 'pending',
                                'key': 'k1', 'started_at': time.time() - 5}])
        fake = self._deliverer()
        got, _ = CTM.get_delivery('t1', 'dlv-0000aa')
        self.assertEqual(got['state'], CTM.INTERRUPTED)
        again = CTM.followup('t1', 'one', key='k1')
        self.assertEqual(again['kind'], 'duplicate')
        self.assertEqual(fake.calls, 0)
        self.assertEqual(CTM.get_task('t1')['deliveries'][0]['state'],
                         CTM.INTERRUPTED)

    def test_paste_only_is_recorded_as_pasted(self):
        self._task()
        self._deliverer()
        res = CTM.followup('t1', 'draft', submit=False)
        self.assertEqual(res['delivery']['state'], CTM.PASTED)
        self.assertFalse(self._read().get('followups'))

    def test_history_is_bounded(self):
        self._task()
        self._deliverer()
        with mock.patch.object(CTM, 'DELIVERY_HISTORY', 3):
            for i in range(5):
                CTM.followup('t1', f'msg {i}')
        self.assertEqual(len(self._read()['deliveries']), 3)

    def test_budget_covers_the_paste_appear_timeout(self):
        # The 30 s client of #927 was shorter than the 45 s appear wait alone.
        self.assertGreater(CTM.delivery_budget(), CTM.PASTE_APPEAR_TIMEOUT)


class RealPasteRetryTests(_Base):
    """The real paste path, against the scripted pane."""

    def test_retry_after_left_unsent_clears_the_box_and_sends_once(self):
        self._task()
        sess = FakeSession(swallow=99, unclearable=True)
        with mock.patch.object(server.subprocess, 'run', side_effect=sess.run), \
             mock.patch.object(server.time, 'sleep', side_effect=sess.sleep), \
             mock.patch.object(server.time, 'time', side_effect=sess.time):
            first = CTM.followup('t1', FakeSession.PROMPT, key='k1')
            self.assertEqual(first['delivery']['state'], CTM.LEFT_UNSENT)
            self.assertEqual(sess.visible, 1)
            # The box now accepts keys again (a human freed it, say).
            sess.swallow = 0
            sess.unclearable = False
            second = CTM.followup('t1', FakeSession.PROMPT, key='k1')
        self.assertEqual(second['kind'], 'sent')
        self.assertEqual(sess.submitted, [1])   # one copy, sent once
        self.assertEqual(sess.pastes, 2)


class _HttpBase(_Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(server.subprocess, 'run', side_effect=_alive_tmux)
        p.start()
        self.addCleanup(p.stop)
        auth = mock.patch.object(server.BrowserHandler, 'check_claude_auth',
                                 lambda self: True)
        auth.start()
        self.addCleanup(auth.stop)
        self.httpd = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0), server.BrowserHandler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        for p in (mock.patch.object(md, 'BASE_URL',
                                    f'http://127.0.0.1:{self.port}'),
                  mock.patch.object(md, '_token', lambda: 'tok'),
                  mock.patch.object(md, '_FOLLOWUP_POLL', 0.05)):
            p.start()
            self.addCleanup(p.stop)

    def _req(self, method, path, body=None, timeout=10):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(
            f'http://127.0.0.1:{self.port}{path}', data=data, method=method,
            headers={'Content-Type': 'application/json'} if data else {})
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read() or b'{}')
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}')


class HttpContractTests(_HttpBase):
    def test_sync_send_still_returns_the_task(self):
        # The web and mobile apps POST {prompt, submit} and read a task back.
        self._task()
        self._deliverer()
        status, body = self._req('POST', '/api/claude/tasks/t1/message',
                                 {'prompt': 'hi', 'submit': True})
        self.assertEqual(status, 200)
        self.assertEqual(body['task_id'], 't1')
        self.assertEqual(body['status'], 'running')
        self.assertEqual(body['delivery']['state'], 'delivered')

    def test_failed_send_names_the_box_state(self):
        self._task()
        self._deliverer(outcome=CTM.LEFT_UNSENT)
        status, body = self._req('POST', '/api/claude/tasks/t1/message',
                                 {'prompt': 'hi'})
        self.assertEqual(status, 409)
        self.assertEqual(body['delivery']['state'], 'left_unsent')
        self.assertIn('UNSENT', body['error'])

    def test_async_send_returns_202_and_the_delivery_reads_back(self):
        self._task()
        fake = self._deliverer(block=True)
        status, body = self._req('POST', '/api/claude/tasks/t1/message',
                                 {'prompt': 'hi', 'wait': False})
        self.assertEqual(status, 202)
        did = body['delivery']['id']
        self.assertGreater(body['delivery_budget_s'], 45)
        status, got = self._req('GET', f'/api/claude/tasks/t1/deliveries/{did}')
        self.assertEqual((status, got['delivery']['state']), (200, 'pending'))
        fake.release.set()
        self._settle()
        status, got = self._req('GET', f'/api/claude/tasks/t1/deliveries/{did}')
        self.assertEqual(got['delivery']['state'], 'delivered')
        status, _ = self._req('GET', '/api/claude/tasks/t1/deliveries/dlv-ffff')
        self.assertEqual(status, 404)

    def test_concurrent_message_is_409_not_a_second_paste(self):
        self._task()
        fake = self._deliverer(block=True)
        self._req('POST', '/api/claude/tasks/t1/message',
                  {'prompt': 'one', 'wait': False})
        self.assertTrue(fake.started.wait(5))
        status, body = self._req('POST', '/api/claude/tasks/t1/message',
                                 {'prompt': 'two'})
        self.assertEqual(status, 409)
        self.assertEqual(body['delivery']['state'], 'pending')
        fake.release.set()
        self._settle()
        self.assertEqual(fake.calls, 1)

    def test_bad_idempotency_key_is_400(self):
        self._task()
        status, _ = self._req('POST', '/api/claude/tasks/t1/message',
                              {'prompt': 'hi', 'idempotency_key': 7})
        self.assertEqual(status, 400)


class McpToolTests(_HttpBase):
    def _send(self, **kw):
        return md._t_send_task_message({'task_id': 't1', 'message': 'go', **kw})

    @staticmethod
    def _text(res):
        return res['content'][0]['text']

    def test_waits_for_the_outcome_and_says_delivered(self):
        self._task()
        fake = self._deliverer(block=True)
        threading.Timer(0.3, fake.release.set).start()
        res = self._send()
        self.assertFalse(res.get('isError'), self._text(res))
        self.assertIn('Delivered', self._text(res))

    def test_left_unsent_is_an_error_that_names_the_box(self):
        self._task()
        self._deliverer(outcome=CTM.LEFT_UNSENT)
        res = self._send()
        self.assertTrue(res.get('isError'))
        self.assertIn('UNSENT', self._text(res))

    def test_client_timeout_mid_delivery_then_retry_never_pastes_twice(self):
        # The #927 sequence, end to end. A caller's HTTP request gives up while
        # the server is still pasting; the caller retries. The retry must not
        # paste a second copy, and must end with the first delivery's real
        # outcome rather than a bare timeout.
        self._task()
        fake = self._deliverer(block=True)
        with mock.patch.object(md, 'HTTP_TIMEOUT', 0.3):
            # The old tool's call: a synchronous POST that times out.
            status, payload = md._api(
                'POST', '/api/claude/tasks/t1/message',
                body={'prompt': 'go', 'submit': True,
                      'idempotency_key': md._followup_key('t1', 'go')})
        self.assertEqual(status, 0)
        self.assertTrue(fake.started.is_set())
        # The retry, while the first delivery is still on the box.
        threading.Timer(0.3, fake.release.set).start()
        res = self._send()
        self.assertFalse(res.get('isError'), self._text(res))
        self.assertIn('Delivered', self._text(res))
        self.assertEqual(fake.calls, 1)
        self.assertEqual(len(self._read()['followups']), 1)
        # And a third call, after the fact, is still not a second send.
        res = self._send()
        self.assertIn('NOT sent again', self._text(res))
        self.assertEqual(fake.calls, 1)

    def test_unreachable_server_says_the_outcome_is_unknown(self):
        with mock.patch.object(md, 'BASE_URL', 'http://127.0.0.1:9'):
            res = self._send()
        self.assertTrue(res.get('isError'))
        self.assertIn('unknown', self._text(res))
        self.assertNotIn('HTTP 0', self._text(res))

    def test_still_pending_at_the_cap_hands_back_the_delivery(self):
        self._task()
        self._deliverer(block=True)
        with mock.patch.object(md, '_FOLLOWUP_WAIT_CAP', 0.2):
            res = self._send()
        self.assertTrue(res.get('isError'))
        self.assertIn('Still being delivered', self._text(res))
        self.assertIn('SAME message is safe', self._text(res))

    def test_allow_repeat_sends_an_identical_message_again(self):
        self._task()
        fake = self._deliverer()
        self._send()
        self.assertIn('NOT sent again', self._text(self._send()))
        res = self._send(allow_repeat=True)
        self.assertIn('Delivered', self._text(res))
        self.assertEqual(fake.calls, 2)

    def test_server_from_before_927_still_works(self):
        with mock.patch.object(md, '_api', return_value=(
                200, {'task_id': 't1', 'status': 'running'})):
            res = self._send()
        self.assertFalse(res.get('isError'))
        self.assertIn('running', self._text(res))


if __name__ == '__main__':
    unittest.main()
