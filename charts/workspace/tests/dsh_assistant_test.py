"""Server-side registry for the DeepSeek Harness assistant (issue #639).

Gating (#639, revised by #702): the entry appears whenever the `dsh` binary is
present, so a user can discover it. Without a DeepSeek key it is listed with
ready=False and a reason, is never the default or a fallback target, and every
launch path refuses it with that reason instead of starting a turn that fails.
An older image without the binary still doesn't list it.

Run with:   python3 -m unittest tests.dsh_assistant_test   (from charts/workspace/)
"""

import os
import shlex
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import hypervisor_session as hv  # noqa: E402
import server  # noqa: E402

CTM = server.ClaudeTaskManager
DSH = 'deepseek-harness'


def _ids(assistants):
    return [a['id'] for a in assistants]


def _entry(assistants, rid):
    return next(a for a in assistants if a['id'] == rid)


class _GateBase(unittest.TestCase):
    """available_assistants() reads PATH, os.environ and the stored provider
    keys; pin all three. `stored` is what the user set in Settings, which
    ProviderKeysManager persists on the PVC — it must be stubbed rather than
    left to the real file, or a run inside a workspace pod would read that
    pod's keys and stop testing anything."""

    # A workspace with nothing else configured, so the assertions are about
    # `dsh` alone rather than about whatever this machine happens to have set.
    BASE_ENV = {}

    def listed(self, env, which, stored=None):
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(server.ProviderKeysManager, 'env_overlay',
                                  return_value=dict(stored or {})), \
                mock.patch.object(server.shutil, 'which',
                                  side_effect=lambda n: '/usr/local/bin/' + n
                                  if n in which else None):
            return CTM.available_assistants()


class GatingTest(_GateBase):
    def test_listed_with_binary_and_key(self):
        out = self.listed({'DEEPSEEK_API_KEY': 'sk-x'}, {'dsh'})
        entry = _entry(out, DSH)
        self.assertTrue(entry['ready'])
        self.assertNotIn('needs', entry)
        self.assertNotIn('notReadyReason', entry)

    def test_listed_but_not_ready_with_binary_and_no_key(self):
        # #702: hiding it made the install look broken. It is listed, but
        # flagged so the pickers can ask for the key instead of launching.
        entry = _entry(self.listed({}, {'dsh'}), DSH)
        self.assertIs(entry['ready'], False)
        self.assertEqual(entry['needs'], ['DEEPSEEK_API_KEY'])
        self.assertIn('DeepSeek API key', entry['notReadyReason'])
        self.assertIn('Settings', entry['notReadyReason'])

    def test_a_whitespace_key_is_not_a_key(self):
        entry = _entry(self.listed({'DEEPSEEK_API_KEY': '   '}, {'dsh'}), DSH)
        self.assertIs(entry['ready'], False)

    def test_every_other_entry_is_ready(self):
        out = self.listed({'OPENROUTER_API_KEY': 'k'}, {'dsh', 'codex', 'agy'})
        for a in out:
            if a['id'] != DSH:
                with self.subTest(assistant=a['id']):
                    self.assertIs(a['ready'], True)

    def test_opencode_deepseek_stays_hidden_without_a_key(self):
        # Scope of #702: only the harness becomes always-visible.
        self.assertNotIn('opencode-deepseek', _ids(self.listed({}, {'dsh'})))

    def test_absent_with_key_but_no_binary(self):
        # An older image that predates the install: the entry is simply not
        # listed, and nothing else changes.
        out = self.listed({'DEEPSEEK_API_KEY': 'sk-x'}, set())
        self.assertNotIn(DSH, _ids(out))
        self.assertIn('claude', _ids(out))
        self.assertIn('ante', _ids(out))

    def test_absent_with_neither(self):
        self.assertNotIn(DSH, _ids(self.listed({}, set())))

    def test_an_older_image_leaves_the_other_assistants_alone(self):
        with_dsh = self.listed({'DEEPSEEK_API_KEY': 'sk-x'}, {'dsh', 'codex'})
        without = self.listed({'DEEPSEEK_API_KEY': 'sk-x'}, {'codex'})
        self.assertEqual([i for i in _ids(with_dsh) if i != DSH],
                         _ids(without))

    def test_coexists_with_the_opencode_deepseek_entry(self):
        # Explicit non-goal of #639: the existing OpenCode→DeepSeek path must
        # keep working. One key enables both, and they are different entries.
        out = _ids(self.listed({'DEEPSEEK_API_KEY': 'sk-x'}, {'dsh'}))
        self.assertIn('opencode-deepseek', out)
        self.assertIn(DSH, out)

    def test_carries_a_model_and_a_switcher_list(self):
        out = self.listed({'DEEPSEEK_API_KEY': 'sk-x'}, {'dsh'})
        entry = next(a for a in out if a['id'] == DSH)
        self.assertEqual(entry['label'], 'DeepSeek Harness')
        self.assertEqual(entry['model'], 'deepseek-v4-flash')
        self.assertEqual(entry['models'],
                         ['deepseek-v4-flash', 'deepseek-v4-pro'])
        self.assertEqual(entry['effort'], 'high')
        self.assertEqual(entry['effortCap'], 'max')

    def test_configured_model_leads_the_switcher(self):
        out = self.listed({'DEEPSEEK_API_KEY': 'sk-x',
                           'KC_DSH_MODEL': 'deepseek-v4-pro'}, {'dsh'})
        entry = next(a for a in out if a['id'] == DSH)
        self.assertEqual(entry['model'], 'deepseek-v4-pro')
        self.assertEqual(entry['models'][0], 'deepseek-v4-pro')
        # …and is not duplicated further down the list.
        self.assertEqual(entry['models'].count('deepseek-v4-pro'), 1)


