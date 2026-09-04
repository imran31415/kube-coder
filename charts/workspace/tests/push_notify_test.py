"""Tests for mobile push notifications (Expo) — push_notify.py + the
/api/push/register|unregister HTTP handlers, and the FeedManager.emit hook.

Covers: token shape validation, the high-signal push predicate, the on-disk
token store (idempotent upsert / unregister / prune), fire-and-forget dispatch
(gating, Expo payload, DeviceNotRegistered pruning), the emit→dispatch wiring,
and the HTTP endpoints incl. auth + readonly gating.

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
        self._orig = (pn.PUSH_DIR, pn.TOKENS_PATH, pn.PUSH_ENABLED)
        pn.PUSH_DIR = self.dir
        pn.TOKENS_PATH = os.path.join(self.dir, 'tokens.json')
        pn.PUSH_ENABLED = True

    def tearDown(self):
        pn.PUSH_DIR, pn.TOKENS_PATH, pn.PUSH_ENABLED = self._orig
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


class EmitHookTests(_StoreBase):
    """FeedManager.emit must funnel every item through push_notify.dispatch."""
    def setUp(self):
        super().setUp()
        self.feeddir = tempfile.mkdtemp(prefix='kctest-feed-')
        self._feedorig = (server.FeedManager.FEED_DIR,
                          server.FeedManager.ITEMS_PATH,
                          server.FeedManager.STATE_PATH)
        server.FeedManager.FEED_DIR = self.feeddir
        server.FeedManager.ITEMS_PATH = os.path.join(self.feeddir, 'items.jsonl')
        server.FeedManager.STATE_PATH = os.path.join(self.feeddir, 'state.json')

    def tearDown(self):
        (server.FeedManager.FEED_DIR, server.FeedManager.ITEMS_PATH,
         server.FeedManager.STATE_PATH) = self._feedorig
        shutil.rmtree(self.feeddir, ignore_errors=True)
        super().tearDown()

    def test_emit_calls_dispatch_with_item(self):
        with mock.patch.object(server.EventBroker, 'publish'), \
                mock.patch.object(pn, 'dispatch') as disp:
            item = server.FeedManager.emit('decision', 'A decision', source='test')
        disp.assert_called_once()
        self.assertEqual(disp.call_args.args[0]['id'], item['id'])


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
