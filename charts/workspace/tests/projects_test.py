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
import time
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
        server.ProjectsManager._WORKTREE_ROOTS.clear()

    def tearDown(self):
        server.ProjectsManager.PROJECTS_DIR = self._orig_projdir
        server.ClaudeTaskManager.TASKS_DIR = self._orig_taskdir
        server.ProjectsManager._WORKTREE_ROOTS.clear()
        shutil.rmtree(self.projdir, ignore_errors=True)
        shutil.rmtree(self.taskdir, ignore_errors=True)

    def _write_task(self, task_id, workdir, status='completed',
                    last_activity_at=100.0, prompt='do the thing',
                    project_id=None):
        d = os.path.join(self.taskdir, task_id)
        os.makedirs(d, exist_ok=True)
        meta = {
            'task_id': task_id, 'workdir': workdir, 'status': status,
            'created_at': 50.0, 'last_activity_at': last_activity_at,
            'prompt': prompt, 'assistant': 'claude',
        }
        if project_id is not None:
            meta['project_id'] = project_id
        with open(os.path.join(d, 'task.json'), 'w') as f:
            json.dump(meta, f)


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

    # ── per-project assistant configuration (#483, #362) ──────────────────

    def test_assistant_defaults_start_empty(self):
        # Empty = "inherit the workspace default", the state every existing
        # project is already in.
        cfg, _ = PM.create({'id': 'p', 'workdirs': []})
        self.assertEqual(cfg['default_assistant'], '')
        self.assertEqual(cfg['default_model'], '')
        self.assertEqual(cfg['default_effort'], '')
        self.assertEqual(PM.defaults_for('p'), ('', '', ''))

    def test_assistant_defaults_round_trip(self):
        PM.create({'id': 'p', 'workdirs': [],
                   'default_assistant': 'codex', 'default_model': 'opus',
                   'default_effort': 'xhigh'})
        stored = PM.get_project('p')
        self.assertEqual(stored['default_assistant'], 'codex')
        self.assertEqual(stored['default_model'], 'opus')
        self.assertEqual(stored['default_effort'], 'xhigh')
        self.assertEqual(PM.defaults_for('p'), ('codex', 'opus', 'xhigh'))

    def test_assistant_defaults_are_mutable_via_update(self):
        # This is the path both PUT /api/projects/{id} and the update_project
        # MCP tool take, so the CTO can configure itself.
        PM.create({'id': 'p', 'workdirs': []})
        cfg, err = PM.update('p', {'default_assistant': 'claude',
                                   'default_model': 'opus'})
        self.assertIsNone(err)
        self.assertEqual(cfg['default_assistant'], 'claude')
        self.assertEqual(PM.defaults_for('p'), ('claude', 'opus', ''))

    def test_assistant_defaults_are_trimmed_and_type_checked(self):
        PM.create({'id': 'p', 'workdirs': []})
        cfg, err = PM.update('p', {'default_model': '  opus  '})
        self.assertIsNone(err)
        self.assertEqual(cfg['default_model'], 'opus')
        _, err = PM.update('p', {'default_assistant': ['claude']})
        self.assertEqual(err, 'default_assistant must be a string')

    def test_defaults_for_tolerates_unknown_and_legacy_records(self):
        self.assertEqual(PM.defaults_for('ghost'), ('', '', ''))
        self.assertEqual(PM.defaults_for(''), ('', '', ''))
        self.assertEqual(PM.defaults_for(None), ('', '', ''))
        # A record written before this landed simply has no such keys.
        PM.create({'id': 'old', 'workdirs': []})
        raw = PM.get_project('old')
        for k in ('default_assistant', 'default_model', 'default_effort'):
            raw.pop(k, None)
        PM._write(raw)
        self.assertEqual(PM.defaults_for('old'), ('', '', ''))

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


# ───────────────── task→project attribution (#533 regression) ─────────────

