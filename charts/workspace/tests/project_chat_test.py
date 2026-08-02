"""Binding an ORDINARY chat to a project (issue #358).

Project binding used to be CTO-only: `handle_hypervisor_create_thread` dropped
`project_id` for every other persona, so the surface most people use — the Chat
tab — had no project at all. Nothing could group by it, and its turns never
exported KC_PROJECT_ID, which is what scopes memory retrieval to the project
(#359).

Three surfaces:
  * hypervisor_session.py — set_project persists/clears the binding, and a plain
    bound thread exports KC_PROJECT_ID on its turns.
  * server.py — POST /api/hypervisor/threads accepts project_id for a plain
    chat; POST …/threads/{id}/project re-files an existing one; GET
    …/threads?persona=default&project= filters the Chat tab's list.
  * memory — a memory written under a project's namespace is retrievable from
    that project's chat and NOT from another project's.

Run:  python3 -m unittest tests.project_chat_test
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

try:
    import fcntl  # noqa: F401
except ImportError:  # pragma: no cover - platform shim
    import types
    _shim = types.ModuleType('fcntl')
    _shim.flock = lambda *a, **k: None
    _shim.LOCK_EX = _shim.LOCK_UN = _shim.LOCK_SH = _shim.LOCK_NB = 0
    sys.modules['fcntl'] = _shim

import hypervisor_session as hs  # noqa: E402
import memory_inject_hook as inject  # noqa: E402
import server  # noqa: E402
from memory import store as _store_mod  # noqa: E402
from memory.manager import MemoryManager  # noqa: E402
from memory.store import MemoryStore  # noqa: E402


# ─────────────────────── hypervisor_session: the binding ───────────────────

class SetProjectTest(unittest.TestCase):
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

    def test_files_a_plain_chat_into_a_project(self):
        s = self._mk()
        self.assertEqual(s.read_meta()['project_id'], '')
        summary = s.set_project('kc')
        self.assertEqual(summary['project_id'], 'kc')
        self.assertEqual(s.read_meta()['project_id'], 'kc')
        # Survives a reload — the binding is on disk, not in memory.
        self.assertEqual(
            hs.HypervisorSession.get(s.id).summary()['project_id'], 'kc')

    def test_empty_id_clears_the_binding(self):
        s = self._mk(project_id='kc')
        self.assertEqual(s.set_project('')['project_id'], '')

    def test_refiling_does_not_reorder_the_chat_list(self):
        # updated_at drives the list order; re-filing a chat is not activity.
        s = self._mk()
        before = s.read_meta()['updated_at']
        s.set_project('kc')
        self.assertEqual(s.read_meta()['updated_at'], before)

    def test_binding_stays_a_plain_chat(self):
        s = self._mk()
        s.set_project('kc')
        self.assertEqual(s.read_meta()['persona'], '')

    def test_missing_thread_returns_none(self):
        self.assertIsNone(hs.HypervisorSession('nope').set_project('kc'))

    def test_bound_plain_chat_exports_the_project_env(self):
        """The whole point of the binding: a plain chat's turns now carry
        KC_PROJECT_ID, which is what scopes its memory retrieval (#359) and
        points the project MCP tools at the right project."""
        s = self._mk()
        s.set_project('kc')
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


# ────────────────────────── server: create + re-file ───────────────────────

class CreateThreadProjectBindingTest(unittest.TestCase):
    """POST /api/hypervisor/threads now honours project_id for a plain chat."""

    def _handler(self, body):
        h = mock.Mock(spec=server.BrowserHandler)
        h.check_claude_auth.return_value = True
        h.read_json_body.return_value = body
        self.responses = []
        h.send_json.side_effect = lambda o, s=200: self.responses.append((o, s))
        return h

    def _run(self, body, project=None):
        captured = {}
        fake_session = mock.Mock()
        fake_session.summary.return_value = {'id': 'x'}

        def fake_create(**kw):
            captured.update(kw)
            return fake_session

        with mock.patch.object(server, 'HYPERVISOR_ENABLED', True), \
             mock.patch.object(server, 'cto_available', return_value=True), \
             mock.patch.object(server, '_HYPERVISOR_AVAILABLE', True), \
             mock.patch.object(server.ClaudeTaskManager, 'resolve_assistant',
                               return_value='claude'), \
             mock.patch.object(server.ClaudeTaskManager, 'resolve_model',
                               return_value=''), \
             mock.patch.object(server.ClaudeTaskManager, 'resolve_effort',
                               return_value=''), \
             mock.patch.object(server.ClaudeTaskManager, 'assistant_command',
                               return_value='claude'), \
             mock.patch.object(server.HypervisorSession, 'create',
                               side_effect=fake_create), \
             mock.patch.object(server.HypervisorSession, 'list',
                               return_value=[{'persona': 'cto'}]), \
             mock.patch.object(server.ProjectsManager, 'brief',
                               return_value=None), \
             mock.patch.object(server.ProjectsManager, 'get_project',
                               return_value=project):
            h = self._handler(body)
            server.BrowserHandler.handle_hypervisor_create_thread(h)
        return captured

    def test_plain_chat_is_filed_into_the_project(self):
        cap = self._run({'message': 'hi', 'project_id': 'kc'},
                        project={'id': 'kc'})
        self.assertEqual(cap['project_id'], 'kc')
        # Still a plain chat: no CTO persona, no CTO preamble.
        self.assertEqual(cap['persona'], '')
        self.assertEqual(cap['preamble'], server.HYPERVISOR_PREAMBLE)

    def test_plain_chat_drops_an_unknown_project(self):
        cap = self._run({'message': 'hi', 'project_id': 'ghost'}, project=None)
        self.assertEqual(cap['project_id'], '')

    def test_plain_chat_drops_a_malformed_project_id(self):
        cap = self._run({'message': 'hi', 'project_id': '../../etc'},
                        project={'id': 'x'})
        self.assertEqual(cap['project_id'], '')

    def test_unbound_plain_chat_is_unchanged(self):
        cap = self._run({'message': 'hi'})
        self.assertEqual(cap['project_id'], '')

    def test_plain_chat_keeps_its_own_workdir_not_the_projects(self):
        # Only a CTO thread defaults its folder from the project (#465); the
        # Chat tab has its own folder picker and it must keep winning.
        cap = self._run({'message': 'hi', 'project_id': 'kc'},
                        project={'id': 'kc', 'workdirs': ['/home/dev/kc']})
        self.assertEqual(cap['workdir'], server.HYPERVISOR_WORKDIR)


class SetProjectRouteTest(unittest.TestCase):
    """POST /api/hypervisor/threads/{id}/project — re-file an existing chat."""

    def _run(self, body, meta, project=None, session=None):
        h = mock.Mock(spec=server.BrowserHandler)
        h.check_claude_auth.return_value = True
        h.read_json_body.return_value = body
        self.responses = []
        h.send_json.side_effect = lambda o, s=200: self.responses.append((o, s))
        sess = session or mock.Mock()
        sess.read_meta.return_value = meta
        sess.set_project.return_value = {'id': 't1',
                                         'project_id': body.get('project_id')}
        h._hv_session_or_404.return_value = sess
        with mock.patch.object(server.ProjectsManager, 'get_project',
                               return_value=project):
            server.BrowserHandler.handle_hypervisor_set_project(h, 't1')
        return sess, self.responses[-1]

    def test_files_a_plain_chat(self):
        sess, (body, status) = self._run(
            {'project_id': 'kc'}, meta={'persona': ''}, project={'id': 'kc'})
        sess.set_project.assert_called_once_with('kc')
        self.assertEqual(status, 200)
        self.assertEqual(body['thread']['project_id'], 'kc')

    def test_clears_the_binding(self):
        sess, (_, status) = self._run({'project_id': ''}, meta={'persona': ''})
        sess.set_project.assert_called_once_with('')
        self.assertEqual(status, 200)

    def test_unknown_project_is_a_400_not_a_silent_drop(self):
        sess, (body, status) = self._run(
            {'project_id': 'ghost'}, meta={'persona': ''}, project=None)
        sess.set_project.assert_not_called()
        self.assertEqual(status, 400)
        self.assertIn('unknown project', body['error'])

    def test_malformed_project_id_is_rejected(self):
        sess, (_, status) = self._run({'project_id': '../etc'},
                                      meta={'persona': ''}, project={'id': 'x'})
        sess.set_project.assert_not_called()
        self.assertEqual(status, 400)

    def test_cto_chat_cannot_be_refiled(self):
        # A CTO thread's brief is baked into its preamble at creation, so a
        # re-bind would leave the two disagreeing.
        sess, (body, status) = self._run(
            {'project_id': 'kc'}, meta={'persona': 'cto'}, project={'id': 'kc'})
        sess.set_project.assert_not_called()
        self.assertEqual(status, 400)
        self.assertIn('CTO', body['error'])

    def test_requires_auth(self):
        h = mock.Mock(spec=server.BrowserHandler)
        h.check_claude_auth.return_value = False
        responses = []
        h.send_json.side_effect = lambda o, s=200: responses.append((o, s))
        server.BrowserHandler.handle_hypervisor_set_project(h, 't1')
        self.assertEqual(responses[-1][1], 401)
        h._hv_session_or_404.assert_not_called()


class ChatTabListFilterTest(unittest.TestCase):
    """The Chat tab's own list can be scoped to one project — the persona and
    project filters compose, so filing chats is groupable server-side too."""

    THREADS = [
        {'id': '1', 'persona': '', 'project_id': 'kc'},
        {'id': '2', 'persona': '', 'project_id': ''},
        {'id': '3', 'persona': 'cto', 'project_id': 'kc'},
    ]

    def _run(self, path):
        h = mock.Mock(spec=server.BrowserHandler)
        h.check_claude_auth.return_value = True
        h.path = path
        self.responses = []
        h.send_json.side_effect = lambda o, s=200: self.responses.append((o, s))
        with mock.patch.object(server, '_HYPERVISOR_AVAILABLE', True), \
             mock.patch.object(server.HypervisorSession, 'list',
                               return_value=[dict(t) for t in self.THREADS]):
            server.BrowserHandler.handle_hypervisor_list_threads(h)
        return [t['id'] for t in self.responses[-1][0]['threads']]

    def test_plain_chats_of_one_project(self):
        self.assertEqual(
            self._run('/api/hypervisor/threads?persona=default&project=kc'),
            ['1'])

    def test_plain_chats_of_every_project(self):
        self.assertEqual(
            self._run('/api/hypervisor/threads?persona=default'), ['1', '2'])


# ───────────────────── the binding actually scopes memory ──────────────────

class ProjectChatMemoryScopeTest(unittest.TestCase):
    """End of the chain: filing a chat into a project decides which memories it
    can ever recall, because injection scopes retrieval by namespace (#359).

    Drives the real pieces — the thread's turn env, the hook's scope derivation,
    and the manager's search — so a regression anywhere along it fails here.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._orig_store = MemoryManager._store
        self._orig_init = _store_mod._INITIALIZED
        _store_mod._INITIALIZED = False
        MemoryManager._store = MemoryStore(
            os.path.join(self._tmpdir.name, 'memory.db'))
        self.addCleanup(self._restore_store)
        for ns, key in (('project.alpha', 'a'),
                        ('project.alpha.decisions', 'b'),
                        ('project.beta', 'c'),
                        ('user.preferences', 'd')):
            MemoryManager.upsert(namespace=ns, key=key,
                                 value='deployment pipeline notes')
        self.tmp = tempfile.mkdtemp(prefix='kctest-hv-')
        self._orig_dir = hs.HYPERVISOR_DIR
        hs.HYPERVISOR_DIR = self.tmp
        self.addCleanup(self._restore_dir)
        # The project registry the write side reads its default namespace from.
        self.projdir = tempfile.mkdtemp(prefix='kctest-proj-')
        self._orig_projdir = server.ProjectsManager.PROJECTS_DIR
        server.ProjectsManager.PROJECTS_DIR = self.projdir
        self.addCleanup(self._restore_projdir)
        for pid in ('alpha', 'beta'):
            server.ProjectsManager.create({'id': pid, 'name': pid})

    def _restore_store(self):
        MemoryManager._store = self._orig_store
        _store_mod._INITIALIZED = self._orig_init

    def _restore_dir(self):
        hs.HYPERVISOR_DIR = self._orig_dir

    def _restore_projdir(self):
        server.ProjectsManager.PROJECTS_DIR = self._orig_projdir

    def _turn_env(self, project_id):
        """The env a chat filed into `project_id` runs its turns with."""
        s = hs.HypervisorSession.create(
            assistant='claude', workdir='/home/dev', cli_cmd='claude',
            preamble='p')
        s.set_project(project_id)
        captured = {}

        def fake_popen(argv, **kw):
            captured['env'] = kw.get('env') or {}
            raise RuntimeError('stop before real spawn')

        with mock.patch.dict(os.environ), \
                mock.patch.object(hs.subprocess, 'Popen', side_effect=fake_popen):
            os.environ.pop('KC_PROJECT_ID', None)
            os.environ.pop('KC_MEMORY_NS_SCOPE', None)
            try:
                s._run_turn('hi', first=True, meta=s.read_meta())
            except Exception:
                pass
        return captured['env']

    def _scope(self, env):
        """The scope the injection hook derives inside that turn env."""
        with mock.patch.dict(os.environ, env, clear=True):
            return inject._namespace_scope()

    def _recalled(self, env):
        """Namespaces the injection hook would retrieve from inside that env."""
        rows = MemoryManager.search(q='deployment',
                                    namespace_scope=self._scope(env) or None)
        return sorted(r['namespace'] for r in rows)

    def _injected(self, env, prompt='deployment pipeline'):
        """Namespaces the auto-inject block would actually carry — the same
        retrieval as _recalled but through top_for_prompt, which is the entry
        point create_task and the /api/memory hook path really use."""
        rows = MemoryManager.top_for_prompt(
            prompt, namespace_scope=self._scope(env) or None)
        return [r['namespace'] for r in rows]

    def test_a_project_chat_recalls_its_own_project(self):
        recalled = self._recalled(self._turn_env('alpha'))
        self.assertIn('project.alpha', recalled)
        self.assertIn('project.alpha.decisions', recalled)

    def test_a_project_chat_still_recalls_user_memories(self):
        """#593: scoping was single-rooted, so filing a chat into a project
        silently cost it the user's own name, preferences and working style —
        facts that are true in EVERY project."""
        self.assertIn('user.preferences', self._recalled(self._turn_env('alpha')))

    def test_a_project_chat_never_sees_a_sibling_project(self):
        # The #359 guarantee survives the widening: `user.` came back, the
        # neighbouring project did not.
        self.assertNotIn('project.beta', self._recalled(self._turn_env('alpha')))

    def test_a_sibling_project_chat_never_sees_the_other(self):
        self.assertEqual(self._recalled(self._turn_env('beta')),
                         ['project.beta', 'user.preferences'])

    def test_an_unfiled_chat_still_retrieves_workspace_wide(self):
        # No binding → no scope → exactly the pre-#358 behaviour.
        recalled = self._recalled(self._turn_env(''))
        self.assertEqual(recalled, ['project.alpha', 'project.alpha.decisions',
                                    'project.beta', 'user.preferences'])

    def test_the_injected_block_carries_both_roots(self):
        # search() is one retrieval path; top_for_prompt is the one injection
        # actually calls, and it must widen with it.
        injected = self._injected(self._turn_env('alpha'))
        self.assertIn('user.preferences', injected)
        self.assertIn('project.alpha', injected)
        self.assertNotIn('project.beta', injected)

    def test_a_stopword_only_prompt_leads_with_the_project(self):
        """The no-query-terms fallback ranks by importance alone, so once
        `user.*` is back in scope it could crowd project facts out of the
        injection budget. The chat's own project leads there (#593)."""
        MemoryManager.upsert(namespace='user.preferences', key='editor',
                             value='prefers vim', kind='preference',
                             importance=0.9)
        MemoryManager.upsert(namespace='project.alpha', key='pref',
                             value='deploys on fridays', kind='preference',
                             importance=0.2)
        injected = self._injected(self._turn_env('alpha'), prompt='the and of')
        self.assertEqual(injected[0], 'project.alpha')
        self.assertIn('user.preferences', injected)

    def test_a_project_chats_writes_still_land_in_the_project(self):
        """Read-side widening only (#593): a memory written from a project chat
        still goes to that project's namespace — NOT `user.` — so it stays
        invisible to a sibling project's chat."""
        ns = server.ProjectsManager.get_project('alpha')['memory_namespace']
        self.assertEqual(ns, 'project.alpha')
        MemoryManager.upsert(namespace=ns, key='new',
                             value='deployment runbook lives in ops/')
        row = MemoryManager.get(namespace=ns, key='new')
        self.assertEqual(row['namespace'], 'project.alpha')
        self.assertIn('project.alpha', self._recalled(self._turn_env('alpha')))
        self.assertNotIn('project.alpha',
                         self._recalled(self._turn_env('beta')))


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
