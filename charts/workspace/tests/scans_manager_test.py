"""Tests for ScansManager — lifecycle, the poller and the boot sweep (#726).

Driven entirely through a stub backend, so nothing here spawns a process or
reaches a network. That is the seam paying for itself: the manager's own
behaviour is testable because it only ever talks to `ScanBackend`.

`KC_SCANS_DIR` is redirected in setUp so no test writes the workspace's real
scan store, and the feed/event publishers are recorded rather than wired to the
live ones — an emit that reached `FeedManager` would page whoever has a phone
registered on this workspace.

Run with:
    cd charts/workspace && python3 -m unittest tests.scans_manager_test
"""

import contextlib
import io
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import scan_backends  # noqa: E402
import scans  # noqa: E402


class StubBackend(scan_backends.ScanBackend):
    """A backend with no process and no files, driven by the test."""

    name = 'stub'

    def __init__(self):
        self.ready = {'ok': True, 'reason': '', 'detail': ''}
        self.state = None          # what artifacts() returns
        self.alive = True
        self.started = []
        self.stopped = []
        self.log = ''
        self.reason = ''           # what explain_end() says to the user
        self.evidence = ''         # ...and to the server log
        self.cause = ''            # ...and what kind of end it was
        self.explained = 0
        self.on_stop = None        # what the scanner does when signalled
        self.start_error = None
        self.resolve_error = None

    def preflight(self):
        return self.ready

    def resolve_target(self, port):
        if self.resolve_error:
            raise scan_backends.BackendError(self.resolve_error)
        return f'http://gateway.test:{port}'

    def start(self, spec):
        if self.start_error:
            raise scan_backends.BackendError(self.start_error)
        self.started.append(spec)
        return {'backend': self.name, 'job': 'j1', 'scan_dir': spec['scan_dir']}

    def artifacts(self, handle):
        return self.state

    def is_alive(self, handle):
        return self.alive

    def stop(self, handle):
        self.stopped.append(handle)
        if self.on_stop:
            self.on_stop()
        self.alive = False

    def tail_log(self, handle, limit=4000):
        return self.log

    def explain_end(self, handle):
        self.explained += 1
        return self.reason, self.evidence, self.cause


class ManagerTestCase(unittest.TestCase):

    TARGETS = [{'port': 3000, 'name': 'shop', 'addr': '0.0.0.0',
                'reachable': True, 'reason': ''}]

    #: Most suites here drive `_absorb` by hand to assert on exactly one read.
    #: A live poller would race those calls and absorb the same state first,
    #: so it is off unless a test is specifically about the poller.
    poller = False

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        previous = os.environ.get('KC_SCANS_DIR')
        os.environ['KC_SCANS_DIR'] = os.path.join(tmp.name, 'scans')

        def restore():
            if previous is None:
                os.environ.pop('KC_SCANS_DIR', None)
            else:
                os.environ['KC_SCANS_DIR'] = previous
        self.addCleanup(restore)

        # The manager logs every start and stop; kept out of the test output.
        # A test that asserts on the log redirects stderr itself, inside this.
        quiet = contextlib.ExitStack()
        quiet.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.addCleanup(quiet.close)

        self.backend = StubBackend()
        self.events = []
        self.feed = []
        scans.ScansManager.configure(
            backend_factory=lambda: self.backend,
            publish=lambda t, d: self.events.append((t, d)),
            emit_feed=lambda *a, **kw: self.feed.append((a, kw)),
            targets_provider=lambda: list(self.TARGETS),
        )
        self.addCleanup(setattr, scans.ScansManager, 'POLL_INTERVAL',
                        scans.ScansManager.POLL_INTERVAL)
        if not self.poller:
            original = scans.ScansManager._spawn_poller
            scans.ScansManager._spawn_poller = classmethod(lambda cls, _id: None)
            self.addCleanup(setattr, scans.ScansManager, '_spawn_poller',
                            original)

    def spec(self, **over):
        body = {'port': 3000, 'mode': 'quick'}
        body.update(over)
        spec, err = scans.validate_create(body, self.TARGETS,
                                          connected_model='prov/model')
        self.assertIsNone(err)
        return spec

    def create(self, **over):
        record, err = scans.ScansManager.create(self.spec(**over))
        self.assertIsNone(err, err)
        return record

    def events_of(self, kind):
        return [d for t, d in self.events if t == kind]


