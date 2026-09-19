"""Isolated sub-agents (#701) — `spawn_agent(isolate=True)` in the agent
orchestrator MCP, against real git with tmux stubbed.

The orchestrator writes its own task.json and runs tmux itself (it never goes
through the dashboard server), so it needs the same guarantees on its own:
the worktree is made before anything else, taken back if the launch fails,
counted against the same KC_MAX_WORKTREES, and recorded in the same shape.

Run:  python3 -m unittest tests.orchestrator_worktree_test   (from charts/workspace/)
"""

import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from tests import git_fixtures as gf  # noqa: E402  (installs the fcntl shim)
from tests.git_fixtures import commit, git, make_repo  # noqa: E402
import mcp_agent_orchestrator as orch  # noqa: E402
import worktrees  # noqa: E402


class _Tmux:
    def __init__(self):
        self.calls = []
        self.fail = False

    def __call__(self, argv, *a, **kw):
        self.calls.append(list(argv))
        if self.fail and argv[:2] == ['tmux', 'new-session']:
            return mock.Mock(returncode=1, stdout='', stderr='no server running')
        return mock.Mock(returncode=0, stdout='', stderr='')

    def launch(self):
        return next(c for c in self.calls if c[:2] == ['tmux', 'new-session'])


def env_of(argv):
    out = {}
    for i, a in enumerate(argv):
        if a == '-e':
            k, _, v = argv[i + 1].partition('=')
            out[k] = v
    return out


@gf.requires_git
class IsolatedSpawnTests(gf.GitTestCase):

    def setUp(self):
        super().setUp()
        self.tasks = os.path.join(self.tmp, 'tasks')
        os.makedirs(self.tasks)
        self.tmux = _Tmux()
        for p in (mock.patch.object(orch, 'TASKS_DIR', self.tasks),
                  mock.patch.object(orch.subprocess, 'run', side_effect=self.tmux),
                  mock.patch.object(orch, '_count_live_agent_sessions', return_value=0),
                  mock.patch.object(orch.threading, 'Thread'),
                  mock.patch.object(worktrees, 'listen_ports', lambda: set()),
                  mock.patch.object(worktrees, 'default_is_owner_live', lambda t: False)):
            p.start()
            self.addCleanup(p.stop)
        self.repo = make_repo(self.home)

    def spawn(self, **args):
        args.setdefault('prompt', 'write the parser')
        args.setdefault('assistant', 'ante')
        args.setdefault('workdir', self.repo)
        res = orch._tool_spawn_agent(args)
        return res, (None if res.get('isError') else json.loads(res['content'][0]['text']))

    def meta(self, task_id):
        with open(os.path.join(self.tasks, task_id, 'task.json')) as f:
            return json.load(f)

    def test_the_sub_agent_gets_its_own_worktree(self):
        res, out = self.spawn(isolate=True)
        self.assertFalse(res.get('isError'), res)
        wt = out['worktree']
        self.assertEqual(wt['branch'], f'kc/sub-{out["task_id"]}')
        self.assertIn('git merge', out['merge_hint'])
        self.assertTrue(os.path.isdir(wt['path']))
        meta = self.meta(out['task_id'])
        self.assertEqual(meta['workdir'], wt['path'])
        self.assertEqual(meta['worktree']['source_workdir'], self.repo)
        self.assertEqual(meta['worktree']['branch'], wt['branch'])
        launch = self.tmux.launch()
        self.assertIn(f"cd {wt['path']}", launch[-1])
        env = env_of(launch)
        self.assertEqual((env['KC_WT'], env['KC_WT_BRANCH'], env['PORT']),
                         (wt['path'], wt['branch'], str(wt['port'])))
        # Headless: the prompt rides the command line, note first.
        self.assertIn('ISOLATED git worktree', launch[-1])
        self.assertEqual(git(self.repo, 'status', '--porcelain'), '')

    def test_it_branches_from_the_parents_current_commit(self):
        parent = os.path.join(self.home, 'parent-wt')
        git(self.repo, 'worktree', 'add', '-q', '-b', 'kc/parent', parent)
        tip = commit(parent, 'parent.txt', 'parent work\n')
        _res, out = self.spawn(isolate=True, workdir=parent)
        self.assertEqual(git(out['worktree']['path'], 'rev-parse', 'HEAD'), tip)
        # Keyed on the MAIN repository, not the parent's worktree.
        self.assertEqual(self.meta(out['task_id'])['worktree']['repo_root'], self.repo)

    def test_two_sub_agents_never_share(self):
        _r1, a = self.spawn(isolate=True)
        _r2, b = self.spawn(isolate=True)
        self.assertNotEqual(a['worktree']['path'], b['worktree']['path'])
        self.assertNotEqual(a['worktree']['port'], b['worktree']['port'])

    def test_refusals_create_nothing(self):
        plain = os.path.join(self.home, 'notes')
        os.makedirs(plain)
        for args, needle in (({'workdir': plain}, 'not inside a git repository'),
                             ({'base_ref': 'nope'}, 'does not name a commit')):
            res, _out = self.spawn(isolate=True, **args)
            self.assertTrue(res.get('isError'))
            self.assertIn(needle, res['content'][0]['text'])
        self.assertEqual(os.listdir(self.tasks), [])
        self.assertEqual([c for c in self.tmux.calls if c[:2] == ['tmux', 'new-session']], [])

    def test_the_worktree_cap_is_shared_with_the_dashboard(self):
        with mock.patch.dict(os.environ, {'KC_MAX_WORKTREES': '1'}):
            first, _o = self.spawn(isolate=True)
            second, _o = self.spawn(isolate=True)
        self.assertFalse(first.get('isError'))
        self.assertTrue(second.get('isError'))
        self.assertIn('worktree limit reached', second['content'][0]['text'])

    def test_a_failed_launch_takes_the_worktree_back(self):
        self.tmux.fail = True
        res, _out = self.spawn(isolate=True)
        self.assertTrue(res.get('isError'))
        (task_id,) = os.listdir(self.tasks)
        wt = self.meta(task_id)['worktree']
        self.assertTrue(wt['rollback'])
        self.assertFalse(os.path.exists(wt['path']))
        self.assertEqual(git(self.repo, 'branch', '--list', 'kc/*'), '')

    def test_without_isolate_nothing_changes(self):
        res, out = self.spawn()
        self.assertFalse(res.get('isError'))
        self.assertNotIn('worktree', out)
        self.assertNotIn('worktree', self.meta(out['task_id']))
        self.assertFalse(set(env_of(self.tmux.launch())) & {'KC_WT', 'PORT'})
        self.assertFalse(os.path.exists(self.wt_root))

    def test_the_schema_offers_isolation(self):
        props = orch.TOOLS['spawn_agent']['schema']['inputSchema']['properties']
        self.assertIn('isolate', props)
        self.assertIn('base_ref', props)


if __name__ == '__main__':
    unittest.main()
