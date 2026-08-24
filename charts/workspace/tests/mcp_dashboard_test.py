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


class TokenPathTest(unittest.TestCase):
    """The MCP and the server must agree on where the bearer token lives.

    They did not (#633): the server hardcoded /home/dev/.claude-tasks, the MCP
    derived its path from `$HOME`. On a pod whose user's home is /home/ubuntu
    the two silently diverged, the MCP minted a token the server had never
    heard of, and every board tool 401'd — reported as a board credential
    problem several layers away.

    Two constants that must be equal, in files that never import each other, is
    exactly the kind of agreement a comment cannot keep. This pins it.
    """

    def test_token_path_matches_the_servers(self):
        import server  # noqa: WPS433 - imported here to keep it off the fast path
        self.assertEqual(m.TOKEN_FILE, server.ClaudeTaskManager.TOKEN_FILE)
        self.assertTrue(m.TOKEN_FILE.startswith(
            server.ClaudeTaskManager.TASKS_DIR + os.sep))

    def test_token_path_ignores_an_unrelated_HOME(self):
        # The whole bug in one assertion: an ephemeral $HOME must not move the
        # token. Re-import under a hostile HOME and the path must not budge.
        #
        # Compared against the path before the change rather than a literal
        # '/home/dev/...': a developer who exports KC_WORKSPACE_HOME would
        # otherwise fail this for a reason that has nothing to do with the code.
        import importlib
        before = m.TOKEN_FILE
        prior = os.environ.get('HOME')
        os.environ['HOME'] = '/home/somebody-else'
        try:
            reloaded = importlib.reload(m)
            self.assertEqual(reloaded.TOKEN_FILE, before)
            self.assertNotIn('somebody-else', reloaded.TOKEN_FILE)
        finally:
            if prior is None:
                os.environ.pop('HOME', None)
            else:
                os.environ['HOME'] = prior
            importlib.reload(m)

    def test_the_server_derives_its_path_from_the_same_knob(self):
        """A knob that moved only the MCP would rebuild the bug it fixes.

        Run in a subprocess, for two reasons. Reloading `server` in-process has
        import-time side effects and leaves earlier tests holding classes from
        the old module object — doing that broke 53 tests here before this was
        written the safe way. And asserting the *shape* of the path in-process
        proves nothing: with the variable unset, a hardcoded '/home/dev/...' is
        string-identical to the derived value, so the check passes for the wrong
        reason. Only actually moving the knob distinguishes them.
        """
        import subprocess
        workspace = os.path.dirname(HERE)
        env = dict(os.environ, KC_WORKSPACE_HOME='/mnt/workspace',
                   HOME='/home/somebody-else')
        out = subprocess.run(
            [sys.executable, '-c',
             'import server, mcp_dashboard;'
             'print(server.ClaudeTaskManager.TOKEN_FILE);'
             'print(mcp_dashboard.TOKEN_FILE)'],
            cwd=workspace, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        server_path, mcp_path = out.stdout.strip().splitlines()[-2:]
        self.assertEqual(server_path, '/mnt/workspace/.claude-tasks/.api-token')
        self.assertEqual(mcp_path, server_path)

    def test_workspace_home_is_overridable_for_odd_layouts(self):
        # Only the MCP is reloaded — small, side-effect-free, and restored in
        # `finally`. The server's half of this is pinned structurally above.
        import importlib
        prior = os.environ.get('KC_WORKSPACE_HOME')
        os.environ['KC_WORKSPACE_HOME'] = '/mnt/workspace'
        try:
            reloaded = importlib.reload(m)
            self.assertEqual(reloaded.TOKEN_FILE,
                             '/mnt/workspace/.claude-tasks/.api-token')
        finally:
            if prior is None:
                os.environ.pop('KC_WORKSPACE_HOME', None)
            else:
                os.environ['KC_WORKSPACE_HOME'] = prior
            importlib.reload(m)


class UnauthorizedMessageTest(unittest.TestCase):
    """A 401 from a loopback API can only be one thing; say so."""

    def test_401_names_the_layer_that_is_not_at_fault(self):
        m_api = m._api
        try:
            m._api = lambda *a, **k: (401, {'error': 'Unauthorized'})
            res = m._call('GET', '/api/boards')
        finally:
            m._api = m_api
        self.assertTrue(res.get('isError'))
        text = res['content'][0]['text']
        # The point of the message is to stop the reader hunting in the wrong
        # place, so that redirection is the thing worth asserting.
        self.assertIn('workspace configuration problem', text)
        self.assertIn('NOT a problem with the board', text)
        self.assertIn(m.TOKEN_FILE, text)
        self.assertIn('KC_WORKSPACE_HOME', text)

    def test_other_statuses_keep_the_plain_message(self):
        m_api = m._api
        try:
            m._api = lambda *a, **k: (500, {'error': 'boom'})
            res = m._call('GET', '/api/boards')
        finally:
            m._api = m_api
        text = res['content'][0]['text']
        self.assertIn('HTTP 500', text)
        self.assertNotIn('KC_WORKSPACE_HOME', text)


if __name__ == '__main__':
    unittest.main()