class SelfServiceKeyTest(_GateBase):
    """A key set in Settings must enable the entry exactly like a pod-env one.

    ProviderKeysManager persists self-service keys on the PVC and applies them
    at CLI spawn, but never copies them into the server's own environ. The
    dropdown gated on os.environ alone, so a user could set a DeepSeek key,
    have it work the moment a turn ran, and still never be offered the
    assistant — which is how it presented on a workspace running v1.60.0.
    """

    def test_stored_key_lists_the_harness(self):
        out = self.listed({}, {'dsh'}, stored={'DEEPSEEK_API_KEY': 'sk-x'})
        self.assertIs(_entry(out, DSH)['ready'], True)

    def test_stored_key_lists_the_opencode_deepseek_entry(self):
        out = self.listed({}, set(), stored={'DEEPSEEK_API_KEY': 'sk-x'})
        self.assertIn('opencode-deepseek', _ids(out))

    def test_stored_key_still_needs_the_binary(self):
        out = self.listed({}, set(), stored={'DEEPSEEK_API_KEY': 'sk-x'})
        self.assertNotIn(DSH, _ids(out))

    def test_stored_openrouter_and_zen_keys_list_their_entries(self):
        out = _ids(self.listed({}, set(), stored={
            'OPENROUTER_API_KEY': 'sk-or', 'OPENCODE_API_KEY': 'sk-oc'}))
        self.assertIn('opencode-openrouter', out)
        self.assertIn('opencode-zen', out)

    def test_stored_key_overrides_the_pod_env(self):
        out = self.listed({'DEEPSEEK_API_KEY': 'sk-pod'}, {'dsh'},
                          stored={'DEEPSEEK_API_KEY': 'sk-user'})
        self.assertIs(_entry(out, DSH)['ready'], True)

    def test_no_key_anywhere_lists_it_as_not_ready(self):
        out = self.listed({}, {'dsh'}, stored={})
        self.assertIs(_entry(out, DSH)['ready'], False)


