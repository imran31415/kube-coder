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


class CreateTaskPreviewTest(unittest.TestCase):
    """The first-win dispatch contract (#485): create_task defaults to a
    runnable-build posture — it appends the dev-server contract to the prompt
    and auto-arms a `port` watcher on the CTO thread so a live preview
    auto-surfaces (#484)."""

    def setUp(self):
        self.calls = []  # (method, path, body) for every _api call
        self._orig_api = m._api

        def fake_api(method, path, body=None, query=None):
            self.calls.append((method, path, body))
            if path == '/api/claude/tasks':
                return 201, {'task_id': 'T-123', 'status': 'running'}
            return 201, {'ok': True}

        m._api = fake_api
        self._orig_thread = os.environ.get('KC_HYPERVISOR_THREAD_ID')
        os.environ['KC_HYPERVISOR_THREAD_ID'] = 'thread-xyz'
        self._orig_project = os.environ.get('KC_PROJECT_ID')
        os.environ.pop('KC_PROJECT_ID', None)

    def tearDown(self):
        m._api = self._orig_api
        if self._orig_thread is None:
            os.environ.pop('KC_HYPERVISOR_THREAD_ID', None)
        else:
            os.environ['KC_HYPERVISOR_THREAD_ID'] = self._orig_thread
        if self._orig_project is None:
            os.environ.pop('KC_PROJECT_ID', None)
        else:
            os.environ['KC_PROJECT_ID'] = self._orig_project

    def _task_body(self):
        return next(b for meth, p, b in self.calls if p == '/api/claude/tasks')

    def _watcher_posts(self):
        return [(p, b) for meth, p, b in self.calls
                if meth == 'POST' and '/watchers' in p]

    def test_default_appends_contract_and_arms_port_watcher(self):
        res = m._t_create_task({'prompt': 'build me a todo app'})
        self.assertFalse(res.get('isError'))
        body = self._task_body()
        self.assertIn('build me a todo app', body['prompt'])
        self.assertIn('Build-preview contract', body['prompt'])
        posts = self._watcher_posts()
        self.assertEqual(len(posts), 1)
        path, wbody = posts[0]
        self.assertIn('thread-xyz', path)
        self.assertEqual(wbody['kind'], 'port')
        self.assertEqual(wbody['target'], 'T-123')

    def test_preview_false_skips_contract_and_watcher(self):
        res = m._t_create_task({'prompt': 'refactor utils', 'preview': False})
        self.assertFalse(res.get('isError'))
        body = self._task_body()
        self.assertNotIn('Build-preview contract', body['prompt'])
        self.assertEqual(self._watcher_posts(), [])

    def test_no_thread_context_skips_watcher_but_keeps_contract(self):
        os.environ.pop('KC_HYPERVISOR_THREAD_ID', None)
        res = m._t_create_task({'prompt': 'build a landing page'})
        self.assertFalse(res.get('isError'))
        # Contract still helps (a server will run), but there's no chat to
        # surface a preview in, so no watcher is armed.
        self.assertIn('Build-preview contract', self._task_body()['prompt'])
        self.assertEqual(self._watcher_posts(), [])

    def test_binds_the_ctos_project_to_the_task(self):
        # A CTO turn exports its bound project; the dispatched build carries it
        # so the project's brief counts it wherever it runs (#533).
        os.environ['KC_PROJECT_ID'] = 'kube-coder'
        m._t_create_task({'prompt': 'ship the thing'})
        self.assertEqual(self._task_body()['project_id'], 'kube-coder')

    def test_no_project_context_sends_no_binding(self):
        m._t_create_task({'prompt': 'ship the thing'})
        self.assertNotIn('project_id', self._task_body())

    def test_task_creation_failure_returns_error_and_arms_nothing(self):
        def failing_api(method, path, body=None, query=None):
            self.calls.append((method, path, body))
            return 500, {'error': 'boom'}
        m._api = failing_api
        res = m._t_create_task({'prompt': 'build something'})
        self.assertTrue(res.get('isError'))
        self.assertEqual(self._watcher_posts(), [])


if __name__ == '__main__':
    unittest.main()
