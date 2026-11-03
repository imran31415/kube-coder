"""Tests for the keeper's sleep poll — `keeper_idle` + /api/keeper/idle (#728).

Two halves, because the module is split that way on purpose: the probes and
the decision are pure functions with their sources injected, so the interesting
cases (a signal that cannot be read, a clock that jumped, a Build parked on a
question) are ordinary assertions rather than a cluster.

The property worth most of this file is the FAIL-SAFE inversion. Every
ambiguity has to come out "busy": the cost of a false "idle" is a killed agent
and a lost terminal, and the cost of a false "busy" is a workspace that stays
up a bit longer. Several tests below exist only to pin which way each ambiguity
falls, so a later refactor that "simplifies" one of them fails here instead of
in somebody's session.

Run with:
    cd charts/workspace && python3 -m unittest tests.keeper_api_test
"""

import json
import os
import shutil
import socket
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import keeper_idle  # noqa: E402
import server  # noqa: E402
from handlers import system as system_routes  # noqa: E402
from tests.http_harness import EndpointTestCase, handler_for  # noqa: E402


# --- /proc/net/tcp fixtures --------------------------------------------------
#
# The encoders below are written by hand rather than reusing the module's
# parser, so the test does not agree with the code by construction: an inverted
# byte order in `_parse_hex_addr` has to show up as a wrong port or a remote
# that stops looking like loopback.

def _hex4(ip, port):
    return f'{socket.inet_aton(ip)[::-1].hex().upper()}:{port:04X}'


def _hex6(ip, port):
    packed = socket.inet_pton(socket.AF_INET6, ip)
    words = b''.join(packed[i:i + 4][::-1] for i in range(0, 16, 4))
    return f'{words.hex().upper()}:{port:04X}'


_TCP_HEADER = ('  sl  local_address rem_address   st tx_queue rx_queue tr '
               'tm->when retrnsmt   uid  timeout inode')


def _proc_tcp(rows, *, v6=False):
    """A /proc/net/tcp{,6} body from `(local_port, remote_ip, state)` rows."""
    enc = _hex6 if v6 else _hex4
    local_ip = '::' if v6 else '0.0.0.0'
    lines = [_TCP_HEADER]
    for i, (port, remote, state) in enumerate(rows):
        lines.append(f'  {i}: {enc(local_ip, port)} {enc(remote, 55555)} '
                     f'{state} 00000000:00000000 00:00000000 00000000  1000 '
                     f'      0 1234 1 0000000000000000 100 0 0 10 0')
    return '\n'.join(lines) + '\n'


class ProcFiles:
    """Temp files standing in for /proc/net/tcp and /proc/net/tcp6."""

    def __init__(self, v4=(), v6=()):
        self.dir = tempfile.mkdtemp()
        self.paths = []
        for name, rows, is6 in (('tcp', v4, False), ('tcp6', v6, True)):
            path = os.path.join(self.dir, name)
            with open(path, 'w') as fh:
                fh.write(_proc_tcp(rows, v6=is6))
            self.paths.append(path)

    def cleanup(self):
        for path in self.paths:
            os.unlink(path)
        os.rmdir(self.dir)


