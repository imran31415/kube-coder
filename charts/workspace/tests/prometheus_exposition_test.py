"""The exposition writer produces something Prometheus will actually accept (#105).

WHY THIS FILE IS PARANOID ABOUT FORMAT. Prometheus parses a scrape as a whole.
An unescaped quote in one label value, a `# TYPE` emitted twice, a missing
trailing newline — any of them makes it discard the ENTIRE response. The
workspace then shows a gap in every graph and no error anywhere, which is the
most expensive way to be wrong. So the format is tested harder than the
numbers: every assertion below goes through `promparse`, a parser written from
the specification rather than from the writer, so a pass is independent
evidence and not the writer agreeing with itself.

`ParserGuardTests` exists because a permissive parser would make every other
test in this file vacuous — it feeds the parser known-bad documents and
requires it to reject them.

Run with:  python3 -m unittest tests.prometheus_exposition_test
(from charts/workspace/)
"""

from __future__ import annotations

import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import prometheus_exposition as prom  # noqa: E402
import promparse  # noqa: E402


class EscapingTests(unittest.TestCase):
    """The three escapes the format defines, and nothing else."""

    def _roundtrip(self, raw):
        """Render `raw` as a label value and read it back through the parser."""
        text = prom.Exposition().gauge(
            'kubecoder_t', 'help', [({'v': raw}, 1)]).render()
        parsed = promparse.parse(text)
        [(labels, _)] = parsed.series('kubecoder_t')
        return labels['v'], text

    def test_double_quote_is_escaped_and_survives_a_roundtrip(self):
        got, text = self._roundtrip('say "hi"')
        self.assertEqual(got, 'say "hi"')
        self.assertIn(r'v="say \"hi\""', text)

    def test_backslash_is_escaped(self):
        got, text = self._roundtrip('back\\slash')
        self.assertEqual(got, 'back\\slash')
        self.assertIn(r'v="back\\slash"', text)

    def test_newline_is_escaped_and_does_not_split_the_line(self):
        got, text = self._roundtrip('two\nlines')
        self.assertEqual(got, 'two\nlines')
        # The whole document is HELP + TYPE + one sample. A raw newline in the
        # value would make four lines and desynchronise the parser.
        self.assertEqual(len(text.rstrip('\n').split('\n')), 3)

    def test_all_three_at_once_in_the_worst_order(self):
        # A backslash immediately before a quote is where a wrong escape order
        # shows up: escaping the quote first would produce \\" — a literal
        # backslash followed by an unquoted quote, i.e. the value ends early.
        raw = 'a\\"b\nc\\\\d"'
        got, _ = self._roundtrip(raw)
        self.assertEqual(got, raw)

    def test_a_value_that_is_only_escapes(self):
        got, _ = self._roundtrip('\\"\n\\')
        self.assertEqual(got, '\\"\n\\')

    def test_help_escapes_backslash_and_newline_but_not_quotes(self):
        text = prom.Exposition().gauge(
            'kubecoder_t', 'a "quoted" help\nwith c:\\path', [({}, 1)]).render()
        parsed = promparse.parse(text)
        # The quote is literal in help text; escaping it would corrupt the
        # rendered string with a stray backslash.
        self.assertEqual(parsed.helps['kubecoder_t'],
                         r'a "quoted" help\nwith c:\\path')
        self.assertEqual(len(text.rstrip('\n').split('\n')), 3)

    def test_control_characters_are_dropped_not_emitted_raw(self):
        # \r and \t-adjacent controls have no escape in this format, so
        # emitting one raw is a parse hazard in a line-oriented document.
        got, text = self._roundtrip('a\rb\x00c\x1fd')
        self.assertEqual(got, 'abcd')
        self.assertNotIn('\r', text)

    def test_tab_is_legal_raw_inside_a_quoted_value(self):
        got, _ = self._roundtrip('a\tb')
        self.assertEqual(got, 'a\tb')

    def test_long_values_are_truncated(self):
        raw = 'x' * (prom.MAX_LABEL_VALUE_LEN * 2)
        got, text = self._roundtrip(raw)
        promparse.parse(text)
        self.assertLess(len(got), len(raw))
        self.assertTrue(got.startswith('x'))

    def test_truncation_cannot_leave_a_dangling_escape(self):
        # Truncating AFTER escaping cuts `\\` in half and leaves a lone
        # backslash at the end of the value — a dangling escape that takes the
        # whole scrape with it. The shape that exposes it is a value whose
        # escape sequence straddles the cut, NOT a value of pure backslashes
        # (those escape to an even length and get cut on a pair boundary).
        for pad in range(prom.MAX_LABEL_VALUE_LEN + 4):
            raw = 'x' * pad + '\\' + 'y' * prom.MAX_LABEL_VALUE_LEN
            _, text = self._roundtrip(raw)
            promparse.parse(text)      # raises on a dangling/invalid escape
        for pad in range(prom.MAX_LABEL_VALUE_LEN + 4):
            raw = 'x' * pad + '"' + 'y' * prom.MAX_LABEL_VALUE_LEN
            _, text = self._roundtrip(raw)
            promparse.parse(text)

    def test_unicode_survives(self):
        got, _ = self._roundtrip('café — 模型')
        self.assertEqual(got, 'café — 模型')


