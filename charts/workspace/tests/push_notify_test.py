"""Tests for mobile push notifications (Expo) — push_notify.py + the
/api/push/register|unregister HTTP handlers, and the FeedManager.emit hook.

Covers: token shape validation, the high-signal push predicate, the on-disk
token store (idempotent upsert / unregister / prune), the sent ledger that stops
repeats (#685), fire-and-forget dispatch (gating, Expo payload incl. collapse +
ttl, DeviceNotRegistered pruning), the emit→dispatch wiring incl. the read flag,
and the HTTP endpoints incl. auth + readonly gating. The full path over real
sockets is in push_dedupe_e2e_test.py.

Run:  python3 -m unittest tests.push_notify_test   (from charts/workspace/)
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
import server  # noqa: E402
import push_notify as pn  # noqa: E402

sys.path.insert(0, HERE)
from live_state import isolate_feed_and_push  # noqa: E402


class _FakeResp:
    """Context-manager stand-in for urlopen's return value."""
    def __init__(self, payload):
        self._b = json.dumps(payload).encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


def _wait_for(fn, timeout=2.0):
    """Poll until fn() is truthy (dispatch runs on a daemon thread)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(0.01)
    return False


class PredicateTests(unittest.TestCase):
    def test_is_expo_token(self):
        self.assertTrue(pn.is_expo_token('ExponentPushToken[abc123XYZ]'))
        self.assertTrue(pn.is_expo_token('ExpoPushToken[abc]'))
        self.assertFalse(pn.is_expo_token(''))
        self.assertFalse(pn.is_expo_token('random-string'))
        self.assertFalse(pn.is_expo_token('ExponentPushToken[unterminated'))
        self.assertFalse(pn.is_expo_token(None))
        self.assertFalse(pn.is_expo_token('ExponentPushToken[' + 'x' * 300 + ']'))

    def test_should_push_high_signal_only(self):
        self.assertTrue(pn.should_push({'waiting': True, 'kind': 'activity'}))
        self.assertTrue(pn.should_push({'waiting': False, 'kind': 'decision'}))
        self.assertFalse(pn.should_push({'waiting': False, 'kind': 'activity'}))
        self.assertFalse(pn.should_push({'kind': 'briefing'}))
        self.assertFalse(pn.should_push({'kind': 'news'}))
        self.assertFalse(pn.should_push(None))


class _StoreBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kctest-push-')
        self._orig = (pn.PUSH_DIR, pn.TOKENS_PATH, pn.SENT_PATH, pn.PUSH_ENABLED,
                      pn.MIN_INTERVAL, pn._clock)
        pn.PUSH_DIR = self.dir
        pn.TOKENS_PATH = os.path.join(self.dir, 'tokens.json')
        pn.SENT_PATH = os.path.join(self.dir, 'sent.json')
        pn.PUSH_ENABLED = True
        pn.MIN_INTERVAL = 1800

    def tearDown(self):
        (pn.PUSH_DIR, pn.TOKENS_PATH, pn.SENT_PATH, pn.PUSH_ENABLED,
         pn.MIN_INTERVAL, pn._clock) = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)


class StoreTests(_StoreBase):
    def test_register_unregister_roundtrip(self):
        pn.PushTokenStore.register('ExponentPushToken[t1]', 'ios', 'api:aaa')
        pn.PushTokenStore.register('ExponentPushToken[t2]', 'android', 'api:aaa')
        self.assertEqual(set(pn.PushTokenStore.all_tokens()),
                         {'ExponentPushToken[t1]', 'ExponentPushToken[t2]'})
        # re-register is an idempotent upsert, not a duplicate
        pn.PushTokenStore.register('ExponentPushToken[t1]', 'ios', 'api:bbb')
        self.assertEqual(len(pn.PushTokenStore.all_tokens()), 2)
        self.assertTrue(pn.PushTokenStore.unregister('ExponentPushToken[t1]'))
        self.assertFalse(pn.PushTokenStore.unregister('ExponentPushToken[t1]'))
        self.assertEqual(pn.PushTokenStore.all_tokens(), ['ExponentPushToken[t2]'])

    def test_tokens_file_is_private(self):
        pn.PushTokenStore.register('ExponentPushToken[t1]', 'ios', 'api:aaa')
        self.assertEqual(os.stat(pn.TOKENS_PATH).st_mode & 0o777, 0o600)

    def test_prune(self):
        pn.PushTokenStore.register('ExponentPushToken[t1]', 'ios', 'api:aaa')
        pn.PushTokenStore.register('ExponentPushToken[t2]', 'ios', 'api:aaa')
        pn.PushTokenStore.prune(['ExponentPushToken[t1]'])
        self.assertEqual(pn.PushTokenStore.all_tokens(), ['ExponentPushToken[t2]'])
        pn.PushTokenStore.prune([])  # no-op, no raise
        self.assertEqual(pn.PushTokenStore.all_tokens(), ['ExponentPushToken[t2]'])

    def test_corrupt_tokens_file_reads_as_empty(self):
        with open(pn.TOKENS_PATH, 'w') as f:
            f.write('{not json')
        self.assertEqual(pn.PushTokenStore.all_tokens(), [])
        pn.PushTokenStore.register('ExponentPushToken[t1]', 'ios', 'api:aaa')
        self.assertEqual(pn.PushTokenStore.all_tokens(), ['ExponentPushToken[t1]'])


class ConfigTests(unittest.TestCase):
    """The env knobs resolve the way docs/environment-variables.md says."""

    def test_push_dir_blank_means_default(self):
        self.assertEqual(pn._resolve_push_dir({}), pn.DEFAULT_PUSH_DIR)
        self.assertEqual(pn._resolve_push_dir({'KC_PUSH_DIR': '  '}), pn.DEFAULT_PUSH_DIR)
        self.assertEqual(pn._resolve_push_dir({'KC_PUSH_DIR': ' /tmp/p \n'}), '/tmp/p')
        # Every workspace's registered phones already live here.
        self.assertEqual(pn.DEFAULT_PUSH_DIR, '/home/dev/.claude-push')

    def test_min_interval_parsing(self):
        cases = {None: 1800, '': 1800, '   ': 1800, 'abc': 1800, '-5': 1800,
                 'nan': 1800, '0': 0, '3600': 3600, ' 90 ': 90, '2.5': 2.5}
        for raw, want in cases.items():
            env = {} if raw is None else {'KC_PUSH_MIN_INTERVAL': raw}
            with self.subTest(raw=raw):
                self.assertEqual(pn._resolve_min_interval(env), want)


class LedgerTests(_StoreBase):
    """PushLedger.claim — the #685 decision table, one row per test."""
    K = 'task:t1:waiting'

    def claim(self, item_id, seen, now, key=None):
        return pn.PushLedger.claim(key or self.K, item_id, seen, now=now)

    def test_a_key_never_pushed_goes_out(self):
        self.assertTrue(self.claim('fd_1', seen=False, now=0))
        self.assertEqual(pn.PushLedger.entries()[self.K], {'item_id': 'fd_1', 'ts': 0})

    def test_same_row_unread_never_repeats_however_long(self):
        self.assertTrue(self.claim('fd_1', False, now=0))
        for later in (60, 1800, 86400, 6 * 86400):
            self.assertFalse(self.claim('fd_1', False, now=later), later)

    def test_same_row_read_repeats_only_after_the_interval(self):
        self.assertTrue(self.claim('fd_1', False, now=0))
        self.assertFalse(self.claim('fd_1', True, now=60))
        self.assertFalse(self.claim('fd_1', True, now=1799))
        self.assertTrue(self.claim('fd_1', True, now=1800))  # boundary is inclusive

    def test_new_row_after_dismiss_repeats_only_after_the_interval(self):
        self.assertTrue(self.claim('fd_1', False, now=0))
        self.assertFalse(self.claim('fd_2', False, now=100))
        self.assertTrue(self.claim('fd_2', False, now=1800))
        self.assertEqual(pn.PushLedger.entries()[self.K]['item_id'], 'fd_2')

    def test_a_denied_claim_does_not_restart_the_interval(self):
        self.assertTrue(self.claim('fd_1', False, now=0))
        self.assertFalse(self.claim('fd_1', True, now=1000))
        self.assertTrue(self.claim('fd_1', True, now=1800))

    def test_interval_zero_leaves_only_the_seen_rule(self):
        pn.MIN_INTERVAL = 0
        self.assertTrue(self.claim('fd_1', False, now=0))
        self.assertFalse(self.claim('fd_1', False, now=1))
        self.assertTrue(self.claim('fd_1', True, now=2))

    def test_clock_stepping_backwards_does_not_hold_the_key(self):
        self.assertTrue(self.claim('fd_1', False, now=10_000))
        self.assertTrue(self.claim('fd_1', True, now=5_000))

    def test_keys_are_independent(self):
        self.assertTrue(self.claim('fd_1', False, now=0, key='task:a:waiting'))
        self.assertTrue(self.claim('fd_2', False, now=1, key='task:b:waiting'))
        self.assertFalse(self.claim('fd_1', False, now=2, key='task:a:waiting'))

    def test_corrupt_or_misshapen_file_counts_as_empty(self):
        for junk in ('{oops', '[]', '{"keys": []}', '{"keys": {"%s": "junk"}}' % self.K):
            with self.subTest(junk=junk):
                with open(pn.SENT_PATH, 'w') as f:
                    f.write(junk)
                self.assertTrue(self.claim('fd_1', False, now=0))
                with open(pn.SENT_PATH) as f:
                    self.assertIn(self.K, json.load(f)['keys'])  # rewritten valid

    def test_old_entries_are_dropped_on_write(self):
        self.assertTrue(self.claim('fd_1', False, now=0, key='task:old:waiting'))
        eight_days = 8 * 24 * 3600
        self.assertTrue(self.claim('fd_2', False, now=eight_days, key='task:new:waiting'))
        self.assertEqual(set(pn.PushLedger.entries()), {'task:new:waiting'})

    def test_a_denied_claim_writes_nothing(self):
        self.assertTrue(self.claim('fd_1', False, now=0))
        with mock.patch.object(pn, '_write_json') as w:
            self.assertFalse(self.claim('fd_1', False, now=5))
        w.assert_not_called()

    def test_sent_file_is_private(self):
        self.claim('fd_1', False, now=0)
        self.assertEqual(os.stat(pn.SENT_PATH).st_mode & 0o777, 0o600)

    def test_concurrent_claims_have_one_winner(self):
        barrier = threading.Barrier(10)
        wins = []

        def race():
            barrier.wait()
            wins.append(pn.PushLedger.claim(self.K, 'fd_1', False, now=0))

        threads = [threading.Thread(target=race) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(wins.count(True), 1)


class BuildMessagesTests(unittest.TestCase):
    def test_build_messages_carries_deeplink_and_body(self):
        item = {'title': 'Task waiting: deploy', 'body_md': 'needs input\nsecond line',
                'links': [{'label': 'Open', 'ref': 'task:99'}], 'id': 'fd_1',
                'waiting': True, 'kind': 'activity'}
        msgs = pn._build_messages(item, ['ExponentPushToken[a]', 'ExponentPushToken[b]'])
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]['to'], 'ExponentPushToken[a]')
        self.assertEqual(msgs[1]['to'], 'ExponentPushToken[b]')
        self.assertEqual(msgs[0]['data']['ref'], 'task:99')
        self.assertEqual(msgs[0]['body'], 'needs input')
        self.assertTrue(msgs[0]['title'].startswith('Task waiting'))
        self.assertEqual(msgs[0]['priority'], 'high')

    def test_build_messages_defaults_body_for_waiting(self):
        msgs = pn._build_messages({'title': 't', 'waiting': True, 'kind': 'activity'},
                                  ['ExponentPushToken[a]'])
        self.assertEqual(msgs[0]['body'], 'Action needed')
        self.assertEqual(msgs[0]['data']['ref'], '')

    def test_repeat_replaces_the_notification_on_the_phone(self):
        """collapseId (iOS) and tag (Android) are the feed id, which a coalesced
        re-emit keeps — so a repeat replaces the earlier notification (#685)."""
        msgs = pn._build_messages({'title': 't', 'waiting': True, 'id': 'fd_17_abc123'},
                                  ['ExponentPushToken[a]', 'ExponentPushToken[b]'])
        for m in msgs:
            self.assertEqual(m['collapseId'], 'fd_17_abc123')
            self.assertEqual(m['tag'], 'fd_17_abc123')
            self.assertEqual(m['data']['feedId'], 'fd_17_abc123')

    def test_every_message_expires(self):
        msgs = pn._build_messages({'title': 't', 'kind': 'decision', 'id': 'fd_1'},
                                  ['ExponentPushToken[a]'])
        self.assertEqual(msgs[0]['ttl'], pn.PUSH_TTL_SECONDS)
        self.assertEqual(pn.PUSH_TTL_SECONDS, 3600)

    def test_no_collapse_without_a_usable_id(self):
        for item in ({'title': 't', 'kind': 'decision'},
                     {'title': 't', 'kind': 'decision', 'id': 'x' * 65}):  # APNs cap
            with self.subTest(id_len=len(item.get('id', ''))):
                m = pn._build_messages(item, ['ExponentPushToken[a]'])[0]
                self.assertNotIn('collapseId', m)
                self.assertNotIn('tag', m)
                self.assertEqual(m['ttl'], pn.PUSH_TTL_SECONDS)


