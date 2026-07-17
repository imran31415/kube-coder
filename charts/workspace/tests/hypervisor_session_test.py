"""Unit tests for charts/workspace/hypervisor_session.py.

Covers the canonical-event normalization that the whole Hypervisor redesign
rests on — the Claude stream-json adapter and the fallback adapter — plus the
session's event append/read and turn bookkeeping. No live CLI is spawned.

Run with:    python3 -m unittest tests.hypervisor_session_test
(from charts/workspace/)
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import hypervisor_session as hs  # noqa: E402


class ClaudeAdapterParseTest(unittest.TestCase):
    def setUp(self):
        self.a = hs.ClaudeAdapter()
        self.ctx = {'workdir': '/home/dev', 'preamble': 'PRE'}

    def test_init_captures_session_id_and_emits_nothing(self):
        line = json.dumps({'type': 'system', 'subtype': 'init',
                           'session_id': 'sess-123'})
        out = self.a.parse(self.ctx, line)
        self.assertEqual(out, [])
        self.assertEqual(self.ctx['claude_session_id'], 'sess-123')

    def test_assistant_text_becomes_message(self):
        line = json.dumps({'type': 'assistant', 'message': {'content': [
            {'type': 'text', 'text': 'Everything is healthy.'}]}})
        out = self.a.parse(self.ctx, line)
        self.assertEqual(out, [{'role': 'assistant', 'type': 'message',
                                'text': 'Everything is healthy.'}])

    def test_tool_use_becomes_tool_call(self):
        line = json.dumps({'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 't1', 'name': 'Bash',
             'input': {'command': 'ps aux'}}]}})
        out = self.a.parse(self.ctx, line)
        self.assertEqual(out[0]['type'], 'tool_call')
        self.assertEqual(out[0]['tool']['name'], 'Bash')
        self.assertEqual(out[0]['tool_id'], 't1')

    def test_tool_result_becomes_tool_result(self):
        line = json.dumps({'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 't1',
             'content': [{'type': 'text', 'text': 'ok'}], 'is_error': False}]}})
        out = self.a.parse(self.ctx, line)
        self.assertEqual(out[0]['type'], 'tool_result')
        self.assertEqual(out[0]['tool_use_id'], 't1')
        self.assertIn('ok', out[0]['text'])

    def test_result_error_subtype_emits_error(self):
        line = json.dumps({'type': 'result', 'subtype': 'error_max_turns',
                           'session_id': 'sess-9'})
        out = self.a.parse(self.ctx, line)
        self.assertEqual(out[0]['type'], 'error')
        self.assertEqual(self.ctx['claude_session_id'], 'sess-9')

    def test_garbage_line_is_ignored(self):
        self.assertEqual(self.a.parse(self.ctx, 'not json at all'), [])

    def test_build_resumes_when_session_id_present(self):
        self.ctx['claude_session_id'] = 'sess-abc'
        spec = self.a.build(self.ctx, 'hello', first=False)
        self.assertIn('--resume', spec['argv'])
        self.assertIn('sess-abc', spec['argv'])
        # No preamble injection once resuming.
        self.assertNotIn('--append-system-prompt', spec['argv'])

    def test_build_first_turn_appends_preamble(self):
        spec = self.a.build(self.ctx, 'hello', first=True)
        self.assertIn('--append-system-prompt', spec['argv'])
        self.assertIn('--permission-mode', spec['argv'])
        self.assertIn('bypassPermissions', spec['argv'])

    def test_build_uses_minimal_strict_mcp_config(self):
        spec = self.a.build(self.ctx, 'hello', first=True)
        self.assertIn('--mcp-config', spec['argv'])
        self.assertIn('--strict-mcp-config', spec['argv'])
        cfg = json.loads(spec['argv'][spec['argv'].index('--mcp-config') + 1])
        self.assertEqual(set(cfg['mcpServers']), {'dashboard', 'memory'})

    def test_build_forces_home_and_drops_api_key(self):
        spec = self.a.build(self.ctx, 'hello', first=True)
        self.assertEqual(spec['env']['HOME'], hs.WORKSPACE_HOME)
        self.assertNotIn('ANTHROPIC_API_KEY', spec['env'])

    def test_build_passes_selected_model(self):
        self.ctx['model'] = 'opus'
        spec = self.a.build(self.ctx, 'hello', first=True)
        self.assertIn('--model', spec['argv'])
        self.assertEqual(spec['argv'][spec['argv'].index('--model') + 1], 'opus')

    def test_build_omits_model_flag_for_default_and_empty(self):
        for m in ('default', '', '   '):
            self.ctx['model'] = m
            spec = self.a.build(self.ctx, 'hello', first=True)
            self.assertNotIn('--model', spec['argv'], m)

    def test_build_model_carries_across_resume(self):
        self.ctx['claude_session_id'] = 'sess-abc'
        self.ctx['model'] = 'sonnet'
        spec = self.a.build(self.ctx, 'hello', first=False)
        self.assertIn('--resume', spec['argv'])
        self.assertIn('--model', spec['argv'])
        self.assertIn('sonnet', spec['argv'])


class FallbackAdapterTest(unittest.TestCase):
    def test_strips_ansi_and_emits_message(self):
        a = hs.FallbackAdapter()
        out = a.finalize_buffered({}, 0, '\x1b[32mhello\x1b[0m world')
        self.assertEqual(out, [{'role': 'assistant', 'type': 'message',
                                'text': 'hello world'}])

    def test_nonzero_exit_no_output_is_error(self):
        a = hs.FallbackAdapter()
        out = a.finalize_buffered({'assistant': 'ante'}, 1, '')
        self.assertEqual(out[0]['type'], 'error')


class ProviderKeyOverlayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = hs._PROVIDER_KEYS_FILE
        hs._PROVIDER_KEYS_FILE = os.path.join(self.tmp, 'provider-keys.json')

    def tearDown(self):
        hs._PROVIDER_KEYS_FILE = self._orig

    def test_missing_file_is_empty_overlay(self):
        self.assertEqual(hs._provider_key_overlay(), {})

    def test_only_allowed_nonempty_keys_surface(self):
        with open(hs._PROVIDER_KEYS_FILE, 'w') as f:
            json.dump({'OPENROUTER_API_KEY': 'k1', 'DEEPSEEK_API_KEY': '  ',
                       'EVIL': 'nope'}, f)
        self.assertEqual(hs._provider_key_overlay(), {'OPENROUTER_API_KEY': 'k1'})

    def test_garbage_file_is_empty_overlay(self):
        with open(hs._PROVIDER_KEYS_FILE, 'w') as f:
            f.write('not json')
        self.assertEqual(hs._provider_key_overlay(), {})


class SessionEventsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = hs.HYPERVISOR_DIR
        hs.HYPERVISOR_DIR = self.tmp

    def tearDown(self):
        hs.HYPERVISOR_DIR = self._orig

    def test_create_append_read_roundtrip(self):
        s = hs.HypervisorSession.create(
            assistant='claude', workdir='/home/dev', cli_cmd='claude',
            preamble='PRE', title='hi there')
        self.assertEqual(s.read_meta()['adapter_kind'], 'claude')
        s._append([{'role': 'user', 'type': 'message', 'text': 'one'}])
        s._append([{'role': 'assistant', 'type': 'message', 'text': 'two'}])
        evs = s.read_events()
        self.assertEqual([e['seq'] for e in evs], [1, 2])
        self.assertEqual(evs[1]['text'], 'two')
        # since-cursor returns only newer events.
        self.assertEqual(len(s.read_events(since_seq=1)), 1)

    def test_assistant_adapter_routing(self):
        cases = {'claude': 'claude', 'ante': 'ante', 'codex': 'codex',
                 'opencode-openrouter': 'opencode', 'opencode-deepseek': 'opencode',
                 'librefang': 'fallback', 'kc-harness': 'fallback'}
        for assistant, kind in cases.items():
            s = hs.HypervisorSession.create(
                assistant=assistant, workdir='/home/dev', cli_cmd=assistant,
                preamble='', title='x')
            self.assertEqual(s.read_meta()['adapter_kind'], kind, assistant)


class SetTitleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = hs.HYPERVISOR_DIR
        hs.HYPERVISOR_DIR = self.tmp

    def tearDown(self):
        hs.HYPERVISOR_DIR = self._orig

    def _new(self, title='New chat'):
        return hs.HypervisorSession.create(
            assistant='claude', workdir='/home/dev', cli_cmd='claude',
            preamble='', title=title)

    def test_set_title_renames_and_marks_custom(self):
        s = self._new()
        summary = s.set_title('  My deploy chat  ')
        self.assertEqual(summary['title'], 'My deploy chat')  # trimmed
        m = s.read_meta()
        self.assertEqual(m['title'], 'My deploy chat')
        self.assertTrue(m['title_custom'])

    def test_set_title_caps_at_80_chars(self):
        s = self._new()
        s.set_title('x' * 200)
        self.assertEqual(len(s.read_meta()['title']), 80)

    def test_blank_title_falls_back_to_new_chat(self):
        s = self._new()
        self.assertEqual(s.set_title('   ')['title'], 'New chat')

    def test_set_title_on_missing_thread_returns_none(self):
        s = hs.HypervisorSession('nope-does-not-exist')
        self.assertIsNone(s.set_title('whatever'))

    def test_custom_title_survives_first_message_autotitle(self):
        # A manual rename before the first message must not be clobbered by the
        # first-user-message auto-title in send().
        s = self._new()
        s.set_title('Pinned name')
        # Simulate what send() does on the first turn without spawning a CLI.
        meta = s.read_meta()
        first = not s._has_assistant_turn()
        if (first and not meta.get('title_custom')
                and meta.get('title', 'New chat') in ('New chat', '')):
            meta['title'] = 'auto title from message'
        self.assertEqual(meta['title'], 'Pinned name')


class SetModelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = hs.HYPERVISOR_DIR
        hs.HYPERVISOR_DIR = self.tmp

    def tearDown(self):
        hs.HYPERVISOR_DIR = self._orig

    def _new(self, model=''):
        return hs.HypervisorSession.create(
            assistant='claude', workdir='/home/dev', cli_cmd='claude',
            preamble='', title='hi', model=model)

    def test_create_stores_model_in_ctx_and_summary(self):
        s = self._new(model='opus')
        self.assertEqual(s.read_meta()['adapter']['model'], 'opus')
        self.assertEqual(s.summary()['model'], 'opus')

    def test_create_defaults_model_to_empty(self):
        s = self._new()
        self.assertEqual(s.read_meta()['adapter']['model'], '')
        self.assertEqual(s.summary()['model'], '')

    def test_set_model_updates_ctx_and_summary(self):
        s = self._new()
        summary = s.set_model('  sonnet  ')
        self.assertEqual(summary['model'], 'sonnet')  # trimmed
        self.assertEqual(s.read_meta()['adapter']['model'], 'sonnet')

    def test_set_model_does_not_bump_updated_at(self):
        s = self._new()
        before = s.read_meta()['updated_at']
        s.set_model('haiku')
        self.assertEqual(s.read_meta()['updated_at'], before)

    def test_set_model_on_missing_thread_returns_none(self):
        s = hs.HypervisorSession('nope-does-not-exist')
        self.assertIsNone(s.set_model('opus'))


class AnteAdapterTest(unittest.TestCase):
    def setUp(self):
        self.a = hs.AnteAdapter()

    def test_agentmessage_becomes_assistant_text(self):
        ctx = {'workdir': '/home/dev'}
        self.a._reset_turn(ctx)
        # SessionStart captures the resumable id, emits nothing.
        self.assertEqual(self.a.parse(ctx, json.dumps(
            {'event': {'SessionStart': {'session_id': 'ses_1'}}})), [])
        self.assertEqual(ctx['ante_session_id'], 'ses_1')
        # AgentMessage inner is the bare text string.
        out = self.a.parse(ctx, json.dumps({'event': {'AgentMessage': 'PONG'}}))
        self.assertEqual(out, [{'role': 'assistant', 'type': 'message', 'text': 'PONG'}])

    def test_noise_ignored_and_resume_flag(self):
        ctx = {'ante_session_id': 'ses_9', 'workdir': '/home/dev'}
        for noise in ('TurnStart', 'UsageUpdate', 'ExtensionRefreshed'):
            self.assertEqual(self.a.parse(ctx, json.dumps({'event': {noise: {}}})), [])
        spec = self.a.build(ctx, 'hi', first=False)
        self.assertIn('-r', spec['argv'])
        self.assertIn('ses_9', spec['argv'])
        self.assertIn('--output-format', spec['argv'])

    def test_raw_fallback_when_no_structured_events(self):
        ctx = {}
        self.a.build(ctx, 'hi', first=True)       # resets per-turn state
        self.a.parse(ctx, 'plain non-json line')  # accumulates raw
        out = self.a.finalize(ctx, 0)
        self.assertEqual(out, [{'role': 'assistant', 'type': 'message',
                                'text': 'plain non-json line'}])


class OpencodeAdapterTest(unittest.TestCase):
    def setUp(self):
        self.a = hs.OpencodeAdapter()

    def test_text_event_part(self):
        ctx = {}
        self.a._reset_turn(ctx)
        out = self.a.parse(ctx, json.dumps(
            {'type': 'text', 'sessionID': 'ses_x', 'part': {'text': 'PONG'}}))
        self.assertEqual(out, [{'role': 'assistant', 'type': 'message', 'text': 'PONG'}])
        self.assertEqual(ctx['opencode_session_id'], 'ses_x')

    def test_step_events_are_noise(self):
        ctx = {}
        self.a._reset_turn(ctx)
        self.assertEqual(self.a.parse(ctx, json.dumps({'type': 'step_start', 'part': {}})), [])
        self.assertEqual(self.a.parse(ctx, json.dumps({'type': 'step_finish', 'part': {}})), [])

    def test_error_event_surfaces_provider_message(self):
        ctx = {}
        self.a._reset_turn(ctx)
        out = self.a.parse(ctx, json.dumps(
            {'type': 'error', 'error': {'name': 'APIError',
                                        'data': {'message': 'Key limit exceeded'}}}))
        self.assertEqual(out, [{'role': 'system', 'type': 'error', 'text': 'Key limit exceeded'}])

    def test_build_model_from_cli_cmd_and_resume(self):
        ctx = {'cli_cmd': "opencode --model 'openrouter/anthropic/claude-sonnet-4'"}
        spec = self.a.build(ctx, 'hi', first=True)
        self.assertEqual(spec['argv'][:4], ['opencode', 'run', 'hi', '--format'])
        self.assertIn('openrouter/anthropic/claude-sonnet-4', spec['argv'])
        ctx['opencode_session_id'] = 'ses_x'
        self.assertIn('-s', self.a.build(ctx, 'again', first=False)['argv'])

    def test_build_selected_model_keeps_openrouter_prefix(self):
        # A per-thread switch (#308) stores the OpenRouter model id; the adapter
        # keeps the opencode `openrouter/` provider prefix from cli_cmd.
        ctx = {'cli_cmd': "opencode --model 'openrouter/anthropic/claude-sonnet-4'",
               'model': 'deepseek/deepseek-chat-v3-0324:free'}
        spec = self.a.build(ctx, 'hi', first=True)
        i = spec['argv'].index('--model')
        self.assertEqual(spec['argv'][i + 1],
                         'openrouter/deepseek/deepseek-chat-v3-0324:free')

    def test_build_selected_model_keeps_deepseek_prefix(self):
        ctx = {'cli_cmd': "opencode --model 'deepseek/deepseek-chat'",
               'model': 'deepseek-reasoner'}
        spec = self.a.build(ctx, 'hi', first=True)
        i = spec['argv'].index('--model')
        self.assertEqual(spec['argv'][i + 1], 'deepseek/deepseek-reasoner')


class CodexAdapterTest(unittest.TestCase):
    def setUp(self):
        self.a = hs.CodexAdapter()

    def test_thread_started_captures_session_id(self):
        ctx = {}
        self.a._reset_turn(ctx)
        out = self.a.parse(ctx, json.dumps(
            {'type': 'thread.started', 'thread_id': 'tid-123'}))
        self.assertEqual(out, [])
        self.assertEqual(ctx['codex_session_id'], 'tid-123')

    def test_item_completed_agent_message_becomes_text(self):
        ctx = {}
        self.a._reset_turn(ctx)
        out = self.a.parse(ctx, json.dumps(
            {'type': 'item.completed',
             'item': {'id': 'item_1', 'type': 'agent_message', 'text': 'PONG'}}))
        self.assertEqual(out, [{'role': 'assistant', 'type': 'message', 'text': 'PONG'}])

    def test_item_completed_error_surfaces(self):
        ctx = {}
        self.a._reset_turn(ctx)
        out = self.a.parse(ctx, json.dumps(
            {'type': 'item.completed',
             'item': {'id': 'item_0', 'type': 'error', 'message': 'boom'}}))
        self.assertEqual(out, [{'role': 'system', 'type': 'error', 'text': 'boom'}])

    def test_turn_failed_is_terminal_error(self):
        ctx = {}
        self.a._reset_turn(ctx)
        out = self.a.parse(ctx, json.dumps(
            {'type': 'turn.failed', 'error': {'message': '401 Unauthorized'}}))
        self.assertEqual(out, [{'role': 'system', 'type': 'error', 'text': '401 Unauthorized'}])

    def test_transient_error_and_step_events_are_noise(self):
        ctx = {}
        self.a._reset_turn(ctx)
        # top-level reconnect chatter is NOT a turn outcome — dropped.
        self.assertEqual(self.a.parse(ctx, json.dumps(
            {'type': 'error', 'message': 'Reconnecting... 2/5'}), ), [])
        for noise in ('turn.started', 'turn.completed'):
            self.assertEqual(self.a.parse(ctx, json.dumps({'type': noise})), [])
        self.assertEqual(self.a.parse(ctx, json.dumps(
            {'type': 'item.started', 'item': {'type': 'reasoning'}})), [])
        # reasoning item.completed carries no message marker → skipped.
        self.assertEqual(self.a.parse(ctx, json.dumps(
            {'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'hmm'}})), [])

    def test_build_first_turn_and_resume(self):
        ctx = {'workdir': '/home/dev', 'preamble': 'ROLE'}
        spec = self.a.build(ctx, 'hi', first=True)
        self.assertEqual(spec['argv'][:3], ['codex', 'exec', '--json'])
        self.assertIn('--dangerously-bypass-approvals-and-sandbox', spec['argv'])
        self.assertIn('--skip-git-repo-check', spec['argv'])
        self.assertEqual(spec['argv'][-1], 'ROLE\n\nhi')  # preamble prepended, turn 1
        # resume uses the captured session id via the `exec resume <id>` form.
        ctx['codex_session_id'] = 'tid-9'
        spec2 = self.a.build(ctx, 'again', first=False)
        self.assertEqual(spec2['argv'][:3], ['codex', 'exec', 'resume'])
        self.assertIn('tid-9', spec2['argv'])
        self.assertEqual(spec2['argv'][-1], 'again')

    def test_build_prefers_ctx_model_over_env(self):
        # A per-thread model (#308) beats KC_CODEX_MODEL.
        ctx = {'workdir': '/home/dev', 'model': 'gpt-5-codex'}
        with mock.patch.dict(hs.os.environ, {'KC_CODEX_MODEL': 'o4-mini'}):
            spec = self.a.build(ctx, 'hi', first=True)
        i = spec['argv'].index('--model')
        self.assertEqual(spec['argv'][i + 1], 'gpt-5-codex')

    def test_raw_fallback_when_no_structured_events(self):
        ctx = {}
        self.a.build(ctx, 'hi', first=True)
        self.a.parse(ctx, 'plain non-json line')
        out = self.a.finalize(ctx, 0)
        self.assertEqual(out, [{'role': 'assistant', 'type': 'message',
                                'text': 'plain non-json line'}])


class SoftDeleteTest(unittest.TestCase):
    """Soft-delete / revive / purge lifecycle (issue #260)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = hs.HYPERVISOR_DIR
        hs.HYPERVISOR_DIR = self.tmp

    def tearDown(self):
        hs.HYPERVISOR_DIR = self._orig

    def _mk(self, title='x'):
        return hs.HypervisorSession.create(
            assistant='claude', workdir='/home/dev', cli_cmd='claude',
            preamble='', title=title)

    def test_delete_is_soft_and_hides_from_default_list(self):
        s = self._mk('keep-me')
        s.delete()
        # Files survive — a hard rmtree would have removed the dir.
        self.assertTrue(os.path.isfile(s.meta_path))
        self.assertIsNotNone(s.read_meta().get('deleted_at'))
        # Excluded from the default listing, present in the trash view.
        self.assertEqual(hs.HypervisorSession.list(), [])
        deleted = hs.HypervisorSession.list(only_deleted=True)
        self.assertEqual([t['id'] for t in deleted], [s.id])
        self.assertIsNotNone(deleted[0]['deleted_at'])
        # include_deleted returns both live + tombstoned.
        self.assertEqual(len(hs.HypervisorSession.list(include_deleted=True)), 1)

    def test_delete_preserves_updated_at_ordering(self):
        s = self._mk()
        before = s.read_meta()['updated_at']
        s.delete()
        self.assertEqual(s.read_meta()['updated_at'], before)

    def test_revive_clears_tombstone(self):
        s = self._mk('oops')
        s.delete()
        self.assertTrue(s.revive())
        self.assertIsNone(s.read_meta().get('deleted_at'))
        self.assertEqual([t['id'] for t in hs.HypervisorSession.list()], [s.id])
        self.assertEqual(hs.HypervisorSession.list(only_deleted=True), [])

    def test_revive_on_live_thread_is_false(self):
        s = self._mk()
        self.assertFalse(s.revive())

    def test_revive_preserves_full_event_history(self):
        s = self._mk('with history')
        s._append([{'role': 'user', 'type': 'message', 'text': 'one'}])
        s._append([{'role': 'assistant', 'type': 'message', 'text': 'two'}])
        s._append([{'role': 'user', 'type': 'message', 'text': 'three'}])
        before = s.read_events()

        s.delete()
        # Soft-delete must not touch events.jsonl at all.
        self.assertEqual(s.read_events(), before)

        self.assertTrue(s.revive())
        after = s.read_events()
        self.assertEqual(after, before)
        self.assertEqual([e['seq'] for e in after], [1, 2, 3])
        self.assertEqual([e['text'] for e in after], ['one', 'two', 'three'])

    def test_purge_removes_only_old_tombstones(self):
        live = self._mk('live')
        recent = self._mk('recent')
        old = self._mk('old')
        recent.delete()
        old.delete()
        # Backdate the "old" tombstone well past the cutoff.
        m = old.read_meta()
        m['deleted_at'] = int(hs._now()) - 40 * 86400
        old._write_meta(m, touch=False)

        res = hs.HypervisorSession.purge_deleted(older_than_days=30)
        self.assertEqual(res['purged'], 1)
        self.assertFalse(os.path.isdir(old.dir))        # hard-removed
        self.assertTrue(os.path.isfile(recent.meta_path))  # recent tombstone kept
        self.assertTrue(os.path.isfile(live.meta_path))    # live untouched

    def test_purge_all_tombstones_when_no_cutoff(self):
        s = self._mk()
        s.delete()
        res = hs.HypervisorSession.purge_deleted()
        self.assertEqual(res['purged'], 1)
        self.assertFalse(os.path.isdir(s.dir))


class StopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = hs.HYPERVISOR_DIR
        hs.HYPERVISOR_DIR = self.tmp
        self.s = hs.HypervisorSession.create(
            assistant='claude', workdir='/home/dev', cli_cmd='claude',
            preamble='', title='x')

    def tearDown(self):
        hs.HYPERVISOR_DIR = self._orig
        with hs._RUNLOCK:
            hs._RUNNING.pop(self.s.id, None)
            hs._PROCS.pop(self.s.id, None)
            hs._STOPPING.discard(self.s.id)

    def test_stop_on_idle_thread_is_noop(self):
        self.assertFalse(self.s.stop())
        self.assertFalse(self.s._stop_requested())

    def test_stop_kills_running_process_group(self):
        import subprocess
        proc = subprocess.Popen(['sleep', '30'], start_new_session=True)
        with hs._RUNLOCK:
            hs._RUNNING[self.s.id] = True
            hs._PROCS[self.s.id] = proc
        try:
            self.assertTrue(self.s.stop())
            self.assertTrue(self.s._stop_requested())
            self.assertIsNotNone(proc.poll())  # actually terminated
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_stop_flag_set_before_process_registered(self):
        # Turn is marked running but its Popen hasn't landed yet — stop() should
        # still flag it so the runner skips finalize once the process appears.
        with hs._RUNLOCK:
            hs._RUNNING[self.s.id] = True
        self.assertTrue(self.s.stop())
        self.assertTrue(self.s._stop_requested())


class ChoiceExpansionTest(unittest.TestCase):
    """The ```choice fence → canonical `choice` event split (harness-agnostic)."""

    def test_parse_body_with_question_and_options(self):
        ev = hs._parse_choice_body('Which database?\n- Postgres\n- MySQL\n')
        self.assertEqual(ev, {'role': 'assistant', 'type': 'choice',
                              'options': ['Postgres', 'MySQL'],
                              'question': 'Which database?'})

    def test_parse_body_numbered_and_no_question(self):
        ev = hs._parse_choice_body('1) A\n2. B\n')
        self.assertEqual(ev['options'], ['A', 'B'])
        self.assertNotIn('question', ev)

    def test_parse_body_no_options_is_none(self):
        self.assertIsNone(hs._parse_choice_body('just prose, no bullets'))

    def test_expand_splits_message_into_prose_plus_choice(self):
        msg = {'role': 'assistant', 'type': 'message',
               'text': 'Here are the options.\n```choice\nPick one\n- A\n- B\n```'}
        out = hs._expand_choices(msg)
        self.assertEqual(out[0], {'role': 'assistant', 'type': 'message',
                                  'text': 'Here are the options.'})
        self.assertEqual(out[1], {'role': 'assistant', 'type': 'choice',
                                  'options': ['A', 'B'], 'question': 'Pick one'})

    def test_expand_passthrough_for_plain_message(self):
        msg = {'role': 'assistant', 'type': 'message', 'text': 'no fence here'}
        self.assertEqual(hs._expand_choices(msg), [msg])

    def test_expand_passthrough_for_non_assistant(self):
        msg = {'role': 'user', 'type': 'message', 'text': '```choice\n- A\n```'}
        self.assertEqual(hs._expand_choices(msg), [msg])

    def test_unparseable_fence_kept_as_raw_text(self):
        msg = {'role': 'assistant', 'type': 'message',
               'text': '```choice\nno options at all\n```'}
        out = hs._expand_choices(msg)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['type'], 'message')

    def test_append_persists_split_events(self):
        tmp = tempfile.mkdtemp()
        orig = hs.HYPERVISOR_DIR
        hs.HYPERVISOR_DIR = tmp
        try:
            s = hs.HypervisorSession.create(
                assistant='claude', workdir='/home/dev', cli_cmd='claude',
                preamble='', title='x')
            s._append([{'role': 'assistant', 'type': 'message',
                        'text': 'Choose:\n```choice\n- A\n- B\n```'}])
            evs = s.read_events()
            self.assertEqual([e['type'] for e in evs], ['message', 'choice'])
            self.assertEqual(evs[1]['options'], ['A', 'B'])
            self.assertEqual([e['seq'] for e in evs], [1, 2])
        finally:
            hs.HYPERVISOR_DIR = orig


