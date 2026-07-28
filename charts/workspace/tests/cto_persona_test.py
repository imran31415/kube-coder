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

    def _run(self, body, brief=None, project=None, cto_enabled=True,
             existing=None):
        captured = {}
        fake_session = mock.Mock()
        fake_session.summary.return_value = {'id': 'x'}

        def fake_create(**kw):
            captured.update(kw)
            return fake_session

        with mock.patch.object(server, 'HYPERVISOR_ENABLED', True), \
             mock.patch.object(server, 'cto_available', return_value=cto_enabled), \
             mock.patch.object(server, '_HYPERVISOR_AVAILABLE', True), \
             mock.patch.object(server.ClaudeTaskManager, 'resolve_assistant',
                               return_value='claude'), \
             mock.patch.object(server.ClaudeTaskManager, 'resolve_model',
                               return_value=''), \
             mock.patch.object(server.ClaudeTaskManager, 'assistant_command',
                               return_value='claude'), \
             mock.patch.object(server.HypervisorSession, 'create',
                               side_effect=fake_create), \
             mock.patch.object(server.HypervisorSession, 'list',
                               return_value=list(existing or [])), \
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

    def test_cto_drops_unknown_project_binding(self):
        # persona cto but the project doesn't exist (get_project → None, #469
        # review L5) → keep the CTO persona, drop the bogus binding.
        cap, _ = self._run({'message': 'hi', 'persona': 'cto',
                            'project_id': 'ghost'}, project=None)
        self.assertEqual(cap['persona'], 'cto')
        self.assertEqual(cap['project_id'], '')
        self.assertIn('AI CTO', cap['preamble'])

    def test_cto_persona_downgraded_when_feature_disabled(self):
        # cto requested but the feature is off (#467) → plain Hypervisor thread.
        cap, _ = self._run({'message': 'hi', 'persona': 'cto', 'project_id': 'kc'},
                           cto_enabled=False)
        self.assertEqual(cap['persona'], '')
        self.assertEqual(cap['project_id'], '')
        self.assertEqual(cap['preamble'], server.HYPERVISOR_PREAMBLE)

    # ── first-win fast-path (#486) ──────────────────────────────────────────

    def test_first_cto_thread_gets_first_win_addendum(self):
        # No prior CTO thread + an opening message → the no-gate build addendum
        # is appended so the user's one sentence starts a build immediately.
        cap, _ = self._run(
            {'message': 'build me a todo app', 'persona': 'cto',
             'project_id': 'kc'},
            existing=[{'id': '0', 'persona': ''}])  # only a plain thread exists
        self.assertIn('FIRST-WIN ONBOARDING', cap['preamble'])
        self.assertIn('AI CTO', cap['preamble'])  # base preamble still present
        # Addendum is LAST so it overrides the DISPATCH gate.
        self.assertTrue(cap['preamble'].endswith(server.CTO_FIRST_WIN_ADDENDUM))

    def test_later_cto_thread_stays_gated(self):
        # A prior CTO thread already exists → not a first-timer, so the normal
        # propose-then-confirm preamble is used (no addendum).
        cap, _ = self._run(
            {'message': 'build me a todo app', 'persona': 'cto',
             'project_id': 'kc'},
            existing=[{'id': '9', 'persona': 'cto'}])
        self.assertNotIn('FIRST-WIN ONBOARDING', cap['preamble'])
        self.assertIn('AI CTO', cap['preamble'])

    def test_first_cto_thread_without_message_stays_gated(self):
        # An empty opener isn't a build request → no fast-path even when first.
        cap, _ = self._run({'message': '', 'persona': 'cto', 'project_id': 'kc'},
                           existing=[])
        self.assertNotIn('FIRST-WIN ONBOARDING', cap['preamble'])

    def test_first_win_never_applies_to_plain_thread(self):
        # A default (non-cto) thread is never a first-win, regardless of history.
        cap, _ = self._run({'message': 'hi'}, existing=[])
        self.assertEqual(cap['preamble'], server.HYPERVISOR_PREAMBLE)


# ─────────────────────── server: thread-list filter ───────────────────────