class CreateTests(ManagerTestCase):

    def test_a_scan_is_recorded_running_with_the_resolved_target(self):
        record = self.create()
        self.assertEqual(record['status'], 'running')
        self.assertEqual(record['target']['url'], 'http://gateway.test:3000')
        self.assertEqual(record['backend'], 'stub')
        self.assertTrue(scans.valid_scan_id(record['id']))

    def test_the_backend_receives_the_resolved_target_and_its_own_directory(self):
        self.create()
        spec = self.backend.started[0]
        self.assertEqual(spec['target_url'], 'http://gateway.test:3000')
        self.assertTrue(os.path.isdir(spec['scan_dir']))

    def test_create_returns_without_waiting_for_the_scan(self):
        """The caller answers an HTTP request with this. A backend that takes
        its time must not hold the response."""
        def slow_start(spec):
            time.sleep(0.4)
            return {'backend': 'stub', 'job': 'slow',
                    'scan_dir': spec['scan_dir']}
        self.backend.start = slow_start
        began = time.time()
        self.create()
        # The stub deliberately sleeps; what is asserted is that the manager
        # adds no waiting of its own on top of the backend's own call.
        self.assertLess(time.time() - began, 1.0)

    def test_an_unavailable_backend_refuses_before_any_record_exists(self):
        self.backend.ready = {'ok': False, 'reason': 'no_container_runtime',
                              'detail': 'Needs buildkit.'}
        record, err = scans.ScansManager.create(self.spec())
        self.assertIsNone(record)
        self.assertEqual(err, 'Needs buildkit.')
        self.assertEqual(scans.ScansManager.list_scans(), [])

    def test_an_address_that_cannot_be_resolved_leaves_nothing_behind(self):
        """A folder with no record in it is invisible to pruning, so a refused
        start that made one first leaked a directory per click, forever."""
        self.backend.resolve_error = 'Could not work out the address.'
        record, err = scans.ScansManager.create(self.spec())
        self.assertIsNone(record)
        self.assertEqual(err, 'Could not work out the address.')
        self.assertEqual(self.backend.started, [])
        store = scans.ScansManager.scans_dir()
        self.assertEqual(os.listdir(store) if os.path.isdir(store) else [], [])

    def test_a_backend_that_cannot_start_records_a_failed_scan(self):
        """Not a silent nothing: the user gets a scan they can open and read."""
        self.backend.start_error = 'The scanner is not installed.'
        record, err = scans.ScansManager.create(self.spec())
        self.assertIsNone(record)
        self.assertIn('not installed', err)
        rows = scans.ScansManager.list_scans()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'failed')

    def test_a_credential_in_a_start_error_is_redacted(self):
        self.backend.start_error = 'refused: api_key=sk-abcdefghijklmno'
        _, err = scans.ScansManager.create(self.spec())
        self.assertNotIn('sk-abcdefghijklmno', err)
        self.assertIn('[redacted]', err)

    def test_starting_publishes_a_running_event(self):
        record = self.create()
        self.assertIn({'id': record['id'], 'status': 'running'},
                      self.events_of('scan.status'))


