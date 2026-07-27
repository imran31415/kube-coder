"""Unit tests for the Hypervisor render tools in mcp_dashboard.py.

The render tools (show_app_preview, show_media) exist so the agent can render
live app previews / images / videos inline in the chat. The render signal is the
tool CALL (name + input) which the frontend keys off; these tests cover the
argument validation and registration.

Run with:    python3 -m unittest tests.mcp_dashboard_test
(from charts/workspace/)
"""

import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import mcp_dashboard as m  # noqa: E402


class RenderToolsTest(unittest.TestCase):
    def test_tools_registered_as_read(self):
        for name in ('show_app_preview', 'show_media', 'show_file'):
            self.assertIn(name, m.TOOLS)
            self.assertEqual(m.TOOLS[name]['kind'], 'read')  # available under READONLY_MODE

    def test_show_file_requires_a_path(self):
        self.assertTrue(m._t_show_file({}).get('isError'))
        self.assertTrue(m._t_show_file({'path': ''}).get('isError'))
        # A URL is not a workspace file — reject it (that's show_media's job).
        self.assertTrue(m._t_show_file({'path': 'https://x/a.pdf'}).get('isError'))

    def test_show_file_accepts_a_workspace_path(self):
        self.assertFalse(m._t_show_file({'path': 'docs/plan.md'}).get('isError'))
        self.assertFalse(m._t_show_file({'path': 'report.pdf', 'title': 'Q3'}).get('isError'))

    def test_show_media_requires_exactly_one_source(self):
        self.assertTrue(m._t_show_media({'media_kind': 'image'}).get('isError'))
        self.assertTrue(m._t_show_media(
            {'media_kind': 'image', 'path': 'a.png', 'url': 'http://x/a.png'}).get('isError'))

    def test_show_media_rejects_bad_kind_and_scheme(self):
        self.assertTrue(m._t_show_media({'media_kind': 'gif', 'path': 'a.gif'}).get('isError'))
        self.assertTrue(m._t_show_media(
            {'media_kind': 'image', 'url': 'ftp://x/a.png'}).get('isError'))

    def test_show_media_accepts_path_and_url(self):
        self.assertFalse(m._t_show_media({'media_kind': 'image', 'path': 'shot.png'}).get('isError'))
        self.assertFalse(m._t_show_media(
            {'media_kind': 'video', 'url': 'https://x/clip.mp4'}).get('isError'))

    def test_show_app_preview_requires_positive_port(self):
        self.assertTrue(m._t_show_app_preview({}).get('isError'))
        self.assertTrue(m._t_show_app_preview({'port': 'abc'}).get('isError'))
        self.assertTrue(m._t_show_app_preview({'port': 0}).get('isError'))
        # A valid port doesn't error even when /api/apps is unreachable in tests.
        self.assertFalse(m._t_show_app_preview({'port': 3000}).get('isError'))


class CreateTaskAutoPreviewTest(unittest.TestCase):
    """create_task arms a `port` watcher so a build's dev server auto-surfaces
    as a live preview in the chat (issue #484)."""

    def _run(self, args, thread_id='thread-xyz'):
        calls = []

        def fake_api(method, path, body=None, query=None):
            calls.append((method, path, body))
            if path == '/api/claude/tasks':
                return 201, {'task_id': 'task-123', 'status': 'running'}
            return 201, {'watcher': {'id': 'w1'}}

        env = {} if thread_id is None else {'KC_HYPERVISOR_THREAD_ID': thread_id}
        with mock.patch.object(m, '_api', side_effect=fake_api), \
                mock.patch.dict(os.environ, env, clear=False):
            if thread_id is None:
                os.environ.pop('KC_HYPERVISOR_THREAD_ID', None)
            res = m._t_create_task(args)
        return res, calls

    def test_arms_port_watcher_after_creating_task(self):
        res, calls = self._run({'prompt': 'build my app'})
        self.assertFalse(res.get('isError'))
        watcher_calls = [c for c in calls if c[1].endswith('/watchers')]
        self.assertEqual(len(watcher_calls), 1)
        method, path, body = watcher_calls[0]
        self.assertEqual(method, 'POST')
        self.assertIn('thread-xyz', path)
        self.assertEqual(body['kind'], 'port')
        self.assertEqual(body['target'], 'new')
        self.assertIn('task-123', body['note'])

    def test_preview_false_skips_the_watcher(self):
        _, calls = self._run({'prompt': 'build', 'preview': False})
        self.assertEqual([c for c in calls if c[1].endswith('/watchers')], [])

    def test_no_watcher_outside_a_hypervisor_thread(self):
        _, calls = self._run({'prompt': 'build'}, thread_id=None)
        self.assertEqual([c for c in calls if c[1].endswith('/watchers')], [])

    def test_task_create_failure_short_circuits(self):
        def fake_api(method, path, body=None, query=None):
            return 500, {'error': 'boom'}
        with mock.patch.object(m, '_api', side_effect=fake_api), \
                mock.patch.dict(os.environ,
                                {'KC_HYPERVISOR_THREAD_ID': 't'}, clear=False):
            res = m._t_create_task({'prompt': 'build'})
        self.assertTrue(res.get('isError'))


if __name__ == '__main__':
    unittest.main()