class AttributionTests(ProjectsManagerBase):
    """The AI CTO brief's "Now" row read 0 running / 0 waiting forever: every
    task is launched by the kc-issue skill into /home/dev/.worktrees/<proj>/…,
    which no project workdir prefixes, and memory-discovered projects carried
    `workdirs: []` so they could never match anything. These cover the three
    attribution paths — worktree→main-repo resolution, a stamped project_id,
    and workdir backfill — against a fake home laid out in a tmpdir."""

    def setUp(self):
        super().setUp()
        self.home = tempfile.mkdtemp(prefix='kctest-home-')
        self._orig_home = server.ProjectsManager.HOME_ROOT
        server.ProjectsManager.HOME_ROOT = self.home
        self.repo = os.path.join(self.home, 'kube-coder')
        os.makedirs(os.path.join(self.repo, '.git'))

    def tearDown(self):
        server.ProjectsManager.HOME_ROOT = self._orig_home
        shutil.rmtree(self.home, ignore_errors=True)
        super().tearDown()

    def _worktree(self, name):
        """Lay out a linked git worktree of self.repo exactly as `git worktree
        add` does: a `.git` FILE pointing at <repo>/.git/worktrees/<name>, whose
        commondir walks back to the main .git."""
        wt = os.path.join(self.home, '.worktrees', 'kube-coder', name)
        os.makedirs(wt)
        gitdir = os.path.join(self.repo, '.git', 'worktrees', name)
        os.makedirs(gitdir)
        with open(os.path.join(gitdir, 'commondir'), 'w') as f:
            f.write('../..\n')
        with open(os.path.join(wt, '.git'), 'w') as f:
            f.write(f'gitdir: {gitdir}\n')
        return wt

    # ── worktree → main repo ─────────────────────────────────────────────

    def test_task_matches_resolves_worktree_to_main_repo(self):
        wt = self._worktree('issue-533')
        self.assertTrue(PM._task_matches(wt, [self.repo]))
        # …and a path under the worktree, not just its root.
        self.assertTrue(PM._task_matches(os.path.join(wt, 'charts'), [self.repo]))
        # A plain directory that merely looks like one still doesn't match.
        plain = os.path.join(self.home, '.worktrees', 'other', 'x')
        os.makedirs(plain)
        self.assertFalse(PM._task_matches(plain, [self.repo]))

    def test_attribution_cache_expires(self):
        """A worktree created after a miss must still attribute once the cached
        answer ages out — the server runs for weeks."""
        wt = os.path.join(self.home, '.worktrees', 'kube-coder', 'issue-9')
        os.makedirs(wt)
        self.assertFalse(PM._task_matches(wt, [self.repo]))
        shutil.rmtree(wt)
        self._worktree('issue-9')
        self.assertFalse(PM._task_matches(wt, [self.repo]))   # still cached
        with mock.patch.object(server.time, 'time',
                               return_value=time.time() + PM._WORKTREE_CACHE_TTL + 1):
            self.assertTrue(PM._task_matches(wt, [self.repo]))

    def test_brief_counts_running_and_waiting_tasks_in_worktrees(self):
        PM.create({'id': 'kube-coder', 'workdirs': [self.repo]})
        self._write_task('t1', self._worktree('issue-1'), status='running',
                         last_activity_at=200.0)
        self._write_task('t2', self._worktree('issue-2'),
                         status='waiting-for-input')
        self._write_task('t3', self._worktree('issue-3'), status='completed')
        self._write_task('t4', os.path.join(self.home, 'elsewhere'),
                         status='running')
        with mock.patch.object(server, 'MemoryManager', _FakeMemory([])):
            brief = PM.brief('kube-coder')
        self.assertEqual(brief['tasks']['running'], 1)
        self.assertEqual(brief['tasks']['waiting'], 1)
        self.assertEqual(brief['tasks']['total'], 3)
        self.assertEqual([t['task_id'] for t in brief['tasks']['recent']][0], 't1')

    def test_pulse_counts_worktree_tasks(self):
        PM.create({'id': 'kube-coder', 'workdirs': [self.repo]})
        self._write_task('t1', self._worktree('issue-1'), status='running')
        self._write_task('t2', self._worktree('issue-2'),
                         status='waiting-for-input')
        pulse = PM.list_projects()[0]['pulse']
        self.assertEqual(pulse['running'], 1)
        self.assertEqual(pulse['waiting'], 1)

    # ── stamped project_id ───────────────────────────────────────────────

    def test_brief_counts_task_stamped_with_project_id(self):
        """A CTO-dispatched build can run anywhere; its stamped project_id is
        what keeps it attributed."""
        PM.create({'id': 'kube-coder', 'workdirs': [self.repo]})
        self._write_task('t1', os.path.join(self.home, 'scratch'),
                         status='running', project_id='kube-coder')
        self._write_task('t2', os.path.join(self.home, 'scratch'),
                         status='running', project_id='other')
        with mock.patch.object(server, 'MemoryManager', _FakeMemory([])):
            brief = PM.brief('kube-coder')
        self.assertEqual(brief['tasks']['running'], 1)
        self.assertEqual(brief['tasks']['total'], 1)

    def test_meta_matches_falls_back_to_path_for_unstamped_tasks(self):
        proj = {'id': 'kube-coder', 'workdirs': [self.repo]}
        self.assertTrue(PM._meta_matches({'workdir': self.repo}, proj))
        self.assertTrue(
            PM._meta_matches({'workdir': self.repo, 'project_id': ''}, proj))
        # A task stamped elsewhere but living in the tree still counts — one
        # workdir can legitimately host more than one project's work.
        self.assertTrue(PM._meta_matches(
            {'workdir': self.repo, 'project_id': 'other'}, proj))
        self.assertFalse(PM._meta_matches(
            {'workdir': os.path.join(self.home, 'nope')}, proj))

    def test_project_for_workdir_prefers_most_specific(self):
        PM.create({'id': 'kube-coder', 'workdirs': [self.repo]})
        PM.create({'id': 'everything', 'workdirs': [self.home]})
        self.assertEqual(
            PM.project_for_workdir(os.path.join(self.repo, 'charts')),
            'kube-coder')
        self.assertEqual(PM.project_for_workdir(self._worktree('issue-7')),
                         'kube-coder')
        self.assertEqual(PM.project_for_workdir(os.path.join(self.home, 'x')),
                         'everything')
        self.assertEqual(PM.project_for_workdir('/tmp/outside'), '')

    def test_create_task_stamps_inferred_project_id(self):
        PM.create({'id': 'kube-coder', 'workdirs': [self.repo]})
        wt = self._worktree('issue-533')
        with mock.patch.object(server.subprocess, 'run',
                               return_value=mock.Mock(returncode=0, stdout='',
                                                      stderr='')):
            task = server.ClaudeTaskManager.create_task('go', workdir=wt)
        self.assertEqual(task['project_id'], 'kube-coder')
        with open(os.path.join(self.taskdir, task['task_id'], 'task.json')) as f:
            self.assertEqual(json.load(f)['project_id'], 'kube-coder')

    def test_create_task_explicit_project_id_wins(self):
        PM.create({'id': 'kube-coder', 'workdirs': [self.repo]})
        with mock.patch.object(server.subprocess, 'run',
                               return_value=mock.Mock(returncode=0, stdout='',
                                                      stderr='')):
            task = server.ClaudeTaskManager.create_task(
                'go', workdir=os.path.join(self.home, 'scratch'),
                project_id='kube-coder')
        self.assertEqual(task['project_id'], 'kube-coder')

    # ── workdir backfill for memory-discovered projects ──────────────────

    def test_implied_workdirs_from_id_and_path_slug(self):
        self.assertEqual(PM._implied_workdirs('kube-coder'), [self.repo])
        # `claude.home-<slug>` namespaces name the cwd they were written from.
        slug = PM._slugify(self.home) + '-kube-coder'
        self.assertEqual(PM._implied_workdirs(slug), [self.repo])
        self.assertEqual(PM._implied_workdirs('ghost'), [])
        # Never the home root itself — it would swallow every task.
        self.assertEqual(PM._implied_workdirs(PM._slugify(self.home)), [])

    def test_discover_backfills_workdirs_for_memory_only_project(self):
        rows = [_mem('claude.kube-coder', 'k', 'v', tags=[])]
        with mock.patch.object(server.WorkspaceManager, 'list_dirs',
                               return_value=[]), \
             mock.patch.object(server, 'MemoryManager', _FakeMemory(rows)):
            PM.discover(auto_provision=True)
        self.assertEqual(PM.get_project('kube-coder')['workdirs'], [self.repo])

    def test_discover_heals_registered_project_with_empty_workdirs(self):
        PM.create({'id': 'kube-coder', 'workdirs': [],
                   'memory_namespace': 'claude.kube-coder'})
        self._write_task('t1', self._worktree('issue-1'), status='running')
        rows = [_mem('claude.kube-coder', 'k', 'v', tags=[])]
        with mock.patch.object(server.WorkspaceManager, 'list_dirs',
                               return_value=[]), \
             mock.patch.object(server, 'MemoryManager', _FakeMemory(rows)):
            result = PM.discover(auto_provision=True)
        self.assertIn('kube-coder', result['backfilled'])
        self.assertEqual(PM.get_project('kube-coder')['workdirs'], [self.repo])
        # …and the worktree task now actually counts.
        with mock.patch.object(server, 'MemoryManager', _FakeMemory([])):
            brief = PM.brief('kube-coder')
        self.assertEqual(brief['tasks']['running'], 1)

    def test_discover_leaves_curated_workdirs_alone(self):
        os.makedirs(os.path.join(self.home, 'curated'))
        PM.create({'id': 'kube-coder',
                   'workdirs': [os.path.join(self.home, 'curated')],
                   'memory_namespace': 'claude.kube-coder'})
        rows = [_mem('claude.kube-coder', 'k', 'v', tags=[])]
        with mock.patch.object(server.WorkspaceManager, 'list_dirs',
                               return_value=[]), \
             mock.patch.object(server, 'MemoryManager', _FakeMemory(rows)):
            result = PM.discover(auto_provision=True)
        self.assertEqual(result['backfilled'], [])
        self.assertEqual(PM.get_project('kube-coder')['workdirs'],
                         [os.path.join(self.home, 'curated')])


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
