"""Unit tests for hypervisor observability (issue: hypervisor activity/logs).

Covers:
  * build_activity() — the pure events.jsonl -> activity-timeline serializer:
    tool_call/tool_result pairing, durations, ok/error status, pending and
    orphan cases, error/status entries, counts, seq ordering, empty input.
  * HypervisorSession runner.log — the bounded stderr/diagnostics capture:
    append + size cap (tail retained, head dropped, marker added), the
    read_runner_log tail, and best-effort no-raise behavior.

Run with:    python3 -m unittest tests.hypervisor_activity_test
(from charts/workspace/)
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import hypervisor_session as hv  # noqa: E402

build_activity = hv.build_activity


def _ev(seq, typ, ts=None, **kw):
    """Build one canonical event; ts defaults to seq so durations are stable."""
    e = {'seq': seq, 'type': typ, 'ts': float(seq) if ts is None else ts}
    e.update(kw)
    return e


class BuildActivityTests(unittest.TestCase):
    def test_empty(self):
        r = build_activity([])
        self.assertEqual(r['timeline'], [])
        self.assertEqual(r['counts'], {
            'tool_calls': 0, 'tool_results': 0, 'tool_errors': 0,
            'errors': 0, 'messages': 0, 'builds': 0, 'subagents': 0})

    def test_tool_call_paired_with_ok_result_and_duration(self):
        events = [
            _ev(1, 'message', role='user', text='hi'),
            _ev(2, 'tool_call', ts=100.0, tool={'name': 'get_metrics', 'input': {'a': 1}}, tool_id='t1'),
            _ev(3, 'tool_result', ts=100.4, tool_use_id='t1', text='cpu 12%', is_error=False),
        ]
        r = build_activity(events)
        self.assertEqual(len(r['timeline']), 1)
        entry = r['timeline'][0]
        self.assertEqual(entry['kind'], 'tool')
        self.assertEqual(entry['tool'], 'get_metrics')
        self.assertEqual(entry['input'], {'a': 1})
        self.assertEqual(entry['status'], 'ok')
        self.assertEqual(entry['result_text'], 'cpu 12%')
        self.assertEqual(entry['result_seq'], 3)
        self.assertEqual(entry['duration_ms'], 400)
        self.assertEqual(r['counts']['tool_calls'], 1)
        self.assertEqual(r['counts']['tool_results'], 1)
        self.assertEqual(r['counts']['messages'], 1)

    def test_tool_error_result(self):
        events = [
            _ev(1, 'tool_call', tool={'name': 'kill_task', 'input': {}}, tool_id='t1'),
            _ev(2, 'tool_result', tool_use_id='t1', text='boom', is_error=True),
        ]
        r = build_activity(events)
        self.assertEqual(r['timeline'][0]['status'], 'error')
        self.assertEqual(r['counts']['tool_errors'], 1)

    def test_pending_tool_call_without_result(self):
        events = [_ev(1, 'tool_call', tool={'name': 'x', 'input': {}}, tool_id='t1')]
        r = build_activity(events)
        self.assertEqual(r['timeline'][0]['status'], 'pending')
        self.assertIsNone(r['timeline'][0]['duration_ms'])

    def test_orphan_result_kept(self):
        events = [_ev(1, 'tool_result', tool_use_id='ghost', text='late', is_error=False)]
        r = build_activity(events)
        self.assertEqual(len(r['timeline']), 1)
        self.assertEqual(r['timeline'][0]['kind'], 'tool_result_orphan')
        self.assertEqual(r['timeline'][0]['tool_use_id'], 'ghost')
        self.assertEqual(r['counts']['tool_results'], 1)

    def test_interleaved_calls_matched_by_id(self):
        events = [
            _ev(1, 'tool_call', ts=1.0, tool={'name': 'a', 'input': {}}, tool_id='t1'),
            _ev(2, 'tool_call', ts=2.0, tool={'name': 'b', 'input': {}}, tool_id='t2'),
            _ev(3, 'tool_result', ts=2.5, tool_use_id='t2', text='B', is_error=False),
            _ev(4, 'tool_result', ts=5.0, tool_use_id='t1', text='A', is_error=False),
        ]
        r = build_activity(events)
        by_tool = {e['tool']: e for e in r['timeline'] if e['kind'] == 'tool'}
        self.assertEqual(by_tool['a']['result_text'], 'A')
        self.assertEqual(by_tool['a']['duration_ms'], 4000)
        self.assertEqual(by_tool['b']['result_text'], 'B')
        self.assertEqual(by_tool['b']['duration_ms'], 500)

    def test_error_and_status_entries(self):
        events = [
            _ev(1, 'status', status='running'),
            _ev(2, 'error', text='provider 500'),
            _ev(3, 'status', status='idle'),
        ]
        r = build_activity(events)
        kinds = [e['kind'] for e in r['timeline']]
        self.assertEqual(kinds, ['status', 'error', 'status'])
        self.assertEqual(r['timeline'][0]['status'], 'running')
        self.assertEqual(r['timeline'][1]['text'], 'provider 500')
        self.assertEqual(r['counts']['errors'], 1)

    def test_out_of_order_input_is_sorted(self):
        events = [
            _ev(3, 'tool_result', tool_use_id='t1', text='done', is_error=False),
            _ev(1, 'tool_call', tool={'name': 'a', 'input': {}}, tool_id='t1'),
            _ev(2, 'message', text='working'),
        ]
        r = build_activity(events)
        # Despite the result arriving first in the list, it pairs to the call.
        self.assertEqual(len(r['timeline']), 1)
        self.assertEqual(r['timeline'][0]['status'], 'ok')
        self.assertEqual(r['timeline'][0]['result_text'], 'done')

    def test_missing_ts_leaves_duration_none(self):
        events = [
            {'seq': 1, 'type': 'tool_call', 'tool': {'name': 'a', 'input': {}}, 'tool_id': 't1'},
            {'seq': 2, 'type': 'tool_result', 'tool_use_id': 't1', 'text': 'x', 'is_error': False},
        ]
        r = build_activity(events)
        self.assertEqual(r['timeline'][0]['status'], 'ok')
        self.assertIsNone(r['timeline'][0]['duration_ms'])

    def test_choice_and_unknown_types_ignored_in_timeline(self):
        events = [
            _ev(1, 'choice', options=['a', 'b'], question='pick'),
            _ev(2, 'weird'),
        ]
        r = build_activity(events)
        self.assertEqual(r['timeline'], [])


class ToolClassificationTests(unittest.TestCase):
    """Tier A: build_activity classifies tool calls into semantic categories and
    lifts the identifiers the UI needs (created task_id, sub-agent info), so the
    activity view can call out sub-builds / sub-agents as first-class entries."""

    def test_classify_and_base_name(self):
        self.assertEqual(hv._tool_base_name('mcp__dashboard__create_task'), 'create_task')
        self.assertEqual(hv._tool_base_name('Bash'), 'Bash')
        self.assertEqual(hv._classify_tool('mcp__dashboard__create_task'), 'build')
        self.assertEqual(hv._classify_tool('Task'), 'subagent')
        self.assertEqual(hv._classify_tool('mcp__dashboard__pin_app'), 'app')
        self.assertEqual(hv._classify_tool('mcp__memory__add_memory'), 'memory')
        self.assertEqual(hv._classify_tool('mcp__dashboard__kill_task'), 'task')
        self.assertEqual(hv._classify_tool('Bash'), 'tool')
        self.assertEqual(hv._classify_tool(None), 'tool')

    def test_extract_task_id(self):
        self.assertEqual(hv._extract_task_id('{"task_id": "kube-coder-abc", "status": "queued"}'), 'kube-coder-abc')
        # Pretty-printed / surrounded by prose still works via the regex fallback.
        self.assertEqual(hv._extract_task_id('Created task.\n{\n  "task_id": "t-9"\n}'), 't-9')
        self.assertIsNone(hv._extract_task_id('no id here'))
        self.assertIsNone(hv._extract_task_id(None))

    def test_build_call_gets_category_and_task_id(self):
        events = [
            _ev(1, 'tool_call', tool={'name': 'mcp__dashboard__create_task', 'input': {'prompt': 'run tests'}}, tool_id='b1'),
            _ev(2, 'tool_result', tool_use_id='b1', text='{"task_id": "kube-coder-xyz"}', is_error=False),
        ]
        r = build_activity(events)
        entry = r['timeline'][0]
        self.assertEqual(entry['category'], 'build')
        self.assertEqual(entry['label'], 'create_task')
        self.assertEqual(entry['task_id'], 'kube-coder-xyz')
        self.assertEqual(r['counts']['builds'], 1)

    def test_failed_build_has_no_task_id(self):
        events = [
            _ev(1, 'tool_call', tool={'name': 'mcp__dashboard__create_task', 'input': {}}, tool_id='b1'),
            _ev(2, 'tool_result', tool_use_id='b1', text='error: prompt is required', is_error=True),
        ]
        r = build_activity(events)
        self.assertEqual(r['timeline'][0]['category'], 'build')
        self.assertIsNone(r['timeline'][0]['task_id'])
        self.assertEqual(r['counts']['builds'], 1)

    def test_subagent_call_lifts_type_and_description(self):
        events = [
            _ev(1, 'tool_call', tool={'name': 'Task', 'input': {
                'subagent_type': 'Explore', 'description': 'map the codebase'}}, tool_id='s1'),
        ]
        r = build_activity(events)
        entry = r['timeline'][0]
        self.assertEqual(entry['category'], 'subagent')
        self.assertEqual(entry['subagent_type'], 'Explore')
        self.assertEqual(entry['description'], 'map the codebase')
        self.assertEqual(r['counts']['subagents'], 1)

    def test_generic_tool_is_category_tool(self):
        events = [_ev(1, 'tool_call', tool={'name': 'Bash', 'input': {'command': 'ls'}}, tool_id='x1')]
        r = build_activity(events)
        self.assertEqual(r['timeline'][0]['category'], 'tool')
        self.assertEqual(r['timeline'][0]['label'], 'Bash')
        self.assertEqual(r['counts']['builds'], 0)
        self.assertEqual(r['counts']['subagents'], 0)


class RunnerLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_dir = hv.HYPERVISOR_DIR
        self._orig_cap = hv.RUNNER_LOG_MAX_BYTES
        hv.HYPERVISOR_DIR = self.tmp
        self.session = hv.HypervisorSession('thread-1')
        os.makedirs(self.session.dir, exist_ok=True)

    def tearDown(self):
        hv.HYPERVISOR_DIR = self._orig_dir
        hv.RUNNER_LOG_MAX_BYTES = self._orig_cap
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_append_and_read_below_cap(self):
        self.session.append_runner_log('first line')
        self.session.append_runner_log('second line')
        out = self.session.read_runner_log()
        self.assertIn('first line', out)
        self.assertIn('second line', out)
        self.assertNotIn('truncated', out)

    def test_append_empty_is_noop(self):
        self.session.append_runner_log('')
        self.assertFalse(os.path.exists(self.session.runner_log_path))

    def test_cap_keeps_tail_drops_head_and_bounds_size(self):
        hv.RUNNER_LOG_MAX_BYTES = 2048
        # Write well past the cap with identifiable ordered lines.
        for i in range(800):
            self.session.append_runner_log(f'line-{i:05d} ' + 'x' * 20)
        size = os.path.getsize(self.session.runner_log_path)
        # Bounded: the 1.5x hysteresis means it never exceeds ~1.5x the cap
        # (plus one overshooting line before the trim fires).
        self.assertLessEqual(size, int(hv.RUNNER_LOG_MAX_BYTES * 1.5) + 128)
        with open(self.session.runner_log_path, encoding='utf-8') as f:
            raw = f.read()
        self.assertIn('truncated', raw)          # marker present
        self.assertIn('line-00799', raw)         # newest retained
        self.assertNotIn('line-00000', raw)      # oldest dropped

    def test_cap_trims_are_amortized_not_per_append(self):
        # Regression guard for the O(n^2) trap: with hysteresis, appending many
        # lines past the cap must stay fast (each trim covers ~0.5x the cap of
        # new data, not every line).
        hv.RUNNER_LOG_MAX_BYTES = 4096
        import time as _t
        start = _t.time()
        for i in range(3000):
            self.session.append_runner_log(f'x-{i:05d} ' + 'y' * 30)
        elapsed = _t.time() - start
        self.assertLess(elapsed, 5.0)
        self.assertLessEqual(
            os.path.getsize(self.session.runner_log_path),
            int(hv.RUNNER_LOG_MAX_BYTES * 1.5) + 128)

    def test_read_runner_log_tail_trims_partial_leading_line(self):
        for i in range(500):
            self.session.append_runner_log(f'entry-{i:04d}')
        out = self.session.read_runner_log(tail_bytes=200)
        self.assertLessEqual(len(out.encode('utf-8')), 200)
        # Starts on a clean line boundary (no half-line at the front).
        first = out.splitlines()[0]
        self.assertTrue(first.startswith('entry-') or first.startswith('['))
        self.assertIn('entry-0499', out)         # newest present

    def test_read_missing_log_returns_empty(self):
        self.assertEqual(self.session.read_runner_log(), '')

    def test_append_on_missing_dir_does_not_raise(self):
        s = hv.HypervisorSession('never-created')  # dir does not exist
        try:
            s.append_runner_log('should not raise')
        except Exception as e:  # noqa: BLE001
            self.fail(f'append_runner_log raised: {e}')


class StderrDrainDeadlockTests(unittest.TestCase):
    """The core reason this feature exists: the streaming path consumes stdout
    and never reads stderr, so a CLI that floods stderr past the OS pipe buffer
    (~64 KB) blocks on write while we wait on stdout that never comes — a real
    deadlock that looked like a 'hung chat'. _drain_stderr must prevent it AND
    persist what the child wrote."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_dir = hv.HYPERVISOR_DIR
        hv.HYPERVISOR_DIR = self.tmp
        self.session = hv.HypervisorSession('drain-thread')
        os.makedirs(self.session.dir, exist_ok=True)

    def tearDown(self):
        hv.HYPERVISOR_DIR = self._orig_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_large_stderr_does_not_deadlock_and_is_captured(self):
        # Child writes 200 KB to stderr (well past the pipe buffer) BEFORE its
        # stdout line — the exact ordering that deadlocks an undrained reader.
        child = (
            "import sys\n"
            "sys.stderr.write('E' * 200000)\n"
            "sys.stderr.flush()\n"
            "sys.stdout.write('DONE\\n')\n"
            "sys.stdout.flush()\n"
        )
        result = {}
        done = threading.Event()

        def run():
            proc = subprocess.Popen(
                [sys.executable, '-c', child],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1)
            # Mirror _run_turn's streaming path: drain stderr concurrently,
            # consume stdout in the main loop.
            t = threading.Thread(target=self.session._drain_stderr,
                                 args=(proc,), daemon=True)
            t.start()
            out = ''.join(list(proc.stdout))
            proc.wait()
            t.join(timeout=5)
            result['out'] = out
            done.set()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        finished = done.wait(timeout=20)
        self.assertTrue(finished,
                        'streaming read + stderr drain deadlocked')
        self.assertIn('DONE', result['out'])
        # The subprocess stderr was persisted to runner.log for debuggability.
        self.assertGreater(os.path.getsize(self.session.runner_log_path), 1000)