class StructureTests(unittest.TestCase):
    def test_help_and_type_precede_samples_exactly_once(self):
        text = prom.Exposition().gauge(
            'kubecoder_t', 'help', [({'a': '1'}, 1), ({'a': '2'}, 2)]).render()
        lines = text.rstrip('\n').split('\n')
        self.assertEqual(lines[0], '# HELP kubecoder_t help')
        self.assertEqual(lines[1], '# TYPE kubecoder_t gauge')
        self.assertEqual(len(lines), 4)

    def test_document_ends_with_exactly_one_newline(self):
        text = prom.Exposition().gauge('kubecoder_t', 'h', [({}, 1)]).render()
        self.assertTrue(text.endswith('\n'))
        self.assertFalse(text.endswith('\n\n'))

    def test_empty_document_is_empty_not_a_bare_newline(self):
        self.assertEqual(prom.Exposition().render(), '')

    def test_output_is_byte_stable_across_renders(self):
        def build():
            return prom.Exposition().gauge(
                'kubecoder_t', 'h',
                [({'b': '2', 'a': '1'}, 1), ({'a': '0', 'b': '9'}, 2)]).render()
        self.assertEqual(build(), build())

    def test_labels_are_emitted_in_name_order(self):
        text = prom.Exposition().gauge(
            'kubecoder_t', 'h', [({'zeta': '1', 'alpha': '2'}, 1)]).render()
        self.assertIn('kubecoder_t{alpha="2",zeta="1"} 1', text)

    def test_a_family_with_no_samples_still_declares_itself(self):
        text = prom.Exposition().gauge('kubecoder_t', 'h', []).render()
        parsed = promparse.parse(text)
        self.assertEqual(parsed.types['kubecoder_t'], 'gauge')
        self.assertEqual(parsed.series('kubecoder_t'), [])


class RefusalTests(unittest.TestCase):
    """Things that would break a scrape must raise, not render."""

    def test_duplicate_family_is_refused(self):
        exp = prom.Exposition().gauge('kubecoder_t', 'h', [({}, 1)])
        with self.assertRaises(prom.ExpositionError):
            exp.gauge('kubecoder_t', 'h', [({}, 2)])

    def test_duplicate_label_set_is_refused(self):
        with self.assertRaises(prom.ExpositionError):
            prom.Exposition().gauge(
                'kubecoder_t', 'h', [({'a': '1'}, 1), ({'a': '1'}, 2)])

    def test_label_sets_that_differ_only_after_escaping_are_still_distinct(self):
        text = prom.Exposition().gauge(
            'kubecoder_t', 'h', [({'a': 'x"y'}, 1), ({'a': 'x\\"y'}, 2)]).render()
        self.assertEqual(len(promparse.parse(text).series('kubecoder_t')), 2)

    def test_invalid_metric_name_is_refused(self):
        for bad in ('', '1kubecoder', 'kube-coder_t', 'kubecoder t', None):
            with self.assertRaises(prom.ExpositionError, msg=repr(bad)):
                prom.Exposition().gauge(bad, 'h', [({}, 1)])

    def test_invalid_label_name_is_refused(self):
        for bad in ('', 'a-b', '1a', 'a b'):
            with self.assertRaises(prom.ExpositionError, msg=repr(bad)):
                prom.Exposition().gauge('kubecoder_t', 'h', [({bad: 'v'}, 1)])

    def test_reserved_label_namespace_is_refused(self):
        with self.assertRaises(prom.ExpositionError):
            prom.Exposition().gauge('kubecoder_t', 'h', [({'__name__': 'x'}, 1)])

    def test_counter_must_end_in_total(self):
        with self.assertRaises(prom.ExpositionError):
            prom.Exposition().counter('kubecoder_things', 'h', [({}, 1)])
        prom.Exposition().counter('kubecoder_things_total', 'h', [({}, 1)])

    def test_gauge_must_not_end_in_total(self):
        # The suffix means "monotonic counter" to everyone reading a dashboard;
        # a gauge wearing it invites a rate() that quietly lies.
        with self.assertRaises(prom.ExpositionError):
            prom.Exposition().gauge('kubecoder_things_total', 'h', [({}, 1)])

    def test_non_numeric_sample_value_is_refused(self):
        for bad in ('abc', None, object(), [1]):
            with self.assertRaises(prom.ExpositionError, msg=repr(bad)):
                prom.Exposition().gauge('kubecoder_t', 'h', [({}, bad)])

    def test_unknown_metric_type_is_refused(self):
        with self.assertRaises(prom.ExpositionError):
            prom.Exposition().add('kubecoder_t', 'histogram', 'h', [({}, 1)])