class CreateThreadProjectDefaultsTest(unittest.TestCase):
    """Per-project assistant configuration (#483, #362).

    A CTO thread whose body omits assistant/model/effort inherits the bound
    project's defaults, then the workspace default — so the MCP and cron paths,
    which never send those fields, are correct too. An explicit body value wins.
    Nothing bypasses resolve_*, so a project pinned to a provider this workspace
    no longer has a key for degrades instead of launching a dead CLI.
    """

    def _handler(self, body):
        h = mock.Mock(spec=server.BrowserHandler)
        h.check_claude_auth.return_value = True
        h.read_json_body.return_value = body
        self.responses = []
        h.send_json.side_effect = lambda o, s=200: self.responses.append((o, s))
        return h

    def _run(self, body, project=None):
        """Returns (create_kwargs, requested) where `requested` records what was
        handed to each resolver — the fallback chain under test."""
        captured = {}
        requested = {}
        fake_session = mock.Mock()
        fake_session.summary.return_value = {'id': 'x'}

        def fake_create(**kw):
            captured.update(kw)
            return fake_session

        def fake_resolve_assistant(req):
            requested['assistant'] = req
            return req or 'claude'

        def fake_resolve_model(assistant, req):
            requested['model'] = req
            return req or ''

        def fake_resolve_effort(assistant, req):
            requested['effort'] = req
            return req or ''

        with mock.patch.object(server, 'HYPERVISOR_ENABLED', True), \
             mock.patch.object(server, 'cto_available', return_value=True), \
             mock.patch.object(server, '_HYPERVISOR_AVAILABLE', True), \
             mock.patch.object(server, 'HYPERVISOR_DEFAULT_ASSISTANT', 'ante'), \
             mock.patch.object(server.ClaudeTaskManager, 'resolve_assistant',
                               side_effect=fake_resolve_assistant), \
             mock.patch.object(server.ClaudeTaskManager, 'resolve_model',
                               side_effect=fake_resolve_model), \
             mock.patch.object(server.ClaudeTaskManager, 'resolve_effort',
                               side_effect=fake_resolve_effort), \
             mock.patch.object(server.ClaudeTaskManager, 'assistant_command',
                               return_value='claude'), \
             mock.patch.object(server.HypervisorSession, 'create',
                               side_effect=fake_create), \
             mock.patch.object(server.HypervisorSession, 'list',
                               return_value=[{'persona': 'cto'}]), \
             mock.patch.object(server.ProjectsManager, 'brief',
                               return_value=None), \
             mock.patch.object(server.ProjectsManager, 'get_project',
                               return_value=project or {'id': 'kc'}), \
             mock.patch.object(server.ProjectsManager, 'defaults_for',
                               side_effect=lambda pid: (
                                   ((project or {}).get('default_assistant', ''),
                                    (project or {}).get('default_model', ''),
                                    (project or {}).get('default_effort', ''))
                                   if pid else ('', '', ''))):
            h = self._handler(body)
            server.BrowserHandler.handle_hypervisor_create_thread(h)
        return captured, requested

    _CONFIGURED = {'id': 'kc', 'default_assistant': 'codex',
                   'default_model': 'opus', 'default_effort': 'xhigh'}

    def test_project_defaults_used_when_body_omits_them(self):
        cap, req = self._run({'message': 'hi', 'persona': 'cto',
                              'project_id': 'kc'}, project=self._CONFIGURED)
        self.assertEqual(req['assistant'], 'codex')
        self.assertEqual(req['model'], 'opus')
        self.assertEqual(req['effort'], 'xhigh')
        self.assertEqual(cap['model'], 'opus')
        self.assertEqual(cap['effort'], 'xhigh')

    def test_explicit_body_values_win_over_the_project(self):
        cap, req = self._run({'message': 'hi', 'persona': 'cto',
                              'project_id': 'kc', 'assistant': 'claude',
                              'model': 'haiku', 'effort': 'low'},
                             project=self._CONFIGURED)
        self.assertEqual(req['assistant'], 'claude')
        self.assertEqual(req['model'], 'haiku')
        self.assertEqual(req['effort'], 'low')
        self.assertEqual(cap['assistant'], 'claude')

    def test_unconfigured_project_falls_through_to_the_workspace_default(self):
        _, req = self._run({'message': 'hi', 'persona': 'cto',
                            'project_id': 'kc'}, project={'id': 'kc'})
        self.assertEqual(req['assistant'], 'ante')
        self.assertEqual(req['model'], '')
        self.assertEqual(req['effort'], '')

    def test_plain_chat_never_reads_a_project_default(self):
        # No persona → no project binding → the Chat tab is untouched by #483.
        _, req = self._run({'message': 'hi'}, project=self._CONFIGURED)
        self.assertEqual(req['assistant'], 'ante')
        self.assertEqual(req['model'], '')

    def test_inherited_values_are_stamped_on_the_session(self):
        # The adapters read model/effort out of the thread's ctx fresh every turn
        # (#308/#362), so the inherited values only take effect if they are
        # recorded at creation — not just used to build the launch command.
        cap, _ = self._run({'message': 'hi', 'persona': 'cto',
                            'project_id': 'kc'}, project=self._CONFIGURED)
        self.assertEqual(cap['assistant'], 'codex')
        self.assertEqual(cap['model'], 'opus')
        self.assertEqual(cap['effort'], 'xhigh')
        self.assertEqual(cap['project_id'], 'kc')