class DispatchTests(_StoreBase):
    def _register(self, *toks):
        for t in toks:
            pn.PushTokenStore.register(t, 'ios', 'api:aaa')

    def test_low_signal_never_sends(self):
        self._register('ExponentPushToken[a]')
        with mock.patch.object(pn.urllib.request, 'urlopen') as uo:
            pn.dispatch({'kind': 'activity', 'waiting': False})
            time.sleep(0.1)
            uo.assert_not_called()

    def test_disabled_never_sends(self):
        self._register('ExponentPushToken[a]')
        pn.PUSH_ENABLED = False
        with mock.patch.object(pn.urllib.request, 'urlopen') as uo:
            pn.dispatch({'kind': 'decision', 'waiting': False})
            time.sleep(0.1)
            uo.assert_not_called()

    def test_no_tokens_no_send(self):
        with mock.patch.object(pn.urllib.request, 'urlopen') as uo:
            pn.dispatch({'kind': 'decision'})
            time.sleep(0.1)
            uo.assert_not_called()

    def test_high_signal_sends_expo_payload(self):
        self._register('ExponentPushToken[a]')
        ok = _FakeResp({'data': [{'status': 'ok'}]})
        with mock.patch.object(pn.urllib.request, 'urlopen', return_value=ok) as uo:
            pn.dispatch({'kind': 'decision', 'title': 'Decided X',
                         'links': [{'ref': 'memory:foo'}], 'id': 'fd_9'})
            self.assertTrue(_wait_for(lambda: uo.call_count > 0))
        req = uo.call_args.args[0]
        self.assertEqual(req.full_url, pn.EXPO_PUSH_URL)
        sent = json.loads(req.data.decode('utf-8'))
        self.assertEqual(sent[0]['to'], 'ExponentPushToken[a]')
        self.assertEqual(sent[0]['data']['ref'], 'memory:foo')

    def test_device_not_registered_is_pruned(self):
        self._register('ExponentPushToken[dead]', 'ExponentPushToken[live]')
        resp = _FakeResp({'data': [
            {'status': 'error', 'details': {'error': 'DeviceNotRegistered'}},
            {'status': 'ok'},
        ]})
        with mock.patch.object(pn.urllib.request, 'urlopen', return_value=resp) as uo:
            pn.dispatch({'kind': 'decision', 'title': 't'})
            self.assertTrue(_wait_for(lambda: uo.call_count > 0))
        self.assertTrue(_wait_for(
            lambda: pn.PushTokenStore.all_tokens() == ['ExponentPushToken[live]']))

    def test_dispatch_swallows_network_error(self):
        self._register('ExponentPushToken[a]')
        with mock.patch.object(pn.urllib.request, 'urlopen',
                               side_effect=urllib.error.URLError('boom')) as uo:
            pn.dispatch({'kind': 'decision', 'title': 't'})  # must not raise
            self.assertTrue(_wait_for(lambda: uo.call_count > 0))
        # token survives a transient failure (only DeviceNotRegistered prunes)
        self.assertEqual(pn.PushTokenStore.all_tokens(), ['ExponentPushToken[a]'])

    def test_dispatch_reports_whether_it_queued(self):
        with mock.patch.object(pn, '_deliver'):
            self.assertFalse(pn.dispatch({'kind': 'decision', 'id': 'fd_1'}))  # no phone
            self._register('ExponentPushToken[a]')
            self.assertFalse(pn.dispatch({'kind': 'activity', 'waiting': False}))
            self.assertTrue(pn.dispatch({'kind': 'decision', 'id': 'fd_1'}))
            pn.PUSH_ENABLED = False
            self.assertFalse(pn.dispatch({'kind': 'decision', 'id': 'fd_2'}))

    def test_keyed_repeat_is_held_back_by_the_ledger(self):
        self._register('ExponentPushToken[a]')
        item = {'kind': 'activity', 'waiting': True, 'id': 'fd_1',
                'dedupe_key': 'task:t1:waiting', 'title': 'Task waiting on you: x'}
        with mock.patch.object(pn, '_deliver') as deliver:
            self.assertTrue(pn.dispatch(item))
            self.assertFalse(pn.dispatch(item))             # unread repeat
            self.assertFalse(pn.dispatch(item, seen=True))  # read, but too soon
        self.assertEqual(deliver.call_count, 1)

    def test_keyless_items_always_push(self):
        self._register('ExponentPushToken[a]')
        item = {'kind': 'activity', 'waiting': True, 'id': 'fd_1', 'dedupe_key': ''}
        with mock.patch.object(pn, '_deliver') as deliver:
            self.assertTrue(pn.dispatch(item))
            self.assertTrue(pn.dispatch(item))
        self.assertEqual(deliver.call_count, 2)
        self.assertEqual(pn.PushLedger.entries(), {})

    def test_nothing_recorded_while_no_phone_or_disabled(self):
        """A phone that registers later must still hear the next repeat."""
        item = {'kind': 'decision', 'id': 'fd_1', 'dedupe_key': 'decision:ns/k'}
        with mock.patch.object(pn, '_deliver'):
            self.assertFalse(pn.dispatch(item))
            self.assertEqual(pn.PushLedger.entries(), {})
            self._register('ExponentPushToken[a]')
            pn.PUSH_ENABLED = False
            self.assertFalse(pn.dispatch(item))
            self.assertEqual(pn.PushLedger.entries(), {})
            pn.PUSH_ENABLED = True
            self.assertTrue(pn.dispatch(item))

    def test_a_broken_ledger_still_pushes(self):
        """A duplicate is a nuisance; a lost 'waiting on you' is not."""
        self._register('ExponentPushToken[a]')
        item = {'kind': 'decision', 'id': 'fd_1', 'dedupe_key': 'decision:ns/k'}
        with mock.patch.object(pn.PushLedger, 'claim', side_effect=OSError('disk full')), \
                mock.patch.object(pn, '_deliver') as deliver:
            self.assertTrue(pn.dispatch(item))
        deliver.assert_called_once()