class ConnectionProbeTests(unittest.TestCase):

    def tearDown(self):
        proc = getattr(self, 'proc', None)
        if proc:
            proc.cleanup()

    def _proc(self, v4=(), v6=()):
        self.proc = ProcFiles(v4, v6)
        return self.proc.paths

    def test_counts_only_established_on_the_asked_port(self):
        paths = self._proc(v4=[(7681, '10.42.0.9', '01'),   # counted
                               (7681, '10.42.0.9', '06'),   # TIME_WAIT
                               (8080, '10.42.0.9', '01')])   # other port
        self.assertEqual(keeper_idle.established(7681, proc_files=paths), 1)
        self.assertEqual(keeper_idle.established(8080, proc_files=paths), 1)
        self.assertEqual(keeper_idle.established(22, proc_files=paths), 0)

    def test_ipv6_and_v4_mapped_rows_count(self):
        paths = self._proc(v6=[(6080, '2001:db8::5', '01'),
                               (6080, '::ffff:10.42.0.9', '01')])
        self.assertEqual(keeper_idle.established(6080, proc_files=paths), 2)

    def test_loopback_is_excluded_only_when_asked(self):
        # The pod's own probes and MCP servers hold loopback connections to
        # 6080 all day; counting them would pin the workspace awake for ever.
        paths = self._proc(v4=[(6080, '127.0.0.1', '01'),
                               (6080, '10.42.0.9', '01')],
                           v6=[(6080, '::1', '01')])
        self.assertEqual(keeper_idle.established(6080, proc_files=paths), 3)
        self.assertEqual(
            keeper_idle.established(6080, exclude_loopback=True,
                                    proc_files=paths), 1)

    def test_unreadable_proc_is_none_not_zero(self):
        self.assertIsNone(keeper_idle.established(
            7681, proc_files=('/nonexistent/tcp',)))

    def test_unreadable_proc_becomes_an_unknown_signal(self):
        sig = keeper_idle.probe_connections((7681,),
                                            proc_files=('/nonexistent/tcp',))
        self.assertFalse(sig['known'])
        self.assertTrue(sig['busy'], 'an unreadable source must block sleep')

    def test_a_named_peer_is_excluded(self):
        # The keeper's own poll arrives from another pod, so without this it
        # would read as a dashboard client every 60s for ever.
        paths = self._proc(v4=[(6080, '10.42.0.7', '01'),    # the keeper
                               (6080, '10.42.0.9', '01')])   # a real browser
        self.assertEqual(keeper_idle.established(
            6080, exclude_loopback=True, exclude_peers=('10.42.0.7',),
            proc_files=paths), 1)
        self.assertEqual(keeper_idle.established(
            6080, exclude_loopback=True,
            exclude_peers=('10.42.0.7', '10.42.0.9'), proc_files=paths), 0)

    def test_a_malformed_peer_is_ignored_not_fatal(self):
        paths = self._proc(v4=[(6080, '10.42.0.9', '01')])
        self.assertEqual(keeper_idle.established(
            6080, exclude_peers=('not-an-ip', ''), proc_files=paths), 1)

    def test_signal_detail_reports_the_count(self):
        paths = self._proc(v4=[(7681, '10.42.0.9', '01')])
        sig = keeper_idle.probe_connections((7681, 22), label='terminal session',
                                            proc_files=paths)
        self.assertTrue(sig['busy'])
        self.assertEqual(sig['detail'], '1 terminal session')


# --- task / board-run probes -------------------------------------------------

