"""Unit tests for harness.py — the kc-harness tool layer + XML tool-call parser.

NOTE: charts/workspace/harness_test.py is a *live* end-to-end driver (it talks
to a real model endpoint) and is intentionally not under tests/. This file is
the pure-unit complement that CI's `discover -s tests` actually runs: it
exercises the tool implementations, the dispatch table, the env pickers, the
string caps, and the XML/JSON tool-call fallback parser — none of which need a
model or network.

Run with:    python3 -m unittest tests.harness_tools_test
(from charts/workspace/)
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import harness  # noqa: E402


# ───────────────────────── env pickers ─────────────────────────

class PickEnvTests(unittest.TestCase):
    def test_pick_model_precedence(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in ('KC_HARNESS_MODEL', 'KC_FALLBACK_MODEL'):
                os.environ.pop(k, None)
            self.assertEqual(harness.pick_model(), harness.DEFAULT_MODEL)
            os.environ['KC_FALLBACK_MODEL'] = 'fallback'
            self.assertEqual(harness.pick_model(), 'fallback')
            os.environ['KC_HARNESS_MODEL'] = 'override'
            self.assertEqual(harness.pick_model(), 'override')

    def test_pick_base_url_default_and_override(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in ('KC_HARNESS_BASE_URL', 'KC_FALLBACK_BASE_URL'):
                os.environ.pop(k, None)
            self.assertEqual(harness.pick_base_url(), 'http://localhost:11434/v1')
            os.environ['KC_HARNESS_BASE_URL'] = 'http://x/v1'
            self.assertEqual(harness.pick_base_url(), 'http://x/v1')

    def test_pick_api_key_default_empty(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in ('KC_HARNESS_API_KEY', 'KC_FALLBACK_API_KEY'):
                os.environ.pop(k, None)
            self.assertEqual(harness.pick_api_key(), '')


# ───────────────────────── string caps ─────────────────────────

class StringCapTests(unittest.TestCase):
    def test_short_passthrough(self):
        self.assertEqual(harness._short('hi'), 'hi')

    def test_short_strips_trailing_newlines(self):
        self.assertEqual(harness._short('hi\n\n'), 'hi')

    def test_short_truncates_with_byte_suffix(self):
        out = harness._short('x' * 50, limit=10)
        self.assertTrue(out.startswith('x' * 10))
        self.assertIn('+40b', out)

    def test_truncate_passthrough_and_cap(self):
        self.assertEqual(harness._truncate('small'), 'small')
        with mock.patch.object(harness, 'TOOL_OUTPUT_CAP', 100):
            out = harness._truncate('y' * 500)
        self.assertIn('truncated', out)
        self.assertLess(len(out), 500)


# ───────────────────────── tool implementations ─────────────────────────

class ToolFsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Run bash in the tempdir so cwd-relative commands are isolated.
        p = mock.patch.object(harness, 'WORKDIR', self.tmp)
        p.start()
        self.addCleanup(p.stop)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)


class ToolBashTests(ToolFsTestCase):
    def test_requires_cmd(self):
        self.assertTrue(harness.tool_bash({}).startswith('ERROR'))

    def test_runs_and_returns_stdout(self):
        self.assertEqual(harness.tool_bash({'cmd': 'echo hello'}).strip(), 'hello')

    def test_command_alias_accepted(self):
        self.assertEqual(harness.tool_bash({'command': 'echo hi'}).strip(), 'hi')

    def test_stderr_and_nonzero_exit_annotated(self):
        out = harness.tool_bash({'cmd': 'echo oops >&2; exit 3'})
        self.assertIn('[stderr]', out)
        self.assertIn('[exit 3]', out)

    def test_timeout_returns_error(self):
        out = harness.tool_bash({'cmd': 'sleep 5', 'timeout': 1})
        self.assertIn('timeout', out)


class ToolFileTests(ToolFsTestCase):
    def test_read_requires_path(self):
        self.assertTrue(harness.tool_read_file({}).startswith('ERROR'))

    def test_read_missing_file(self):
        self.assertIn('not found', harness.tool_read_file({'path': self.path('nope')}))

    def test_write_then_read_round_trip(self):
        res = harness.tool_write_file({'path': self.path('a/b.txt'), 'content': 'data'})
        self.assertTrue(res.startswith('OK'))
        self.assertEqual(harness.tool_read_file({'path': self.path('a/b.txt')}), 'data')

    def test_write_requires_path(self):
        self.assertTrue(harness.tool_write_file({'content': 'x'}).startswith('ERROR'))

    def test_list_dir_tags_and_missing(self):
        os.mkdir(self.path('sub'))
        harness.tool_write_file({'path': self.path('f.txt'), 'content': 'x'})
        out = harness.tool_list_dir({'path': self.tmp})
        self.assertIn('d sub', out)
        self.assertIn('- f.txt', out)
        self.assertIn('not found', harness.tool_list_dir({'path': self.path('ghost')}))

    def test_list_dir_empty(self):
        os.mkdir(self.path('empty'))
        self.assertEqual(harness.tool_list_dir({'path': self.path('empty')}), '(empty)')


class ToolEditTests(ToolFsTestCase):
    def _file(self, content):
        p = self.path('e.txt')
        harness.tool_write_file({'path': p, 'content': content})
        return p

    def test_requires_path_and_find(self):
        self.assertTrue(harness.tool_edit_file({'path': 'x'}).startswith('ERROR'))

    def test_missing_file(self):
        self.assertIn('not found',
                      harness.tool_edit_file({'path': self.path('no'), 'find': 'a', 'replace': 'b'}))

    def test_zero_matches(self):
        p = self._file('hello world')
        self.assertIn('not present',
                      harness.tool_edit_file({'path': p, 'find': 'xyz', 'replace': 'b'}))

    def test_multiple_matches_rejected(self):
        p = self._file('a a a')
        self.assertIn('occurs 3 times',
                      harness.tool_edit_file({'path': p, 'find': 'a', 'replace': 'b'}))

    def test_single_match_replaced(self):
        p = self._file('hello world')
        res = harness.tool_edit_file({'path': p, 'find': 'world', 'replace': 'there'})
        self.assertTrue(res.startswith('OK'))
        self.assertEqual(harness.tool_read_file({'path': p}), 'hello there')


# ───────────────────────── dispatch + schema ─────────────────────────

class DispatchTests(unittest.TestCase):
    def test_tools_schema_lists_functions(self):
        schema = harness.tools_schema()
        names = {d['function']['name'] for d in schema}
        self.assertIn('bash', names)
        self.assertIn('edit_file', names)

    def test_execute_unknown_tool(self):
        self.assertIn('unknown tool', harness.execute_tool('nope', {}))

    def test_execute_dispatches(self):
        self.assertTrue(harness.execute_tool('bash', {'cmd': 'echo ok'}).strip() == 'ok')

    def test_execute_wraps_tool_exception(self):
        with mock.patch.dict(harness.TOOLS,
                             {'boom': (mock.Mock(side_effect=RuntimeError('x')), {})}):
            self.assertIn('raised RuntimeError', harness.execute_tool('boom', {}))


# ───────────────────────── arg coercion + XML parse ─────────────────────────

class CoerceArgTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(harness._coerce_arg('  '), '')

    def test_int_and_float(self):
        self.assertEqual(harness._coerce_arg('42'), 42)
        self.assertEqual(harness._coerce_arg('-3.5'), -3.5)

    def test_bool_and_null(self):
        self.assertIs(harness._coerce_arg('true'), True)
        self.assertIs(harness._coerce_arg('False'), False)
        self.assertIsNone(harness._coerce_arg('null'))
        self.assertIsNone(harness._coerce_arg('none'))

    def test_json_object_and_array(self):
        self.assertEqual(harness._coerce_arg('{"a": 1}'), {'a': 1})
        self.assertEqual(harness._coerce_arg('[1, 2]'), [1, 2])

    def test_invalid_json_object_stays_string(self):
        self.assertEqual(harness._coerce_arg('{not json}'), '{not json}')

    def test_plain_string(self):
        self.assertEqual(harness._coerce_arg('ls -la'), 'ls -la')


class ParseXmlToolCallsTests(unittest.TestCase):
    def test_hermes_json_form(self):
        content = '<tool_call>{"name": "bash", "arguments": {"cmd": "ls"}}</tool_call>'
        calls = harness.parse_xml_tool_calls(content)
        self.assertEqual(calls, [{'name': 'bash', 'arguments': {'cmd': 'ls'}}])

    def test_hermes_string_arguments_decoded(self):
        content = '<tool_call>{"name": "bash", "arguments": "{\\"cmd\\": \\"ls\\"}"}</tool_call>'
        calls = harness.parse_xml_tool_calls(content)
        self.assertEqual(calls[0]['arguments'], {'cmd': 'ls'})

    def test_function_parameter_form(self):
        content = ('<function=write_file>'
                   '<parameter=path>/tmp/x</parameter>'
                   '<parameter=content>hi</parameter>'
                   '</function>')
        calls = harness.parse_xml_tool_calls(content)
        self.assertEqual(calls[0]['name'], 'write_file')
        self.assertEqual(calls[0]['arguments'], {'path': '/tmp/x', 'content': 'hi'})

    def test_bash_bare_body_becomes_cmd(self):
        content = '<function=bash>ls -la</function>'
        calls = harness.parse_xml_tool_calls(content)
        self.assertEqual(calls[0]['arguments'], {'cmd': 'ls -la'})

    def test_no_tool_calls(self):
        self.assertEqual(harness.parse_xml_tool_calls('just prose, no tools'), [])

    def test_multiple_calls_collected(self):
        content = ('<function=bash><parameter=cmd>ls</parameter></function>'
                   '<function=bash><parameter=cmd>pwd</parameter></function>')
        self.assertEqual(len(harness.parse_xml_tool_calls(content)), 2)


# ───────────────────────── event emitters (stdout framing) ─────────────────────────

class EmitTests(unittest.TestCase):
    def _capture(self, fn, *a):
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn(*a)
        return buf.getvalue()

    def test_emit_event_writes_jsonl(self):
        out = self._capture(harness.emit_event, {'type': 'result', 'result': 'x'})
        self.assertEqual(json.loads(out.strip()), {'type': 'result', 'result': 'x'})

    def test_emit_tool_use_emits_structured_first_line(self):
        out = self._capture(harness.emit_tool_use, 'bash', {'cmd': 'ls'})
        first = out.splitlines()[0]
        evt = json.loads(first)
        self.assertEqual(evt['message']['content'][0]['name'], 'bash')

    def test_emit_tool_result_marks_error(self):
        out = self._capture(harness.emit_tool_result, 'bash', 'ERROR: boom')
        # First line is the JSONL event; a pretty line follows.
        self.assertIn('ERROR: boom', out)


if __name__ == '__main__':
    unittest.main()
