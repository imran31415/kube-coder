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
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402
import mcp_dashboard as mcp  # noqa: E402

FM = server.FeedManager


class FeedManagerBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kctest-feed-')
        self._orig = (FM.FEED_DIR, FM.ITEMS_PATH, FM.STATE_PATH)
        FM.FEED_DIR = self.dir
        FM.ITEMS_PATH = os.path.join(self.dir, 'items.jsonl')
        FM.STATE_PATH = os.path.join(self.dir, 'state.json')

    def tearDown(self):
        FM.FEED_DIR, FM.ITEMS_PATH, FM.STATE_PATH = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)


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