class TaskProbeTests(unittest.TestCase):

    def test_running_task_is_busy(self):
        sig = keeper_idle.probe_tasks(
            lambda: [{'status': 'running', 'created_at': 100.0}])
        self.assertTrue(sig['busy'])
        self.assertEqual(sig['detail'], '1 live task of 1')

    def test_a_build_waiting_for_input_is_busy(self):
        # Not idle — that is a human's turn, and they are coming back to it.
        # No predicate injected at all, which is the conservative reading:
        # every waiting row stays live.
        sig = keeper_idle.probe_tasks(
            lambda: [{'status': 'waiting-for-input', 'created_at': 100.0}])
        self.assertTrue(sig['busy'])

    def test_terminal_statuses_are_quiet(self):
        sig = keeper_idle.probe_tasks(lambda: [
            {'status': 'completed', 'finished_at': 100.0},
            {'status': 'failed', 'finished_at': 90.0},
            {'status': 'killed', 'finished_at': 80.0},
        ])
        self.assertFalse(sig['busy'])
        self.assertEqual(sig['detail'], '0 live tasks of 3')

    def test_finished_tasks_still_pin_quiet_since(self):
        # A Build that ended a moment ago means the user was here a moment ago.
        sig = keeper_idle.probe_tasks(lambda: [
            {'status': 'completed', 'created_at': 10.0, 'finished_at': 500.0},
            {'status': 'completed', 'created_at': 5.0, 'last_activity_at': 20.0},
        ])
        self.assertEqual(sig['since'], 500.0)

    def test_no_tasks_has_no_timestamp(self):
        sig = keeper_idle.probe_tasks(lambda: [])
        self.assertFalse(sig['busy'])
        self.assertIsNone(sig['since'])

    def test_a_failing_listing_is_unknown_and_blocks(self):
        def boom():
            raise OSError('tasks dir gone')
        sig = keeper_idle.probe_tasks(boom)
        self.assertFalse(sig['known'])
        self.assertTrue(sig['busy'])

    def test_non_numeric_timestamps_are_ignored(self):
        sig = keeper_idle.probe_tasks(
            lambda: [{'status': 'completed', 'created_at': None,
                      'finished_at': 'yesterday', 'last_activity_at': True}])
        self.assertIsNone(sig['since'])


class FinishedUnattendedBuildTests(unittest.TestCase):
    """A finished unattended Build must not hold the workspace awake (#804).

    Builds run the interactive CLI and nothing closes the session when the work
    is done, so `waiting-for-input` is ALSO the resting state of every finished
    Build — which made one cron Build enough to keep a workspace awake for
    ever. The discriminator is the task's source, and the predicate is the real
    `ClaudeTaskManager._is_unattended_source` rather than a copy of its prefix
    list, so a change to that list shows up here.

    Each case states which way the ambiguity falls, and
    `test_every_case_here_flips_under_the_old_rule` proves these assertions are
    about the new rule and not about something that was already true.
    """

    predicate = staticmethod(server.ClaudeTaskManager._is_unattended_source)

    def probe(self, rows, predicate=None):
        return keeper_idle.probe_tasks(
            lambda: rows, self.predicate if predicate is None else predicate)

    def waiting(self, source, **extra):
        row = {'status': 'waiting-for-input', 'source': source,
               'last_activity_at': 100.0}
        row.update(extra)
        return row

    # --- quiet: nobody is in front of that terminal --------------------------

    def test_each_unattended_source_waiting_is_quiet(self):
        for source in ('webhook:gh-123', 'cron:nightly', 'desktop:mac-1',
                       'board:run-7', 'hypervisor-tool'):
            with self.subTest(source=source):
                sig = self.probe([self.waiting(source)])
                self.assertTrue(sig['known'])
                self.assertFalse(sig['busy'])
                self.assertEqual(sig['detail'],
                                 '0 live tasks of 1 (1 finished unattended)')

    def test_a_discounted_build_still_pins_quiet_since(self):
        # Quiet, but not quiet SINCE LONGER AGO than its last screen change:
        # the workspace still waits the full `idleMinutes` from there.
        sig = self.probe([self.waiting('cron:nightly', last_activity_at=500.0)])
        self.assertFalse(sig['busy'])
        self.assertEqual(sig['since'], 500.0)

    def test_the_detail_says_what_was_discounted(self):
        # "Why DID it sleep?" has to be answerable from the endpoint alone.
        sig = self.probe([{'status': 'running', 'source': 'cron:a',
                           'last_activity_at': 10.0},
                          self.waiting('webhook:b'),
                          self.waiting('board:c'),
                          {'status': 'completed', 'finished_at': 5.0}])
        self.assertTrue(sig['busy'])
        self.assertEqual(sig['detail'],
                         '1 live task of 4 (2 finished unattended)')

    # --- busy: a person's turn, or an unreadable source ----------------------

    def test_an_attended_build_waiting_is_still_busy(self):
        # The Build tab. A human is looking at that terminal, and sleep is
        # scale-to-0 — it would kill the session they are about to type into.
        for source in (None, '', 'manual'):
            with self.subTest(source=source):
                sig = self.probe([self.waiting(source)])
                self.assertTrue(sig['busy'])
                self.assertEqual(sig['detail'], '1 live task of 1')

    def test_an_unrecognised_source_waiting_is_busy(self):
        # Fail safe: only sources this server KNOWS nobody is watching are
        # discounted. Anything else — including page-watch:, which passes
        # auto_approve=False explicitly — keeps the workspace up.
        for source in ('page-watch:p1', 'some-new-caller', 'webhook',
                       'cronjob:nightly'):
            with self.subTest(source=source):
                self.assertTrue(self.probe([self.waiting(source)])['busy'])

    def test_a_missing_source_key_is_busy(self):
        sig = self.probe([{'status': 'waiting-for-input',
                           'last_activity_at': 100.0}])
        self.assertTrue(sig['busy'])

    def test_a_running_unattended_build_is_busy(self):
        # Only the ambiguous status is reinterpreted. `running` is work in
        # progress whatever started it.
        for source in ('cron:nightly', 'webhook:gh-123', 'board:run-7'):
            with self.subTest(source=source):
                sig = self.probe([{'status': 'running', 'source': source,
                                   'last_activity_at': 100.0}])
                self.assertTrue(sig['busy'])
                self.assertEqual(sig['detail'], '1 live task of 1')

    def test_a_raising_predicate_is_unknown_and_blocks(self):
        def boom(_source):
            raise KeyError('source')
        sig = self.probe([self.waiting('cron:nightly')], predicate=boom)
        self.assertFalse(sig['known'])
        self.assertTrue(sig['busy'])

    # --- the inversion -------------------------------------------------------

    def test_every_case_here_flips_under_the_old_rule(self):
        # The old rule — `status in {running, waiting-for-input}` with no
        # regard for the source — is `probe_tasks` with no predicate. Putting
        # it back has to make the quiet cases busy again, or the tests above
        # are not testing this fix.
        for source in ('webhook:gh-123', 'cron:nightly', 'desktop:mac-1',
                       'board:run-7', 'hypervisor-tool'):
            with self.subTest(source=source):
                rows = [self.waiting(source)]
                self.assertFalse(self.probe(rows)['busy'], 'new rule')
                self.assertTrue(keeper_idle.probe_tasks(lambda: rows)['busy'],
                                'old rule')


