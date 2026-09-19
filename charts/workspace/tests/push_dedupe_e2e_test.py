"""End-to-end: a task that keeps flipping to "waiting" pages the phone once (#685).

The bug report's own timeline — one task, six identical "Task waiting on you"
pushes between 02:06 and 03:37 — is replayed here through the real path:

    _reconcile_status (the running → waiting flip)
      → FeedManager.emit_task_waiting → FeedManager.emit (coalesce + read flag)
      → push_notify.dispatch (ledger) → urllib POST → an Expo push service

Two real sockets on loopback: the workspace's own BrowserHandler (devices
register, and the user reads / dismisses rows, over HTTP exactly as the apps
do) and a fake Expo push service that records every batch it receives. Only
three things are stubbed: tmux (there is none here), auth, and the ledger's
clock, so half an hour can pass in a millisecond. Nothing in push_notify or
FeedManager is mocked.

Run:  python3 -m unittest tests.push_dedupe_e2e_test   (from charts/workspace/)
"""

import http.server
import json
import os
import shutil
import socket
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
sys.path.insert(0, HERE)
import push_notify as pn  # noqa: E402
import server  # noqa: E402
from live_state import isolate_feed_and_push  # noqa: E402

CTM = server.ClaudeTaskManager
PHONE = 'ExponentPushToken[e2e-phone]'
MIN = 60