class AbsorbTests(ManagerTestCase):

    def absorb(self, scan_id, *, alive=True):
        return scans.ScansManager._absorb(
            scan_id, self.backend.artifacts(None), alive=alive,
            backend=self.backend, handle={'job': 'j1'})

    def test_findings_reach_the_record_and_fire_one_event_each(self):
        record = self.create()
        self.backend.state = {
            'run': {'status': 'running', 'llm_usage': {'cost': 0.2}},
            'findings': [{'id': 'v1', 'severity': 'critical'}],
        }
        self.absorb(record['id'])
        stored = scans.ScansManager.get(record['id'])
        self.assertEqual(stored['counts']['critical'], 1)
        self.assertAlmostEqual(stored['usage']['cost_usd'], 0.2)
        added = [e for e in self.events_of('scan.finding')
                 if e['op'] == 'added']
        self.assertEqual([e['finding_id'] for e in added], ['v1'])

    def test_an_unchanged_read_fires_no_finding_event(self):
        record = self.create()
        self.backend.state = {'run': {'status': 'running'},
                              'findings': [{'id': 'v1', 'severity': 'low'}]}
        self.absorb(record['id'])
        before = len(self.events_of('scan.finding'))
        self.absorb(record['id'])
        self.assertEqual(len(self.events_of('scan.finding')), before)

    def test_a_revised_finding_fires_a_change_not_an_addition(self):
        record = self.create()
        self.backend.state = {'run': {'status': 'running'},
                              'findings': [{'id': 'v1', 'severity': 'low'}]}
        self.absorb(record['id'])
        self.backend.state = {'run': {'status': 'running'},
                              'findings': [{'id': 'v1', 'severity': 'high'}]}
        self.absorb(record['id'])
        changed = [e for e in self.events_of('scan.finding')
                   if e['op'] == 'changed']
        self.assertEqual([e['finding_id'] for e in changed], ['v1'])

    def test_completion_settles_the_record_and_announces_once(self):
        record = self.create()
        self.backend.state = {'run': {'status': 'completed'},
                              'findings': [{'id': 'v1', 'severity': 'high'}]}
        self.backend.alive = False
        self.absorb(record['id'], alive=False)
        stored = scans.ScansManager.get(record['id'])
        self.assertEqual(stored['status'], 'done')
        self.assertIsNotNone(stored['ended_at'])
        self.assertEqual(len(self.feed), 1)

    def test_a_dead_process_that_produced_nothing_is_failed_with_a_reason(self):
        """A bad model or credential exits at once, before any output."""
        record = self.create()
        self.backend.state = None
        self.backend.reason = ('The provider did not accept this key. The '
                               'scanner said: invalid key')
        self.absorb(record['id'], alive=False)
        stored = scans.ScansManager.get(record['id'])
        self.assertEqual(stored['status'], 'failed')
        self.assertEqual(stored['error'], self.backend.reason)

    def test_a_failure_the_backend_cannot_explain_still_says_something(self):
        """What the user saw on the live workspace: a failed scan, and no
        reason anywhere on the page."""
        record = self.create()
        self.backend.state = None
        self.backend.reason = ''
        self.absorb(record['id'], alive=False)
        stored = scans.ScansManager.get(record['id'])
        self.assertEqual(stored['status'], 'failed')
        self.assertIn('did not say why', stored['error'])

    def test_a_scan_the_scanner_failed_itself_gets_a_reason_too(self):
        """The scanner can write `failed` into its own record — a crash
        halfway — which reached the page with no reason at all, because only
        the dead-process path ever asked for one."""
        record = self.create()
        self.backend.state = {'run': {'status': 'failed',
                                      'llm_usage': {'requests': 40}},
                              'findings': []}
        self.backend.reason = 'The key has no quota left.'
        self.absorb(record['id'], alive=False)
        stored = scans.ScansManager.get(record['id'])
        self.assertEqual(stored['status'], 'failed')
        self.assertEqual(stored['error'], 'The key has no quota left.')

    def test_a_scan_the_scanner_ended_early_by_itself_says_why(self):
        """Its spending limit, or a provider refusing requests: the scanner
        records `stopped`, but nobody pressed Stop."""
        record = self.create()
        self.backend.state = {'run': {'status': 'stopped'}, 'findings': []}
        self.backend.reason = 'The scan reached its spending limit.'
        self.absorb(record['id'], alive=False)
        stored = scans.ScansManager.get(record['id'])
        self.assertEqual(stored['status'], 'stopped')
        self.assertEqual(stored['error'], 'The scan reached its spending limit.')
        self.assertIn('scanner ended', scans.result_summary(stored))

    def test_the_backends_cause_decides_the_end_reason(self):
        """The page picks "Model settings" by this, never by the wording."""
        record = self.create()
        self.backend.state = None
        self.backend.reason = 'The provider is busy right now.'
        self.backend.cause = 'provider'
        self.absorb(record['id'], alive=False)
        stored = scans.ScansManager.get(record['id'])
        self.assertEqual(scans.end_reason_of(stored), 'provider_error')
        self.assertEqual(scans.public_view(stored)['end_reason'],
                         'provider_error')

    def test_a_cause_the_manager_does_not_know_is_not_stored(self):
        record = self.create()
        self.backend.state = None
        self.backend.reason = 'Something odd.'
        self.backend.cause = 'cosmic-rays'
        self.absorb(record['id'], alive=False)
        stored = scans.ScansManager.get(record['id'])
        self.assertNotIn('end_cause', stored)
        self.assertEqual(scans.end_reason_of(stored), 'scanner_error')

    def test_a_scan_that_finished_is_not_asked_why(self):
        record = self.create()
        self.backend.state = {'run': {'status': 'completed'}, 'findings': []}
        self.absorb(record['id'], alive=False)
        self.assertEqual(self.backend.explained, 0)
        self.assertIsNone(scans.ScansManager.get(record['id'])['error'])

    def test_a_backend_that_raises_while_explaining_loses_no_status(self):
        record = self.create()
        self.backend.state = None

        def boom(handle):
            raise OSError('log vanished')
        self.backend.explain_end = boom
        with contextlib.redirect_stderr(io.StringIO()):
            self.absorb(record['id'], alive=False)
        stored = scans.ScansManager.get(record['id'])
        self.assertEqual(stored['status'], 'failed')
        self.assertTrue(stored['error'])

    def test_a_dead_process_that_was_working_is_interrupted_not_failed(self):
        record = self.create()
        self.backend.state = {'run': {'status': 'running'},
                              'findings': [{'id': 'v1', 'severity': 'low'}]}
        self.absorb(record['id'], alive=False)
        stored = scans.ScansManager.get(record['id'])
        self.assertEqual(stored['status'], 'interrupted')
        self.assertEqual(len(stored['findings']), 1)

    def test_a_credential_echoed_in_the_log_is_redacted_from_the_record(self):
        record = self.create()
        self.backend.state = None
        self.backend.reason = 'provider said: Authorization: Bearer sk-lmnopqrstuv'
        self.backend.evidence = 'console: Authorization: Bearer sk-lmnopqrstuv'
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.absorb(record['id'], alive=False)
        stored = scans.ScansManager.get(record['id'])
        self.assertNotIn('sk-lmnopqrstuv', stored['error'])
        self.assertNotIn('sk-lmnopqrstuv', err.getvalue())

    def test_a_terminal_scan_absorbs_nothing_further(self):
        record = self.create()
        scans.ScansManager.stop(record['id'])
        feed_before = len(self.feed)
        self.backend.state = {'run': {'status': 'completed'},
                              'findings': [{'id': 'v1', 'severity': 'high'}]}
        self.absorb(record['id'], alive=False)
        stored = scans.ScansManager.get(record['id'])
        self.assertEqual(stored['status'], 'stopped')
        self.assertEqual(len(self.feed), feed_before)