class UnattendedBuildFromDiskTests(unittest.TestCase):
    """The same rule over real `task.json` rows, through `list_tasks` (#804).

    The unit tests above hand `probe_tasks` dicts, so they cannot catch the
    wiring mistake that would actually break this in a pod: `list_tasks`
    not putting the source on the row under the key the predicate is given.
    These fixtures carry no `tmux_session`, which is what keeps
    `_reconcile_status` from deciding they are over.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self._save = server.ClaudeTaskManager.TASKS_DIR
        server.ClaudeTaskManager.TASKS_DIR = self.dir
        self.addCleanup(setattr, server.ClaudeTaskManager, 'TASKS_DIR',
                        self._save)

    def write_task(self, task_id, **meta):
        task_dir = os.path.join(self.dir, task_id)
        os.makedirs(task_dir, exist_ok=True)
        meta.setdefault('task_id', task_id)
        meta.setdefault('created_at', 100.0)
        with open(os.path.join(task_dir, 'task.json'), 'w') as fh:
            json.dump(meta, fh)

    def report(self):
        return keeper_idle.report(
            list_tasks=server.ClaudeTaskManager.list_tasks,
            list_runs=None, is_live=None,
            is_unattended=server.ClaudeTaskManager._is_unattended_source,
            idle_minutes=0, now=1000.0,
            proc_files=(os.path.join(self.dir, 'no-such-proc'),))

    def test_a_finished_cron_build_lets_the_workspace_sleep(self):
        self.write_task('t-cron', status='waiting-for-input',
                        source='cron:nightly', last_activity_at=200.0)
        out = self.report()
        self.assertNotIn('tasks', out['busy_signals'])
        self.assertEqual(out['signals']['tasks']['detail'],
                         '0 live tasks of 1 (1 finished unattended)')

    def test_a_build_tab_build_still_blocks(self):
        self.write_task('t-manual', status='waiting-for-input',
                        source='manual', last_activity_at=200.0)
        self.assertIn('tasks', self.report()['busy_signals'])


class BoardRunProbeTests(unittest.TestCase):

    def test_liveness_comes_from_the_injected_predicate(self):
        rows = [{'id': 'r1', 'status': 'running', 'updated_at': 42.0}]
        busy = keeper_idle.probe_board_runs(lambda: rows, lambda r: True)
        quiet = keeper_idle.probe_board_runs(lambda: rows, lambda r: False)
        self.assertTrue(busy['busy'])
        self.assertFalse(quiet['busy'])
        self.assertEqual(quiet['since'], 42.0)

    def test_absent_boards_package_is_known_quiet(self):
        # `_BOARDS_AVAILABLE=False` means there are no board runs at all, so
        # calling it "unknown" would make such a workspace un-sleepable.
        sig = keeper_idle.probe_board_runs(None, None)
        self.assertTrue(sig['known'])
        self.assertFalse(sig['busy'])

    def test_a_raising_predicate_is_unknown_and_blocks(self):
        def boom(_run):
            raise KeyError('counts')
        sig = keeper_idle.probe_board_runs(lambda: [{'id': 'r1'}], boom)
        self.assertFalse(sig['known'])
        self.assertTrue(sig['busy'])


# --- the decision ------------------------------------------------------------

def _quiet(since=None):
    return keeper_idle.signal(False, since=since, detail='quiet')


def _signals(**overrides):
    base = {name: _quiet() for name in
            ('tasks', 'board_runs', 'terminals', 'code_server', 'dashboard')}
    base.update(overrides)
    return base


class DecisionTests(unittest.TestCase):

    def test_quiet_for_long_enough_is_idle(self):
        out = keeper_idle.decide(_signals(tasks=_quiet(since=1000.0)),
                                idle_minutes=30, now=1000.0 + 1801,
                                watching_since=0.0)
        self.assertTrue(out['idle'])
        self.assertEqual(out['busy_signals'], [])
        self.assertEqual(out['idle_threshold_s'], 1800)

    def test_quiet_but_too_recently_is_not_idle(self):
        out = keeper_idle.decide(_signals(tasks=_quiet(since=1000.0)),
                                idle_minutes=30, now=1000.0 + 60,
                                watching_since=0.0)
        self.assertFalse(out['idle'])
        self.assertEqual(out['idle_for_s'], 60.0)
        self.assertIn('needs 1800s', out['reason'])

    def test_one_busy_signal_blocks_however_long_the_quiet(self):
        out = keeper_idle.decide(
            _signals(tasks=keeper_idle.signal(True, detail='1 live task of 3')),
            idle_minutes=0, now=10_000.0, watching_since=0.0)
        self.assertFalse(out['idle'])
        self.assertEqual(out['busy_signals'], ['tasks'])
        self.assertEqual(out['reason'], 'busy: 1 live task of 3')

    def test_an_unknown_signal_blocks(self):
        out = keeper_idle.decide(
            _signals(dashboard=keeper_idle.unknown('/proc/net/tcp unreadable')),
            idle_minutes=0, now=10_000.0, watching_since=0.0)
        self.assertFalse(out['idle'])
        self.assertEqual(out['busy_signals'], ['dashboard'])

    def test_several_busy_signals_are_all_reported(self):
        out = keeper_idle.decide(
            _signals(tasks=keeper_idle.signal(True, detail='2 live tasks of 2'),
                     terminals=keeper_idle.signal(True, detail='1 terminal')),
            idle_minutes=0, now=10_000.0, watching_since=0.0)
        self.assertEqual(sorted(out['busy_signals']), ['tasks', 'terminals'])
        self.assertIn('+1 more', out['reason'])

    def test_quiet_since_never_predates_this_process(self):
        # A server that booted 10s ago cannot attest to an hour of quiet, even
        # though nothing on disk shows any activity at all.
        out = keeper_idle.decide(_signals(), idle_minutes=30, now=10_000.0,
                                 watching_since=9_990.0)
        self.assertEqual(out['quiet_since'], 9_990.0)
        self.assertEqual(out['idle_for_s'], 10.0)
        self.assertFalse(out['idle'])

    def test_the_newest_signal_wins(self):
        out = keeper_idle.decide(
            _signals(tasks=_quiet(since=1000.0),
                     board_runs=_quiet(since=5000.0)),
            idle_minutes=1, now=5060.0, watching_since=0.0)
        self.assertEqual(out['quiet_since'], 5000.0)
        self.assertTrue(out['idle'])

    def test_a_future_timestamp_does_not_go_negative(self):
        # Clock skew between the PVC's mtimes and now; idle_for must not wrap
        # into a huge number via a negative round-trip.
        out = keeper_idle.decide(_signals(tasks=_quiet(since=9_999_999.0)),
                                 idle_minutes=0, now=1000.0,
                                 watching_since=0.0)
        self.assertEqual(out['idle_for_s'], 0.0)
        self.assertEqual(out['quiet_since'], 1000.0)

    def test_zero_threshold_sleeps_as_soon_as_everything_is_quiet(self):
        out = keeper_idle.decide(_signals(), idle_minutes=0, now=1000.0,
                                 watching_since=1000.0)
        self.assertTrue(out['idle'])

    def test_report_is_json_serialisable(self):
        out = keeper_idle.report(list_tasks=lambda: [], list_runs=None,
                                 is_live=None, idle_minutes=5, now=1000.0,
                                 proc_files=('/nonexistent/tcp',))
        json.dumps(out)
        self.assertEqual(set(out['signals']),
                         {'tasks', 'board_runs', 'terminals', 'code_server',
                          'dashboard'})


class IdleMinutesParsingTests(unittest.TestCase):

    def test_absent_and_malformed_fall_back_to_the_default(self):
        for raw in (None, '', 'soon', '-5', 'nan', 'inf', '1e999'):
            self.assertEqual(keeper_idle.parse_idle_minutes(raw, default=30), 30,
                             raw)

    def test_a_number_is_taken(self):
        self.assertEqual(keeper_idle.parse_idle_minutes('45', default=30), 45)
        self.assertEqual(keeper_idle.parse_idle_minutes('0', default=30), 0)
        self.assertEqual(keeper_idle.parse_idle_minutes('1.5', default=30), 1.5)


# --- the endpoint ------------------------------------------------------------

class KeeperRouteTests(unittest.TestCase):

    def test_the_route_is_registered_and_named(self):
        self.assertEqual(
            handler_for(system_routes.ROUTES, 'GET', '/api/keeper/idle'),
            'send_keeper_idle')
        self.assertTrue(hasattr(server.BrowserHandler, 'send_keeper_idle'))

    def test_it_survives_the_spa_oauth_prefix(self):
        # The dashboard SPA prefixes its calls with /oauth; server.py normalizes
        # that off before dispatch, so a normalized-path route still matches.
        self.assertEqual(
            handler_for(system_routes.ROUTES, 'GET', '/api/keeper/idle',
                        raw='/oauth/api/keeper/idle?idle_minutes=5'),
            'send_keeper_idle')

    def test_near_misses_do_not_match(self):
        for path in ('/api/keeper', '/api/keeper/idlex', '/api/keeper/'):
            self.assertIsNone(
                handler_for(system_routes.ROUTES, 'GET', path), path)

    def test_it_is_read_only(self):
        self.assertIsNone(
            handler_for(system_routes.ROUTES, 'POST', '/api/keeper/idle'))


class KeeperEndpointAuthTests(EndpointTestCase):
    """Unauthenticated in oauth2 mode — server.py is its own enforcer there."""

    def test_no_credential_is_401(self):
        status, _ = self.get('/api/keeper/idle')
        self.assertEqual(status, 401)


class KeeperEndpointNoneModeTests(EndpointTestCase):
    """AUTH_MODE=none still 401s: allow_none_mode=False on this route.

    The public demo runs unauthenticated, and "is somebody at the keyboard
    right now" is an activity side-channel — and a wake decision.
    """

    auth_mode = 'none'

    def test_none_mode_does_not_open_the_endpoint(self):
        status, _ = self.get('/api/keeper/idle')
        self.assertEqual(status, 401)
        # Contrast: /metrics does short-circuit in none mode.
        self.assertEqual(self.get('/metrics')[0], 200)


class KeeperEndpointBasicModeTests(EndpointTestCase):
    """AUTH_MODE=basic answers with no credential at all — pinned, not endorsed.

    `check_claude_auth` returns True for basic mode before it looks at any
    header (server.py trusts the ingress's basic auth there). The keeper does
    not come through the ingress, so on a basic-mode workspace its poll needs
    no secret. That is today's behaviour; this test exists so that changing it
    is a decision somebody makes, and so nobody reads a green basic-mode e2e
    run as proof that the keeper's secret works (#728 step 5).
    """

    auth_mode = 'basic'

    def test_basic_mode_needs_no_credential(self):
        self.assertEqual(self.get('/api/keeper/idle')[0], 200)


SECRET = 'synthetic-keeper-secret-for-tests-0123456789'


class KeeperSecretTests(EndpointTestCase):
    """The keeper's shared secret opens the idle poll, and nothing else (#728 step 5).

    oauth2 mode, so `check_claude_auth` refuses everything these requests
    carry and the only way in is `X-Keeper-Secret`.
    """

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {'KEEPER_CONTROL_SECRET': SECRET})
        patcher.start()
        self.addCleanup(patcher.stop)

    def idle(self, headers):
        return self.get('/api/keeper/idle', headers=headers)[0]

    def test_the_right_secret_is_200(self):
        self.assertEqual(self.idle({'X-Keeper-Secret': SECRET}), 200)

    def test_the_header_name_is_case_insensitive(self):
        self.assertEqual(self.idle({'x-keeper-secret': SECRET}), 200)

    def test_surrounding_whitespace_is_ignored(self):
        self.assertEqual(self.idle({'X-Keeper-Secret': f'  {SECRET} '}), 200)

    def test_anything_else_is_401(self):
        for name, value in [
                ('wrong', 'not-the-secret'),
                ('empty', ''),
                ('blank', '   '),
                ('a prefix of it', SECRET[:-1]),
                ('it plus one', SECRET + 'x'),
                ('not ascii', SECRET[:-1] + chr(0xe9))]:
            with self.subTest(name):
                headers = {'X-Keeper-Secret': value.encode('utf-8').decode('latin-1')}
                self.assertEqual(self.idle(headers), 401)

    def test_no_secret_configured_refuses_even_a_matching_empty_header(self):
        # A pod whose Secret is missing must close this door, not open it:
        # an empty expected value never matches, whatever is sent.
        for configured in ('', '   '):
            with self.subTest(configured=configured), \
                    mock.patch.dict(os.environ, {'KEEPER_CONTROL_SECRET': configured}):
                self.assertEqual(self.idle({'X-Keeper-Secret': configured}), 401)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('KEEPER_CONTROL_SECRET', None)
            self.assertEqual(self.idle({'X-Keeper-Secret': SECRET}), 401)

    def test_the_secret_is_not_a_bearer_token_here(self):
        # The keeper never sends it as a bearer; accepting it as one would make
        # it interchangeable with the API token.
        self.assertEqual(self.idle({'Authorization': f'Bearer {SECRET}'}), 401)

    def test_the_secret_opens_no_other_route(self):
        # The privilege-escalation check: the keeper's secret answers "are you
        # busy?" and must not become a way to read tasks or start one. Each
        # route below is gated by check_claude_auth and 401s without auth.
        for path in ('/api/claude/tasks', '/api/files/list', '/api/feed', '/metrics'):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 401, 'precondition: gated')
                for headers in ({'X-Keeper-Secret': SECRET},
                                {'Authorization': f'Bearer {SECRET}'}):
                    self.assertEqual(self.get(path, headers=headers)[0], 401)


class KeeperEndpointBodyTests(EndpointTestCase):
    """With auth satisfied, the endpoint answers the keeper's poll."""

    @classmethod
    def setUpClass(cls):
        cls._auth_save = server.BrowserHandler.check_claude_auth
        server.BrowserHandler.check_claude_auth = lambda self, **kw: True
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        server.BrowserHandler.check_claude_auth = cls._auth_save

    def test_shape(self):
        status, body = self.get('/api/keeper/idle')
        self.assertEqual(status, 200)
        data = json.loads(body)
        for key in ('idle', 'idle_for_s', 'quiet_since', 'idle_threshold_s',
                    'busy_signals', 'reason', 'signals', 'now'):
            self.assertIn(key, data)
        self.assertIsInstance(data['idle'], bool)
        self.assertEqual(set(data['signals']),
                         {'tasks', 'board_runs', 'terminals', 'code_server',
                          'dashboard'})

    def test_idle_minutes_override_is_echoed(self):
        _, body = self.get('/api/keeper/idle?idle_minutes=7')
        self.assertEqual(json.loads(body)['idle_threshold_s'], 420)

    def test_a_nonsense_override_falls_back_to_the_default(self):
        _, body = self.get('/api/keeper/idle?idle_minutes=forever')
        self.assertEqual(json.loads(body)['idle_threshold_s'],
                         keeper_idle.DEFAULT_IDLE_MINUTES * 60)

    def test_the_task_source_predicate_is_the_real_one(self):
        # The endpoint must hand `probe_tasks` the manager's own definition of
        # unattended (#804), not leave it None — which would quietly restore
        # the old "every waiting Build is live" rule in the pod while every
        # unit test above still passed.
        seen = {}
        real = keeper_idle.report

        def spy(**kwargs):
            seen.update(kwargs)
            return real(**kwargs)

        keeper_idle.report = spy
        try:
            self.assertEqual(self.get('/api/keeper/idle')[0], 200)
        finally:
            keeper_idle.report = real
        predicate = seen.get('is_unattended')
        self.assertTrue(callable(predicate))
        self.assertTrue(predicate('cron:nightly'))
        self.assertFalse(predicate('manual'))

    def test_the_caller_is_not_counted_as_a_dashboard_client(self):
        # The endpoint passes the requesting peer to `exclude_peers`. Asserted
        # here at the wiring level only — that the argument is threaded through
        # at all — because the real 6080 of a live pod is not a fixture. The
        # exclusion itself is pinned hermetically in ConnectionProbeTests.
        seen = {}
        real = keeper_idle.report

        def spy(**kwargs):
            seen.update(kwargs)
            return real(**kwargs)

        keeper_idle.report = spy
        try:
            self.assertEqual(self.get('/api/keeper/idle')[0], 200)
        finally:
            keeper_idle.report = real
        self.assertEqual(seen.get('exclude_peers'), ('127.0.0.1',))


if __name__ == '__main__':
    unittest.main()
