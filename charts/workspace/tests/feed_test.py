"""Unit tests for the Feed backend (issue #469).

Covers FeedManager (emit/coalesce, list filtering + cursor, unread counting,
read/dismiss overlays, JSONL rotation preserving overlays), the deterministic
system emitters (task terminal/waiting, decision memory, trigger), the HTTP
handlers incl. readonly gating, and the post_update MCP tool wiring.

Run:  python3 -m unittest tests.feed_test
"""

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
import server  # noqa: E402
import mcp_dashboard as mcp  # noqa: E402

sys.path.insert(0, HERE)
from live_state import isolate_feed_and_push  # noqa: E402

FM = server.FeedManager


class FeedManagerBase(unittest.TestCase):
    def setUp(self):
        # Feed files AND the push token store: redirecting only the feed (as
        # this base did before #685) still let waiting/decision emits push to
        # the workspace's real phone.
        isolate_feed_and_push(self)
        self.dir = FM.FEED_DIR


class FeedDirTests(unittest.TestCase):
    """`$KC_FEED_DIR` — the throwaway-directory override the Makefile's
    python-tests target sets so a test run cannot write the live Feed."""

    def test_unset_or_blank_is_the_deployment_path(self):
        self.assertEqual(server._resolve_feed_dir({}), server.DEFAULT_FEED_DIR)
        for blank in ('', '   ', '\n'):
            self.assertEqual(server._resolve_feed_dir({'KC_FEED_DIR': blank}),
                             server.DEFAULT_FEED_DIR)

    def test_set_is_used_trimmed(self):
        self.assertEqual(server._resolve_feed_dir({'KC_FEED_DIR': ' /tmp/feed-x \n'}),
                         '/tmp/feed-x')

    def test_default_is_the_pvc_path(self):
        # Every workspace's Feed already lives here — a typo empties them all.
        self.assertEqual(server.DEFAULT_FEED_DIR, '/home/dev/.claude-feed')


class EmitAndListTests(FeedManagerBase):
    def test_emit_and_list_roundtrip(self):
        with mock.patch.object(server.EventBroker, 'publish') as pub:
            item = FM.emit('activity', 'Task finished', source='system:task',
                           project_id='kc')
        self.assertIsNotNone(item)
        self.assertTrue(item['id'].startswith('fd_'))
        pub.assert_called_once()
        self.assertEqual(pub.call_args[0][0], 'feed.item')
        items = FM.list()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['title'], 'Task finished')
        self.assertFalse(items[0]['read'])
        # dedupe_key is internal — not exposed to clients.
        self.assertNotIn('dedupe_key', items[0])

    def test_invalid_kind_returns_none(self):
        self.assertIsNone(FM.emit('bogus', 'x'))

    def test_coalesce_by_dedupe_key_updates_in_place(self):
        a = FM.emit('activity', 'waiting v1', dedupe_key='task:t1:waiting', waiting=True)
        b = FM.emit('activity', 'waiting v2', dedupe_key='task:t1:waiting', waiting=True)
        self.assertEqual(a['id'], b['id'])  # same id → in-place update
        items = FM.list()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['title'], 'waiting v2')

    def test_list_newest_first_and_filters(self):
        FM.emit('activity', 'first', project_id='a')
        FM.emit('decision', 'second', project_id='b')
        FM.emit('briefing', 'third', project_id='a')
        titles = [i['title'] for i in FM.list()]
        self.assertEqual(titles, ['third', 'second', 'first'])
        # project filter
        self.assertEqual([i['title'] for i in FM.list(project='a')], ['third', 'first'])
        # kind filter
        self.assertEqual([i['title'] for i in FM.list(kinds=['decision'])], ['second'])

    def test_since_cursor(self):
        i1 = FM.emit('activity', 'old')
        i2 = FM.emit('activity', 'new')
        # Everything strictly newer than i1's ts.
        newer = FM.list(since=i1['ts'])
        self.assertEqual([i['id'] for i in newer], [i2['id']])

    def test_read_dismiss_and_unread_count(self):
        i1 = FM.emit('activity', 'one')
        i2 = FM.emit('activity', 'two')
        self.assertEqual(FM.unread_count(), 2)
        self.assertTrue(FM.mark_read(i1['id']))
        self.assertEqual(FM.unread_count(), 1)
        self.assertEqual([i['title'] for i in FM.list(unread_only=True)], ['two'])
        # dismiss drops it from the list entirely
        self.assertTrue(FM.dismiss(i2['id']))
        self.assertEqual([i['title'] for i in FM.list()], ['one'])
        self.assertEqual(FM.unread_count(), 0)

    def test_flag_unknown_id_is_404_signal(self):
        self.assertFalse(FM.mark_read('fd_nope'))
        self.assertFalse(FM.dismiss('fd_nope'))

    def test_rotation_keeps_newest_and_prunes_overlays(self):
        with mock.patch.object(FM, 'MAX_BYTES', 200), \
             mock.patch.object(FM, 'KEEP_ON_ROTATE', 3):
            ids = [FM.emit('activity', f'item {i}').get('id') for i in range(12)]
            FM.mark_read(ids[0])  # overlay for an item that will be rotated out
            FM.emit('activity', 'trigger rotation')
        items = FM.list()
        # Compacted to at most KEEP_ON_ROTATE-ish recent items; the oldest are gone.
        self.assertLessEqual(len(items), 4)
        self.assertNotIn('item 0', [i['title'] for i in items])
        # The pruned overlay didn't survive for a dropped id.
        state = FM._load_state()
        self.assertNotIn(ids[0], state['read'])