FIXTURE_LOG = os.path.join(HERE, 'fixtures', 'claude_session_log_sample.jsonl')


class ClaudeSessionLogParseTest(unittest.TestCase):
    """Parsing Claude Code's own JSONL session log into canonical events
    (issue #208) — sourced from a sample log under tests/fixtures/."""

    def setUp(self):
        self.events = hs.parse_claude_session_log(FIXTURE_LOG)

    def test_user_string_prompt_becomes_message(self):
        first = self.events[0]
        self.assertEqual(first, {'role': 'user', 'type': 'message',
                                 'text': 'Check the git status and pick a fix'})

    def test_assistant_text_becomes_message(self):
        msgs = [e for e in self.events
                if e['role'] == 'assistant' and e['type'] == 'message']
        self.assertIn('On it — let me look at the tree.',
                      [m['text'] for m in msgs])

    def test_tool_use_and_result_are_paired_and_distinct(self):
        calls = [e for e in self.events if e['type'] == 'tool_call']
        results = [e for e in self.events if e['type'] == 'tool_result']
        self.assertEqual([c['tool']['name'] for c in calls], ['Bash', 'Read'])
        self.assertEqual(calls[0]['tool_id'], 'toolu_1')
        # tool_result carries the matching id, error flag, and stringified body.
        by_id = {r['tool_use_id']: r for r in results}
        self.assertFalse(by_id['toolu_1']['is_error'])
        self.assertIn('server.py', by_id['toolu_1']['text'])
        self.assertTrue(by_id['toolu_2']['is_error'])
        self.assertEqual(by_id['toolu_2']['text'], 'File not found')
        # A tool_call/result is a different event type than plain prose.
        self.assertNotIn('tool_call',
                         [e['type'] for e in self.events if e['role'] == 'user'])

    def test_thinking_blocks_are_dropped(self):
        self.assertNotIn('internal reasoning that must be dropped',
                         [e.get('text') for e in self.events])

    def test_choice_fence_is_expanded(self):
        choices = [e for e in self.events if e['type'] == 'choice']
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0]['options'], ['Revert the change', 'Patch forward'])
        self.assertEqual(choices[0]['question'], 'Pick an approach')
        # The prose before the fence survives as its own message.
        self.assertIn('Which fix do you want?',
                      [e.get('text') for e in self.events])

    def test_noise_records_are_skipped(self):
        texts = [e.get('text') or '' for e in self.events]
        self.assertFalse(any('synthetic reminder' in t for t in texts))    # isMeta
        self.assertFalse(any('sub-agent chatter' in t for t in texts))     # sidechain
        # summary / system / file-history-snapshot lines contribute nothing.
        roles = {(e['role'], e['type']) for e in self.events}
        self.assertNotIn(('system', 'message'), roles)

    def test_missing_file_returns_empty(self):
        self.assertEqual(hs.parse_claude_session_log('/no/such/file.jsonl'), [])


