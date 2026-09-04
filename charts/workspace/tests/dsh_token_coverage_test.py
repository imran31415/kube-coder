"""Token-accounting coverage for the DeepSeek Harness (#639, #574).

The acceptance criterion is deliberately either/or: the assistant must
either report spend, or be *correctly marked* `not_instrumented` — what it
must never do is read as a measured zero, which is indistinguishable from
"ran and cost nothing".

It is marked `not_instrumented`, and these tests pin that end to end rather
than just asserting a frozenset. They also pin the reason it is not
promoted, and the probe that would let someone promote it.

Run with:  python3 -m unittest tests.dsh_token_coverage_test  (from charts/workspace/)
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import token_usage as tu  # noqa: E402
import hypervisor_session as hs  # noqa: E402

# Reuse the scripted ACP agent from the bridge suite rather than a second copy.
from acp_bridge_test import STUB_AGENT, _base_scenario, _chunk  # noqa: E402

DSH = 'deepseek-harness'


class CoverageClassificationTest(unittest.TestCase):
    def test_marked_not_instrumented(self):
        self.assertFalse(tu.is_instrumented(DSH))
        self.assertEqual(tu.assistant_coverage(DSH),
                         tu.COVERAGE_NOT_INSTRUMENTED)

    def test_a_zero_is_never_reported_as_measured(self):
        # The distinction the marker exists for: 0 tokens under
        # `not_instrumented` means "unknown", not "free".
        summary = tu.coverage_summary(measured=0, not_instrumented=1)
        self.assertEqual(summary['measured'], 0)
        self.assertEqual(summary['not_instrumented'], 1)

    def test_claude_is_still_the_only_instrumented_assistant(self):
        # A guard on the promotion path: adding an id here without also
        # mapping its usage would reintroduce the silent zero.
        self.assertEqual(tu.INSTRUMENTED_ASSISTANTS, frozenset({'claude'}))

    def test_every_registered_assistant_classifies_without_raising(self):
        import server
        for aid in server.ClaudeTaskManager.ASSISTANTS:
            with self.subTest(assistant=aid):
                self.assertIn(tu.assistant_coverage(aid),
                              (tu.COVERAGE_MEASURED, tu.COVERAGE_NOT_INSTRUMENTED))


class HypervisorRollupTest(unittest.TestCase):
    """A dsh thread must land in the not_instrumented bucket of the fleet
    summary, not silently drag the measured average down."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='dsh-cov-')
        self._orig = hs.HYPERVISOR_DIR
        hs.HYPERVISOR_DIR = self.tmp

    def tearDown(self):
        hs.HYPERVISOR_DIR = self._orig

    def _thread(self, tid, assistant):
        d = os.path.join(self.tmp, tid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'thread.json'), 'w') as f:
            json.dump({'id': tid, 'assistant': assistant, 'title': tid,
                       'created_at': 1, 'updated_at': 1}, f)
        open(os.path.join(d, 'events.jsonl'), 'a').close()

    def test_a_dsh_thread_counts_as_not_instrumented(self):
        self._thread('t-dsh', DSH)
        self._thread('t-claude', 'claude')
        cov = hs.HypervisorSession.product_totals()['tokens']['coverage']
        self.assertEqual(cov['measured'], 1)
        self.assertEqual(cov['not_instrumented'], 1)


class UsageProbeTest(unittest.TestCase):
    """The bridge says, once per turn, whether this harness build reported
    token usage — so promoting it later is a one-run question, not an
    archaeology exercise."""

    def _run(self, prompt_step):
        tmp = tempfile.mkdtemp(prefix='dsh-usage-')
        scenario_path = os.path.join(tmp, 'scenario.json')
        agent_path = os.path.join(tmp, 'stub_agent.py')
        recorded = os.path.join(tmp, 'recorded.jsonl')
        with open(scenario_path, 'w') as f:
            json.dump(_base_scenario(prompt_step), f)
        with open(agent_path, 'w') as f:
            f.write(STUB_AGENT)
        bridge = os.path.join(os.path.dirname(HERE), 'acp_bridge.py')
        env = dict(os.environ)
        env['KC_DSH_ARGV'] = json.dumps(
            [sys.executable, agent_path, scenario_path, recorded])
        env['KC_DSH_HANDSHAKE_TIMEOUT'] = '20'
        return subprocess.run([sys.executable, bridge, '--cwd', tmp],
                              input='go', capture_output=True, text=True,
                              env=env, timeout=60)

    def test_reports_absence_explicitly(self):
        p = self._run({'emit': [_chunk('ok')],
                       'result': {'stopReason': 'end_turn'}})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('turn reported no token usage', p.stderr)
        self.assertIn('not_instrumented', p.stderr)

    def test_reports_presence_with_the_figures(self):
        p = self._run({'emit': [_chunk('ok')],
                       'result': {'stopReason': 'end_turn',
                                  'usage': {'totalTokens': 1200,
                                            'inputTokens': 1000,
                                            'outputTokens': 200}}})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('turn reported token usage', p.stderr)
        self.assertIn('"inputTokens": 1000', p.stderr)

    def test_the_probe_does_not_leak_into_the_event_stream(self):
        # Diagnostics belong on stderr; stdout is the event stream the adapter
        # parses and nothing else.
        p = self._run({'emit': [_chunk('ok')],
                       'result': {'stopReason': 'end_turn',
                                  'usage': {'totalTokens': 5}}})
        events = [json.loads(x) for x in p.stdout.splitlines() if x.strip()]
        self.assertEqual([e['type'] for e in events],
                         ['session', 'message', 'done'])
        self.assertNotIn('usage', {k for e in events for k in e})

    def test_a_malformed_usage_field_is_treated_as_absent(self):
        p = self._run({'emit': [_chunk('ok')],
                       'result': {'stopReason': 'end_turn', 'usage': 'lots'}})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('turn reported no token usage', p.stderr)


if __name__ == '__main__':
    unittest.main()