class SystemEmitterTests(FeedManagerBase):
    def test_emit_task_terminal(self):
        with mock.patch.object(FM, '_project_for_workdir', return_value='kc'):
            item = FM.emit_task_terminal(
                {'task_id': 't1', 'prompt': 'build the thing\nmore',
                 'workdir': '/home/dev/kc'}, 'completed')
        self.assertEqual(item['kind'], 'activity')
        self.assertIn('finished', item['title'])
        self.assertEqual(item['project_id'], 'kc')
        self.assertEqual(item['links'][0]['ref'], 'task:t1')

    def test_emit_task_waiting_flagged(self):
        with mock.patch.object(FM, '_project_for_workdir', return_value=''):
            item = FM.emit_task_waiting({'task_id': 't2', 'prompt': 'do x'})
        self.assertTrue(item['waiting'])
        self.assertEqual(item['dedupe_key'], 'task:t2:waiting')

    def test_emit_decision(self):
        with mock.patch.object(FM, '_project_for_namespace', return_value='kc'):
            item = FM.emit_decision('project.kc.decisions', 'sse',
                                    'SSE over websockets')
        self.assertEqual(item['kind'], 'decision')
        self.assertIn('SSE over websockets', item['title'])
        self.assertEqual(item['links'][0]['ref'], 'memory:project.kc.decisions/sse')


class _OwnsTask:
    """Stands in for hypervisor_session.WATCHERS: owns exactly `owned`."""

    def __init__(self, owned=()):
        self.owned = set(owned)
        self.asked = []

    def owns_task(self, thread_id, task_id):
        self.asked.append((thread_id, task_id))
        return (thread_id, task_id) in self.owned