class ModelListTest(unittest.TestCase):
    def test_builtin_list(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(CTM.available_models(DSH),
                             ['deepseek-v4-flash', 'deepseek-v4-pro'])

    def test_env_override_replaces_the_list(self):
        # The experimental vision model is not offered by default; an operator
        # who wants it curates the list.
        with mock.patch.dict(os.environ, {
                'KC_DSH_MODELS': 'deepseek-v4-pro, deepseek-v4-flash-vision-exp'},
                clear=True):
            self.assertEqual(CTM.available_models(DSH),
                             ['deepseek-v4-pro', 'deepseek-v4-flash-vision-exp'])

    def test_resolve_model_rejects_anything_off_the_list(self):
        # Webhooks and CLI callers are free-form, so the boundary is defended.
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(CTM.resolve_model(DSH, 'gpt-9'), 'deepseek-v4-flash')
            self.assertEqual(CTM.resolve_model(DSH, 'deepseek-v4-pro'),
                             'deepseek-v4-pro')


class EffortTest(unittest.TestCase):
    def test_cap_table_mirrors_the_adapter_module(self):
        # Same lockstep discipline the existing cap table has: the two live in
        # separate modules because server imports hypervisor_session, not the
        # other way round.
        self.assertEqual(CTM._EFFORT_CAP, hv.EFFORT_CAP)
        self.assertEqual(CTM._EFFORT_CAP[DSH], 'max')

    def test_vocab_mirrors_the_adapter(self):
        self.assertEqual(CTM._DSH_EFFORT_VOCAB,
                         hv.DeepseekHarnessAdapter._EFFORT_NATIVE)

    def test_delivery_and_cap_tables_stay_in_lockstep(self):
        self.assertEqual(set(CTM._EFFORT_DELIVERY), set(CTM._EFFORT_CAP))

    def test_nothing_clamps_because_max_is_real(self):
        for level in CTM._EFFORT_LEVELS:
            with self.subTest(level=level):
                self.assertEqual(CTM.resolve_native_effort(DSH, level), level)

    def test_cli_args_translate_into_the_harness_vocabulary(self):
        for canonical, native in (('low', 'low'), ('medium', 'high'),
                                  ('high', 'high'), ('xhigh', 'max'),
                                  ('max', 'max')):
            with self.subTest(effort=canonical):
                self.assertEqual(CTM.effort_cli_args(DSH, canonical),
                                 ['--effort', native])

    def test_no_flag_when_effort_is_unset_or_unknown(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            # Unset falls back to the built-in default rather than nothing…
            self.assertEqual(CTM.effort_cli_args(DSH, ''), ['--effort', 'high'])
        self.assertEqual(CTM.effort_cli_args(DSH, 'turbo'), ['--effort', 'high'])

    def test_it_takes_a_flag_not_an_env_var(self):
        self.assertEqual(CTM.effort_env(DSH, 'max'), {})

    def test_the_flag_delivery_shape_does_not_disturb_the_others(self):
        self.assertEqual(CTM.effort_cli_args('codex', 'high'),
                         ['-c', 'model_reasoning_effort=high'])
        self.assertEqual(CTM.effort_cli_args('claude', 'high'), [])
        self.assertEqual(CTM.effort_env('claude', 'high'),
                         {'CLAUDE_CODE_EFFORT_LEVEL': 'high'})


class AssistantCommandTest(unittest.TestCase):
    def cmd(self, **kw):
        with mock.patch.dict(os.environ, kw.pop('env', {}), clear=True):
            return CTM.assistant_command(DSH, **kw)

    def test_builds_run_the_bridge_in_serve_mode(self):
        # `dsh` ships no usable REPL: the `tui` profile is not among the
        # bundles the npm package installs, and `--profile headless` is
        # one-shot prose with no tool output.
        cmd = self.cmd()
        self.assertTrue(cmd.startswith(
            'python3 /tmp/browser/acp_bridge.py --serve '
            '--format pretty --cwd "$PWD"'))
        self.assertNotIn('dsh ', cmd)

    def test_the_pane_gets_prose_not_a_machine_format(self):
        """#639: the pane is read by a person on both surfaces — ttyd on web,
        TerminalView on mobile — and parsed by neither, so stream-json printed
        every event twice."""
        cmd = self.cmd()
        self.assertIn('--format pretty', cmd)
        self.assertNotIn('stream-json', cmd)

    def test_cwd_is_the_tasks_workdir(self):
        # create_task wraps this in `cd <workdir> && …` under `bash -lc`.
        self.assertIn('--cwd "$PWD"', self.cmd())

    @staticmethod
    def _flag(cmd, flag):
        """The value the shell would actually pass for `flag`."""
        argv = shlex.split(cmd)
        return argv[argv.index(flag) + 1]

    def test_model_is_passed(self):
        self.assertEqual(
            self._flag(self.cmd(model='deepseek-v4-pro'), '--model'),
            'deepseek-v4-pro')
        self.assertEqual(
            self._flag(self.cmd(env={'KC_DSH_MODEL': 'deepseek-v4-flash'}),
                       '--model'),
            'deepseek-v4-flash')

    def test_a_hostile_env_var_cannot_break_out_of_the_shell_command(self):
        # It must survive as exactly ONE argument, metacharacters and all —
        # not as a model name plus an injected command.
        hostile = "x'; touch /tmp/pwned; #"
        cmd = self.cmd(env={'KC_DSH_MODEL': hostile})
        self.assertEqual(self._flag(cmd, '--model'), hostile)
        argv = shlex.split(cmd)
        self.assertNotIn('touch', argv)
        self.assertNotIn(';', argv)

    def test_effort_rides_the_flag(self):
        self.assertEqual(self._flag(self.cmd(effort='xhigh'), '--effort'), 'max')

    def test_auto_approve_is_a_no_op(self):
        # A real difference from the other assistants: ACP permission requests
        # are JSON-RPC calls that must be answered inside the turn, and a tmux
        # pane cannot put that question to a user. The bridge always approves,
        # so the Build tab does not keep prompting the way it does for
        # claude/ante. Documented in docs/llm-setup.md.
        self.assertEqual(self.cmd(auto_approve=True), self.cmd(auto_approve=False))

    def test_other_assistants_are_untouched(self):
        self.assertEqual(CTM.assistant_command('ante'), 'ante')
        self.assertEqual(CTM.assistant_command('ante', auto_approve=True),
                         'ante --yolo')
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(CTM.assistant_command('opencode-deepseek'),
                             'opencode --model deepseek/deepseek-chat')


class ResolveAssistantTest(unittest.TestCase):
    def test_selectable_when_enabled(self):
        with mock.patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'sk-x'},
                             clear=True), \
                mock.patch.object(server.shutil, 'which',
                                  side_effect=lambda n: '/x/' + n
                                  if n == 'dsh' else None):
            self.assertEqual(CTM.resolve_assistant(DSH), DSH)

    def test_rejected_when_not_enabled(self):
        # A webhook or cron asking for an assistant this workspace cannot run
        # must fall back loudly, not launch a broken task.
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(server.shutil, 'which', return_value=None):
            self.assertNotEqual(CTM.resolve_assistant(DSH), DSH)

    def test_listed_but_not_ready_is_never_resolved(self):
        # #702: listed ≠ launchable. Unattended paths fall back instead.
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(server.ProviderKeysManager, 'env_overlay',
                                  return_value={}), \
                mock.patch.object(server.shutil, 'which',
                                  side_effect=lambda n: '/x/' + n
                                  if n == 'dsh' else None):
            self.assertNotEqual(CTM.resolve_assistant(DSH), DSH)