class DeleteTests(ManagerTestCase):

    def test_deleting_a_running_scan_announces_nothing(self):
        """delete() stops the scan first, and stop() announced -- posting a
        feed row and a `waiting` push whose "Open scan" link pointed at a
        directory removed on the very next line. The row outlived its target
        and the link 404d."""
        record = self.create()
        feed_before = len(self.feed)
        self.assertTrue(scans.ScansManager.delete(record['id']))
        self.assertEqual(len(self.feed), feed_before,
                         'delete posted a feed item for a deleted scan')
        self.assertIsNone(scans.ScansManager.get(record['id']))

    def test_stopping_a_running_scan_still_announces(self):
        """The announce must stay for the ordinary stop -- that is the signal
        the user is waiting on."""
        record = self.create()
        feed_before = len(self.feed)
        scans.ScansManager.stop(record['id'])
        self.assertEqual(len(self.feed), feed_before + 1)

    def test_stop_returns_none_once_the_scan_is_gone(self):
        """The handler reads the record, then stop() re-reads it; a DELETE
        landing between the two made `record.get(...)` raise and 500 a
        request that merely lost a race."""
        record = self.create()
        scans.ScansManager.delete(record['id'])
        self.assertIsNone(scans.ScansManager.stop(record['id']))


class PollerTests(ManagerTestCase):

    poller = True

    def test_the_poller_carries_a_scan_to_completion_on_its_own(self):
        scans.ScansManager.POLL_INTERVAL = 0.01
        self.backend.state = {'run': {'status': 'running'}, 'findings': []}
        record = self.create()
        self.backend.state = {'run': {'status': 'completed'},
                              'findings': [{'id': 'v1', 'severity': 'medium'}]}
        self.backend.alive = False
        deadline = time.time() + 5
        while time.time() < deadline:
            if scans.ScansManager.get(record['id'])['status'] != 'running':
                break
            time.sleep(0.02)
        stored = scans.ScansManager.get(record['id'])
        self.assertEqual(stored['status'], 'done')
        self.assertEqual(stored['counts']['medium'], 1)

    def test_a_poller_that_crashes_ends_the_scan_instead_of_leaving_it_running(self):
        """A crashed poller used to print one line and leave the scan on
        "Scanning…" forever, still spending, with nothing reading it."""
        scans.ScansManager.POLL_INTERVAL = 0.01
        self.backend.state = {'run': {'status': 'running'}, 'findings': []}

        def broken(handle):
            raise RuntimeError('unexpected shape')
        self.backend.is_alive = broken
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            record = self.create()
            deadline = time.time() + 5
            while time.time() < deadline:
                if scans.ScansManager.get(record['id'])['status'] != 'running':
                    break
                time.sleep(0.02)
        stored = scans.ScansManager.get(record['id'])
        self.assertEqual(stored['status'], 'failed')
        self.assertEqual(scans.end_reason_of(stored), 'lost_track')
        self.assertEqual(stored['error'], scans.ScansManager.LOST_TRACK_REASON)
        self.assertEqual(len(self.backend.stopped), 1, 'it was left running')
        self.assertIn('poller crashed', err.getvalue())
        self.assertIn('RuntimeError: unexpected shape', err.getvalue())