class ChatOwnedBuildFeedTests(FeedManagerBase):
    """A Build its chat is watching notifies through the chat (#929): no
    waiting row of its own, a history row that opens the chat, one waiting
    row per thread once the chat has answered, and today's rows whenever the
    chat cannot be shown to own it."""

    def setUp(self):
        super().setUp()
        self.tasks = tempfile.mkdtemp(prefix='kctest-feed-tasks-')
        self.addCleanup(shutil.rmtree, self.tasks, True)
        p = mock.patch.object(server.ClaudeTaskManager, 'TASKS_DIR', self.tasks)
        p.start()
        self.addCleanup(p.stop)
        for target, attr in ((server.EventBroker, 'publish'),
                             (FM, '_project_for_workdir')):
            p = mock.patch.object(target, attr, return_value='')
            p.start()
            self.addCleanup(p.stop)

    def _owner(self, owned):
        p = mock.patch.object(server, 'hv_watchers', _OwnsTask(owned))
        watchers = p.start()
        self.addCleanup(p.stop)
        return watchers

    def _task(self, tid, status, **extra):
        meta = {'task_id': tid, 'prompt': 'fix the login page\ndetails',
                'status': status, **extra}
        os.makedirs(os.path.join(self.tasks, tid), exist_ok=True)
        with open(os.path.join(self.tasks, tid, 'task.json'), 'w') as f:
            json.dump(meta, f)
        return meta

    def _transition(self, meta, to):
        meta['status'] = to
        server.ClaudeTaskManager._after_transition(
            meta, os.path.join(self.tasks, meta['task_id']), meta['task_id'],
            to, False, False)

    def test_a_chat_owned_build_parks_without_a_waiting_row(self):
        self._owner({('th-1', 't1')})
        self._transition(self._task('t1', 'running', origin_thread_id='th-1'),
                         'waiting-for-input')
        self.assertEqual(FM.list(), [])

    def test_a_chat_owned_build_still_records_its_end_linking_the_chat(self):
        self._owner({('th-1', 't1')})
        self._transition(self._task('t1', 'running', origin_thread_id='th-1'),
                         'completed')
        [row] = FM.list()
        self.assertFalse(row['waiting'])
        self.assertEqual([lk['ref'] for lk in row['links']], ['thread:th-1', 'task:t1'])

    def test_without_a_watcher_today_s_rows_are_unchanged(self):
        self._owner(set())  # the watcher expired, was cancelled, or never was
        self._transition(self._task('t1', 'running', origin_thread_id='th-1'),
                         'waiting-for-input')
        [row] = FM.list()
        self.assertTrue(row['waiting'])
        self.assertEqual(row['title'], 'Task waiting on you: fix the login page')
        self.assertEqual([lk['ref'] for lk in row['links']], ['task:t1'])

    def test_a_build_with_no_origin_never_asks_the_watchers(self):
        watchers = self._owner({('th-1', 't1')})
        self._transition(self._task('t1', 'running'), 'waiting-for-input')
        self.assertEqual(watchers.asked, [])
        self.assertTrue(FM.list()[0]['waiting'])

    def test_a_raising_ownership_check_keeps_the_build_s_rows(self):
        watchers = self._owner(set())
        watchers.owns_task = mock.Mock(side_effect=RuntimeError('disk'))
        self.assertEqual(server.chat_owner_of_task(
            {'task_id': 't1', 'origin_thread_id': 'th-1'}), '')

    def test_the_chat_level_row(self):
        self._task('t1', 'waiting-for-input', origin_thread_id='th-1')
        with mock.patch.object(server, '_hv_thread_meta',
                               return_value={'title': 'CTO: kube-coder',
                                             'project_id': 'kc'}):
            server.hv_watch_reply_notify('th-1', 't1', 'Done: login fixed.\nPR #12')
        [row] = FM.list()
        self.assertEqual((row['kind'], row['source'], row['waiting']),
                         ('activity', 'system:hypervisor', True))
        self.assertEqual(row['title'], 'CTO: kube-coder: fix the login page')
        self.assertEqual(row['body_md'], 'Done: login fixed.\nPR #12')
        self.assertEqual(row['project_id'], 'kc')
        # Thread first: push and the desktop app open the first ref.
        self.assertEqual([lk['ref'] for lk in row['links']], ['thread:th-1', 'task:t1'])

    def test_two_builds_reporting_on_one_thread_share_a_row(self):
        self._task('t1', 'completed', origin_thread_id='th-1')
        self._task('t2', 'completed', origin_thread_id='th-1')
        server.hv_watch_reply_notify('th-1', 't1', 'first')
        server.hv_watch_reply_notify('th-1', 't2', 'second')
        [row] = FM.list()
        self.assertEqual(row['body_md'], 'second')
        self.assertEqual(row['links'][1]['ref'], 'task:t2')

    def test_answering_the_chat_marks_its_row_read(self):
        server.hv_watch_reply_notify('th-1', 't1', 'done')
        self.assertTrue(FM.mark_read_by_dedupe_key(FM.watch_reply_key('th-1')))
        self.assertTrue(FM.list()[0]['read'])
        # Already read, or no such row: a quiet no-op.
        self.assertFalse(FM.mark_read_by_dedupe_key(FM.watch_reply_key('th-1')))
        self.assertFalse(FM.mark_read_by_dedupe_key(FM.watch_reply_key('th-2')))

    def test_release_emits_what_the_build_would_have(self):
        self._task('t1', 'waiting-for-input', origin_thread_id='th-1')
        server.hv_watch_release('th-1', 't1')
        [row] = FM.list()
        self.assertEqual((row['title'], row['waiting']),
                         ('Task waiting on you: fix the login page', True))
        self._task('t2', 'error', origin_thread_id='th-1')
        server.hv_watch_release('th-1', 't2')
        failed = [r for r in FM.list() if r['title'].startswith('Task failed')]
        self.assertEqual([lk['ref'] for lk in failed[0]['links']], ['task:t2'])

    def test_release_is_a_no_op_for_a_running_or_foreign_build(self):
        self._task('t1', 'running', origin_thread_id='th-1')
        server.hv_watch_release('th-1', 't1')
        # Another chat's watcher on a Build it did not dispatch never
        # suppressed anything, so it has nothing to hand back.
        self._task('t2', 'waiting-for-input', origin_thread_id='th-other')
        server.hv_watch_release('th-1', 't2')
        self._task('t3', 'waiting-for-input')
        server.hv_watch_release('th-1', 't3')
        server.hv_watch_release('th-1', 'no-such-task')
        self.assertEqual(FM.list(), [])


