"""Tests for the Mission Control queue endpoint (issue #425).

Covers the normalization of builds / sub-agents / hypervisor chats into the
unified card queue: state mapping, waiting-prompt surfacing, lineage
resolution, recency-window pruning, pulse counts, and the HTTP handler's
auth + shape contract.
"""
import json
import os
import sys
import tempfile
import shutil
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402


def _fake_tmux(pane_text=''):
    """subprocess.run stub: sessions alive, capture-pane returns pane_text."""
    def run(*args, **kwargs):
        argv = args[0] if args else kwargs.get('args', [])
        if len(argv) >= 2 and argv[0] == 'tmux' and argv[1] == 'capture-pane':
            return mock.Mock(returncode=0, stdout=pane_text, stderr='')
        return mock.Mock(returncode=0, stdout='', stderr='')
    return run


PERMISSION_PANE = """\
Do you want to proceed?
❯ 1. Yes
  2. Yes, and don't ask again
  3. No, and tell Claude what to do differently
"""


class MissionControlQueueTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='kctest-mc-')
        self._orig_tasks_dir = server.ClaudeTaskManager.TASKS_DIR
        server.ClaudeTaskManager.TASKS_DIR = os.path.join(self.tmpdir, 'tasks')
        os.makedirs(server.ClaudeTaskManager.TASKS_DIR)
        # Hypervisor threads live in their own dir; patch the module global the
        # card builder reads. Session listing itself is faked per-test.
        self._orig_hv_dir = server.HYPERVISOR_DIR
        server.HYPERVISOR_DIR = os.path.join(self.tmpdir, 'hypervisor')
        os.makedirs(server.HYPERVISOR_DIR)
        # No real status reconciliation: trust the task.json as written so
        # tests control state directly (reconcile is covered by server_test).
        recon = mock.patch.object(
            server.ClaudeTaskManager, '_reconcile_status',
            side_effect=lambda meta, task_dir: None)
        recon.start()
        self.addCleanup(recon.stop)

    def tearDown(self):
        server.ClaudeTaskManager.TASKS_DIR = self._orig_tasks_dir
        server.HYPERVISOR_DIR = self._orig_hv_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ── helpers ──────────────────────────────────────────────────────────

    def _write_task(self, task_id, output=None, **meta):
        base = {
            'task_id': task_id,
            'name': f'task {task_id}',
            'prompt': f'prompt for {task_id}',
            'status': 'running',
            'created_at': time.time() - 600,
            'last_activity_at': time.time() - 60,
            'assistant': 'claude',
            'sub_task_ids': [],
        }
        base.update(meta)
        task_dir = os.path.join(server.ClaudeTaskManager.TASKS_DIR, task_id)
        os.makedirs(task_dir, exist_ok=True)
        with open(os.path.join(task_dir, 'task.json'), 'w') as f:
            json.dump(base, f)
        if output is not None:
            with open(os.path.join(task_dir, 'output.log'), 'w') as f:
                f.write(output)
        return base

    def _queue(self, threads=()):
        """Run the assembler with hypervisor listing faked to `threads`."""
        with mock.patch.object(server, '_HYPERVISOR_AVAILABLE', True), \
             mock.patch.object(server, 'HypervisorSession') as hv, \
             mock.patch.object(server.subprocess, 'run', _fake_tmux()):
            hv.list.return_value = list(threads)
            return server.missioncontrol_queue()

    @staticmethod
    def _thread(thread_id, **kw):
        base = {
            'id': thread_id, 'title': f'chat {thread_id}',
            'assistant': 'claude', 'model': '', 'status': 'idle',
            'created_at': time.time() - 3600,
            'updated_at': time.time() - 120, 'deleted_at': None,
        }
        base.update(kw)
        return base

    def _card(self, result, card_id):
        matches = [c for c in result['cards'] if c['id'] == card_id]
        self.assertEqual(
            len(matches), 1,
            f'{card_id} not found in {[c["id"] for c in result["cards"]]}')
        return matches[0]

    # ── state mapping ────────────────────────────────────────────────────

    def test_running_build_maps_to_running_card(self):
        self._write_task('t_run', status='running',
                         output='step one\nEditing server.py — adding route\n')
        result = self._queue()
        card = self._card(result, 'build:t_run')
        self.assertEqual(card['state'], 'running')
        self.assertEqual(card['kind'], 'build')
        self.assertEqual(card['headline'],
                         'Editing server.py — adding route')

    def test_waiting_task_surfaces_quick_reply_prompt(self):
        self._write_task('t_wait', status='waiting-for-input')
        with mock.patch.object(server, '_HYPERVISOR_AVAILABLE', False), \
             mock.patch.object(server.subprocess, 'run',
                               _fake_tmux(PERMISSION_PANE)):
            result = server.missioncontrol_queue()
        card = self._card(result, 'build:t_wait')
        self.assertEqual(card['state'], 'waiting')
        self.assertIsNotNone(card['waiting_prompt'])
        self.assertEqual(card['waiting_prompt']['kind'], 'choice')
        self.assertEqual(len(card['waiting_prompt']['options']), 3)
        self.assertIsNotNone(card['waiting_since'])

    def test_completed_build_goes_to_review_with_ok_outcome(self):
        self._write_task('t_ok', status='completed',
                         finished_at=time.time() - 300)
        card = self._card(self._queue(), 'build:t_ok')
        self.assertEqual(card['state'], 'review')
        self.assertEqual(card['outcome'], {'ok': True, 'detail': 'completed'})

    def test_error_and_killed_go_to_done_with_bad_outcome(self):
        self._write_task('t_err', status='error', exit_code=1,
                         finished_at=time.time() - 300)
        self._write_task('t_kill', status='killed',
                         killed_at=time.time() - 300)
        result = self._queue()
        err = self._card(result, 'build:t_err')
        self.assertEqual(err['state'], 'done')
        self.assertEqual(err['outcome'], {'ok': False, 'detail': 'error · exit 1'})
        kill = self._card(result, 'build:t_kill')
        self.assertEqual(kill['state'], 'done')
        self.assertFalse(kill['outcome']['ok'])

    def test_old_terminal_tasks_fall_off_the_board(self):
        self._write_task('t_old', status='completed',
                         finished_at=time.time() - server.MC_RECENT_SECONDS - 60)
        result = self._queue()
        self.assertEqual(
            [c for c in result['cards'] if c['ref_id'] == 't_old'], [])

    # ── sub-agents & lineage ─────────────────────────────────────────────

    def test_subagent_kind_and_lineage_links(self):
        self._write_task('t_parent', status='running',
                         sub_task_ids=['t_child'])
        self._write_task('t_child', status='running',
                         parent_task_id='t_parent')
        result = self._queue()
        parent = self._card(result, 'build:t_parent')
        child = self._card(result, 'subagent:t_child')
        self.assertEqual(child['kind'], 'subagent')
        self.assertEqual(child['parent_id'], 'build:t_parent')
        self.assertEqual(parent['children'],
                         [{'id': 'subagent:t_child',
                           'title': 'task t_child', 'state': 'running'}])
        # Internal plumbing must not leak into the payload.
        self.assertNotIn('_sub_task_ids', parent)

    # ── hypervisor chats ─────────────────────────────────────────────────

    def test_running_chat_and_parked_chat(self):
        threads = [
            self._thread('h_live', status='running'),
            self._thread('h_idle', status='idle'),
        ]
        result = self._queue(threads=threads)
        live = self._card(result, 'chat:h_live')
        self.assertEqual(live['state'], 'running')
        idle = self._card(result, 'chat:h_idle')
        self.assertEqual(idle['state'], 'done')
        self.assertTrue(idle['outcome']['ok'])

    def test_deleted_and_stale_chats_are_excluded(self):
        threads = [
            self._thread('h_del', deleted_at=time.time()),
            self._thread('h_stale',
                         updated_at=time.time() - server.MC_RECENT_SECONDS - 60),
        ]
        result = self._queue(threads=threads)
        self.assertEqual(
            [c for c in result['cards'] if c['kind'] == 'chat'], [])

    def test_chat_headline_from_events_jsonl(self):
        thread = self._thread('h_ev', status='running')
        tdir = os.path.join(server.HYPERVISOR_DIR, 'h_ev')
        os.makedirs(tdir)
        with open(os.path.join(tdir, 'events.jsonl'), 'w') as f:
            f.write(json.dumps({'seq': 1, 'role': 'user', 'type': 'message',
                                'text': 'do the thing'}) + '\n')
            f.write(json.dumps({'seq': 2, 'role': 'assistant',
                                'type': 'message',
                                'text': 'Refactoring the gateway adapter now'})
                    + '\n')
        card = self._card(self._queue(threads=[thread]), 'chat:h_ev')
        self.assertEqual(card['headline'],
                         'Refactoring the gateway adapter now')

    # ── ordering & pulse ─────────────────────────────────────────────────

    def test_cards_sorted_waiting_first_and_pulse_counts(self):
        self._write_task('t_a', status='running')
        self._write_task('t_b', status='waiting-for-input',
                         last_activity_at=time.time() - 500)
        self._write_task('t_c', status='completed',
                         finished_at=time.time() - 100)
        result = self._queue(threads=[self._thread('h_x', status='running')])
        states = [c['state'] for c in result['cards']]
        self.assertEqual(states, sorted(
            states, key=lambda s: {'waiting': 0, 'running': 1,
                                   'review': 2, 'done': 3}[s]))
        self.assertEqual(result['cards'][0]['id'], 'build:t_b')
        pulse = result['pulse']
        self.assertEqual(pulse['running'], 2)   # build + chat
        self.assertEqual(pulse['waiting'], 1)
        self.assertEqual(pulse['review'], 1)
        self.assertEqual(pulse['done_today'], 1)
        self.assertGreaterEqual(pulse['oldest_wait_s'], 499)

    def test_headline_falls_back_to_prompt_and_strips_ansi(self):
        self._write_task('t_ansi', status='running',
                         output='\x1b[32mAll tests green\x1b[0m\n│ ── │\n')
        card = self._card(self._queue(), 'build:t_ansi')
        self.assertEqual(card['headline'], 'All tests green')
        self._write_task('t_bare', status='running')  # no output.log at all
        card = self._card(self._queue(), 'build:t_bare')
        self.assertEqual(card['headline'], 'prompt for t_bare')

    # ── branch detection ─────────────────────────────────────────────────

    def test_git_branch_read_from_head_file(self):
        repo = os.path.join(self.tmpdir, 'repo')
        os.makedirs(os.path.join(repo, '.git'))
        with open(os.path.join(repo, '.git', 'HEAD'), 'w') as f:
            f.write('ref: refs/heads/kc/my-feature\n')
        self.assertEqual(server._mc_git_branch(repo), 'my-feature')
        self.assertEqual(server._mc_git_branch(''), '')
        self.assertEqual(server._mc_git_branch('/nonexistent'), '')

    def test_git_branch_follows_worktree_gitdir_pointer(self):
        gitdir = os.path.join(self.tmpdir, 'main', '.git', 'worktrees', 'wt')
        os.makedirs(gitdir)
        with open(os.path.join(gitdir, 'HEAD'), 'w') as f:
            f.write('ref: refs/heads/kc/wt-branch\n')
        wt = os.path.join(self.tmpdir, 'wt')
        os.makedirs(wt)
        with open(os.path.join(wt, '.git'), 'w') as f:
            f.write(f'gitdir: {gitdir}\n')
        self.assertEqual(server._mc_git_branch(wt), 'wt-branch')

    # ── HTTP handler contract ────────────────────────────────────────────

    def _handler(self, authed=True):
        h = mock.Mock(spec=server.BrowserHandler)
        h.check_claude_auth.return_value = authed
        h.path = '/api/missioncontrol/queue'
        self.responses = []
        h.send_json.side_effect = \
            lambda obj, status=200: self.responses.append((obj, status))
        return h

    def test_handler_requires_auth(self):
        h = self._handler(authed=False)
        server.BrowserHandler.handle_missioncontrol_queue(h)
        self.assertEqual(self.responses, [({'error': 'Unauthorized'}, 401)])

    def test_handler_returns_cards_and_pulse(self):
        self._write_task('t_run', status='running')
        h = self._handler()
        with mock.patch.object(server, '_HYPERVISOR_AVAILABLE', False), \
             mock.patch.object(server.subprocess, 'run', _fake_tmux()):
            server.BrowserHandler.handle_missioncontrol_queue(h)
        (body, status), = self.responses
        self.assertEqual(status, 200)
        self.assertIn('cards', body)
        self.assertIn('pulse', body)
        self.assertEqual(body['cards'][0]['id'], 'build:t_run')


if __name__ == '__main__':
    unittest.main()