class EmitHookTests(unittest.TestCase):
    """FeedManager.emit must funnel every item through push_notify.dispatch,
    telling it whether the user had read the row (#685)."""

    FM = server.FeedManager
    KEY = 'task:t1:waiting'

    def setUp(self):
        isolate_feed_and_push(self)
        for attr, value in (('PUSH_ENABLED', True), ('MIN_INTERVAL', 1800)):
            p = mock.patch.object(pn, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.now = [1_000_000.0]
        p = mock.patch.object(pn, '_clock', lambda: self.now[0])
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(server.EventBroker, 'publish')
        p.start()
        self.addCleanup(p.stop)

    def _waiting(self, title='Task waiting on you: x'):
        return self.FM.emit('activity', title, waiting=True, dedupe_key=self.KEY)

    def _row(self, item_id):
        return next(i for i in self.FM.list() if i['id'] == item_id)

    def _pushes(self, expected):
        """Delivery runs on a daemon thread: wait for `expected`, then give a
        stray extra push a moment to show up before asserting."""
        _wait_for(lambda: len(self.expo_calls) >= expected)
        time.sleep(0.05)
        self.assertEqual(len(self.expo_calls), expected)

    def test_emit_calls_dispatch_with_item(self):
        with mock.patch.object(pn, 'dispatch', return_value=False) as disp:
            item = self.FM.emit('decision', 'A decision', source='test')
        disp.assert_called_once()
        self.assertEqual(disp.call_args.args[0]['id'], item['id'])
        self.assertIs(disp.call_args.kwargs['seen'], False)

    def test_seen_is_true_only_for_a_read_coalesced_row(self):
        with mock.patch.object(pn, 'dispatch', return_value=False) as disp:
            first = self._waiting()
            self.assertIs(disp.call_args.kwargs['seen'], False)
            self._waiting()
            self.assertIs(disp.call_args.kwargs['seen'], False)  # unread
            self.FM.mark_read(first['id'])
            self._waiting()
            self.assertIs(disp.call_args.kwargs['seen'], True)
            self.FM.emit('activity', 'other', waiting=True, dedupe_key='task:t2:waiting')
            self.assertIs(disp.call_args.kwargs['seen'], False)  # a different key

    def test_a_dismissed_row_comes_back_as_a_new_unseen_row(self):
        with mock.patch.object(pn, 'dispatch', return_value=False) as disp:
            first = self._waiting()
            self.FM.mark_read(first['id'])
            self.FM.dismiss(first['id'])
            again = self._waiting()
        self.assertNotEqual(again['id'], first['id'])
        self.assertIs(disp.call_args.kwargs['seen'], False)

    def test_unread_repeats_push_once_and_still_update_the_row(self):
        pn.PushTokenStore.register('ExponentPushToken[a]', 'ios', 'api:aaa')
        first = self._waiting('Task waiting on you: v1')
        self._waiting('Task waiting on you: v2')
        self.now[0] += 86400
        self._waiting('Task waiting on you: v3')
        self._pushes(1)
        rows = self.FM.list()
        self.assertEqual([r['id'] for r in rows], [first['id']])
        self.assertEqual(rows[0]['title'], 'Task waiting on you: v3')

    def test_a_repush_makes_the_row_unread_again(self):
        pn.PushTokenStore.register('ExponentPushToken[a]', 'ios', 'api:aaa')
        first = self._waiting()
        self._pushes(1)
        self.FM.mark_read(first['id'])
        self.now[0] += 1800
        self._waiting()
        self._pushes(2)
        self.assertFalse(self._row(first['id'])['read'])
        # ...so the next repeat is held until the user reads it again.
        self.now[0] += 7200
        self._waiting()
        self._pushes(2)

    def test_a_held_repeat_keeps_the_row_read(self):
        pn.PushTokenStore.register('ExponentPushToken[a]', 'ios', 'api:aaa')
        first = self._waiting()
        self.FM.mark_read(first['id'])
        self.now[0] += 60  # inside the interval
        self._waiting()
        self._pushes(1)
        self.assertTrue(self._row(first['id'])['read'])
        # Not lost: the next repeat after the interval goes out.
        self.now[0] += 1800
        self._waiting()
        self._pushes(2)

    def test_without_a_phone_the_read_flag_is_left_alone(self):
        first = self._waiting()
        self.FM.mark_read(first['id'])
        self.now[0] += 7200
        self._waiting()
        self.assertTrue(self._row(first['id'])['read'])
        self.assertEqual(self.expo_calls, [])


class HttpEndpointTests(unittest.TestCase):
    READONLY = False
    AUTH_OK = True

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix='kc-push-api-')
        cls._porig = (pn.PUSH_DIR, pn.TOKENS_PATH)
        pn.PUSH_DIR = cls.tmpdir
        pn.TOKENS_PATH = os.path.join(cls.tmpdir, 'tokens.json')
        cls._auth_save = server.BrowserHandler.check_claude_auth
        server.BrowserHandler.check_claude_auth = lambda self: cls.AUTH_OK
        cls._ro_save, server.READONLY_MODE = server.READONLY_MODE, cls.READONLY
        cls.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), server.BrowserHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.BrowserHandler.check_claude_auth = cls._auth_save
        server.READONLY_MODE = cls._ro_save
        pn.PUSH_DIR, pn.TOKENS_PATH = cls._porig
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        type(self).AUTH_OK = True
        type(self).READONLY = False
        server.READONLY_MODE = False
        try:
            os.remove(pn.TOKENS_PATH)
        except OSError:
            pass

    def _post(self, path, body):
        data = json.dumps(body).encode('utf-8')
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}',
                                     data=data, headers={'Content-Type': 'application/json'},
                                     method='POST')
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_register_persists_token(self):
        status, body = self._post('/api/push/register',
                                  {'token': 'ExponentPushToken[abc]', 'platform': 'ios'})
        self.assertEqual(status, 201)
        self.assertTrue(body['ok'])
        self.assertEqual(pn.PushTokenStore.all_tokens(), ['ExponentPushToken[abc]'])

    def test_register_rejects_bad_token(self):
        status, body = self._post('/api/push/register', {'token': 'nope'})
        self.assertEqual(status, 400)
        self.assertEqual(pn.PushTokenStore.all_tokens(), [])

    def test_register_requires_auth(self):
        type(self).AUTH_OK = False
        status, _ = self._post('/api/push/register',
                               {'token': 'ExponentPushToken[abc]'})
        self.assertEqual(status, 401)

    def test_register_blocked_in_readonly(self):
        type(self).READONLY = True
        server.READONLY_MODE = True
        status, body = self._post('/api/push/register',
                                  {'token': 'ExponentPushToken[abc]'})
        self.assertEqual(status, 403)
        self.assertEqual(body.get('code'), 'readonly')

    def test_unregister_removes_token(self):
        self._post('/api/push/register', {'token': 'ExponentPushToken[abc]'})
        status, body = self._post('/api/push/unregister', {'token': 'ExponentPushToken[abc]'})
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])
        self.assertEqual(pn.PushTokenStore.all_tokens(), [])


if __name__ == '__main__':
    unittest.main()