class WhatsAppReviewHookTests(FeedManagerBase):
    """#709 Part 4: emit hands a Board review to the WhatsApp notifier beside
    the push. Whatever that side does, the row and the push already happened."""

    REVIEW = dict(kind='activity', title='SUP-46 needs your review',
                  source='board:acme', waiting=True, dedupe_key='board:acme:46',
                  links=[{'ref': 'board:acme:46', 'label': 'Open item'}])

    def setUp(self):
        super().setUp()
        server.push_notify.PushTokenStore.register(
            'ExponentPushToken[t1]', 'ios', 'api:owner')
        for target, attr, value in ((server, 'GATEWAY_ENABLED', True),
                                    (server, 'get_gateway', lambda: None)):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def _with_notifier(self, notifier):
        p = mock.patch.object(server, '_GATEWAY_REVIEW', notifier)
        p.start()
        self.addCleanup(p.stop)

    def _wait_for_push(self):
        for _ in range(200):
            if self.expo_calls:
                return
            time.sleep(0.01)

    def test_a_review_item_reaches_the_notifier(self):
        notifier = mock.Mock()
        self._with_notifier(notifier)
        item = FM.emit(**self.REVIEW)
        notifier.on_feed_item.assert_called_once_with(item)

    def test_other_items_never_build_the_gateway(self):
        notifier = mock.Mock()
        self._with_notifier(notifier)
        with mock.patch.object(server, 'get_gateway') as built:
            FM.emit('activity', 'Task waiting on you', waiting=True,
                    links=[{'ref': 'task:t1'}])
        built.assert_not_called()
        notifier.on_feed_item.assert_not_called()

    def test_a_RAISING_notifier_breaks_neither_the_emit_nor_the_push(self):
        notifier = mock.Mock()
        notifier.on_feed_item.side_effect = RuntimeError('gateway down')
        self._with_notifier(notifier)
        item = FM.emit(**self.REVIEW)
        self.assertIsNotNone(item)
        self.assertEqual(FM.list()[0]['id'], item['id'])
        self._wait_for_push()
        self.assertEqual(len(self.expo_calls), 1)

    def test_a_RAISING_gateway_send_breaks_neither(self):
        """A real notifier whose adapter raises on send, delivering inline."""
        import gateway
        import gateway_review
        gwdir = tempfile.mkdtemp(prefix='kc-feed-wa-')
        self.addCleanup(shutil.rmtree, gwdir, True)
        gw = gateway.ConversationGateway(registry=gateway.IdentityRegistry(gwdir))
        gw.registry.bind('whatsapp:+15550001111', 'whatsapp', workspace='w',
                         workspace_host='h', token='t')
        gw._last_inbound['whatsapp:+15550001111'] = time.time()
        adapter = mock.Mock(capabilities=gateway.Capabilities(buttons=True))
        adapter.provider.name = 'meta'
        adapter.outbound.side_effect = RuntimeError('provider down')
        record = {'item_id': '46', 'item_key': 'SUP-46', 'content_hash': 'c' * 64,
                  'state': 'pending',
                  'actions': [{'id': 'a1', 'action': 'comment', 'state': 'pending',
                               'params': {'body': 'Hello'}}]}
        self._with_notifier(gateway_review.ReviewNotifier(
            gw, adapter_getter=lambda: adapter,
            record_getter=lambda b, i: record, decider=mock.Mock(),
            spawn=lambda fn: fn()))
        item = FM.emit(**self.REVIEW)
        self.assertIsNotNone(item)
        adapter.outbound.assert_called_once()
        self._wait_for_push()
        self.assertEqual(len(self.expo_calls), 1)