class ClaudeContentHelperTest(unittest.TestCase):
    def test_user_string_vs_blocklist(self):
        self.assertEqual(hs._claude_user_events('hello'),
                         [{'role': 'user', 'type': 'message', 'text': 'hello'}])
        self.assertEqual(hs._claude_user_events('   '), [])
        out = hs._claude_user_events(
            [{'type': 'tool_result', 'tool_use_id': 't9', 'content': 'ok'}])
        self.assertEqual(out[0]['type'], 'tool_result')
        self.assertEqual(out[0]['tool_use_id'], 't9')

    def test_assistant_text_and_tool_use(self):
        out = hs._claude_assistant_events([
            {'type': 'text', 'text': 'hi'},
            {'type': 'thinking', 'thinking': 'secret'},
            {'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {'command': 'ls'}},
        ])
        self.assertEqual([e['type'] for e in out], ['message', 'tool_call'])
        self.assertEqual(out[1]['tool']['name'], 'Bash')


class LocateSessionLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = hs.WORKSPACE_HOME
        hs.WORKSPACE_HOME = self.tmp
        # /home/dev/.worktrees/kc → escaped-cwd dir under <tmp>/.claude/projects.
        self.workdir = '/home/dev/.worktrees/kc'
        self.projdir = hs.claude_project_dir(self.workdir)
        os.makedirs(self.projdir, exist_ok=True)

    def tearDown(self):
        hs.WORKSPACE_HOME = self._orig

    def test_escaped_cwd_slug(self):
        self.assertTrue(self.projdir.endswith('-home-dev--worktrees-kc'))

    def test_exact_session_id_match(self):
        p = os.path.join(self.projdir, 'sess-abc.jsonl')
        open(p, 'w').close()
        self.assertEqual(
            hs.locate_claude_session_log(self.workdir, 'sess-abc'), p)

    def test_missing_session_id_file_is_none(self):
        self.assertIsNone(
            hs.locate_claude_session_log(self.workdir, 'nope'))

    def test_newest_jsonl_when_no_id(self):
        old = os.path.join(self.projdir, 'old.jsonl')
        new = os.path.join(self.projdir, 'new.jsonl')
        open(old, 'w').close()
        open(new, 'w').close()
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        self.assertEqual(hs.locate_claude_session_log(self.workdir), new)

    def test_no_project_dir_is_none(self):
        self.assertIsNone(
            hs.locate_claude_session_log('/some/other/dir', 'x'))


class TranscriptSourceTest(unittest.TestCase):
    """HypervisorSession.transcript() source selection + fallback (issue #208)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_dir = hs.HYPERVISOR_DIR
        self._orig_home = hs.WORKSPACE_HOME
        hs.HYPERVISOR_DIR = os.path.join(self.tmp, 'threads')
        hs.WORKSPACE_HOME = os.path.join(self.tmp, 'home')
        os.makedirs(hs.HYPERVISOR_DIR, exist_ok=True)

    def tearDown(self):
        hs.HYPERVISOR_DIR = self._orig_dir
        hs.WORKSPACE_HOME = self._orig_home

    def _mk(self, assistant='claude', workdir='/w'):
        return hs.HypervisorSession.create(
            assistant=assistant, workdir=workdir, cli_cmd=assistant,
            preamble='', title='x')

    def _write_log(self, workdir, sid, lines):
        proj = hs.claude_project_dir(workdir)
        os.makedirs(proj, exist_ok=True)
        with open(os.path.join(proj, f'{sid}.jsonl'), 'w') as f:
            for o in lines:
                f.write(json.dumps(o) + '\n')

    def test_falls_back_to_capture_for_non_claude(self):
        s = self._mk(assistant='ante')
        s._append([{'role': 'user', 'type': 'message', 'text': 'hi'}])
        tx = s.transcript()
        self.assertEqual(tx['source'], 'capture')
        self.assertEqual([e['text'] for e in tx['events']], ['hi'])

    def test_falls_back_to_capture_when_no_session_id(self):
        s = self._mk()  # claude, but no claude_session_id captured yet
        s._append([{'role': 'user', 'type': 'message', 'text': 'hi'}])
        self.assertEqual(s.transcript()['source'], 'capture')

    def test_sources_from_session_log_when_available(self):
        s = self._mk(workdir='/w')
        # Record the captured Claude session id, as an idle finished turn would.
        m = s.read_meta()
        m['adapter']['claude_session_id'] = 'sid-1'
        s._write_meta(m)
        self._write_log('/w', 'sid-1', [
            {'type': 'user', 'message': {'role': 'user', 'content': 'do it'}},
            {'type': 'assistant', 'message': {'role': 'assistant',
                'content': [{'type': 'text', 'text': 'done'}]}},
        ])
        tx = s.transcript()
        self.assertEqual(tx['source'], 'session_log')
        self.assertEqual([(e['role'], e['text']) for e in tx['events']],
                         [('user', 'do it'), ('assistant', 'done')])
        # Re-stamped with contiguous seq for the frontend cursor.
        self.assertEqual([e['seq'] for e in tx['events']], [1, 2])

    def test_running_turn_prefers_live_capture(self):
        s = self._mk(workdir='/w')
        m = s.read_meta()
        m['adapter']['claude_session_id'] = 'sid-2'
        m['status'] = 'running'
        s._write_meta(m)
        self._write_log('/w', 'sid-2', [
            {'type': 'assistant', 'message': {'role': 'assistant',
                'content': [{'type': 'text', 'text': 'from log'}]}}])
        s._append([{'role': 'user', 'type': 'message', 'text': 'live'}])
        with hs._RUNLOCK:
            hs._RUNNING[s.id] = True
        try:
            tx = s.transcript()
        finally:
            with hs._RUNLOCK:
                hs._RUNNING.pop(s.id, None)
        self.assertEqual(tx['source'], 'capture')
        self.assertEqual([e['text'] for e in tx['events']], ['live'])

    def test_carries_over_error_marker_from_capture(self):
        s = self._mk(workdir='/w')
        m = s.read_meta()
        m['adapter']['claude_session_id'] = 'sid-3'
        s._write_meta(m)
        self._write_log('/w', 'sid-3', [
            {'type': 'assistant', 'message': {'role': 'assistant',
                'content': [{'type': 'text', 'text': 'ok'}]}}])
        s._append([{'role': 'system', 'type': 'error', 'text': 'claude exited 1'}])
        tx = s.transcript()
        self.assertEqual(tx['source'], 'session_log')
        self.assertEqual(tx['events'][-1],
                         {'role': 'system', 'type': 'error',
                          'text': 'claude exited 1',
                          'seq': tx['events'][-1]['seq'],
                          'ts': tx['events'][-1]['ts']})


if __name__ == '__main__':
    unittest.main()