def _no_key_dsh_installed(extra_env=None):
    """Patches for a workspace with `dsh` installed and no DeepSeek key."""
    return (
        mock.patch.dict(os.environ, dict(extra_env or {}), clear=True),
        mock.patch.object(server.ProviderKeysManager, 'env_overlay',
                          return_value={}),
        mock.patch.object(server.shutil, 'which',
                          side_effect=lambda n: '/x/' + n
                          if n == 'dsh' else None),
    )


class DefaultFlagTest(unittest.TestCase):
    def test_a_not_ready_harness_is_never_the_default(self):
        p = _no_key_dsh_installed()
        with p[0], p[1], p[2], \
                mock.patch.object(server, 'WORKSPACE_DEFAULT_ASSISTANT', DSH), \
                mock.patch('sys.stderr'):
            out = CTM.available_assistants()
        self.assertIs(_entry(out, DSH)['default'], False)
        self.assertEqual(out[0]['id'], 'claude')
        self.assertTrue(out[0]['default'])

    def test_a_ready_harness_can_be_the_default(self):
        p = _no_key_dsh_installed({'DEEPSEEK_API_KEY': 'sk-x'})
        with p[0], p[1], p[2], \
                mock.patch.object(server, 'WORKSPACE_DEFAULT_ASSISTANT', DSH):
            out = CTM.available_assistants()
        self.assertEqual(out[0]['id'], DSH)
        self.assertTrue(out[0]['default'])