class EndToEndActivityTests(unittest.TestCase):
    """Exercise the full read path the /activity endpoint depends on — real
    events.jsonl round-trip via _append(), then the exact composition the
    handler does (build_activity + summary + runner-log tail). Covers the
    endpoint's logic without needing to import fcntl-bound server.py."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_dir = hv.HYPERVISOR_DIR
        hv.HYPERVISOR_DIR = self.tmp
        self.s = hv.HypervisorSession('e2e')
        os.makedirs(self.s.dir, exist_ok=True)
        open(self.s.events_path, 'a').close()
        self.s._write_meta({'id': 'e2e', 'title': 'Metrics check', 'status': 'idle'})

    def tearDown(self):
        hv.HYPERVISOR_DIR = self._orig_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_read_path_events_to_activity(self):
        self.s._append([{'role': 'user', 'type': 'message', 'text': 'check metrics'}])
        self.s._append([{'role': 'assistant', 'type': 'tool_call',
                         'tool': {'name': 'get_metrics', 'input': {}}, 'tool_id': 'c1'}])
        self.s._append([{'role': 'assistant', 'type': 'tool_result',
                         'tool_use_id': 'c1', 'text': 'cpu 5%', 'is_error': False}])
        self.s._append([{'role': 'system', 'type': 'error', 'text': 'transient blip'}])
        self.s.append_runner_log('claude: connected MCP server dashboard')

        # Exactly what handle_hypervisor_get_activity composes.
        activity = hv.build_activity(self.s.read_events())
        activity['thread'] = self.s.summary()
        activity['runner_log'] = self.s.read_runner_log()

        self.assertEqual(activity['counts']['tool_calls'], 1)
        self.assertEqual(activity['counts']['messages'], 1)
        self.assertEqual(activity['counts']['errors'], 1)
        tool = next(e for e in activity['timeline'] if e['kind'] == 'tool')
        self.assertEqual(tool['tool'], 'get_metrics')
        self.assertEqual(tool['status'], 'ok')
        self.assertEqual(tool['result_text'], 'cpu 5%')
        self.assertIsNotNone(tool['duration_ms'])
        self.assertIn('dashboard', activity['runner_log'])
        self.assertEqual(activity['thread']['id'], 'e2e')


class HealthSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_dir = hv.HYPERVISOR_DIR
        hv.HYPERVISOR_DIR = self.tmp

    def tearDown(self):
        hv.HYPERVISOR_DIR = self._orig_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_health_empty(self):
        h = hv.hypervisor_health()
        self.assertEqual(h['running_count'], 0)
        self.assertEqual(h['subprocess_count'], 0)
        self.assertEqual(h['threads'], [])


if __name__ == '__main__':
    unittest.main()