class ValueTests(unittest.TestCase):
    def test_ints_render_without_a_decimal_point(self):
        text = prom.Exposition().gauge('kubecoder_t', 'h', [({}, 42)]).render()
        self.assertIn('kubecoder_t 42\n', text)

    def test_bools_render_as_one_and_zero(self):
        text = prom.Exposition().gauge(
            'kubecoder_t', 'h', [({'a': '1'}, True), ({'a': '0'}, False)]).render()
        parsed = promparse.parse(text)
        self.assertEqual(parsed.value('kubecoder_t', a='1'), 1.0)
        self.assertEqual(parsed.value('kubecoder_t', a='0'), 0.0)

    def test_specials_use_the_spellings_the_format_defines(self):
        text = prom.Exposition().gauge('kubecoder_t', 'h', [
            ({'k': 'nan'}, float('nan')),
            ({'k': 'pos'}, float('inf')),
            ({'k': 'neg'}, float('-inf')),
        ]).render()
        self.assertIn('NaN', text)
        self.assertIn('+Inf', text)
        self.assertIn('-Inf', text)
        parsed = promparse.parse(text)
        self.assertTrue(math.isnan(parsed.value('kubecoder_t', k='nan')))
        self.assertEqual(parsed.value('kubecoder_t', k='pos'), math.inf)

    def test_large_and_small_floats_stay_parseable(self):
        text = prom.Exposition().gauge('kubecoder_t', 'h', [
            ({'k': 'big'}, 1e21), ({'k': 'small'}, 1e-9),
        ]).render()
        parsed = promparse.parse(text)
        self.assertEqual(parsed.value('kubecoder_t', k='big'), 1e21)


class ParserGuardTests(unittest.TestCase):
    """The parser above only means something if it rejects bad documents.

    Every case here is a real way an exposition breaks; if the parser accepted
    any of them, the corresponding test in this file would pass vacuously.
    """

    BAD = {
        'no trailing newline': '# TYPE a gauge\na 1',
        'blank line at the end': '# TYPE a gauge\na 1\n\n',
        'unescaped quote in a value': '# TYPE a gauge\na{l="x"y"} 1\n',
        'invalid escape': '# TYPE a gauge\na{l="x\\ty"} 1\n',
        'dangling escape': '# TYPE a gauge\na{l="x\\"} 1\n',
        'unterminated label value': '# TYPE a gauge\na{l="x} 1\n',
        'unterminated label set': '# TYPE a gauge\na{l="x" 1\n',
        'duplicate series': '# TYPE a gauge\na{l="1"} 1\na{l="1"} 2\n',
        'duplicate label name': '# TYPE a gauge\na{l="1",l="2"} 1\n',
        'second TYPE': '# TYPE a gauge\n# TYPE a counter\na 1\n',
        'second HELP': '# HELP a x\n# HELP a y\na 1\n',
        'TYPE after samples': 'a 1\n# TYPE a gauge\n',
        'HELP after samples': 'a 1\n# HELP a h\n',
        'unknown type': '# TYPE a bucket\na 1\n',
        'missing value': '# TYPE a gauge\na\n',
        'no space before value': '# TYPE a gauge\na{l="1"}1\n',
        'value is not a number': '# TYPE a gauge\na abc\n',
        'python-only numeric literal': '# TYPE a gauge\na 1_0\n',
        'non-canonical infinity': '# TYPE a gauge\na infinity\n',
        'bad metric name': '# TYPE a gauge\n1a 1\n',
        'bad timestamp': '# TYPE a gauge\na 1 not-a-ts\n',
        'trailing junk after value': '# TYPE a gauge\na 1 2 3\n',
    }

    def test_parser_rejects_every_known_break(self):
        for label, text in self.BAD.items():
            with self.subTest(label):
                with self.assertRaises(promparse.ParseError):
                    promparse.parse(text)

    def test_parser_accepts_a_well_formed_document(self):
        parsed = promparse.parse(
            '# HELP a some help\n'
            '# TYPE a gauge\n'
            'a{l="1",m="x\\ny"} 1.5\n'
            'a 2 1700000000\n'
            '# a bare comment\n'
            '# TYPE b_total counter\n'
            '# HELP b_total h\n'
            'b_total 7\n')
        self.assertEqual(parsed.value('a', l='1', m='x\ny'), 1.5)
        self.assertEqual(parsed.value('a'), 2.0)
        self.assertEqual(parsed.types['b_total'], 'counter')
        self.assertEqual(parsed.helps['a'], 'some help')

    def test_assert_valid_enforces_repo_conventions(self):
        with self.assertRaises(promparse.ParseError):  # counter without _total
            promparse.assert_valid('# HELP a h\n# TYPE a counter\na 1\n')
        with self.assertRaises(promparse.ParseError):  # gauge with _total
            promparse.assert_valid('# HELP a_total h\n# TYPE a_total gauge\n'
                                   'a_total 1\n')
        with self.assertRaises(promparse.ParseError):  # untyped family
            promparse.assert_valid('# HELP a h\na 1\n')
        with self.assertRaises(promparse.ParseError):  # wrong prefix
            promparse.assert_valid('# HELP a h\n# TYPE a gauge\na 1\n',
                                   prefix='kubecoder_')


if __name__ == '__main__':
    unittest.main()