class NotReadyReasonTest(unittest.TestCase):
    def test_shape_when_the_key_is_missing(self):
        p = _no_key_dsh_installed()
        with p[0], p[1], p[2]:
            body = CTM.not_ready_reason(DSH)
        self.assertEqual(body['code'], 'assistant_not_ready')
        self.assertEqual(body['assistant'], DSH)
        self.assertEqual(body['needs'], ['DEEPSEEK_API_KEY'])
        self.assertIn('DeepSeek API key', body['error'])

    def test_none_for_ready_unknown_unlisted_or_empty(self):
        p = _no_key_dsh_installed({'DEEPSEEK_API_KEY': 'sk-x'})
        with p[0], p[1], p[2]:
            self.assertIsNone(CTM.not_ready_reason(DSH))
        p = _no_key_dsh_installed()
        with p[0], p[1], p[2]:
            self.assertIsNone(CTM.not_ready_reason('claude'))
            self.assertIsNone(CTM.not_ready_reason('no-such-agent'))
            self.assertIsNone(CTM.not_ready_reason(''))
            self.assertIsNone(CTM.not_ready_reason(None))
        with mock.patch.object(server.shutil, 'which', return_value=None):
            self.assertIsNone(CTM.not_ready_reason(DSH))


class MissingKeysTest(unittest.TestCase):
    def test_declared_on_the_harness_only(self):
        import runtimes
        self.assertEqual(runtimes.missing_keys(DSH, {}), ['DEEPSEEK_API_KEY'])
        self.assertEqual(runtimes.missing_keys(DSH, {'DEEPSEEK_API_KEY': ' '}),
                         ['DEEPSEEK_API_KEY'])
        self.assertEqual(runtimes.missing_keys(DSH, {'DEEPSEEK_API_KEY': 'k'}), [])
        for rid in runtimes.RUNTIMES:
            if rid != DSH:
                with self.subTest(runtime=rid):
                    self.assertEqual(runtimes.missing_keys(rid, {}), [])
        self.assertEqual(runtimes.missing_keys('no-such-agent', {}), [])


