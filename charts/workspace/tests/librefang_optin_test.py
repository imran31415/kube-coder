"""LibreFang is opt-in, and off by default (#754).

The workspace used to install and run LibreFang for every user: boot seeded
its home onto the PVC, rewrote its config and pre-warmed its kernel daemon,
and every assistant picker listed it. One chart value
(assistant.librefang.enabled -> KC_LIBREFANG_ENABLED) now gates all of it.

This suite covers the Python half: the runtime catalog's notion of an opt-in
runtime, and that a disabled runtime is ABSENT from the orchestrator's options
rather than offered and then broken. The boot half (start.sh) and the env
plumbing live in tests/librefang_optin_test.yaml.

Run with:  python3 -m unittest tests.librefang_optin_test  (from charts/workspace/)
"""

import importlib
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import mcp_agent_orchestrator as orch  # noqa: E402
import runtimes  # noqa: E402

LF = 'librefang'


class OptInRuntimeTest(unittest.TestCase):
    """runtimes.is_enabled — the prior question to every other gate."""

    def test_librefang_declares_its_flag(self):
        self.assertEqual(runtimes.RUNTIMES[LF]['opt_in_env'],
                         'KC_LIBREFANG_ENABLED')

    def test_off_when_the_flag_is_absent_or_falsy(self):
        for env in ({}, {'KC_LIBREFANG_ENABLED': ''},
                    {'KC_LIBREFANG_ENABLED': 'false'},
                    {'KC_LIBREFANG_ENABLED': '0'}):
            with self.subTest(env=env):
                self.assertFalse(runtimes.is_enabled(LF, env=env))

    def test_on_for_the_usual_truthy_spellings(self):
        for value in ('true', 'True', '1', 'yes', 'on', ' true '):
            with self.subTest(value=value):
                self.assertTrue(runtimes.is_enabled(
                    LF, env={'KC_LIBREFANG_ENABLED': value}))

    def test_every_other_runtime_is_unconditional(self):
        # Opt-in is the exception, not a new gate on the catalog: nothing that
        # used to be offered may quietly need a flag it has no value for.
        for rid in runtimes.RUNTIMES:
            if rid == LF:
                continue
            with self.subTest(runtime=rid):
                self.assertNotIn('opt_in_env', runtimes.RUNTIMES[rid])
                self.assertTrue(runtimes.is_enabled(rid, env={}))

    def test_the_command_templates_are_untouched(self):
        # The flag decides whether the runtime is OFFERED. Rendering a command
        # for it stays a pure function of the catalog — the matrix test in
        # tests/runtime_command_matrix_test.py owns those bytes.
        self.assertEqual(runtimes.RUNTIMES[LF]['launch_args'],
                         ['librefang', 'chat', runtimes.ARG])


class OrchestratorOptionsTest(unittest.TestCase):
    """spawn_agent must not offer — or accept — a runtime that is off."""

    def _reloaded(self, env):
        """The orchestrator as it would be imported under `env`.

        Both the assistant list and the tool's enum are computed at import,
        which is once per MCP-server launch, so the flag has to be read there.
        """
        with mock.patch.dict(os.environ, env, clear=False):
            for key in ('KC_LIBREFANG_ENABLED',):
                if key not in env:
                    os.environ.pop(key, None)
            return importlib.reload(orch)

    def tearDown(self):
        # Leave the module as the rest of the suite imported it (flag off).
        self._reloaded({})

    @staticmethod
    def _enum(mod):
        return mod.TOOLS['spawn_agent']['schema']['inputSchema'][
            'properties']['assistant']['enum']

    def test_absent_from_the_options_by_default(self):
        mod = self._reloaded({})
        self.assertNotIn(LF, [a['id'] for a in mod._ASSISTANTS_LIST])
        self.assertNotIn(LF, self._enum(mod))
        # …and nothing else disappeared with it.
        self.assertIn('claude', self._enum(mod))
        self.assertIn('ante', self._enum(mod))

    def test_offered_once_the_workspace_opts_in(self):
        mod = self._reloaded({'KC_LIBREFANG_ENABLED': 'true'})
        self.assertIn(LF, [a['id'] for a in mod._ASSISTANTS_LIST])
        self.assertIn(LF, self._enum(mod))

    def test_the_enum_still_covers_every_listed_assistant(self):
        for env in ({}, {'KC_LIBREFANG_ENABLED': 'true'}):
            with self.subTest(env=env):
                mod = self._reloaded(env)
                self.assertEqual({a['id'] for a in mod._ASSISTANTS_LIST},
                                 set(self._enum(mod)))

    def test_spawning_a_disabled_assistant_is_refused_with_the_reason(self):
        # An MCP client can send any string, so the enum is not the only gate:
        # launching anyway would start a session whose CLI has no config and
        # no daemon, because boot skipped its setup.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('KC_LIBREFANG_ENABLED', None)
            res = orch._tool_spawn_agent({'prompt': 'go', 'assistant': LF})
        self.assertTrue(res.get('isError'))
        text = res['content'][0]['text']
        self.assertIn('not enabled', text)
        # The refusal names what the workspace does offer.
        self.assertIn('claude', text)

    def test_a_disabled_assistant_cannot_slip_in_as_the_workspace_default(self):
        # KC_DEFAULT_ASSISTANT is operator-set and could name a runtime the
        # same operator left off; refuse rather than launch it.
        with mock.patch.dict(os.environ, {'KC_DEFAULT_ASSISTANT': LF},
                             clear=False):
            os.environ.pop('KC_LIBREFANG_ENABLED', None)
            res = orch._tool_spawn_agent({'prompt': 'go'})
        self.assertTrue(res.get('isError'))
        self.assertIn('not enabled', res['content'][0]['text'])


if __name__ == '__main__':
    unittest.main()
