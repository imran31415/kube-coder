"""Server-side registry for the DeepSeek Harness assistant (issue #639).

The acceptance criteria that matter here are the gating ones: the entry must
appear only when the binary AND the key are both present, and be *absent*
rather than broken otherwise — an older image, or a workspace with no DeepSeek
key, must not be offered an assistant whose every turn fails.

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


class _GateBase(unittest.TestCase):
    """available_assistants() reads os.environ and PATH; pin both."""

    # A workspace with nothing else configured, so the assertions are about
    # `dsh` alone rather than about whatever this machine happens to have set.
    BASE_ENV = {}

    def listed(self, env, which):
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(server.shutil, 'which',
                                  side_effect=lambda n: '/usr/local/bin/' + n
                                  if n in which else None):
            return CTM.available_assistants()


class GatingTest(_GateBase):
    def test_listed_with_binary_and_key(self):
        out = self.listed({'DEEPSEEK_API_KEY': 'sk-x'}, {'dsh'})
        self.assertIn(DSH, _ids(out))

    def test_absent_with_binary_but_no_key(self):
        # `dsh` authenticates with an API key — unlike agy/codex, binary
        # presence alone is NOT enough. Listing it here would offer an entry
        # whose every turn fails with "Authentication Fails".
        self.assertNotIn(DSH, _ids(self.listed({}, {'dsh'})))

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
            '--format stream-json --cwd "$PWD"'))
        self.assertNotIn('dsh ', cmd)

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


class RegistryEntryTest(unittest.TestCase):
    def test_entry_shape(self):
        self.assertEqual(CTM.ASSISTANTS[DSH],
                         {'id': DSH, 'label': 'DeepSeek Harness'})

    def test_the_id_matches_the_adapter_route(self):
        self.assertIsInstance(hv._adapter_for(DSH), hv.DeepseekHarnessAdapter)


if __name__ == '__main__':
    unittest.main()