class DispatchedBuildDefaultsTest(unittest.TestCase):
    """create_task honours the bound project's assistant config (#483) — this is
    the path the CTO's own dispatch tool takes, and it sends only project_id."""

    def _create(self, project_defaults, **kwargs):
        seen = {}

        def fake_command(assistant, auto_approve=False, model='', effort=''):
            seen.update(assistant=assistant, model=model, effort=effort)
            return 'true'

        def fake_resolve_assistant(req):
            seen['requested_assistant'] = req
            return req or 'claude'

        def fake_resolve_model(assistant, req):
            seen['requested_model'] = req
            return req or ''

        def fake_resolve_effort(assistant, req):
            seen['requested_effort'] = req
            return req or ''

        with mock.patch.object(server.ProjectsManager, 'defaults_for',
                               return_value=project_defaults), \
             mock.patch.object(server.ClaudeTaskManager, 'resolve_assistant',
                               side_effect=fake_resolve_assistant), \
             mock.patch.object(server.ClaudeTaskManager, 'resolve_model',
                               side_effect=fake_resolve_model), \
             mock.patch.object(server.ClaudeTaskManager, 'resolve_effort',
                               side_effect=fake_resolve_effort), \
             mock.patch.object(server.ClaudeTaskManager, 'ensure_tasks_dir'), \
             mock.patch.object(server.ClaudeTaskManager, '_ensure_claude_trust'), \
             mock.patch.object(server.ProviderKeysManager, 'env_overlay',
                               return_value={}), \
             mock.patch.object(server.ClaudeTaskManager, 'at_capacity',
                               return_value=(False, 0, 12)), \
             mock.patch.object(server.ClaudeTaskManager, 'assistant_command',
                               side_effect=fake_command), \
             mock.patch('os.makedirs'), \
             mock.patch('builtins.open', mock.mock_open()), \
             mock.patch('subprocess.run') as run, \
             mock.patch('threading.Thread'), \
             mock.patch.object(server.EventBroker, 'publish'):
            run.return_value = mock.Mock(returncode=0, stderr='')
            meta = server.ClaudeTaskManager.create_task(
                'build it', project_id='kc', **kwargs)
        return seen, meta, run

    def test_dispatch_inherits_the_projects_assistant_config(self):
        seen, meta, _ = self._create(('codex', 'opus', 'xhigh'))
        self.assertEqual(seen['requested_assistant'], 'codex')
        self.assertEqual(seen['requested_model'], 'opus')
        self.assertEqual(seen['requested_effort'], 'xhigh')
        self.assertEqual(meta['model'], 'opus')
        self.assertEqual(meta['effort'], 'xhigh')

    def test_explicit_assistant_beats_the_project_default(self):
        seen, _, _ = self._create(('codex', 'opus', 'xhigh'),
                                  assistant='claude', model='haiku',
                                  effort='low')
        self.assertEqual(seen['requested_assistant'], 'claude')
        self.assertEqual(seen['requested_model'], 'haiku')
        self.assertEqual(seen['requested_effort'], 'low')

    def test_effort_env_reaches_the_tmux_session(self):
        # Claude Code takes effort as an env var, so it must be exported onto
        # the new session rather than appended to argv.
        with mock.patch.object(server.ClaudeTaskManager, 'effort_env',
                               return_value={'CLAUDE_CODE_EFFORT_LEVEL': 'xhigh'}):
            _, _, run = self._create(('claude', 'opus', 'xhigh'))
        argv = run.call_args_list[0][0][0]
        self.assertIn('CLAUDE_CODE_EFFORT_LEVEL=xhigh', argv)


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

    def test_list_projects_trims_to_essential_fields(self):
        full = {'projects': [{
            'id': 'kc', 'name': 'kube-coder', 'status': 'active',
            'repo': 'o/kc', 'memory_namespace': 'project.kc',
            'workdirs': ['/home/dev/kc'], 'north_star': 'ship it',
            'created_at': 1, 'updated_at': 2, 'last_seen_at': 3,
            'pulse': {'running': 2, 'waiting': 1, 'last_activity_at': 2},
        }]}
        with mock.patch.object(mcp, '_api', return_value=(200, full)):
            out = mcp._t_list_projects({})
        text = out['content'][0]['text']
        self.assertIn('kube-coder', text)
        self.assertIn('ship it', text)
        # Verbose registry fields are dropped to keep the portfolio token-cheap.
        self.assertNotIn('memory_namespace', text)
        self.assertNotIn('created_at', text)
        self.assertIn('"running": 2', text)

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