class FeedHandlerTests(FeedManagerBase):
    def _handler(self, authed=True, body=None):
        h = mock.Mock(spec=server.BrowserHandler)
        h.check_claude_auth.return_value = authed
        h.read_json_body.return_value = body if body is not None else {}
        h.path = '/api/feed'
        self.responses = []
        h.send_json.side_effect = \
            lambda o, s=200: self.responses.append((o, s))
        return h

    def test_create_requires_title_and_valid_kind(self):
        h = self._handler(body={'kind': 'briefing'})  # no title
        server.BrowserHandler.handle_feed_create(h)
        self.assertEqual(self.responses[-1][1], 400)

        h = self._handler(body={'kind': 'bogus', 'title': 'hi'})
        with mock.patch.object(server.EventBroker, 'publish'):
            server.BrowserHandler.handle_feed_create(h)
        self.assertEqual(self.responses[-1][1], 400)

    def test_create_ok_and_list(self):
        h = self._handler(body={'kind': 'briefing', 'title': 'Morning digest',
                                'project_id': 'kc'})
        with mock.patch.object(server.EventBroker, 'publish'):
            server.BrowserHandler.handle_feed_create(h)
        obj, status = self.responses[-1]
        self.assertEqual(status, 201)
        self.assertEqual(obj['title'], 'Morning digest')

    def test_create_unauthorized(self):
        h = self._handler(authed=False, body={'kind': 'briefing', 'title': 'x'})
        server.BrowserHandler.handle_feed_create(h)
        self.assertEqual(self.responses[-1][1], 401)

    def test_readonly_blocks_create_at_chokepoint(self):
        orig = server.READONLY_MODE
        try:
            server.READONLY_MODE = True
            h = mock.Mock(spec=server.BrowserHandler)
            h.send_json.side_effect = lambda o, s=200: None
            self.assertTrue(server.BrowserHandler._readonly_block(h))
        finally:
            server.READONLY_MODE = orig


class PostUpdateMcpTests(unittest.TestCase):
    def test_registered_as_write_tool(self):
        self.assertIn('post_update', mcp.TOOLS)
        self.assertEqual(mcp.TOOLS['post_update']['kind'], 'write')

    def test_requires_title(self):
        self.assertTrue(mcp._t_post_update({}).get('isError'))

    def test_posts_to_feed_with_thread_source(self):
        seen = {}

        def fake_api(method, path, body=None, **kw):
            seen.update(method=method, path=path, body=body)
            return (201, {'id': 'fd_1'})

        with mock.patch.dict(os.environ,
                             {'KC_HYPERVISOR_THREAD_ID': 'th1', 'KC_PROJECT_ID': 'kc'}), \
             mock.patch.object(mcp, '_api', side_effect=fake_api):
            out = mcp._t_post_update({'title': 'Dep advisory', 'kind': 'news'})
        self.assertFalse(out.get('isError'))
        self.assertEqual(seen['method'], 'POST')
        self.assertEqual(seen['path'], '/api/feed')
        self.assertEqual(seen['body']['title'], 'Dep advisory')
        self.assertEqual(seen['body']['kind'], 'news')
        self.assertEqual(seen['body']['project_id'], 'kc')  # bound project default
        self.assertEqual(seen['body']['source'], 'agent:th1')


if __name__ == '__main__':
    unittest.main()