class AnnounceTests(ManagerTestCase):

    def finish(self, findings, status='completed', report=None):
        record = self.create()
        self.backend.state = {'run': {'status': status}, 'findings': findings,
                              'report': report}
        self.backend.alive = False
        scans.ScansManager._absorb(record['id'], self.backend.state,
                                   alive=False, backend=self.backend,
                                   handle={})
        return self.feed[-1]

    def test_a_scan_with_findings_asks_for_attention(self):
        """`waiting` is what turns a feed item into a phone notification."""
        _, kwargs = self.finish([{'id': 'v1', 'severity': 'critical'}])
        self.assertTrue(kwargs['waiting'])

    def test_a_clean_scan_is_recorded_without_notifying_anyone(self):
        """A tool that pings on every all-clear gets muted before it matters."""
        _, kwargs = self.finish([])
        self.assertFalse(kwargs['waiting'])

    def test_the_item_deep_links_to_the_scan(self):
        args, kwargs = self.finish([{'id': 'v1', 'severity': 'high'}])
        self.assertEqual(kwargs['links'][0]['ref'].split(':')[0], 'scan')

    def test_one_item_per_scan_even_if_announced_twice(self):
        """Coalescing is what keeps a scan from stacking rows in the feed."""
        _, kwargs = self.finish([{'id': 'v1', 'severity': 'high'}])
        self.assertTrue(kwargs['dedupe_key'].startswith('scan:'))

    def test_the_body_never_reports_a_bare_zero(self):
        _, kwargs = self.finish([])
        self.assertIn('quick', kwargs['body_md'])

    def test_a_failed_scan_tells_the_phone_why(self):
        self.backend.reason = 'The provider is busy right now.'
        _, kwargs = self.finish([], status='failed')
        self.assertIn('could not run', kwargs['body_md'])
        self.assertIn('The provider is busy right now.', kwargs['body_md'])

    def test_a_failed_scan_is_not_titled_finished(self):
        """The notification's first line is all most people read."""
        args, _ = self.finish([], status='failed')
        self.assertEqual(args[1], 'Security scan of shop could not finish')

    def test_the_item_says_where_the_report_is_saved(self):
        """The report outlives the notification, so it says where to find it."""
        report = {'text': '# Summary', 'location': '/runs/r1/report.md'}
        _, kwargs = self.finish([], report=report)
        self.assertIn("report is saved at `/runs/r1/report.md`", kwargs['body_md'])

    def test_no_report_means_no_location_line(self):
        _, kwargs = self.finish([])
        self.assertNotIn('report is saved', kwargs['body_md'])

    def test_the_server_log_names_where_the_report_landed(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.finish([], report={'text': 'x', 'location': '/runs/r1/report.md'})
        self.assertIn('report saved at /runs/r1/report.md', err.getvalue())


class StopTests(ManagerTestCase):

    def test_stopping_asks_the_backend_and_settles_the_record(self):
        record = self.create()
        scans.ScansManager.stop(record['id'])
        self.assertEqual(len(self.backend.stopped), 1)
        stored = scans.ScansManager.get(record['id'])
        self.assertEqual(stored['status'], 'stopped')
        self.assertIsNotNone(stored['ended_at'])

    def test_stopping_an_already_finished_scan_changes_nothing(self):
        record = self.create()
        scans.ScansManager.stop(record['id'])
        calls = len(self.backend.stopped)
        scans.ScansManager.stop(record['id'])
        self.assertEqual(len(self.backend.stopped), calls)

    def test_stopping_an_unknown_scan_is_not_an_error(self):
        self.assertIsNone(scans.ScansManager.stop('scn_000000000000'))

    def test_a_backend_that_fails_to_stop_still_settles_the_record(self):
        """Otherwise a scan the user stopped sits at `running` forever."""
        record = self.create()
        def boom(handle):
            raise OSError('no such process')
        self.backend.stop = boom
        with contextlib.redirect_stderr(io.StringIO()):
            scans.ScansManager.stop(record['id'])
        self.assertEqual(scans.ScansManager.get(record['id'])['status'],
                         'stopped')

    def test_the_scanners_own_word_for_the_signal_does_not_turn_it_into_a_crash(self):
        """Stopping the scanner makes it write `interrupted` into its run
        record — its word for any signal, a crash included. Pressing Stop
        on the live workspace therefore produced "the scanner process ended
        unexpectedly"."""
        record = self.create()

        def scanner_exits():
            self.backend.state = {'run': {'status': 'interrupted',
                                          'llm_usage': {'cost': 0.63}},
                                  'findings': []}
        self.backend.on_stop = scanner_exits
        scans.ScansManager.stop(record['id'])
        stored = scans.ScansManager.get(record['id'])
        self.assertEqual(stored['status'], 'stopped')
        self.assertIsNone(stored['error'])
        summary = scans.result_summary(stored)
        self.assertIn('You stopped', summary)
        self.assertNotIn('unexpectedly', summary)

    def test_the_poller_reading_mid_stop_also_settles_it_as_stopped(self):
        """The race itself: the scanner takes seconds to exit, and the
        poller reads the record every few. Its read lands while stop() is
        still waiting on the process."""
        record = self.create()

        def poller_reads_meanwhile():
            scans.ScansManager._absorb(
                record['id'], {'run': {'status': 'interrupted'}, 'findings': []},
                alive=False, backend=self.backend, handle={})
        self.backend.on_stop = poller_reads_meanwhile
        scans.ScansManager.stop(record['id'])
        stored = scans.ScansManager.get(record['id'])
        self.assertEqual(stored['status'], 'stopped')
        self.assertNotIn('interrupted_reason', stored)
        self.assertEqual(self.backend.explained, 0,
                         'a stop the user asked for owes no reason')
        self.assertEqual(len(self.feed), 1, 'announced exactly once')

    def test_a_process_that_simply_vanishes_mid_stop_is_still_stopped(self):
        record = self.create()
        self.backend.on_stop = lambda: scans.ScansManager._absorb(
            record['id'], None, alive=False, backend=self.backend, handle={})
        scans.ScansManager.stop(record['id'])
        self.assertEqual(scans.ScansManager.get(record['id'])['status'],
                         'stopped')

    def test_the_spend_the_scanner_saved_on_its_way_out_is_kept(self):
        record = self.create()

        def scanner_exits():
            self.backend.state = {'run': {'status': 'interrupted',
                                          'llm_usage': {'cost': 0.63}},
                                  'findings': [{'id': 'v1', 'severity': 'low'}]}
        self.backend.on_stop = scanner_exits
        scans.ScansManager.stop(record['id'])
        stored = scans.ScansManager.get(record['id'])
        self.assertAlmostEqual(stored['usage']['cost_usd'], 0.63)
        self.assertEqual(len(stored['findings']), 1)

    def test_a_scan_that_completed_as_stop_was_pressed_says_it_completed(self):
        record = self.create()

        def scanner_finishes():
            self.backend.state = {'run': {'status': 'completed'}, 'findings': []}
        self.backend.on_stop = scanner_finishes
        scans.ScansManager.stop(record['id'])
        self.assertEqual(scans.ScansManager.get(record['id'])['status'], 'done')


class ServerLogTests(ManagerTestCase):
    """A scan that ends badly leaves its evidence in the server log — the
    page shows only the plain reason."""

    def fail(self):
        record = self.create()
        self.backend.state = None
        self.backend.reason = 'The provider is busy right now.'
        self.backend.evidence = ('exit status: 1\n'
                                 'console output: /scans/x/scanner.log')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            scans.ScansManager._absorb(record['id'], None, alive=False,
                                       backend=self.backend, handle={})
        return record['id'], err.getvalue()

    def test_a_failed_scan_is_logged_with_its_evidence(self):
        scan_id, log = self.fail()
        self.assertIn(f'[scan] {scan_id} ended failed after', log)
        self.assertIn('reason shown: The provider is busy right now.', log)
        self.assertIn('exit status: 1', log)
        self.assertIn('/scans/x/scanner.log', log)
        # Every line is tagged, so a grep for "[scan]" finds the whole entry.
        self.assertTrue(all(ln.startswith('[scan]')
                            for ln in log.strip().splitlines()), log)

    def test_the_evidence_never_reaches_the_record(self):
        scan_id, _ = self.fail()
        stored = scans.ScansManager.get(scan_id)
        self.assertNotIn('scanner.log', stored['error'])

    def test_a_refused_start_is_logged(self):
        self.backend.resolve_error = 'Nothing answered on port 3000.'
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            scans.ScansManager.create(self.spec())
        self.assertIn('[scan] start refused for port 3000: Nothing answered',
                      err.getvalue())

    def test_a_stop_the_user_asked_for_is_not_logged_as_a_failure(self):
        record = self.create()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            scans.ScansManager.stop(record['id'])
        self.assertNotIn('ended', err.getvalue())
        self.assertIn(f'[scan] {record["id"]} stop requested by the user',
                      err.getvalue())
        self.assertIn('stop complete: stopped (user_stopped)', err.getvalue())

    def test_a_start_is_logged_with_what_it_will_cost_but_never_a_key(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            record = self.create(budget_usd=5)
        self.assertIn(f'[scan] {record["id"]} started on port 3000 (shop): '
                      f'quick scan, model prov/model, budget 5.0',
                      err.getvalue())

    def test_a_kill_that_did_not_take_is_logged_loudly(self):
        record = self.create()
        self.backend.stop = lambda handle: None     # the signal changed nothing
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            stored = scans.ScansManager.stop(record['id'])
        self.assertIn('STILL RUNNING', err.getvalue())
        # The user's stop still stands on the page.
        self.assertEqual(stored['status'], 'stopped')


class DispositionTests(ManagerTestCase):

    def scan_with_finding(self):
        record = self.create()
        self.backend.state = {'run': {'status': 'running'},
                              'findings': [{'id': 'v1', 'severity': 'high',
                                            'title': 'SQLi'}]}
        scans.ScansManager._absorb(record['id'], self.backend.state,
                                   alive=True, backend=self.backend, handle={})
        return record['id']

    def test_dismissing_records_it_beside_the_finding_not_inside_it(self):
        scan_id = self.scan_with_finding()
        scans.ScansManager.set_disposition(scan_id, 'v1', 'dismissed')
        stored = scans.ScansManager.get(scan_id)
        self.assertEqual(stored['dispositions'], {'v1': 'dismissed'})
        self.assertEqual(stored['findings'][0],
                         {'id': 'v1', 'severity': 'high', 'title': 'SQLi'})

    def test_reopening_removes_the_mark(self):
        scan_id = self.scan_with_finding()
        scans.ScansManager.set_disposition(scan_id, 'v1', 'dismissed')
        scans.ScansManager.set_disposition(scan_id, 'v1', 'open')
        self.assertEqual(scans.ScansManager.get(scan_id)['dispositions'], {})

    def test_an_unknown_finding_or_disposition_is_refused(self):
        scan_id = self.scan_with_finding()
        _, err = scans.ScansManager.set_disposition(scan_id, 'nope', 'dismissed')
        self.assertIsNotNone(err)
        _, err = scans.ScansManager.set_disposition(scan_id, 'v1', 'deleted')
        self.assertIsNotNone(err)

    def test_a_disposition_survives_the_next_artifact_read(self):
        """The scanner keeps rewriting its findings; the user's decision about
        one must not be erased by the next poll."""
        scan_id = self.scan_with_finding()
        scans.ScansManager.set_disposition(scan_id, 'v1', 'dismissed')
        self.backend.state = {'run': {'status': 'running'},
                              'findings': [{'id': 'v1', 'severity': 'critical',
                                            'title': 'SQLi'}]}
        scans.ScansManager._absorb(scan_id, self.backend.state, alive=True,
                                   backend=self.backend, handle={})
        self.assertEqual(scans.ScansManager.get(scan_id)['dispositions'],
                         {'v1': 'dismissed'})


class BootSweepTests(ManagerTestCase):

    def test_a_scan_whose_process_died_is_marked_interrupted(self):
        """Issue #462's failure mode: a status that sits at `running` forever
        because the thing it described is long gone."""
        record = self.create()
        self.backend.alive = False
        touched = scans.ScansManager.start()
        self.assertEqual(touched, [record['id']])
        stored = scans.ScansManager.get(record['id'])
        self.assertEqual(stored['status'], 'interrupted')
        self.assertIn('restarted', stored['error'])

    def test_a_restart_in_the_middle_of_a_stop_keeps_it_a_stop(self):
        record = self.create()

        def mark(rec):
            rec['stop_requested_at'] = time.time()
        scans.ScansManager._record(record['id']).update(mark)
        self.backend.alive = False
        with contextlib.redirect_stderr(io.StringIO()):
            scans.ScansManager.start()
        stored = scans.ScansManager.get(record['id'])
        self.assertEqual(stored['status'], 'stopped')
        self.assertIsNone(stored['error'])

    def test_a_scan_still_genuinely_running_is_resumed_not_killed(self):
        record = self.create()
        self.backend.alive = True
        self.assertEqual(scans.ScansManager.start(), [])
        self.assertEqual(scans.ScansManager.get(record['id'])['status'],
                         'running')

    def test_finished_scans_are_left_alone(self):
        record = self.create()
        scans.ScansManager.stop(record['id'])
        self.assertEqual(scans.ScansManager.start(), [])
        self.assertEqual(scans.ScansManager.get(record['id'])['status'],
                         'stopped')

    def test_the_sweep_survives_a_store_that_does_not_exist_yet(self):
        os.environ['KC_SCANS_DIR'] = os.path.join(
            os.environ['KC_SCANS_DIR'], 'never-created')
        self.assertEqual(scans.ScansManager.start(), [])


class ListingTests(ManagerTestCase):

    def test_scans_are_listed_newest_first_without_findings(self):
        first = self.create()
        second = self.create()
        rows = scans.ScansManager.list_scans()
        self.assertEqual([r['id'] for r in rows][:2],
                         [second['id'], first['id']])
        self.assertNotIn('findings', rows[0])
        self.assertNotIn('handle', rows[0])

    def test_a_stray_directory_is_ignored_rather_than_crashing_the_list(self):
        self.create()
        os.makedirs(os.path.join(scans.ScansManager.scans_dir(), 'not-a-scan'),
                    exist_ok=True)
        self.assertEqual(len(scans.ScansManager.list_scans()), 1)

    def test_an_unknown_or_malformed_id_reads_as_nothing(self):
        for bad in ('scn_000000000000', '../etc', 'nope'):
            self.assertIsNone(scans.ScansManager.get(bad))

    def test_deleting_removes_the_scan_and_everything_it_produced(self):
        record = self.create()
        directory = scans.ScansManager.scan_dir(record['id'])
        self.assertTrue(os.path.isdir(directory))
        self.assertTrue(scans.ScansManager.delete(record['id']))
        self.assertFalse(os.path.exists(directory))
        self.assertIsNone(scans.ScansManager.get(record['id']))

    def test_pruning_keeps_the_newest_and_never_touches_a_running_scan(self):
        scans.ScansManager.MAX_SCANS_KEPT = 2
        self.addCleanup(setattr, scans.ScansManager, 'MAX_SCANS_KEPT', 100)
        finished = []
        for _ in range(4):
            record = self.create()
            scans.ScansManager.stop(record['id'])
            finished.append(record['id'])
        live = self.create()
        scans.ScansManager._prune()
        ids = {r['id'] for r in scans.ScansManager.list_scans()}
        self.assertIn(live['id'], ids)
        self.assertNotIn(finished[0], ids)
        self.assertEqual(len(ids), 3)   # 2 kept + the running one


if __name__ == '__main__':
    unittest.main()