class LaunchHandlerTest(unittest.TestCase):
    """Every human launch path refuses a not-ready pick with the reason (#702)."""

    def _handler(self, body):
        h = mock.Mock(spec=server.BrowserHandler)
        h.check_claude_auth.return_value = True
        h.read_json_body.return_value = body
        self.responses = []
        h.send_json.side_effect = lambda o, s=200: self.responses.append((o, s))
        return h

    def _create_task(self, body, env=None):
        p = _no_key_dsh_installed(env)
        with p[0], p[1], p[2], \
                mock.patch.object(CTM, 'create_task',
                                  return_value={'id': 't1'}) as create:
            server.BrowserHandler.handle_claude_create_task(self._handler(body))
        return create

    def test_build_with_a_not_ready_harness_is_refused(self):
        create = self._create_task({'assistant': DSH, 'prompt': 'hi'})
        create.assert_not_called()
        body, status = self.responses[-1]
        self.assertEqual(status, 400)
        self.assertEqual(body['code'], 'assistant_not_ready')
        self.assertIn('DeepSeek API key', body['error'])

    def test_build_with_a_ready_harness_starts(self):
        create = self._create_task({'assistant': DSH},
                                   env={'DEEPSEEK_API_KEY': 'sk-x'})
        create.assert_called_once()

    def test_build_without_an_assistant_or_with_an_unknown_one_starts(self):
        self._create_task({'prompt': 'hi'}).assert_called_once()
        self._create_task({'assistant': 'no-such-agent'}).assert_called_once()

    def _create_thread(self, body, env=None):
        p = _no_key_dsh_installed(env)
        session = mock.Mock()
        session.summary.return_value = {'id': 'x'}
        with p[0], p[1], p[2], \
                mock.patch.object(server, 'HYPERVISOR_ENABLED', True), \
                mock.patch.object(server, '_HYPERVISOR_AVAILABLE', True), \
                mock.patch.object(server.HypervisorSession, 'create',
                                  return_value=session) as create, \
                mock.patch.object(server.HypervisorSession, 'list',
                                  return_value=[]):
            try:
                server.BrowserHandler.handle_hypervisor_create_thread(
                    self._handler(body))
            except Exception:
                # Past the gate the handler touches far more of the server
                # than this test stubs; only whether it got there matters.
                pass
        return create

    def test_chat_with_a_not_ready_harness_is_refused(self):
        create = self._create_thread({'assistant': DSH, 'message': 'hi'})
        create.assert_not_called()
        self.assertEqual(self.responses[-1][1], 400)
        self.assertEqual(self.responses[-1][0]['code'], 'assistant_not_ready')

    def test_chat_with_a_ready_harness_is_not_refused(self):
        self._create_thread({'assistant': DSH, 'message': 'hi'},
                            env={'DEEPSEEK_API_KEY': 'sk-x'})
        self.assertFalse(any(b.get('code') == 'assistant_not_ready'
                             for b, _ in self.responses))

    def _send(self, thread_assistant, env=None):
        p = _no_key_dsh_installed(env)
        session = mock.Mock()
        session.status.return_value = 'idle'
        session.read_meta.return_value = {'assistant': thread_assistant}
        h = self._handler({'message': 'hi'})
        h._hv_session_or_404.return_value = session
        with p[0], p[1], p[2]:
            server.BrowserHandler.handle_hypervisor_send_message(h, 't1')
        return session

    def test_send_on_a_chat_whose_key_was_removed_is_refused(self):
        session = self._send(DSH)
        session.send.assert_not_called()
        self.assertEqual(self.responses[-1][1], 400)
        self.assertEqual(self.responses[-1][0]['code'], 'assistant_not_ready')

    def test_send_on_other_chats_is_untouched(self):
        self._send('claude').send.assert_called_once_with('hi')
        self._send(DSH, env={'DEEPSEEK_API_KEY': 'sk-x'}).send.assert_called_once()


class RegistryEntryTest(unittest.TestCase):
    def test_entry_shape(self):
        self.assertEqual(CTM.ASSISTANTS[DSH],
                         {'id': DSH, 'label': 'DeepSeek Harness'})

    def test_the_id_matches_the_adapter_route(self):
        self.assertIsInstance(hv._adapter_for(DSH), hv.DeepseekHarnessAdapter)


if __name__ == '__main__':
    unittest.main()
