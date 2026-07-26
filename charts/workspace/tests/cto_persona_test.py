"""Tests for the AI CTO persona (issue #465, part 2).

Three surfaces:
  * hypervisor_session.py — persona/project_id persist to thread meta + summary,
    and a bound project_id rides KC_PROJECT_ID on the turn env.
  * server.py — POST /api/hypervisor/threads selects CTO_PREAMBLE + injects the
    project brief for persona=cto (and is unchanged otherwise); GET
    …/threads?persona=&project= filters the list.
  * mcp_dashboard.py — the three project tools are wired and update_project is a
    write tool (stripped under READONLY_MODE).

Handler tests drive the real BrowserHandler methods against a
mock.Mock(spec=...) (same style as hypervisor_routes_test), patching the domain
objects so nothing spawns a CLI.

Run:  python3 -m unittest tests.cto_persona_test
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

try:
    import fcntl  # noqa: F401
except ImportError:  # pragma: no cover - platform shim
    import types
    _shim = types.ModuleType('fcntl')
    _shim.flock = lambda *a, **k: None
    _shim.LOCK_EX = _shim.LOCK_UN = _shim.LOCK_SH = _shim.LOCK_NB = 0
    sys.modules['fcntl'] = _shim

import hypervisor_session as hs  # noqa: E402
import mcp_dashboard as mcp  # noqa: E402
import server  # noqa: E402


# ──────────────────────── hypervisor_session meta ─────────────────────────

class PersonaMetaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kctest-hv-')
        self._orig = hs.HYPERVISOR_DIR
        hs.HYPERVISOR_DIR = self.tmp

    def tearDown(self):
        hs.HYPERVISOR_DIR = self._orig

    def _mk(self, **kw):
        return hs.HypervisorSession.create(
            assistant='claude', workdir='/home/dev', cli_cmd='claude',
            preamble='p', **kw)

    def test_cto_persona_persists_to_meta_and_summary(self):
        s = self._mk(persona='cto', project_id='kube-coder')
        m = s.read_meta()
        self.assertEqual(m['persona'], 'cto')
        self.assertEqual(m['project_id'], 'kube-coder')
        summ = s.summary()
        self.assertEqual(summ['persona'], 'cto')
        self.assertEqual(summ['project_id'], 'kube-coder')

    def test_default_thread_has_empty_persona(self):
        s = self._mk()
        m = s.read_meta()
        self.assertEqual(m['persona'], '')
        self.assertEqual(m['project_id'], '')
        # Backward compat: summary still carries the (empty) fields.
        self.assertEqual(s.summary()['persona'], '')

    def test_bound_project_exports_env(self):
        """A project_id in meta becomes KC_PROJECT_ID on the turn env; a plain
        thread never sets it."""
        s = self._mk(persona='cto', project_id='kc')
        captured = {}

        def fake_popen(argv, **kw):
            captured['env'] = kw.get('env') or {}
            raise RuntimeError('stop before real spawn')

        with mock.patch.object(hs.subprocess, 'Popen', side_effect=fake_popen):
            try:
                s._run_turn('hi', first=True, meta=s.read_meta())
            except Exception:
                pass
        self.assertEqual(captured['env'].get('KC_PROJECT_ID'), 'kc')
        self.assertEqual(captured['env'].get('KC_HYPERVISOR_THREAD_ID'), s.id)

        # Plain thread: no KC_PROJECT_ID.
        s2 = self._mk()
        captured.clear()
        with mock.patch.object(hs.subprocess, 'Popen', side_effect=fake_popen):
            try:
                s2._run_turn('hi', first=True, meta=s2.read_meta())
            except Exception:
                pass
        self.assertNotIn('KC_PROJECT_ID', captured['env'])


# ─────────────────────── server: create-thread persona ────────────────────

class CreateThreadPersonaTest(unittest.TestCase):
    def _handler(self, body):
        h = mock.Mock(spec=server.BrowserHandler)
        h.check_claude_auth.return_value = True
        h.read_json_body.return_value = body
        self.responses = []
        h.send_json.side_effect = \
            lambda o, s=200: self.responses.append((o, s))
        return h

    def _run(self, body, brief=None, project=None):
        captured = {}
        fake_session = mock.Mock()
        fake_session.summary.return_value = {'id': 'x'}

        def fake_create(**kw):
            captured.update(kw)
            return fake_session

        with mock.patch.object(server, 'HYPERVISOR_ENABLED', True), \
             mock.patch.object(server, '_HYPERVISOR_AVAILABLE', True), \
             mock.patch.object(server.ClaudeTaskManager, 'resolve_assistant',
                               return_value='claude'), \
             mock.patch.object(server.ClaudeTaskManager, 'resolve_model',
                               return_value=''), \
             mock.patch.object(server.ClaudeTaskManager, 'assistant_command',
                               return_value='claude'), \
             mock.patch.object(server.HypervisorSession, 'create',
                               side_effect=fake_create), \
             mock.patch.object(server.ProjectsManager, 'brief',
                               return_value=brief), \
             mock.patch.object(server.ProjectsManager, 'get_project',
                               return_value=project):
            h = self._handler(body)
            server.BrowserHandler.handle_hypervisor_create_thread(h)
        return captured, fake_session

    def test_cto_persona_selects_preamble_and_injects_brief(self):
        cap, sess = self._run(
            {'message': 'hi', 'persona': 'cto', 'project_id': 'kc'},
            brief={'brief_markdown': '# BRIEF-MARKER goals: ship'},
            project={'workdirs': ['/home/dev/kc']})
        self.assertEqual(cap['persona'], 'cto')
        self.assertEqual(cap['project_id'], 'kc')
        self.assertIn('AI CTO', cap['preamble'])
        self.assertIn('BRIEF-MARKER', cap['preamble'])
        # workdir defaulted from the project (no explicit workdir in the body)
        self.assertEqual(cap['workdir'], '/home/dev/kc')
        sess.send.assert_called_once_with('hi')
        self.assertEqual(self.responses[-1][1], 201)

    def test_cto_without_project_still_uses_cto_preamble(self):
        cap, _ = self._run({'message': 'hi', 'persona': 'cto'})
        self.assertEqual(cap['persona'], 'cto')
        self.assertEqual(cap['project_id'], '')
        self.assertIn('AI CTO', cap['preamble'])

    def test_default_persona_unchanged(self):
        cap, _ = self._run({'message': 'hi'})
        self.assertEqual(cap['persona'], '')
        self.assertEqual(cap['project_id'], '')
        self.assertEqual(cap['preamble'], server.HYPERVISOR_PREAMBLE)

    def test_unknown_persona_falls_back_to_default(self):
        cap, _ = self._run({'message': 'hi', 'persona': 'wizard',
                            'project_id': 'kc'})
        self.assertEqual(cap['persona'], '')
        self.assertEqual(cap['project_id'], '')  # dropped for non-cto
        self.assertEqual(cap['preamble'], server.HYPERVISOR_PREAMBLE)


# ─────────────────────── server: thread-list filter ───────────────────────

class ListThreadsFilterTest(unittest.TestCase):
    THREADS = [
        {'id': '1', 'persona': 'cto', 'project_id': 'kc'},
        {'id': '2', 'persona': 'cto', 'project_id': 'other'},
        {'id': '3', 'persona': '', 'project_id': ''},
    ]

    def _run(self, path):
        h = mock.Mock(spec=server.BrowserHandler)
        h.check_claude_auth.return_value = True
        h.path = path
        self.responses = []
        h.send_json.side_effect = \
            lambda o, s=200: self.responses.append((o, s))
        with mock.patch.object(server, '_HYPERVISOR_AVAILABLE', True), \
             mock.patch.object(server.HypervisorSession, 'list',
                               return_value=[dict(t) for t in self.THREADS]):
            server.BrowserHandler.handle_hypervisor_list_threads(h)
        return [t['id'] for t in self.responses[-1][0]['threads']]

    def test_no_filter_returns_all(self):
        self.assertEqual(self._run('/api/hypervisor/threads'), ['1', '2', '3'])

    def test_persona_cto(self):
        self.assertEqual(self._run('/api/hypervisor/threads?persona=cto'),
                         ['1', '2'])

    def test_persona_cto_and_project(self):
        self.assertEqual(
            self._run('/api/hypervisor/threads?persona=cto&project=kc'), ['1'])

    def test_persona_default_excludes_cto(self):
        self.assertEqual(self._run('/api/hypervisor/threads?persona=default'),
                         ['3'])


# ───────────────────────────── mcp tools ──────────────────────────────────

class ProjectMcpToolsTest(unittest.TestCase):
    def test_tools_registered_with_expected_kinds(self):
        self.assertEqual(mcp.TOOLS['list_projects']['kind'], 'read')
        self.assertEqual(mcp.TOOLS['get_project_brief']['kind'], 'read')
        self.assertEqual(mcp.TOOLS['update_project']['kind'], 'write')

    def test_update_project_stripped_under_readonly(self):
        with mock.patch.object(mcp, '_readonly', return_value=True):
            enabled = mcp._enabled_tools()
        self.assertNotIn('update_project', enabled)
        self.assertIn('list_projects', enabled)       # read survives
        self.assertIn('get_project_brief', enabled)

    def test_get_project_brief_returns_markdown(self):
        with mock.patch.object(mcp, '_api',
                               return_value=(200, {'brief_markdown': '# HELLO'})):
            out = mcp._t_get_project_brief({'project_id': 'kc'})
        self.assertFalse(out.get('isError'))
        self.assertIn('HELLO', out['content'][0]['text'])

    def test_get_project_brief_defaults_to_bound_project(self):
        seen = {}

        def fake_api(method, path, **kw):
            seen['path'] = path
            return (200, {'brief_markdown': 'x'})

        with mock.patch.dict(os.environ, {'KC_PROJECT_ID': 'bound-proj'}), \
             mock.patch.object(mcp, '_api', side_effect=fake_api):
            mcp._t_get_project_brief({})
        self.assertIn('bound-proj', seen['path'])

    def test_get_project_brief_requires_a_project(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('KC_PROJECT_ID', None)
            out = mcp._t_get_project_brief({})
        self.assertTrue(out.get('isError'))

    def test_update_project_requires_fields_object(self):
        with mock.patch.dict(os.environ, {'KC_PROJECT_ID': 'kc'}):
            self.assertTrue(mcp._t_update_project({}).get('isError'))
            self.assertTrue(
                mcp._t_update_project({'fields': 'nope'}).get('isError'))

    def test_update_project_puts_fields(self):
        seen = {}

        def fake_api(method, path, body=None, **kw):
            seen.update(method=method, path=path, body=body)
            return (200, {'id': 'kc', 'status': 'paused'})

        with mock.patch.object(mcp, '_api', side_effect=fake_api):
            out = mcp._t_update_project(
                {'project_id': 'kc', 'fields': {'status': 'paused'}})
        self.assertFalse(out.get('isError'))
        self.assertEqual(seen['method'], 'PUT')
        self.assertIn('/api/projects/kc', seen['path'])
        self.assertEqual(seen['body'], {'status': 'paused'})


if __name__ == '__main__':
    unittest.main()
