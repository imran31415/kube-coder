"""Unit tests for server.py's ProjectsManager (AI CTO part 1, #464).

ProjectsManager is the file-backed project registry + deterministic brief
aggregator. Like WebhookManagerTests, we point its storage dir at a tmpdir and
exercise the pure logic: CRUD round-trip, discovery/auto-provision union rules,
workdir-prefix task matching, namespace memory partitioning into
goals/decisions, brief markdown capping, and pulse counts.

Task and memory stores are swapped for tmp/stub fakes so the tests are
hermetic — no real tmux, no real SQLite.

Run with:    python3 -m unittest tests.projects_test
(from charts/workspace/)
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

PM = server.ProjectsManager


class _FakeMemory:
    """Stand-in for server.MemoryManager.list() returning canned rows."""

    def __init__(self, rows):
        self.rows = rows

    def list(self, *, namespace=None, kind=None, q=None, limit=500,
             include_deleted=False):
        return list(self.rows)


def _mem(namespace, key, value, tags=None, importance=0.5, updated_at=1.0):
    return {
        'namespace': namespace, 'key': key, 'value': value,
        'tags_list': tags or [], 'importance': importance,
        'updated_at': updated_at,
    }


class ProjectsManagerBase(unittest.TestCase):
    def setUp(self):
        self.projdir = tempfile.mkdtemp(prefix='kctest-proj-')
        self.taskdir = tempfile.mkdtemp(prefix='kctest-tasks-')
        self._orig_projdir = server.ProjectsManager.PROJECTS_DIR
        self._orig_taskdir = server.ClaudeTaskManager.TASKS_DIR
        server.ProjectsManager.PROJECTS_DIR = self.projdir
        server.ClaudeTaskManager.TASKS_DIR = self.taskdir

    def tearDown(self):
        server.ProjectsManager.PROJECTS_DIR = self._orig_projdir
        server.ClaudeTaskManager.TASKS_DIR = self._orig_taskdir
        shutil.rmtree(self.projdir, ignore_errors=True)
        shutil.rmtree(self.taskdir, ignore_errors=True)

    def _write_task(self, task_id, workdir, status='completed',
                    last_activity_at=100.0, prompt='do the thing'):
        d = os.path.join(self.taskdir, task_id)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'task.json'), 'w') as f:
            json.dump({
                'task_id': task_id, 'workdir': workdir, 'status': status,
                'created_at': 50.0, 'last_activity_at': last_activity_at,
                'prompt': prompt, 'assistant': 'claude',
            }, f)


# ─────────────────────────── id / slug / validation ───────────────────────

class IdAndSlugTests(unittest.TestCase):
    def test_valid_id(self):
        self.assertTrue(PM.valid_id('kube-coder'))
        self.assertTrue(PM.valid_id('a'))
        self.assertFalse(PM.valid_id(''))
        self.assertFalse(PM.valid_id('-lead'))          # must start alnum
        self.assertFalse(PM.valid_id('Upper'))          # lowercase only
        self.assertFalse(PM.valid_id('has.dot'))        # no dots (traversal)
        self.assertFalse(PM.valid_id('has_underscore'))  # keeps _discover clean
        self.assertFalse(PM.valid_id('a' * 65))

    def test_slugify(self):
        self.assertEqual(PM._slugify('Kube Coder'), 'kube-coder')
        self.assertEqual(PM._slugify('  My__Project!!  '), 'my-project')
        self.assertEqual(PM._slugify('already-good'), 'already-good')

    def test_parse_repo_slug(self):
        self.assertEqual(
            PM._parse_repo_slug('https://github.com/imran31415/kube-coder.git'),
            'imran31415/kube-coder')
        self.assertEqual(
            PM._parse_repo_slug('git@github.com:owner/repo.git'), 'owner/repo')
        self.assertEqual(
            PM._parse_repo_slug('https://gitlab.com/grp/sub/repo'), 'sub/repo')
        self.assertEqual(PM._parse_repo_slug('not-a-url'), '')


# ─────────────────────────────── CRUD round-trip ──────────────────────────

class CrudTests(ProjectsManagerBase):
    def test_create_defaults_and_roundtrip(self):
        cfg, err = PM.create({'name': 'Kube Coder',
                              'workdirs': ['/home/dev/kube-coder']})
        self.assertIsNone(err)
        self.assertEqual(cfg['id'], 'kube-coder')
        self.assertEqual(cfg['memory_namespace'], 'project.kube-coder')
        self.assertEqual(cfg['status'], 'active')
        self.assertEqual(cfg['workdirs'], ['/home/dev/kube-coder'])
        # persisted + readable
        got = PM.get_project('kube-coder')
        self.assertEqual(got['name'], 'Kube Coder')

    def test_create_rejects_duplicate(self):
        PM.create({'id': 'dup', 'workdirs': []})
        cfg, err = PM.create({'id': 'dup', 'workdirs': []})
        self.assertIsNone(cfg)
        self.assertIn('already exists', err)

    def test_create_rejects_bad_id_and_workdir_and_status(self):
        _, err = PM.create({'id': 'Bad Id'})
        self.assertIn('invalid id', err)
        _, err = PM.create({'id': 'ok', 'workdirs': ['/etc/passwd']})
        self.assertIn('/home/dev', err)
        _, err = PM.create({'id': 'ok', 'status': 'bogus'})
        self.assertIn('status', err)

    def test_update_partial_merge(self):
        PM.create({'id': 'p', 'name': 'P', 'workdirs': ['/home/dev/p'],
                   'north_star': 'ship it'})
        cfg, err = PM.update('p', {'status': 'archived'})
        self.assertIsNone(err)
        self.assertEqual(cfg['status'], 'archived')
        # untouched fields preserved
        self.assertEqual(cfg['workdirs'], ['/home/dev/p'])
        self.assertEqual(cfg['north_star'], 'ship it')

    def test_update_missing_returns_not_found(self):
        cfg, err = PM.update('nope', {'status': 'paused'})
        self.assertIsNone(cfg)
        self.assertEqual(err, 'not found')

    def test_update_cannot_change_id(self):
        PM.create({'id': 'keep', 'workdirs': []})
        cfg, _ = PM.update('keep', {'id': 'hijack'})
        self.assertEqual(cfg['id'], 'keep')

    def test_delete(self):
        PM.create({'id': 'gone', 'workdirs': []})
        self.assertTrue(PM.delete('gone'))
        self.assertFalse(PM.delete('gone'))
        self.assertIsNone(PM.get_project('gone'))


# ───────────────────────── task matching + pulse counts ───────────────────

class MatchingAndPulseTests(ProjectsManagerBase):
    def test_task_matches_prefix(self):
        wds = ['/home/dev/kube-coder']
        self.assertTrue(PM._task_matches('/home/dev/kube-coder', wds))
        self.assertTrue(PM._task_matches('/home/dev/kube-coder/charts', wds))
        self.assertFalse(PM._task_matches('/home/dev/other', wds))
        # sibling prefix must NOT false-match
        self.assertFalse(PM._task_matches('/home/dev/kube-coder-x', wds))
        self.assertFalse(PM._task_matches('', wds))

    def test_list_projects_embeds_pulse(self):
        PM.create({'id': 'kube-coder', 'workdirs': ['/home/dev/kube-coder']})
        self._write_task('t1', '/home/dev/kube-coder', status='running',
                         last_activity_at=200.0)
        self._write_task('t2', '/home/dev/kube-coder/charts',
                         status='waiting-for-input', last_activity_at=300.0)
        self._write_task('t3', '/home/dev/elsewhere', status='running')
        projects = PM.list_projects()
        self.assertEqual(len(projects), 1)
        pulse = projects[0]['pulse']
        self.assertEqual(pulse['running'], 1)
        self.assertEqual(pulse['waiting'], 1)
        self.assertEqual(pulse['last_activity_at'], 300.0)

    def test_list_projects_sorted_by_activity(self):
        PM.create({'id': 'a', 'workdirs': ['/home/dev/a']})
        PM.create({'id': 'b', 'workdirs': ['/home/dev/b']})
        self._write_task('ta', '/home/dev/a', last_activity_at=100.0)
        self._write_task('tb', '/home/dev/b', last_activity_at=999.0)
        ids = [p['id'] for p in PM.list_projects()]
        self.assertEqual(ids[0], 'b')  # most recently active first


# ─────────────────────── memory partitioning + brief ──────────────────────

class BriefTests(ProjectsManagerBase):
    def _patch_memory(self, rows):
        return mock.patch.object(server, 'MemoryManager', _FakeMemory(rows))

    def test_project_memories_partition_by_tag(self):
        rows = [
            _mem('project.kc', 'g1', 'reach v2', tags=['goal'], importance=0.9),
            _mem('project.kc.decisions', 'd1', 'SSE over websockets',
                 tags=['decision'], importance=0.8),
            _mem('project.kc', 'note', 'random note', tags=[], importance=0.3),
            _mem('project.kc', 'sekret', 'hush', tags=['secret', 'goal']),
            _mem('project.other', 'x', 'not mine', tags=['goal']),
        ]
        with self._patch_memory(rows):
            goals, decisions, others = PM._project_memories('project.kc')
        self.assertEqual([g['key'] for g in goals], ['g1'])
        self.assertEqual([d['key'] for d in decisions], ['d1'])
        self.assertEqual([o['key'] for o in others], ['note'])
        # secret excluded, other-namespace excluded
        self.assertNotIn('sekret', [g['key'] for g in goals])

    def test_brief_aggregates_tasks_and_memory(self):
        PM.create({'id': 'kc', 'name': 'KC',
                   'workdirs': ['/home/dev/kc'], 'north_star': 'be great'})
        self._write_task('t1', '/home/dev/kc', status='running',
                         last_activity_at=200.0)
        self._write_task('t2', '/home/dev/kc', status='waiting-for-input')
        rows = [
            _mem('project.kc', 'g1', 'goal one', tags=['goal']),
            _mem('project.kc', 'd1', 'decision one', tags=['decision']),
        ]
        with self._patch_memory(rows):
            brief = PM.brief('kc')
        self.assertEqual(brief['tasks']['running'], 1)
        self.assertEqual(brief['tasks']['waiting'], 1)
        self.assertEqual(brief['tasks']['total'], 2)
        self.assertEqual(len(brief['goals']), 1)
        self.assertEqual(len(brief['decisions']), 1)
        self.assertIn('be great', brief['brief_markdown'])
        self.assertIn('goal one', brief['brief_markdown'])
        self.assertIn('decision one', brief['brief_markdown'])

    def test_brief_unknown_project_is_none(self):
        with self._patch_memory([]):
            self.assertIsNone(PM.brief('ghost'))

    def test_brief_markdown_caps_and_notes_more(self):
        PM.create({'id': 'kc', 'workdirs': ['/home/dev/kc']})
        rows = [_mem('project.kc', f'g{i}', f'goal number {i}', tags=['goal'],
                     importance=1.0 - i * 0.01) for i in range(20)]
        with self._patch_memory(rows):
            brief = PM.brief('kc')
        # capped to _BRIEF_GOALS in the structured list
        self.assertEqual(len(brief['goals']), PM._BRIEF_GOALS)
        self.assertEqual(brief['counts']['goals'], 20)
        self.assertIn('more', brief['brief_markdown'])

    def test_brief_markdown_hard_char_cap(self):
        PM.create({'id': 'kc', 'workdirs': ['/home/dev/kc']})
        big = 'x' * 500
        rows = [_mem('project.kc', f'm{i}', big, tags=[]) for i in range(200)]
        with self._patch_memory(rows):
            brief = PM.brief('kc')
        self.assertLessEqual(len(brief['brief_markdown']),
                             PM._BRIEF_MARKDOWN_CAP + 120)


# ──────────────────────── discovery / auto-provision ──────────────────────

class DiscoveryTests(ProjectsManagerBase):
    def test_discover_auto_provisions_confident_candidates(self):
        fake_dirs = [
            {'path': '/home/dev/kube-coder', 'label': 'kube-coder',
             'is_git_repo': True, 'is_project': True, 'mtime': 5},
            {'path': '/home/dev/bare', 'label': 'bare',
             'is_git_repo': False, 'is_project': True, 'mtime': 4},
        ]
        rows = [_mem('project.notes', 'k', 'v', tags=[])]
        with mock.patch.object(server.WorkspaceManager, 'list_dirs',
                               return_value=fake_dirs), \
             mock.patch.object(PM, '_git_remote', return_value='o/kube-coder'), \
             mock.patch.object(server, 'MemoryManager', _FakeMemory(rows)):
            result = PM.discover(auto_provision=True)

        by_id = {c['id']: c for c in result['candidates']}
        # git-remote dir → high confidence → auto-registered
        self.assertEqual(by_id['kube-coder']['confidence'], 'high')
        self.assertTrue(by_id['kube-coder']['registered'])
        # memory namespace → high confidence → auto-registered
        self.assertEqual(by_id['notes']['confidence'], 'high')
        self.assertTrue(by_id['notes']['registered'])
        # bare marker dir (no git remote / task / memory) → low, NOT registered
        self.assertEqual(by_id['bare']['confidence'], 'low')
        self.assertFalse(by_id['bare']['registered'])

        self.assertCountEqual(result['registered'], ['kube-coder', 'notes'])
        self.assertIsNotNone(PM.get_project('kube-coder'))
        self.assertIsNone(PM.get_project('bare'))

    def test_discover_no_autoprovision_registers_nothing(self):
        fake_dirs = [{'path': '/home/dev/x', 'label': 'x', 'is_git_repo': True,
                      'is_project': True, 'mtime': 1}]
        with mock.patch.object(server.WorkspaceManager, 'list_dirs',
                               return_value=fake_dirs), \
             mock.patch.object(PM, '_git_remote', return_value='o/x'), \
             mock.patch.object(server, 'MemoryManager', _FakeMemory([])):
            result = PM.discover(auto_provision=False)
        self.assertEqual(result['registered'], [])
        self.assertIsNone(PM.get_project('x'))

    def test_discover_attributes_worktree_task_to_canonical_root(self):
        self._write_task('t1', '/home/dev/.worktrees/kube-coder/issue-1',
                         status='running')
        with mock.patch.object(server.WorkspaceManager, 'list_dirs',
                               return_value=[]), \
             mock.patch.object(os.path, 'isdir', return_value=True), \
             mock.patch.object(server, 'MemoryManager', _FakeMemory([])):
            result = PM.discover(auto_provision=False)
        by_id = {c['id']: c for c in result['candidates']}
        self.assertIn('kube-coder', by_id)
        self.assertTrue(by_id['kube-coder']['has_task'])
        self.assertIn('/home/dev/kube-coder', by_id['kube-coder']['workdirs'])

    def test_workdir_project_id(self):
        self.assertEqual(PM._workdir_project_id('/home/dev/kube-coder'),
                         'kube-coder')
        self.assertEqual(
            PM._workdir_project_id('/home/dev/.worktrees/kube-coder/issue-9'),
            'kube-coder')
        self.assertEqual(PM._workdir_project_id('/home/dev/.claude-tasks/x'), '')
        self.assertEqual(PM._workdir_project_id('/tmp/elsewhere'), '')


# ─────────────────── HTTP handler methods (auth / events) ─────────────────

class HandlerTests(ProjectsManagerBase):
    """Exercise the BrowserHandler.handle_project_* methods directly with a
    mock.Mock(spec=...) (same style as hypervisor_routes_test): real
    ProjectsManager underneath, captured send_json + read_json_body."""

    def _handler(self, authed=True, body=None, project_id=None):
        h = mock.Mock(spec=server.BrowserHandler)
        h.check_claude_auth.return_value = authed
        h.read_json_body.return_value = body if body is not None else {}
        if project_id is not None:
            h._project_id = project_id
        self.responses = []
        h.send_json.side_effect = \
            lambda obj, status=200: self.responses.append((obj, status))
        return h

    def _last(self):
        self.assertTrue(self.responses, 'handler sent no response')
        return self.responses[-1]

    def test_create_publishes_event_and_201(self):
        h = self._handler(body={'id': 'kc', 'workdirs': ['/home/dev/kc']})
        with mock.patch.object(server.EventBroker, 'publish') as pub:
            server.BrowserHandler.handle_project_create(h)
        obj, status = self._last()
        self.assertEqual(status, 201)
        self.assertEqual(obj['id'], 'kc')
        pub.assert_called_once()
        self.assertEqual(pub.call_args[0][0], 'projects.changed')
        self.assertEqual(pub.call_args[0][1], {'op': 'create', 'id': 'kc'})

    def test_create_unauthorized_401(self):
        h = self._handler(authed=False, body={'id': 'kc'})
        server.BrowserHandler.handle_project_create(h)
        self.assertEqual(self._last()[1], 401)
        self.assertIsNone(PM.get_project('kc'))

    def test_get_404_for_missing(self):
        h = self._handler(project_id='ghost')
        server.BrowserHandler.handle_project_get(h)
        self.assertEqual(self._last()[1], 404)

    def test_update_404_then_ok(self):
        h = self._handler(project_id='missing', body={'status': 'paused'})
        server.BrowserHandler.handle_project_update(h)
        self.assertEqual(self._last()[1], 404)

        PM.create({'id': 'kc', 'workdirs': ['/home/dev/kc']})
        h = self._handler(project_id='kc', body={'status': 'archived'})
        with mock.patch.object(server.EventBroker, 'publish'):
            server.BrowserHandler.handle_project_update(h)
        obj, status = self._last()
        self.assertEqual(status, 200)
        self.assertEqual(obj['status'], 'archived')

    def test_delete_404_then_ok(self):
        h = self._handler(project_id='none')
        server.BrowserHandler.handle_project_delete(h)
        self.assertEqual(self._last()[1], 404)

        PM.create({'id': 'kc', 'workdirs': []})
        h = self._handler(project_id='kc')
        with mock.patch.object(server.EventBroker, 'publish') as pub:
            server.BrowserHandler.handle_project_delete(h)
        self.assertEqual(self._last(), ({'ok': True}, 200))
        pub.assert_called_once()

    def test_discover_defaults_to_auto_provision(self):
        h = self._handler(body={})  # empty body → auto_provision defaults True
        with mock.patch.object(PM, 'discover',
                               return_value={'candidates': [], 'registered': []}) as disc, \
             mock.patch.object(server.EventBroker, 'publish'):
            server.BrowserHandler.handle_project_discover(h)
        disc.assert_called_once_with(auto_provision=True)


class ReadonlyChokepointTest(unittest.TestCase):
    """Project mutations live in do_POST/do_PUT/do_DELETE, each of which calls
    _readonly_block() first; verify that chokepoint blocks when READONLY_MODE."""

    def test_readonly_block_active(self):
        orig = server.READONLY_MODE
        try:
            server.READONLY_MODE = True
            h = mock.Mock(spec=server.BrowserHandler)
            h.send_json.side_effect = lambda obj, status=200: None
            self.assertTrue(server.BrowserHandler._readonly_block(h))
        finally:
            server.READONLY_MODE = orig


if __name__ == '__main__':
    unittest.main()
