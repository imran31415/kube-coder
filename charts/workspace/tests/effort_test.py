"""Unit tests for the cross-assistant reasoning-effort selector (#362).

Covers the server-side registry (ClaudeTaskManager), the per-adapter native
translation + injection (hypervisor_session), the per-thread persistence, and
the cross-module cap-table lockstep.

Run with:   python3 -m unittest tests.effort_test   (from charts/workspace/)
"""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import hypervisor_session as hv  # noqa: E402
import server  # noqa: E402

CTM = server.ClaudeTaskManager


class RegistryTests(unittest.TestCase):
    def tearDown(self):
        for k in ('KC_CLAUDE_EFFORT', 'KC_CODEX_EFFORT', 'KC_HARNESS_EFFORT'):
            os.environ.pop(k, None)

    def test_available_efforts_full_axis_for_supported(self):
        for a in ('claude', 'codex', 'kc-harness'):
            self.assertEqual(CTM.available_efforts(a),
                             ['low', 'medium', 'high', 'xhigh', 'max'])

    def test_available_efforts_empty_hides_selector(self):
        for a in ('opencode-openrouter', 'opencode-deepseek', 'ante',
                  'librefang', 'antigravity', 'unknown'):
            self.assertEqual(CTM.available_efforts(a), [])

    def test_default_effort_is_high(self):
        self.assertEqual(CTM.default_effort('claude'), 'high')
        self.assertEqual(CTM.default_effort('librefang'), '')

    def test_default_effort_env_override_validated(self):
        os.environ['KC_CODEX_EFFORT'] = 'xhigh'
        self.assertEqual(CTM.default_effort('codex'), 'xhigh')
        os.environ['KC_CODEX_EFFORT'] = 'BOGUS'
        self.assertEqual(CTM.default_effort('codex'), 'high')  # invalid → default

    def test_resolve_effort_validates_and_defaults(self):
        self.assertEqual(CTM.resolve_effort('claude', 'xhigh'), 'xhigh')
        self.assertEqual(CTM.resolve_effort('claude', 'MAX'), 'max')  # case-insensitive
        self.assertEqual(CTM.resolve_effort('claude', 'nope'), 'high')
        self.assertEqual(CTM.resolve_effort('claude', ''), 'high')
        self.assertEqual(CTM.resolve_effort('librefang', 'max'), '')

    def test_resolve_native_effort_clamps(self):
        self.assertEqual(CTM.resolve_native_effort('claude', 'max'), 'xhigh')
        self.assertEqual(CTM.resolve_native_effort('codex', 'max'), 'xhigh')
        self.assertEqual(CTM.resolve_native_effort('kc-harness', 'xhigh'), 'high')
        self.assertEqual(CTM.resolve_native_effort('kc-harness', 'max'), 'high')
        self.assertEqual(CTM.resolve_native_effort('claude', 'medium'), 'medium')
        self.assertEqual(CTM.resolve_native_effort('ante', 'high'), '')

    def test_effort_cap(self):
        self.assertEqual(CTM.effort_cap('claude'), 'xhigh')
        self.assertEqual(CTM.effort_cap('kc-harness'), 'high')
        self.assertEqual(CTM.effort_cap('ante'), '')


class NativeClampTests(unittest.TestCase):
    def test_native_effort_matches_server_for_valid_levels(self):
        # For any concrete canonical level, the adapter-side clamp and the
        # server-side native resolver must agree.
        for a in ('claude', 'codex', 'kc-harness', 'ante', 'librefang'):
            for lvl in hv.EFFORT_LEVELS:
                self.assertEqual(hv.native_effort(a, lvl),
                                 CTM.resolve_native_effort(a, lvl),
                                 msg=f'{a}/{lvl}')

    def test_native_effort_empty_for_unknown_or_blank(self):
        self.assertEqual(hv.native_effort('claude', ''), '')
        self.assertEqual(hv.native_effort('claude', 'bogus'), '')
        self.assertEqual(hv.native_effort('librefang', 'high'), '')

    def test_cap_table_lockstep(self):
        # The two modules carry independent copies (server imports hv, not the
        # reverse); they must never drift.
        self.assertEqual(CTM._EFFORT_CAP, hv.EFFORT_CAP)
        self.assertEqual(tuple(CTM._EFFORT_LEVELS), tuple(hv.EFFORT_LEVELS))


class AdapterInjectionTests(unittest.TestCase):
    def test_claude_sets_env_clamped(self):
        spec = hv._adapter_for('claude').build({'effort': 'max', 'workdir': '/w'}, 'hi', True)
        self.assertEqual(spec['env'].get('CLAUDE_CODE_EFFORT_LEVEL'), 'xhigh')

    def test_claude_omits_env_when_unset(self):
        spec = hv._adapter_for('claude').build({'workdir': '/w'}, 'hi', True)
        self.assertNotIn('CLAUDE_CODE_EFFORT_LEVEL', spec['env'])

    def test_codex_adds_config_flag(self):
        opts = hv._adapter_for('codex')._opts({'effort': 'high'})
        self.assertIn('-c', opts)
        self.assertTrue(any('model_reasoning_effort="high"' in o for o in opts))

    def test_codex_omits_flag_when_unset(self):
        opts = hv._adapter_for('codex')._opts({})
        self.assertFalse(any('model_reasoning_effort' in o for o in opts))

    def test_harness_sets_env_clamped(self):
        spec = hv._adapter_for('kc-harness').build(
            {'assistant': 'kc-harness', 'effort': 'xhigh', 'cli_cmd': 'x'}, 'hi', True)
        self.assertEqual(spec['env'].get('KC_EFFORT'), 'high')

    def test_fallback_knobless_assistant_injects_nothing(self):
        spec = hv._adapter_for('librefang').build(
            {'assistant': 'librefang', 'effort': 'max', 'cli_cmd': 'x'}, 'hi', True)
        self.assertNotIn('KC_EFFORT', spec['env'])


class PerThreadPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig = hv.HYPERVISOR_DIR
        hv.HYPERVISOR_DIR = self._tmp.name

    def tearDown(self):
        hv.HYPERVISOR_DIR = self._orig

    def test_create_stores_effort_and_summary_exposes_it(self):
        s = hv.HypervisorSession.create(
            assistant='claude', workdir='/w', cli_cmd='claude', effort='xhigh')
        self.assertEqual(s.read_meta()['adapter']['effort'], 'xhigh')
        self.assertEqual(s.summary()['effort'], 'xhigh')

    def test_set_effort_updates_without_reordering(self):
        s = hv.HypervisorSession.create(
            assistant='claude', workdir='/w', cli_cmd='claude', effort='high')
        before = s.read_meta()['updated_at']
        summary = s.set_effort('low')
        self.assertEqual(summary['effort'], 'low')
        self.assertEqual(s.read_meta()['adapter']['effort'], 'low')
        # touch=False: a mid-session effort tweak must not bump updated_at.
        self.assertEqual(s.read_meta()['updated_at'], before)


if __name__ == '__main__':
    unittest.main()
