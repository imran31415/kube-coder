"""Tests for the pure half of security scans (#726).

Covers the decisions that are load-bearing rather than the code that is
obvious: the status mapping's safe default, the pass-through contract for
findings, the diff that tells a new problem from a revised one, and the rule
that a finished scan never reports "nothing found" without saying what it
actually did.

Run with:
    cd charts/workspace && python3 -m unittest tests.scans_test
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import scans  # noqa: E402


def finding(fid, severity='high', **extra):
    """A finding as a scanner would write it — arbitrary extra fields and all."""
    out = {'id': fid, 'severity': severity, 'title': f'Problem {fid}'}
    out.update(extra)
    return out


class StatusMappingTests(unittest.TestCase):

    def test_known_statuses_map_across(self):
        for raw, expected in (('running', 'running'), ('completed', 'done'),
                              ('stopped', 'stopped'), ('failed', 'failed'),
                              ('interrupted', 'interrupted')):
            self.assertEqual(scans.map_backend_status(raw), expected)

    def test_case_and_whitespace_do_not_change_the_mapping(self):
        self.assertEqual(scans.map_backend_status('  COMPLETED '), 'done')

    def test_an_unknown_status_is_failed_never_done(self):
        """The safe unknown. A scanner that grows a terminal state we have
        never seen must not start reporting clean results."""
        for raw in ('finished', 'cancelled', '', None, 'succeeded'):
            self.assertEqual(scans.map_backend_status(raw), 'failed')

    def test_every_mapped_value_is_a_real_status(self):
        for value in scans.BACKEND_STATUS_MAP.values():
            self.assertIn(value, scans.SCAN_STATUSES)

    def test_terminal_and_live_partition_the_statuses(self):
        self.assertEqual(
            sorted(scans.LIVE_SCAN_STATUSES + scans.TERMINAL_SCAN_STATUSES),
            sorted(scans.SCAN_STATUSES))


class ScanIdTests(unittest.TestCase):

    def test_generated_ids_validate(self):
        self.assertTrue(scans.valid_scan_id(scans.new_scan_id()))

    def test_ids_are_unique(self):
        self.assertEqual(len({scans.new_scan_id() for _ in range(200)}), 200)

    def test_path_traversal_shapes_are_refused(self):
        """Every scan path is built from this id, so it gates before the join."""
        for bad in ('', None, '../etc', 'scn_../../x', 'scn_ABCDEF123456',
                    'scn_short', 'scn_' + 'a' * 13, 'x' * 40):
            self.assertFalse(scans.valid_scan_id(bad), bad)


class TargetTests(unittest.TestCase):

    APPS = [
        {'port': 3000, 'name': 'shop', 'status': 'running', 'addr': '0.0.0.0'},
        {'port': 4000, 'name': 'api', 'status': 'running', 'addr': '127.0.0.1'},
        {'port': 5000, 'name': 'old', 'status': 'stopped', 'addr': ''},
        {'port': 8080, 'name': 'ide', 'status': 'running', 'addr': '0.0.0.0'},
    ]

    def test_only_running_apps_are_scannable(self):
        ports = [t['port'] for t in scans.scannable_targets(self.APPS)]
        self.assertNotIn(5000, ports)

    def test_workspace_internal_ports_are_excluded(self):
        ports = [t['port'] for t in
                 scans.scannable_targets(self.APPS, internal_ports=(8080,))]
        self.assertNotIn(8080, ports)

    def test_a_loopback_only_app_is_flagged_as_unreachable(self):
        """The failure users cannot diagnose: the scan connects to nothing and
        reports nothing, with no error to go on."""
        api = next(t for t in scans.scannable_targets(self.APPS)
                   if t['port'] == 4000)
        self.assertFalse(api['reachable'])
        self.assertIn('0.0.0.0', api['reason'])

    def test_a_widely_bound_app_is_reachable_with_no_warning(self):
        shop = next(t for t in scans.scannable_targets(self.APPS)
                    if t['port'] == 3000)
        self.assertTrue(shop['reachable'])
        self.assertEqual(shop['reason'], '')

    def test_an_ipv4_mapped_loopback_is_still_loopback(self):
        """AppsManager canonicalises a v4-mapped listener to
        `::ffff:127.0.0.1` -- which a `127.` prefix test misses, so the app
        was offered as scannable and the scan ended "found nothing" having
        reached nothing. A JVM binding 127.0.0.1 on an AF_INET6 socket
        produces exactly this."""
        for addr in ('::ffff:127.0.0.1', '::ffff:7f00:1', '::1', '127.1.2.3'):
            apps = [{'status': 'running', 'port': 4100, 'name': 'jvm',
                     'addr': addr}]
            target = scans.scannable_targets(apps)[0]
            self.assertFalse(target['reachable'], addr)
            self.assertIn('0.0.0.0', target['reason'])

    def test_a_wildcard_bind_is_not_mistaken_for_loopback(self):
        for addr in ('0.0.0.0', '::'):
            apps = [{'status': 'running', 'port': 4200, 'name': 'app',
                     'addr': addr}]
            self.assertTrue(scans.scannable_targets(apps)[0]['reachable'],
                            addr)

    def test_empty_and_malformed_input_yield_no_targets(self):
        self.assertEqual(scans.scannable_targets(None), [])
        self.assertEqual(scans.scannable_targets([{'status': 'running'}]), [])


class ValidateCreateTests(unittest.TestCase):

    TARGETS = [{'port': 3000, 'name': 'shop', 'addr': '0.0.0.0',
                'reachable': True, 'reason': ''}]

    def create(self, **body):
        body.setdefault('port', 3000)
        return scans.validate_create(body, self.TARGETS,
                                     connected_model='prov/model')

    def test_a_minimal_request_is_accepted_and_defaults_to_the_shallow_mode(self):
        spec, err = self.create()
        self.assertIsNone(err)
        self.assertEqual(spec['mode'], scans.DEFAULT_SCAN_MODE)
        self.assertEqual(spec['model'], 'prov/model')
        self.assertIsNone(spec['budget_usd'])

    def test_the_default_mode_is_never_the_deepest_one(self):
        """A casually started scan must not be a multi-hour bill."""
        self.assertNotEqual(scans.DEFAULT_SCAN_MODE, 'deep')

    def test_a_port_nothing_is_serving_is_refused(self):
        spec, err = scans.validate_create({'port': 9999}, self.TARGETS,
                                          connected_model='prov/model')
        self.assertIsNone(spec)
        self.assertIn('9999', err)

    def test_a_port_the_scanner_cannot_reach_is_refused_with_the_reason(self):
        """Greying the button out is not enough: the API is also reached from
        the phone and the MCP tools, and a scan of a loopback-only app spends
        money and reports a clean result against nothing."""
        targets = scans.scannable_targets(
            [{'status': 'running', 'port': 4545, 'addr': '127.0.0.1'}])
        spec, err = scans.validate_create({'port': 4545}, targets,
                                          connected_model='prov/model')
        self.assertIsNone(spec)
        self.assertIn('127.0.0.1', err)
        self.assertIn('0.0.0.0', err)

    def test_a_missing_or_non_integer_port_is_refused(self):
        for port in (None, '3000', 3000.5, True):
            spec, err = scans.validate_create({'port': port}, self.TARGETS,
                                              connected_model='prov/model')
            self.assertIsNone(spec, port)
            self.assertIn('Pick an app', err)

    def test_no_model_anywhere_is_refused_before_anything_is_spent(self):
        spec, err = scans.validate_create({'port': 3000}, self.TARGETS)
        self.assertIsNone(spec)
        self.assertIn('Connect a model', err)

    def test_an_explicit_model_overrides_the_connected_one(self):
        spec, _ = self.create(model='other/model')
        self.assertEqual(spec['model'], 'other/model')

    def test_any_model_string_is_allowed(self):
        """No allowlist: the user picks the model, exactly as the scanner's own
        configuration does."""
        for model in ('openai/gpt-x', 'ollama/llama', 'vendor/a/b/c',
                      'chatgpt/some-model'):
            spec, err = self.create(model=model)
            self.assertIsNone(err, model)
            self.assertEqual(spec['model'], model)

    def test_control_characters_and_overlong_models_are_refused(self):
        for model in ('bad\nmodel', 'bad\x00model', 'x' * 500):
            spec, err = self.create(model=model)
            self.assertIsNone(spec)
            self.assertIn('not valid', err)

    def test_an_unknown_mode_is_refused(self):
        spec, err = self.create(mode='thorough')
        self.assertIsNone(spec)
        self.assertIn('quick', err)

    def test_every_mode_has_a_time_estimate_to_show_in_the_picker(self):
        for mode in scans.SCAN_MODES:
            self.assertTrue(scans.MODE_ESTIMATES.get(mode))

    def test_the_budget_is_optional_like_the_scanners_own_flag(self):
        for value in (None, ''):
            spec, err = self.create(budget_usd=value)
            self.assertIsNone(err)
            self.assertIsNone(spec['budget_usd'])

    def test_a_budget_is_kept_when_given(self):
        spec, _ = self.create(budget_usd='7.5')
        self.assertEqual(spec['budget_usd'], 7.5)

    def test_a_nonsense_budget_is_refused(self):
        for value in ('abc', 0, -1, 10 ** 9):
            spec, err = self.create(budget_usd=value)
            self.assertIsNone(spec, value)
            self.assertIn('Budget', err)

    def test_an_oversized_instruction_is_refused(self):
        spec, err = self.create(instruction='x' * 99999)
        self.assertIsNone(spec)
        self.assertIn('instructions', err)


class FindingPassThroughTests(unittest.TestCase):

    def test_a_finding_is_returned_byte_identical(self):
        """The pass-through contract: we never reword or re-shape what the
        scanner asserted."""
        raw = finding('v1', description='...', poc_script_code='curl …',
                      cvss=9.1, cwe='CWE-89', unknown_future_field=['x'])
        self.assertEqual(scans.parse_findings([raw]), [raw])

    def test_entries_without_a_usable_id_are_dropped(self):
        """Identity is what dispositions and deep links are keyed on. A
        synthetic id would change between reads and break both."""
        out = scans.parse_findings([
            finding('v1'), {'severity': 'high'}, {'id': ''}, {'id': 42},
            'not a dict', None,
        ])
        self.assertEqual([f['id'] for f in out], ['v1'])

    def test_duplicate_ids_keep_the_first(self):
        out = scans.parse_findings([finding('v1', 'high'), finding('v1', 'low')])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['severity'], 'high')

    def test_a_malformed_payload_yields_no_findings(self):
        for raw in (None, {}, 'nope', 42):
            self.assertEqual(scans.parse_findings(raw), [])


class SeverityTests(unittest.TestCase):

    def test_known_severities_are_ordered_most_severe_first(self):
        ranks = [scans.severity_rank(finding('x', s)) for s in scans.SEVERITIES]
        self.assertEqual(ranks, sorted(ranks))

    def test_an_unrecognised_severity_sorts_last_but_is_never_dropped(self):
        items = [finding('a', 'nonsense'), finding('b', 'critical'),
                 finding('c', 'low')]
        order = [f['id'] for f in scans.sort_findings(items)]
        self.assertEqual(order, ['b', 'c', 'a'])

    def test_counts_bucket_the_unrecognised_separately_and_total_everything(self):
        counts = scans.count_severities([
            finding('a', 'critical'), finding('b', 'low'),
            finding('c', 'low'), finding('d', 'weird'), {'id': 'e'},
        ])
        self.assertEqual(counts['critical'], 1)
        self.assertEqual(counts['low'], 2)
        self.assertEqual(counts['other'], 2)
        self.assertEqual(counts['total'], 5)

    def test_highest_severity_leads_with_the_worst_present(self):
        self.assertEqual(
            scans.highest_severity([finding('a', 'low'), finding('b', 'high')]),
            'high')
        self.assertEqual(scans.highest_severity([]), '')


class SortOrderTests(unittest.TestCase):
    """`sort_findings` promises newest-first within a severity. Its old single
    `(severity_rank, ts)` key sorted ascending, i.e. oldest-first, and no test
    pinned the timestamp order either way."""

    def test_newest_is_first_within_a_severity(self):
        old = dict(finding('v-old', 'high'), timestamp='2026-01-01T00:00:00Z')
        new = dict(finding('v-new', 'high'), timestamp='2026-06-01T00:00:00Z')
        got = scans.sort_findings([old, new])
        self.assertEqual([f['id'] for f in got], ['v-new', 'v-old'])

    def test_severity_still_outranks_recency(self):
        low = dict(finding('v-low', 'low'), timestamp='2026-06-01T00:00:00Z')
        crit = dict(finding('v-crit', 'critical'),
                    timestamp='2026-01-01T00:00:00Z')
        got = scans.sort_findings([low, crit])
        self.assertEqual([f['id'] for f in got], ['v-crit', 'v-low'])


class DiffTests(unittest.TestCase):

    def test_a_first_read_is_all_additions(self):
        added, changed = scans.diff_findings(None, [finding('v1')])
        self.assertEqual([f['id'] for f in added], ['v1'])
        self.assertEqual(changed, [])

    def test_an_unchanged_finding_produces_no_event(self):
        same = [finding('v1')]
        added, changed = scans.diff_findings(same, list(same))
        self.assertEqual((added, changed), ([], []))

    def test_a_revised_finding_is_changed_not_added(self):
        """Scanners revise a finding in place as evidence accumulates; the UI
        has to tell that from a new problem appearing."""
        before = [finding('v1', 'medium')]
        after = [finding('v1', 'critical', poc_script_code='curl …')]
        added, changed = scans.diff_findings(before, after)
        self.assertEqual(added, [])
        self.assertEqual([f['id'] for f in changed], ['v1'])

    def test_additions_and_revisions_are_reported_together(self):
        added, changed = scans.diff_findings(
            [finding('v1', 'low')],
            [finding('v1', 'high'), finding('v2')])
        self.assertEqual([f['id'] for f in added], ['v2'])
        self.assertEqual([f['id'] for f in changed], ['v1'])


class UsageTests(unittest.TestCase):

    def test_counters_are_read_from_the_run_record(self):
        usage = scans.parse_usage({'requests': 12, 'input_tokens': 920100,
                                   'output_tokens': 3700,
                                   'total_tokens': 923800, 'cost': 0.231})
        self.assertEqual(usage['input_tokens'], 920100)
        self.assertAlmostEqual(usage['cost_usd'], 0.231)

    def test_missing_or_badly_typed_values_never_break_the_live_panel(self):
        for raw in (None, {}, {'cost': 'lots'}, {'requests': None}, 'nope'):
            usage = scans.parse_usage(raw)
            self.assertEqual(usage['cost_usd'], 0.0)
            self.assertEqual(usage['requests'], 0)

    def test_a_numeric_string_cost_is_still_read(self):
        self.assertAlmostEqual(scans.parse_usage({'cost': '0.5'})['cost_usd'],
                               0.5)


class ApplyArtifactsTests(unittest.TestCase):

    def record(self, **over):
        rec = {'status': 'running', 'findings': [], 'counts': {},
               'usage': {}, 'ended_at': None,
               'target': {'port': 3000, 'name': 'shop'}, 'mode': 'quick'}
        rec.update(over)
        return rec

    def test_findings_counts_and_usage_are_folded_in(self):
        rec = self.record()
        added, changed = scans.apply_artifacts(rec, {
            'run': {'status': 'running', 'llm_usage': {'cost': 0.1}},
            'findings': [finding('v1', 'critical')],
        })
        self.assertEqual([f['id'] for f in added], ['v1'])
        self.assertEqual(changed, [])
        self.assertEqual(rec['counts']['critical'], 1)
        self.assertAlmostEqual(rec['usage']['cost_usd'], 0.1)
        self.assertEqual(rec['status'], 'running')

    def test_a_mid_write_read_does_not_zero_the_spend(self):
        """`run.json` is caught mid-rewrite on every confirmed finding, and
        the backend then reports an empty run. Overwriting usage from it
        flickered the live spend to $0.00 -- and made it permanent for any
        scan ending through the `not alive` path, which never re-reads a
        good run.json."""
        rec = self.record()
        scans.apply_artifacts(rec, {
            'run': {'status': 'running', 'llm_usage': {'cost': 0.23}},
            'findings': [],
        })
        self.assertAlmostEqual(rec['usage']['cost_usd'], 0.23)

        scans.apply_artifacts(rec, {'run': {}, 'findings': None})
        self.assertAlmostEqual(rec['usage']['cost_usd'], 0.23,
                               msg='a mid-write read wiped the spend')

    def test_completion_moves_the_record_terminal_and_stamps_the_end(self):
        rec = self.record()
        scans.apply_artifacts(rec, {'run': {'status': 'completed'},
                                    'findings': []})
        self.assertEqual(rec['status'], 'done')
        self.assertIsNotNone(rec['ended_at'])

    def test_a_terminal_record_is_never_resurrected_by_a_late_read(self):
        """A stop request and the backend's final write race; whichever lands
        second must not reopen a scan the user has already been told ended."""
        rec = self.record(status='stopped', ended_at=123.0)
        scans.apply_artifacts(rec, {'run': {'status': 'running'},
                                    'findings': [finding('v1')]})
        self.assertEqual(rec['status'], 'stopped')
        self.assertEqual(rec['ended_at'], 123.0)
        # Findings from that last read are still kept — they are real.
        self.assertEqual(len(rec['findings']), 1)

    def test_an_unreadable_artifact_read_leaves_the_scan_running(self):
        rec = self.record()
        scans.apply_artifacts(rec, None)
        self.assertEqual(rec['status'], 'running')
        self.assertEqual(rec['findings'], [])

    def test_a_stop_request_outranks_the_scanners_word_for_the_signal(self):
        """The scanner writes `interrupted` for the SIGTERM a Stop sends —
        the same word it uses when it crashes."""
        for reported in ('interrupted', 'failed', 'stopped'):
            rec = self.record(stop_requested_at=1.0)
            scans.apply_artifacts(rec, {'run': {'status': reported},
                                        'findings': []})
            self.assertEqual(rec['status'], 'stopped', reported)

    def test_a_scan_that_completed_as_stop_was_pressed_stays_done(self):
        rec = self.record(stop_requested_at=1.0)
        scans.apply_artifacts(rec, {'run': {'status': 'completed'},
                                    'findings': []})
        self.assertEqual(rec['status'], 'done')


class ReportTests(unittest.TestCase):
    """The scanner's write-up — the result the scan was run for.

    It already arrived inside the run record and `apply_artifacts` kept only
    `llm_usage` and `status`, so a scan that finished having proven nothing
    rendered a blank page with the word "done" on it.
    """

    def record(self, **over):
        rec = {'status': 'running', 'findings': [], 'counts': {}, 'usage': {},
               'ended_at': None, 'target': {'port': 3000, 'name': 'shop'},
               'mode': 'quick'}
        rec.update(over)
        return rec

    REPORT = {'text': '# Executive Summary\n\nChecked the login page.',
              'location': '/home/dev/.claude-scans/scn_x/report.md'}

    def test_the_report_and_coverage_reach_the_record(self):
        rec = self.record()
        scans.apply_artifacts(rec, {
            'run': {'status': 'completed'}, 'findings': [],
            'report': self.REPORT,
            'coverage': {'complete': False, 'caveats': ['ran out of room']},
        })
        self.assertEqual(rec['report'], self.REPORT)
        self.assertEqual(rec['coverage']['caveats'], ['ran out of room'])

    def test_a_later_read_never_blanks_a_report_already_held(self):
        """The scanner writes the report once and tidies up after itself, so
        the poll that lands next can see nothing. Taking the report back off
        the page at that moment is the bug this guards."""
        rec = self.record()
        scans.apply_artifacts(rec, {'run': {'status': 'running'},
                                    'findings': [], 'report': self.REPORT,
                                    'coverage': {'complete': True,
                                                 'caveats': []}})
        scans.apply_artifacts(rec, {'run': {}, 'findings': None,
                                    'report': None, 'coverage': None})
        self.assertEqual(rec['report'], self.REPORT)
        self.assertTrue(rec['coverage']['complete'])

    def test_a_backend_that_reports_neither_stores_neither(self):
        rec = self.record()
        scans.apply_artifacts(rec, {'run': {'status': 'running'},
                                    'findings': []})
        self.assertNotIn('report', rec)
        self.assertNotIn('coverage', rec)

    def test_only_a_report_with_text_is_kept(self):
        for bad in (None, {}, 'a string', {'text': ''}, {'text': '  '},
                    {'location': '/x'}, {'text': 5}):
            self.assertIsNone(scans.parse_report(bad), bad)

    def test_a_report_with_no_location_is_still_a_report(self):
        """A backend running the scan elsewhere has no file on this machine;
        its write-up is still the result."""
        parsed = scans.parse_report({'text': 'done'})
        self.assertEqual(parsed, {'text': 'done', 'location': ''})

    def test_the_list_row_drops_the_report_but_keeps_the_verdict(self):
        row = scans.summarise({'id': 'scn_x', 'report': self.REPORT,
                               'coverage': {'complete': False,
                                            'caveats': ['short']}})
        self.assertNotIn('report', row)
        self.assertFalse(row['coverage']['complete'])


class CoverageTests(unittest.TestCase):

    def test_unknown_coverage_is_never_read_as_a_yes(self):
        """A record written before coverage was read must not claim the app
        was covered — that is exactly the all-clear this module exists to
        refuse."""
        self.assertIsNone(scans.covered_everything({}))
        self.assertIsNone(scans.covered_everything({'coverage': {}}))
        self.assertIsNone(scans.covered_everything({'coverage': 'yes'}))
        self.assertIs(scans.covered_everything({'coverage': {'complete': True}}),
                      True)
        self.assertIs(scans.covered_everything({'coverage': {'complete': False}}),
                      False)

    def test_caveats_are_the_scanners_own_sentences(self):
        rec = {'coverage': {'complete': False,
                            'caveats': ['not reached', '', 7, '  ']}}
        self.assertEqual(scans.coverage_caveats(rec), ['not reached'])
        self.assertEqual(scans.coverage_caveats({}), [])


class BudgetNoteTests(unittest.TestCase):
    """The spend figure alone does not say the limit is what ended the scan."""

    def note(self, **over):
        rec = {'status': 'done', 'budget_usd': 3.0,
               'usage': {'cost_usd': 2.76}}
        rec.update(over)
        return scans.budget_note(rec)

    def test_a_scan_that_ended_on_its_limit_says_so(self):
        text = self.note()
        self.assertIn('$2.76', text)
        self.assertIn('$3.00', text)
        self.assertIn('stops testing before the limit', text)

    def test_a_scan_nowhere_near_its_limit_says_nothing(self):
        self.assertEqual(self.note(usage={'cost_usd': 0.30}), '')

    def test_no_limit_means_no_note(self):
        self.assertEqual(self.note(budget_usd=None), '')

    def test_a_running_scan_is_not_told_what_it_would_have_bought(self):
        self.assertEqual(self.note(status='running'), '')


class EndedBadlyTests(unittest.TestCase):

    def test_which_ends_owe_the_user_a_reason(self):
        self.assertTrue(scans.ended_badly({'status': 'failed'}))
        self.assertTrue(scans.ended_badly({'status': 'interrupted'}))
        self.assertTrue(scans.ended_badly({'status': 'stopped'}))
        self.assertFalse(scans.ended_badly({'status': 'stopped',
                                            'stop_requested_at': 1.0}))
        self.assertFalse(scans.ended_badly({'status': 'done'}))
        self.assertFalse(scans.ended_badly({'status': 'running'}))

    def test_a_spent_budget_is_named_without_the_backend(self):
        reason = scans.fallback_reason({'budget_usd': 5.0,
                                        'usage': {'cost_usd': 5.01}})
        self.assertEqual(reason, 'The scan reached its $5.00 spending limit.')

    def test_an_unknown_reason_says_it_is_unknown(self):
        reason = scans.fallback_reason({'budget_usd': 5.0,
                                        'usage': {'cost_usd': 0.63}})
        self.assertIn('did not say why', reason)
        self.assertIn('did not say why', scans.fallback_reason(None))


class EndReasonTests(unittest.TestCase):
    """`end_reason_of` — the one code every page picks its headline and next
    step by."""

    def reason(self, **rec):
        return scans.end_reason_of(rec)

    def test_a_running_scan_has_no_end_reason(self):
        self.assertEqual(self.reason(status='running'), '')
        self.assertEqual(scans.end_reason_of(None), '')

    def test_who_ended_it_comes_before_what_the_backend_said(self):
        self.assertEqual(self.reason(status='done', end_cause='provider'),
                         'completed')
        self.assertEqual(self.reason(status='stopped', stop_requested_at=1.0,
                                     end_cause='provider'), 'user_stopped')
        self.assertEqual(self.reason(status='interrupted',
                                     interrupted_reason='restart'),
                         'workspace_restarted')

    def test_a_stop_request_on_a_scan_that_completed_anyway_is_completed(self):
        self.assertEqual(self.reason(status='done', stop_requested_at=1.0),
                         'completed')

    def test_each_backend_cause_maps_to_its_reason(self):
        for cause, expected in (('provider', 'provider_error'),
                                ('budget', 'budget_reached'),
                                ('scanner', 'scanner_error'),
                                ('lost', 'lost_track')):
            self.assertEqual(self.reason(status='failed', end_cause=cause),
                             expected, cause)

    def test_a_spent_budget_is_named_with_no_cause_at_all(self):
        """A record from before causes were stored still reads right."""
        self.assertEqual(self.reason(status='stopped', budget_usd=3.0,
                                     usage={'cost_usd': 3.0}),
                         'budget_reached')

    def test_with_nothing_known_a_vanished_process_is_not_a_scanner_error(self):
        self.assertEqual(self.reason(status='interrupted',
                                     interrupted_reason='vanished'),
                         'process_vanished')
        self.assertEqual(self.reason(status='failed'), 'scanner_error')
        self.assertEqual(self.reason(status='stopped'), 'scanner_error')

    def test_every_answer_is_a_known_code(self):
        for status in scans.TERMINAL_SCAN_STATUSES:
            self.assertIn(self.reason(status=status), scans.END_REASONS)

    def test_both_views_carry_it(self):
        rec = {'status': 'stopped', 'stop_requested_at': 1.0}
        self.assertEqual(scans.summarise(rec)['end_reason'], 'user_stopped')
        self.assertEqual(scans.public_view(rec)['end_reason'], 'user_stopped')


class PublicViewTests(unittest.TestCase):

    def full(self):
        return {'id': 'scn_abcdef123456', 'status': 'done',
                'findings': [finding('v1', 'low'), finding('v2', 'critical')],
                'handle': {'pid': 4242, 'run_dir': '/home/dev/…'},
                'dispositions': {'v1': 'dismissed'},
                'target': {'port': 3000, 'name': 'shop'}, 'mode': 'quick',
                'counts': {'total': 2}}

    def test_the_list_row_carries_no_findings_and_no_backend_internals(self):
        row = scans.summarise(self.full())
        self.assertNotIn('findings', row)
        self.assertNotIn('handle', row)
        self.assertEqual(row['status'], 'done')
        self.assertEqual(row['counts']['total'], 2)

    def test_the_detail_view_sorts_findings_and_hides_backend_internals(self):
        view = scans.public_view(self.full())
        self.assertNotIn('handle', view)
        self.assertEqual([f['id'] for f in view['findings']], ['v2', 'v1'])

    def test_a_disposition_never_edits_the_scanners_own_record(self):
        view = scans.public_view(self.full())
        self.assertEqual(view['dispositions'], {'v1': 'dismissed'})
        self.assertNotIn('disposition', view['findings'][0])
        self.assertNotIn('disposition', view['findings'][1])


class ResultSummaryTests(unittest.TestCase):

    def summary(self, **over):
        rec = {'status': 'done', 'mode': 'quick', 'counts': {'total': 0},
               'target': {'port': 3000, 'name': 'shop'}}
        rec.update(over)
        return scans.result_summary(rec)

    def test_a_clean_scan_never_says_only_zero(self):
        """Every empty-findings failure mode looks identical to a clean pass,
        so the sentence always says what was actually done."""
        text = self.summary()
        self.assertIn('shop', text)
        self.assertIn('quick', text)
        self.assertNotEqual(text.strip(), '0 vulnerabilities')

    def test_a_failed_scan_says_nothing_was_checked(self):
        text = self.summary(status='failed', error='No model configured.')
        self.assertIn('nothing was checked', text)

    def test_the_reason_is_not_repeated_inside_the_sentence(self):
        """Every surface shows the record's `error` beside this sentence. In
        it as well, the page printed the reason twice."""
        text = self.summary(status='failed', error='No model configured.')
        self.assertNotIn('No model configured.', text)

    def test_a_scan_that_failed_after_working_does_not_say_nothing_was_checked(self):
        text = self.summary(status='failed', usage={'requests': 40})
        self.assertIn('partway', text)
        self.assertNotIn('nothing was checked', text)

    def test_a_scan_its_user_stopped_says_so(self):
        text = self.summary(status='stopped', stop_requested_at=1.0)
        self.assertIn('You stopped', text)
        self.assertNotIn('unexpectedly', text)

    def test_a_scan_the_scanner_ended_itself_does_not_blame_the_user(self):
        text = self.summary(status='stopped',
                            error='The scan reached its spending limit.')
        self.assertIn('scanner ended', text)
        self.assertNotIn('You stopped', text)

    def test_a_scan_interrupted_by_a_restart_says_so(self):
        text = self.summary(status='interrupted',
                            interrupted_reason='restart')
        self.assertIn('not', text)
        self.assertIn('restart', text.lower())

    def test_an_interrupted_scan_does_not_invent_a_restart(self):
        """The poller also reaches `interrupted` when the process vanished
        under it (OOM kill, a `kill` from a terminal, dind dropping the
        sandbox). Telling the user the workspace restarted would be a plain
        falsehood on the one surface that must not misrepresent how a scan
        ended, so only the boot sweep's own case may claim it."""
        text = self.summary(status='interrupted',
                            interrupted_reason='vanished')
        self.assertNotIn('restart', text.lower())
        self.assertIn('not', text)
        # A record written before this field existed must still not claim it.
        self.assertNotIn('restart', self.summary(status='interrupted').lower())

    def test_an_interrupted_scan_with_a_reason_does_not_also_say_unexpectedly(self):
        """The reason ("killed by the system…") is printed beside this
        sentence; "ended unexpectedly" next to it reads as a contradiction."""
        text = self.summary(status='interrupted', interrupted_reason='vanished',
                            error='The scanner was killed by the system.')
        self.assertNotIn('unexpectedly', text)
        self.assertIn('stopped before it finished', text)
        self.assertIn('unexpectedly', self.summary(status='interrupted'))

    def test_a_stopped_scan_says_only_part_was_checked(self):
        self.assertIn('part', self.summary(status='stopped'))

    def test_findings_are_counted_in_the_sentence(self):
        self.assertIn('3', self.summary(counts={'total': 3}))

    def test_a_done_scan_that_missed_part_of_the_app_is_not_an_all_clear(self):
        """The exact sentence the user read as "my app is clean": the scan
        reached `done` with nothing proven, because the scanner stopped
        testing near the spending limit and spent the rest writing up."""
        text = self.summary(coverage={'complete': False,
                                      'caveats': ['ran out of room']})
        self.assertIn('not an all-clear', text)
        self.assertNotIn('found nothing', text)

    def test_a_scan_that_did_cover_the_app_still_says_so_plainly(self):
        text = self.summary(coverage={'complete': True, 'caveats': []})
        self.assertIn('found nothing', text)

    def test_findings_from_a_partial_scan_say_there_may_be_more(self):
        text = self.summary(counts={'total': 3},
                            coverage={'complete': False, 'caveats': ['x']})
        self.assertIn('3', text)
        self.assertIn('may be more', text)

    def test_an_older_record_with_no_verdict_keeps_the_old_wording(self):
        self.assertIn('found nothing', self.summary())

    def test_a_target_with_no_name_falls_back_to_its_port(self):
        text = self.summary(target={'port': 3000, 'name': ''})
        self.assertIn('3000', text)


if __name__ == '__main__':
    unittest.main()