class _FakeExpo(http.server.BaseHTTPRequestHandler):
    """Records each POSTed batch; answers ok unless `ticket` says otherwise."""
    batches = []
    ticket = {'status': 'ok'}

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        messages = json.loads(self.rfile.read(length).decode('utf-8'))
        _FakeExpo.batches.append(messages)
        body = json.dumps({'data': [dict(_FakeExpo.ticket) for _ in messages]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _serve(handler):
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class PushDedupeE2E(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.expo = _serve(_FakeExpo)
        cls.expo_url = 'http://127.0.0.1:%d/--/api/v2/push/send' % cls.expo.server_address[1]
        cls._auth = server.BrowserHandler.check_claude_auth
        server.BrowserHandler.check_claude_auth = lambda self, *a, **k: True
        cls._ro, server.READONLY_MODE = server.READONLY_MODE, False
        cls.api = _serve(server.BrowserHandler)
        cls.base = 'http://127.0.0.1:%d' % cls.api.server_address[1]

    @classmethod
    def tearDownClass(cls):
        for httpd in (cls.api, cls.expo):
            httpd.shutdown()
            httpd.server_close()
        server.BrowserHandler.check_claude_auth = cls._auth
        server.READONLY_MODE = cls._ro

    def setUp(self):
        isolate_feed_and_push(self, stub_expo=False)  # the real urllib path
        self.tasks = tempfile.mkdtemp(prefix='kctest-e2e-tasks-')
        self.addCleanup(shutil.rmtree, self.tasks, True)
        self.now = [1_800_000_000.0]
        for target, attr, value in (
            (pn, 'EXPO_PUSH_URL', self.expo_url),
            (pn, 'PUSH_ENABLED', True),
            (pn, 'MIN_INTERVAL', 1800),
            (pn, '_clock', lambda: self.now[0]),
            (CTM, 'TASKS_DIR', self.tasks),
        ):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        _FakeExpo.batches = []
        _FakeExpo.ticket = {'status': 'ok'}
        self.screen_n = 0
        self.assertEqual(self.api_call('POST', '/api/push/register',
                                       {'token': PHONE, 'platform': 'ios'})[0], 201)

    # ── the workspace, over HTTP ────────────────────────────────────────────

    def api_call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode() or 'null')
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or 'null')

    def feed(self):
        status, body = self.api_call('GET', '/api/feed')
        self.assertEqual(status, 200)
        return body['items'] if isinstance(body, dict) else body

    def read_row(self, item_id):
        self.assertEqual(self.api_call('POST', '/api/feed/%s/read' % item_id)[0], 200)

    def dismiss_row(self, item_id):
        self.assertEqual(self.api_call('POST', '/api/feed/%s/dismiss' % item_id)[0], 200)

    # ── a task, through the real reconcile ──────────────────────────────────

    def make_task(self, tid, prompt):
        os.makedirs(os.path.join(self.tasks, tid))
        self._write_meta(tid, {'task_id': tid, 'status': 'running', 'prompt': prompt,
                               'tmux_session': 'claude-' + tid, 'pane_hash': 'none'})

    def _meta_path(self, tid):
        return os.path.join(self.tasks, tid, 'task.json')

    def _write_meta(self, tid, meta):
        with open(self._meta_path(tid), 'w') as f:
            json.dump(meta, f)

    @staticmethod
    def _tmux(screen):
        """subprocess.run stub: the session is alive and shows `screen`."""
        def run(args, *a, **k):
            out = screen if 'capture-pane' in args else ''
            return mock.Mock(returncode=0, stdout=out, stderr='')
        return run

    def _reconcile_unpatched(self, tid):
        with open(self._meta_path(tid)) as f:
            meta = json.load(f)
        CTM._reconcile_status(meta, os.path.join(self.tasks, tid))
        return meta

    def _reconcile(self, tid, screen):
        with mock.patch('server.subprocess.run', side_effect=self._tmux(screen)):
            return self._reconcile_unpatched(tid)

    def task_waits(self, tid, at_minute=None):
        """One real cycle: the agent's screen changes (→ running), then sits
        still past the idle threshold (→ waiting-for-input, which emits)."""
        if at_minute is not None:
            self.now[0] = 1_800_000_000.0 + at_minute * MIN
        self.screen_n += 1
        screen = 'agent output %d\n> ' % self.screen_n
        self.assertEqual(self._reconcile(tid, screen)['status'], 'running')
        with open(self._meta_path(tid)) as f:
            meta = json.load(f)
        meta['last_activity_at'] = time.time() - (server.IDLE_WAITING_SECONDS + 5)
        self._write_meta(tid, meta)
        self.assertEqual(self._reconcile(tid, screen)['status'], 'waiting-for-input')

    # ── the phone ───────────────────────────────────────────────────────────

    def pushes(self, expected):
        """Messages the fake Expo received, once `expected` batches arrived and
        a stray extra one has had a moment to show up."""
        deadline = time.time() + 3
        while len(_FakeExpo.batches) < expected and time.time() < deadline:
            time.sleep(0.01)
        time.sleep(0.15)
        self.assertEqual(len(_FakeExpo.batches), expected,
                         [m[0].get('title') for m in _FakeExpo.batches])
        return [batch[0] for batch in _FakeExpo.batches]

    # ── scenarios ───────────────────────────────────────────────────────────

    def test_01_first_wait_pushes_a_collapsible_expiring_alert(self):
        self.make_task('t-1', 'Work board item 581\nmore detail')
        self.task_waits('t-1')
        [msg] = self.pushes(1)
        [row] = self.feed()
        self.assertEqual(msg['to'], PHONE)
        self.assertEqual(msg['title'], 'Task waiting on you: Work board item 581')
        self.assertEqual(msg['data']['ref'], 'task:t-1')
        self.assertTrue(msg['data']['waiting'])
        self.assertEqual(msg['data']['feedId'], row['id'])
        self.assertEqual(msg['collapseId'], row['id'])
        self.assertEqual(msg['tag'], row['id'])
        self.assertEqual(msg['ttl'], 3600)

    def test_02_the_reported_timeline_unread_pages_once_not_six_times(self):
        self.make_task('t-581', 'Work board item 581')
        for minute in (0, 3, 13, 16, 19, 91):  # 02:06 02:09 02:19 02:22 02:25 03:37
            self.task_waits('t-581', at_minute=minute)
        self.pushes(1)
        rows = self.feed()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['waiting'])

    def test_03_same_timeline_read_at_0207_pages_twice_into_one_slot(self):
        self.make_task('t-581', 'Work board item 581')
        self.task_waits('t-581', at_minute=0)
        row_id = self.feed()[0]['id']
        self.read_row(row_id)                              # 02:07
        for minute in (3, 13, 16, 19, 91):
            self.task_waits('t-581', at_minute=minute)
        first, second = self.pushes(2)
        self.assertEqual(first['collapseId'], second['collapseId'])  # replaces, not stacks
        self.assertEqual(second['collapseId'], row_id)
        # The repeat that went out made the row news again...
        self.assertFalse(self.feed()[0]['read'])
        # ...so without another read, two more hours of flapping stay quiet.
        self.task_waits('t-581', at_minute=91 + 120)
        self.pushes(2)

    def test_05_read_then_repeat_inside_30_minutes_waits_for_the_window(self):
        self.make_task('t-5', 'deploy')
        self.task_waits('t-5', at_minute=0)
        self.read_row(self.feed()[0]['id'])
        self.task_waits('t-5', at_minute=29)
        self.pushes(1)
        self.task_waits('t-5', at_minute=31)
        self.pushes(2)

    def test_06_dismissed_row_comes_back_as_a_fresh_notification(self):
        self.make_task('t-6', 'migrate')
        self.task_waits('t-6', at_minute=0)
        old_id = self.feed()[0]['id']
        self.dismiss_row(old_id)
        self.task_waits('t-6', at_minute=45)
        first, second = self.pushes(2)
        [row] = self.feed()
        self.assertNotEqual(row['id'], old_id)
        self.assertEqual(second['collapseId'], row['id'])
        self.assertNotEqual(second['collapseId'], first['collapseId'])

    def test_07_dismissed_row_back_inside_30_minutes_stays_quiet(self):
        self.make_task('t-7', 'migrate')
        self.task_waits('t-7', at_minute=0)
        self.dismiss_row(self.feed()[0]['id'])
        self.task_waits('t-7', at_minute=10)
        self.pushes(1)
        self.assertEqual(len(self.feed()), 1)  # the Feed still shows it

    def test_08_two_tasks_each_page_once(self):
        self.make_task('t-a', 'task a')
        self.make_task('t-b', 'task b')
        for minute in (0, 2, 5):
            self.task_waits('t-a', at_minute=minute)
            self.task_waits('t-b', at_minute=minute)
        refs = sorted(m['data']['ref'] for m in self.pushes(2))
        self.assertEqual(refs, ['task:t-a', 'task:t-b'])

    def test_09_alerts_without_a_dedupe_key_are_not_deduplicated(self):
        body = {'kind': 'activity', 'title': 'Agent needs a hand', 'waiting': True}
        self.assertEqual(self.api_call('POST', '/api/feed', body)[0], 201)
        self.assertEqual(self.api_call('POST', '/api/feed', body)[0], 201)
        self.pushes(2)

    def test_10_a_phone_registered_later_still_hears_the_next_repeat(self):
        self.assertEqual(self.api_call('POST', '/api/push/unregister', {'token': PHONE})[0], 200)
        self.make_task('t-10', 'long job')
        self.task_waits('t-10', at_minute=0)
        self.pushes(0)
        self.assertEqual(pn.PushLedger.entries(), {})
        self.api_call('POST', '/api/push/register', {'token': PHONE, 'platform': 'ios'})
        self.task_waits('t-10', at_minute=5)  # row still unread
        self.pushes(1)

    def test_11_push_disabled_sends_and_records_nothing_but_feeds(self):
        pn.PUSH_ENABLED = False
        self.make_task('t-11', 'quiet')
        self.task_waits('t-11')
        self.pushes(0)
        self.assertEqual(pn.PushLedger.entries(), {})
        self.assertEqual(len(self.feed()), 1)

    def test_12_unregistered_phone_gets_nothing(self):
        self.assertEqual(self.api_call('POST', '/api/push/unregister', {'token': PHONE})[0], 200)
        self.make_task('t-12', 'x')
        self.task_waits('t-12')
        self.pushes(0)

    def test_13_expo_unreachable_never_breaks_the_feed(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            dead_port = s.getsockname()[1]
        pn.EXPO_PUSH_URL = 'http://127.0.0.1:%d/--/api/v2/push/send' % dead_port
        self.make_task('t-13a', 'during outage')
        self.task_waits('t-13a')  # must not raise
        self.assertEqual(len(self.feed()), 1)
        pn.EXPO_PUSH_URL = self.expo_url
        self.make_task('t-13b', 'after outage')
        self.task_waits('t-13b')
        [msg] = self.pushes(1)
        self.assertEqual(msg['data']['ref'], 'task:t-13b')

    def test_14_device_not_registered_prunes_the_phone(self):
        _FakeExpo.ticket = {'status': 'error', 'details': {'error': 'DeviceNotRegistered'}}
        self.make_task('t-14a', 'x')
        self.task_waits('t-14a')
        self.pushes(1)
        deadline = time.time() + 3
        while pn.PushTokenStore.all_tokens() and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(pn.PushTokenStore.all_tokens(), [])
        self.make_task('t-14b', 'y')
        self.task_waits('t-14b')
        self.pushes(1)

    def test_15_losing_the_ledger_costs_at_most_one_repeat(self):
        self.make_task('t-15', 'x')
        self.task_waits('t-15', at_minute=0)
        os.remove(pn.SENT_PATH)
        self.task_waits('t-15', at_minute=1)
        self.pushes(2)
        self.task_waits('t-15', at_minute=2)
        self.pushes(2)

    def test_16_simultaneous_flips_of_one_task_page_once(self):
        self.make_task('t-16', 'x')
        screen = 'still\n> '
        self._reconcile('t-16', screen)  # running, pane_hash = screen
        with open(self._meta_path('t-16')) as f:
            meta = json.load(f)
        meta['last_activity_at'] = time.time() - (server.IDLE_WAITING_SECONDS + 5)
        self._write_meta('t-16', meta)
        barrier = threading.Barrier(10)

        def flip():
            barrier.wait()
            self._reconcile_unpatched('t-16')

        # Patch once, outside the threads: mock.patch is not thread-safe.
        # Every racer that read "running" emits (the task lock only serialises
        # the write), so this is the ledger's race to win, not the task's.
        with mock.patch('server.subprocess.run', side_effect=self._tmux(screen)):
            threads = [threading.Thread(target=flip) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.pushes(1)
        self.assertEqual(len(self.feed()), 1)

    def test_16b_simultaneous_emits_bypassing_the_task_lock_page_once(self):
        barrier = threading.Barrier(10)
        meta = {'task_id': 't-16b', 'prompt': 'x'}

        def emit():
            barrier.wait()
            server.FeedManager.emit_task_waiting(meta)

        threads = [threading.Thread(target=emit) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.pushes(1)
        self.assertEqual(len(self.feed()), 1)


if __name__ == '__main__':
    unittest.main()
