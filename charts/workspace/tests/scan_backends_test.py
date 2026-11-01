"""Tests for the scan backend seam (#726).

Nothing here spawns a process, talks to a container runtime or reaches a
network: `_SubprocessRunner` is the one place those calls live, so the suite
passes a stub and asserts on what WOULD have been run. That is the point of
the split — the backend's decisions are testable, and the decisions are where
the bugs that matter live.

The test that earns this file is `SeamConformanceTests`: a second, entirely
unrelated backend satisfying the same ABC. The feature was approved on the
condition that moving scans off this pod is a backend swap, and a seam nobody
has ever implemented twice is a seam that does not work yet.

Run with:
    cd charts/workspace && python3 -m unittest tests.scan_backends_test
"""

import ast
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import scan_backends  # noqa: E402
import scans  # noqa: E402


def inspect_output(*gateways):
    """`docker network inspect bridge` output, trimmed to the fields read.

    The shape is the real one — a list of networks, each with IPAM.Config
    entries — as the live workspace's docker:27-dind daemon prints it.
    """
    return json.dumps([{
        'Name': 'bridge',
        'Driver': 'bridge',
        'IPAM': {'Driver': 'default', 'Options': None,
                 'Config': [{'Subnet': '172.17.0.0/16', 'Gateway': g}
                            for g in gateways]},
    }], indent=4)


class StubRunner:
    """Records what the backend asked the OS to do, and answers as told."""

    def __init__(self, *, network=None, runtime=(True, ''), answers=True,
                 exit_code=None):
        self.network = (network if network is not None
                        else (inspect_output('172.17.0.1'), ''))
        self.runtime = runtime
        self.answers = answers
        self.exit_code = exit_code
        self.inspected = []
        self.probed = []
        self.spawned = []
        self.terminated = []

    def alive(self, pid):
        return scan_backends.process_alive(pid)

    def exit_status(self, pid):
        return self.exit_code

    def network_inspect(self, name):
        self.inspected.append(name)
        return self.network

    def port_answers(self, host, port):
        self.probed.append((host, port))
        return self.answers

    def container_runtime_ready(self):
        return self.runtime

    def spawn(self, argv, *, cwd, env, log_path):
        self.spawned.append({'argv': argv, 'cwd': cwd, 'env': env,
                             'log_path': log_path})
        return 4242

    def terminate(self, pid, *, grace=0):
        self.terminated.append(pid)


def backend(**kw):
    kw.setdefault('runner', StubRunner())
    kw.setdefault('executable', sys.executable)   # a path that exists
    kw.setdefault('env', {})
    return scan_backends.LocalStrixBackend(**kw)


class GatewayParsingTests(unittest.TestCase):

    def test_the_gateway_is_read_from_real_inspect_output(self):
        out = inspect_output('172.17.0.1')
        self.assertEqual(scan_backends.parse_bridge_gateway(out), '172.17.0.1')

    def test_a_different_gateway_is_honoured_not_assumed(self):
        """172.17.0.1 is a convention the runtime may change. Assuming it is
        the silent-empty-result failure."""
        self.assertEqual(
            scan_backends.parse_bridge_gateway(inspect_output('10.200.0.1')),
            '10.200.0.1')

    def test_an_ipv6_entry_beside_the_ipv4_one_is_passed_over(self):
        """A dual-stack bridge lists both. The URL is built for IPv4."""
        out = inspect_output('fd00:dead:beef::1', '172.18.0.1')
        self.assertEqual(scan_backends.parse_bridge_gateway(out), '172.18.0.1')

    def test_output_without_a_gateway_yields_nothing(self):
        for out in ('', None, '[]', '{}', 'garbage',
                    'Error response from daemon: network bridge not found',
                    '[{"Name": "bridge", "IPAM": {"Config": []}}]',
                    '[{"IPAM": {"Config": [{"Subnet": "172.17.0.0/16"}]}}]',
                    '[{"IPAM": null}]', '["bridge"]'):
            self.assertIsNone(scan_backends.parse_bridge_gateway(out), out)

    def test_a_non_address_is_refused_rather_than_interpolated(self):
        """The value lands in a URL, so anything unexpected is rejected —
        including addresses that would point the sandbox back at itself."""
        for gateway in ('not-an-ip', '172.17.0.1;rm -rf /',
                        '999.999.999.999', '172.17.0.1:80', 3232235521,
                        '127.0.0.1', '0.0.0.0', ''):
            self.assertIsNone(
                scan_backends.parse_bridge_gateway(inspect_output(gateway)),
                gateway)

    def test_resolve_target_builds_the_url_from_what_it_read(self):
        b = backend(runner=StubRunner(network=(inspect_output('10.0.0.1'), '')))
        self.assertEqual(b.resolve_target(3000), 'http://10.0.0.1:3000')

    def test_resolve_target_refuses_to_guess_when_it_cannot_tell(self):
        b = backend(runner=StubRunner(network=('', '')))
        with self.assertRaises(scan_backends.BackendError) as ctx:
            b.resolve_target(3000)
        self.assertIn('check nothing', str(ctx.exception))

    def test_the_runtimes_own_error_reaches_the_user(self):
        """The first release dropped the cause, so a missing tool read exactly
        like every other failure and could not be diagnosed from the UI."""
        why = 'Error response from daemon: network bridge not found'
        b = backend(runner=StubRunner(network=('', why)))
        with self.assertRaises(scan_backends.BackendError) as ctx:
            b.resolve_target(3000)
        self.assertIn(why, str(ctx.exception))

    def test_the_default_network_is_the_one_the_sandbox_joins_unset(self):
        runner = StubRunner()
        backend(runner=runner).resolve_target(3000)
        self.assertEqual(runner.inspected, ['bridge'])

    def test_the_scanners_own_network_setting_is_followed(self):
        """Set, the sandbox joins that network instead, so its gateway is the
        one to read."""
        runner = StubRunner(network=(inspect_output('172.20.0.1'), ''))
        b = backend(runner=runner,
                    env={'STRIX_DOCKER_SANDBOX_NETWORK': ' scans-net '})
        self.assertEqual(b.resolve_target(3000), 'http://172.20.0.1:3000')
        self.assertEqual(runner.inspected, ['scans-net'])

    def test_a_network_setting_that_is_not_a_name_never_reaches_docker(self):
        """A leading dash would be read as a flag by the docker CLI."""
        for value in ('--format={{json .}}', 'a b', 'net;rm', '-x'):
            runner = StubRunner()
            b = backend(runner=runner,
                        env={'STRIX_DOCKER_SANDBOX_NETWORK': value})
            with self.assertRaises(scan_backends.BackendError):
                b.resolve_target(3000)
            self.assertEqual(runner.inspected, [], value)

    def test_the_address_is_proven_to_answer_before_it_is_returned(self):
        runner = StubRunner()
        backend(runner=runner).resolve_target(3000)
        self.assertEqual(runner.probed, [('172.17.0.1', 3000)])

    def test_an_app_that_does_not_answer_is_refused_before_a_scan(self):
        """Stopped since the list was read, or bound to loopback only: either
        way a scan would spend money and report a clean result against
        nothing."""
        b = backend(runner=StubRunner(answers=False))
        with self.assertRaises(scan_backends.BackendError) as ctx:
            b.resolve_target(4545)
        message = str(ctx.exception)
        self.assertIn('4545', message)
        self.assertIn('0.0.0.0', message)

    def test_no_gateway_address_is_baked_into_the_code(self):
        """Prose may name the conventional address; code may not contain one.

        Checked over string literals with docstrings excluded, so the module
        stays free to explain WHY it refuses to assume the value.
        """
        with open(scan_backends.__file__, encoding='utf-8') as f:
            tree = ast.parse(f.read())
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                first = (node.body or [None])[0]
                if (isinstance(first, ast.Expr)
                        and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)):
                    docstrings.add(id(first.value))
        literals = [n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant)
                    and isinstance(n.value, str)
                    and id(n) not in docstrings]
        offenders = [s for s in literals if '172.17.0.1' in s]
        self.assertEqual(offenders, [])


class BridgeNetworkRunnerTests(unittest.TestCase):
    """The real runner, with only `subprocess.run` replaced.

    Every other test here stubs the runner, which is how the first release
    shipped reading the wrong table with a tool the image does not have: no
    test ever saw which command produced the address.
    """

    def run_with(self, fake):
        with mock.patch.object(scan_backends.subprocess, 'run', fake):
            return scan_backends._SubprocessRunner().network_inspect('bridge')

    def test_the_address_is_asked_of_the_runtime_not_of_this_container(self):
        """`ip route` here reads the pod's table, whose default route leads to
        the Kubernetes node — a scan aimed there attacks the wrong machine."""
        calls = []

        def fake(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv, 0, stdout=inspect_output('172.17.0.1'), stderr='')

        out, why = self.run_with(fake)
        self.assertEqual(calls, [['docker', 'network', 'inspect', 'bridge']])
        self.assertEqual(scan_backends.parse_bridge_gateway(out), '172.17.0.1')
        self.assertEqual(why, '')

    def test_a_daemon_error_is_returned_as_the_reason(self):
        def fake(argv, **kw):
            return subprocess.CompletedProcess(
                argv, 1, stdout='',
                stderr='Cannot connect to the Docker daemon at tcp://localhost:2375.\n')

        out, why = self.run_with(fake)
        self.assertEqual(out, '')
        self.assertIn('Cannot connect to the Docker daemon', why)

    def test_a_missing_docker_command_is_named(self):
        def fake(argv, **kw):
            raise FileNotFoundError(argv[0])

        out, why = self.run_with(fake)
        self.assertEqual(out, '')
        self.assertIn('docker', why)

    def test_a_hung_daemon_does_not_hang_the_request(self):
        def fake(argv, **kw):
            self.assertIn('timeout', kw)
            raise subprocess.TimeoutExpired(argv, kw['timeout'])

        out, why = self.run_with(fake)
        self.assertEqual(out, '')
        self.assertTrue(why)


class PortProbeTests(unittest.TestCase):
    """The real reachability check, against a loopback socket this test owns
    — still nothing leaves the machine."""

    def test_a_listening_port_answers_and_a_closed_one_does_not(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        port = server.getsockname()[1]
        runner = scan_backends._SubprocessRunner()
        try:
            self.assertTrue(runner.port_answers('127.0.0.1', port))
        finally:
            server.close()
        self.assertFalse(runner.port_answers('127.0.0.1', port))


class ArgvTests(unittest.TestCase):

    SPEC = {'mode': 'quick', 'target_url': 'http://10.0.0.1:3000',
            'model': 'prov/model', 'budget_usd': None, 'instruction': ''}

    def argv(self, **over):
        spec = dict(self.SPEC)
        spec.update(over)
        return scan_backends.build_argv(spec, executable='scanner')

    def test_headless_and_depth_are_always_present(self):
        """Depth especially: the scanner's own default is its deepest mode."""
        argv = self.argv()
        self.assertIn('--non-interactive', argv)
        self.assertIn('--scan-mode', argv)
        self.assertEqual(argv[argv.index('--scan-mode') + 1], 'quick')

    def test_the_target_is_whatever_was_resolved(self):
        argv = self.argv(target_url='http://10.9.9.9:8000')
        self.assertEqual(argv[argv.index('--target') + 1],
                         'http://10.9.9.9:8000')

    def test_a_budget_appears_only_when_one_was_set(self):
        self.assertNotIn('--max-budget', self.argv())
        argv = self.argv(budget_usd=5.0)
        self.assertEqual(argv[argv.index('--max-budget') + 1], '5.0')

    def test_extra_instructions_are_passed_only_when_given(self):
        self.assertNotIn('--instruction', self.argv())
        self.assertIn('--instruction', self.argv(instruction='focus on login'))

    def test_a_spec_without_a_mode_or_target_is_a_programming_error(self):
        for over in ({'mode': ''}, {'target_url': ''}):
            with self.assertRaises(ValueError):
                self.argv(**over)

    def test_no_credential_ever_reaches_the_command_line(self):
        """The scanner reads its own config file. A key on an argv is visible
        in the process list to anything that can read /proc."""
        argv = self.argv(model='prov/model', instruction='x')
        joined = ' '.join(argv).lower()
        for leak in ('api_key', 'api-key', 'llm_api_key', '--key', 'sk-'):
            self.assertNotIn(leak, joined)

    def test_arguments_are_a_list_so_no_shell_parses_them(self):
        self.assertTrue(all(isinstance(a, str) for a in self.argv()))


class PreflightTests(unittest.TestCase):

    def test_a_missing_scanner_says_so_plainly(self):
        b = backend(executable='/nonexistent/scanner')
        result = b.preflight()
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'not_installed')

    def test_a_missing_container_runtime_names_the_setting_to_change(self):
        b = backend(runner=StubRunner(runtime=(False, '')))
        result = b.preflight()
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'no_container_runtime')
        self.assertIn('buildkit', result['detail'])

    def test_an_address_that_cannot_be_resolved_is_caught_before_a_scan(self):
        """The live failure: readiness said yes while the one thing that
        could never work went unchecked."""
        b = backend(runner=StubRunner(network=('', 'no such network')))
        result = b.preflight()
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'no_target_address')
        self.assertIn('no such network', result['detail'])

    def test_a_ready_workspace_passes(self):
        self.assertTrue(backend().preflight()['ok'])


class StartTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runner = StubRunner()

    def spec(self, **over):
        spec = {'mode': 'quick', 'target_url': 'http://10.0.0.1:3000',
                'model': 'prov/model', 'budget_usd': None, 'instruction': '',
                'scan_dir': self.tmp.name}
        spec.update(over)
        return spec

    def test_the_handle_identifies_the_backend_that_made_it(self):
        handle = backend(runner=self.runner).start(self.spec())
        self.assertEqual(handle['backend'], 'local-strix')
        self.assertEqual(handle['pid'], 4242)

    def test_the_handle_survives_a_round_trip_through_json(self):
        """It is persisted in the scan record and read back after a restart."""
        handle = backend(runner=self.runner).start(self.spec())
        self.assertEqual(json.loads(json.dumps(handle)), handle)

    def test_each_scan_runs_in_its_own_directory(self):
        backend(runner=self.runner).start(self.spec())
        self.assertEqual(self.runner.spawned[0]['cwd'], self.tmp.name)

    def test_the_model_is_passed_per_scan(self):
        backend(runner=self.runner).start(self.spec(model='other/model'))
        self.assertEqual(self.runner.spawned[0]['env']['STRIX_LLM'],
                         'other/model')

    def test_no_credential_is_placed_in_the_child_environment(self):
        """The scanner reads its own config file; nothing here handles keys."""
        env = {'LLM_API_KEY': 'secret-from-elsewhere', 'PATH': '/usr/bin'}
        b = scan_backends.LocalStrixBackend(
            runner=self.runner, executable=sys.executable, env=env)
        b.start(self.spec())
        child = self.runner.spawned[0]['env']
        # Whatever the ambient environment holds is inherited unchanged; this
        # module neither adds a key nor invents one.
        self.assertNotIn('STRIX_API_KEY', child)
        self.assertEqual(child.get('LLM_API_KEY'), 'secret-from-elsewhere')

    def test_the_pinned_sandbox_image_is_passed_through_when_set(self):
        b = scan_backends.LocalStrixBackend(
            runner=self.runner, executable=sys.executable,
            env={'STRIX_IMAGE': 'example.invalid/sandbox:1.2.3'})
        b.start(self.spec())
        self.assertEqual(self.runner.spawned[0]['env']['STRIX_IMAGE'],
                         'example.invalid/sandbox:1.2.3')

    def test_starting_without_the_scanner_installed_is_a_clean_error(self):
        b = backend(executable='/nonexistent/scanner', runner=self.runner)
        with self.assertRaises(scan_backends.BackendError):
            b.start(self.spec())


class ArtifactReadingTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.handle = {'scan_dir': self.tmp.name, 'pid': 1}

    def write_run(self, name='target-run', run=None, findings=None,
                  report=None, coverage=None):
        d = os.path.join(self.tmp.name, scan_backends.RUNS_SUBDIR, name)
        os.makedirs(d, exist_ok=True)
        if run is not None:
            with open(os.path.join(d, scan_backends.RUN_RECORD), 'w') as f:
                json.dump(run, f)
        if findings is not None:
            with open(os.path.join(d, scan_backends.FINDINGS_RECORD), 'w') as f:
                json.dump(findings, f)
        if report is not None:
            with open(os.path.join(d, scan_backends.REPORT_RECORD), 'w',
                      encoding='utf-8') as f:
                f.write(report)
        if coverage is not None:
            with open(os.path.join(d, scan_backends.END_RECORD), 'w') as f:
                json.dump(coverage, f)
        return d

    def test_nothing_written_yet_reads_as_no_news(self):
        """The normal state for the first seconds of every scan. It must not
        be mistaken for a finished or failed one."""
        self.assertIsNone(backend().artifacts(self.handle))

    def test_the_run_record_and_findings_are_returned_parsed(self):
        self.write_run(run={'status': 'running', 'llm_usage': {'cost': 0.25}},
                       findings=[{'id': 'v1', 'severity': 'high'}])
        got = backend().artifacts(self.handle)
        self.assertEqual(got['run']['status'], 'running')
        self.assertEqual(got['findings'][0]['id'], 'v1')

    def test_a_half_written_file_reads_as_no_news_not_as_no_findings(self):
        """These files are re-read while the scanner rewrites them, so a
        partial read is ordinary — the next poll gets the whole thing. It must
        report `None`, not an empty list: the scanner rewrites the file whole
        on every finding, so calling a caught rewrite "no findings" throws away
        everything found so far."""
        d = self.write_run(run={'status': 'running'})
        with open(os.path.join(d, scan_backends.FINDINGS_RECORD), 'w') as f:
            f.write('[{"id": "v1", "sev')
        got = backend().artifacts(self.handle)
        self.assertIsNone(got['findings'])
        self.assertEqual(got['run']['status'], 'running')

    def test_no_findings_file_at_all_reads_as_genuinely_none(self):
        """The scanner writes no findings file until it confirms something, so
        its absence really does mean zero — unlike a failed read."""
        self.write_run(run={'status': 'completed'})
        self.assertEqual(backend().artifacts(self.handle)['findings'], [])

    def test_a_caught_rewrite_does_not_lose_findings_already_reported(self):
        """The whole point of the distinction, end to end: a poll landing
        mid-write must not blank the list and then re-announce every finding
        on the next poll, which would notify the user twice for one bug."""
        d = self.write_run(run={'status': 'running'},
                           findings=[{'id': 'v1', 'severity': 'high'}])
        record = {'status': 'running', 'findings': [], 'ended_at': None}
        added, _ = scans.apply_artifacts(record, backend().artifacts(self.handle))
        self.assertEqual(len(added), 1)

        with open(os.path.join(d, scan_backends.FINDINGS_RECORD), 'w') as f:
            f.write('[{"id": "v1", "sever')
        added, changed = scans.apply_artifacts(
            record, backend().artifacts(self.handle))
        self.assertEqual([f['id'] for f in record['findings']], ['v1'])
        self.assertEqual(record['counts']['high'], 1)
        self.assertEqual((added, changed), ([], []))

    def test_the_output_directory_is_discovered_not_predicted(self):
        """The scanner names it from the target; we must not encode that."""
        self.write_run(name='some-unexpected-name-1234',
                       run={'status': 'completed'})
        got = backend().artifacts(self.handle)
        self.assertEqual(got['run']['status'], 'completed')

    def test_artifacts_hand_back_data_never_a_path(self):
        """A path-shaped seam would be local-only in disguise.

        `report.location` is the one exception and is covered by its own
        test below; everything the layer above actually reasons over — the
        run record, the findings, the coverage verdict — stays free of this
        machine.
        """
        self.write_run(run={'status': 'running'}, findings=[])
        got = backend().artifacts(self.handle)
        self.assertEqual(set(got), {'run', 'findings', 'report', 'coverage'})
        self.assertNotIn(self.tmp.name,
                         json.dumps({k: v for k, v in got.items()
                                     if k != 'report'}))

    def test_the_report_location_is_the_one_label_allowed_through(self):
        """And it is a LABEL, not an instruction.

        The file is on the user's own workspace, outlives the page, and they
        asked to be told where it is — so it travels. It is built here and
        shown verbatim above; nothing over the seam parses it, joins to it or
        opens it, which is what would make the seam local-only. A backend
        running the scan on another machine returns its own location, or
        none, and no caller changes.
        """
        self.write_run(run={'status': 'completed'}, findings=[],
                       report='# Executive Summary\n\nLooked at the app.\n')
        report = backend().artifacts(self.handle)['report']
        self.assertTrue(report['location'].startswith(self.tmp.name))
        self.assertTrue(report['location'].endswith(scan_backends.REPORT_RECORD))
        # The pure layer accepts a report that has no location at all.
        self.assertEqual(scans.parse_report({'text': 'written elsewhere'}),
                         {'text': 'written elsewhere', 'location': ''})

    def test_the_parsed_artifacts_feed_straight_into_the_pure_layer(self):
        """The two halves agree on a shape — the seam's real acceptance test."""
        self.write_run(run={'status': 'completed',
                            'llm_usage': {'cost': 0.5, 'input_tokens': 10}},
                       findings=[{'id': 'v1', 'severity': 'critical'}])
        record = {'status': 'running', 'findings': [], 'ended_at': None}
        added, changed = scans.apply_artifacts(
            record, backend().artifacts(self.handle))
        self.assertEqual(record['status'], 'done')
        self.assertEqual(record['counts']['critical'], 1)
        self.assertAlmostEqual(record['usage']['cost_usd'], 0.5)
        self.assertEqual(len(added), 1)
        self.assertEqual(changed, [])


class ReportArtifactTests(unittest.TestCase):
    """The scanner's write-up and its own coverage verdict.

    Both are written once, at the end of a run, and neither was ever read —
    so a scan that finished having proven nothing showed the user a page
    with "done" on it and nothing else.
    """

    REPORT = ('# Security Penetration Test Report\n\n'
              '# Executive Summary\n\nChecked the login page.\n')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.handle = {'scan_dir': self.tmp.name, 'pid': 1}

    def write_run(self, **kw):
        d = os.path.join(self.tmp.name, scan_backends.RUNS_SUBDIR, 'run-1')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, scan_backends.RUN_RECORD), 'w') as f:
            json.dump(kw.get('run', {'status': 'completed'}), f)
        if 'report' in kw:
            with open(os.path.join(d, scan_backends.REPORT_RECORD), 'w',
                      encoding='utf-8') as f:
                f.write(kw['report'])
        if 'coverage' in kw:
            with open(os.path.join(d, scan_backends.END_RECORD), 'w') as f:
                json.dump(kw['coverage'], f)
        return d

    def test_the_report_comes_back_with_the_file_it_was_read_from(self):
        """The path travels with the text: the file is on the user's own
        workspace and outlives the page, so they can open it themselves."""
        d = self.write_run(report=self.REPORT)
        got = backend().artifacts(self.handle)
        self.assertIn('Checked the login page.', got['report']['text'])
        self.assertEqual(got['report']['location'],
                         os.path.join(d, scan_backends.REPORT_RECORD))

    def test_no_report_yet_is_not_an_error(self):
        """It is written once, at the very end. Every read before that is an
        ordinary mid-scan read."""
        self.write_run()
        self.assertIsNone(backend().artifacts(self.handle)['report'])

    def test_an_empty_report_file_counts_as_none(self):
        self.write_run(report='   \n')
        self.assertIsNone(backend().artifacts(self.handle)['report'])

    def test_a_runaway_report_is_capped(self):
        self.write_run(report='x' * (scan_backends.REPORT_MAX_BYTES * 2))
        text = backend().artifacts(self.handle)['report']['text']
        self.assertLessEqual(len(text), scan_backends.REPORT_MAX_BYTES)

    def test_a_credential_quoted_in_a_report_is_redacted(self):
        fake = 'kc-test-credential-0002'
        self.write_run(report=f'# Summary\n\nUsed key {fake} to sign in.\n')
        b = backend(redactor=lambda t: t.replace(fake, '[redacted]'))
        text = b.artifacts(self.handle)['report']['text']
        self.assertNotIn(fake, text)
        self.assertIn('[redacted]', text)

    def test_the_coverage_verdict_is_read_down_to_two_fields(self):
        self.write_run(coverage={
            'completeness': {'complete': False, 'scan_status': 'completed',
                             'exit_reason': None,
                             'caveats': ['The run stopped early.', '', 9]},
            'entries': [{'surface': 'anything'}], 'gaps': ['lots'],
        })
        got = backend().artifacts(self.handle)['coverage']
        self.assertEqual(got, {'complete': False,
                               'caveats': ['The run stopped early.']})

    def test_a_report_alone_is_enough_to_be_worth_returning(self):
        """run.json can be caught mid-rewrite while the report is already
        there; returning None would hide it until the next poll."""
        d = os.path.join(self.tmp.name, scan_backends.RUNS_SUBDIR, 'run-1')
        os.makedirs(d)
        with open(os.path.join(d, scan_backends.REPORT_RECORD), 'w') as f:
            f.write(self.REPORT)
        got = backend().artifacts(self.handle)
        self.assertIsNotNone(got)
        self.assertIn('Executive Summary', got['report']['text'])

    def test_a_malformed_coverage_document_reads_as_no_verdict(self):
        for bad in ({}, {'completeness': 'yes'}, {'completeness': None}):
            self.assertIsNone(scan_backends.parse_completeness(bad), bad)
        self.assertIsNone(scan_backends.parse_completeness(None))

    def test_a_verdict_with_no_caveats_is_still_a_verdict(self):
        self.assertEqual(
            scan_backends.parse_completeness({'completeness': {'complete': True}}),
            {'complete': True, 'caveats': []})


class StopTests(unittest.TestCase):

    def test_stopping_signals_the_recorded_process(self):
        runner = StubRunner()
        backend(runner=runner).stop({'pid': 4242})
        self.assertEqual(runner.terminated, [4242])

    def test_stopping_a_scan_with_no_process_is_not_an_error(self):
        runner = StubRunner()
        for handle in (None, {}, {'pid': None}):
            backend(runner=runner).stop(handle)
        self.assertEqual(runner.terminated, [])

    def test_liveness_of_an_unknown_process_is_false(self):
        self.assertFalse(backend().is_alive({'pid': None}))
        self.assertFalse(backend().is_alive(None))

    def test_this_process_reads_as_alive(self):
        self.assertTrue(backend().is_alive({'pid': os.getpid()}))


FIXTURES = os.path.join(HERE, 'fixtures', 'scanner_logs')


def scanner_log(name):
    """A scanner console log, rendered with the scanner's own panel code by
    the same terminal library on an 80-column, non-terminal console — what
    `scanner.log` holds on the workspace. `llm_unavailable.log` carries the
    provider message verbatim from the live workspace's first failed scan."""
    with open(os.path.join(FIXTURES, name), encoding='utf-8') as f:
        return f.read()


class ScannerErrorTests(unittest.TestCase):
    """The scanner's error, out of output written for a terminal."""

    BOX = set('│┃╭╮╰╯┌┐└┘─━')

    def test_a_boxed_provider_error_is_read_whole_not_cut_at_the_border(self):
        """What the live workspace showed was the last six lines of this box:
        `"status": "UNAVAILABLE"`, two closing braces and three lines of
        border. The headline and the provider's sentence were above the cut."""
        error = scan_backends.scanner_error(scanner_log('llm_unavailable.log'))
        self.assertTrue(error.startswith('LLM connection failed:'), error)
        # The box wrapped this sentence across two lines; it reads as one.
        self.assertIn('Spikes in demand are usually temporary.', error)
        self.assertFalse(self.BOX & set(error), error)

    def test_a_setup_error_box_is_read(self):
        error = scan_backends.scanner_error(scanner_log('docker_missing.log'))
        self.assertEqual(
            error, "Docker not installed: The 'docker' CLI was not found in "
                   "your PATH. Please install Docker and ensure the 'docker' "
                   "command is available.")

    def test_a_crash_mid_scan_is_read_from_its_traceback(self):
        """The last thing printed is the exception, after a final status box
        that is not an error at all."""
        error = scan_backends.scanner_error(scanner_log('crash_quota.log'))
        self.assertIn('RateLimitError', error)
        self.assertIn('You exceeded your current quota', error)
        self.assertNotIn('Penetration test in progress', error)

    def test_a_warning_box_is_not_mistaken_for_the_error(self):
        """MODEL QUALITY WARNING is printed and the scan carries on. A scan
        later killed from outside printed no error, and must not be told the
        warning was why."""
        self.assertEqual(
            scan_backends.scanner_error(scanner_log('warning_then_killed.log')),
            '')

    def test_a_process_killed_from_outside_printed_no_error(self):
        self.assertEqual(scan_backends.scanner_error(scanner_log('killed.log')),
                         '')

    def test_a_logged_error_is_the_last_resort(self):
        text = ('2026-10-26 21:54:03.120 ERROR   scn agt strix.core.runner: '
                'sandbox did not start: image pull refused\n')
        self.assertEqual(scan_backends.scanner_error(text),
                         'sandbox did not start: image pull refused')

    def test_nothing_in_gives_nothing_out(self):
        for text in ('', None, '\n\n'):
            self.assertEqual(scan_backends.scanner_error(text), '')

    def test_a_long_error_is_clipped(self):
        text = 'Error during penetration test: ' + 'x' * 2000 + '\n'
        self.assertLessEqual(len(scan_backends.scanner_error(text)), 300)

    def test_the_provider_message_is_lifted_out_of_its_json(self):
        error = scan_backends.scanner_error(scanner_log('llm_unavailable.log'))
        self.assertEqual(
            scan_backends.provider_message(error),
            'This model is currently experiencing high demand. Spikes in '
            'demand are usually temporary. Please try again later.')
        self.assertEqual(scan_backends.provider_message('no json here'), '')

    def test_only_a_provider_error_counts_as_one(self):
        self.assertTrue(scan_backends.is_provider_error(
            'litellm.RateLimitError: GeminiException - {}'))
        self.assertTrue(scan_backends.is_provider_error(
            'LLM connection failed: timed out'))
        self.assertFalse(scan_backends.is_provider_error(
            "Docker not installed: The 'docker' CLI was not found"))


class EndReasonTests(unittest.TestCase):
    """What the user reads, from what the backend found."""

    def test_the_scanners_own_stated_reason_comes_first(self):
        reason = scan_backends.end_reason(
            scanner_reason='budget_exceeded', exit_code=0,
            error='litellm.Something: whatever')
        self.assertIn('spending limit', reason)
        self.assertIn('rate limit', scan_backends.end_reason(
            scanner_reason='rate_limited'))

    def test_a_kill_by_the_system_outranks_an_older_printed_error(self):
        reason = scan_backends.end_reason(
            exit_code=-signal.SIGKILL, error='litellm.Timeout: retried')
        self.assertIn('killed by the system', reason)
        self.assertIn('memory', reason)

    def test_a_signal_the_scanner_caught_is_not_called_a_bare_exit_code(self):
        """It records `interrupted` and exits 1 — the code alone reads as a
        crash with nothing printed."""
        reason = scan_backends.end_reason(exit_code=1,
                                          scanner_status='interrupted')
        self.assertIn('from outside kube-coder', reason)
        self.assertNotIn('code 1', reason)

    def test_another_signal_is_named(self):
        self.assertIn('SIGHUP', scan_backends.end_reason(
            exit_code=-signal.SIGHUP))

    def test_a_printed_error_leads_with_its_hint_then_the_providers_words(self):
        error = scan_backends.scanner_error(scanner_log('llm_unavailable.log'))
        reason = scan_backends.end_reason(error=error,
                                          hint='The provider is busy.')
        self.assertEqual(
            reason,
            'The provider is busy. The scanner said: This model is currently '
            'experiencing high demand. Spikes in demand are usually '
            'temporary. Please try again later.')

    def test_a_bare_exit_code_is_still_a_reason(self):
        self.assertIn('code 1', scan_backends.end_reason(exit_code=1))

    def test_nothing_known_is_an_empty_reason_not_a_guess(self):
        self.assertEqual(scan_backends.end_reason(), '')
        self.assertEqual(scan_backends.end_reason(exit_code=0), '')
        self.assertEqual(scan_backends.end_reason(scanner_reason='new_thing'),
                         '')


class EndCauseTests(unittest.TestCase):
    """What kind of end it was — the page's next step hangs on this, so it
    must agree with the sentence `end_reason` writes for the same inputs."""

    def cause(self, **kw):
        return scan_backends.end_cause(**kw)

    def test_the_scanners_stated_reason_comes_first(self):
        self.assertEqual(self.cause(scanner_reason='budget_exceeded',
                                    error='litellm.Timeout: x'), 'budget')
        self.assertEqual(self.cause(scanner_reason='rate_limited'), 'provider')

    def test_a_provider_error_is_a_provider_cause(self):
        error = scan_backends.scanner_error(scanner_log('llm_unavailable.log'))
        self.assertEqual(self.cause(exit_code=1, error=error), 'provider')

    def test_a_setup_error_is_the_scanners(self):
        error = scan_backends.scanner_error(scanner_log('docker_missing.log'))
        self.assertEqual(self.cause(exit_code=1, error=error), 'scanner')

    def test_a_signal_outranks_an_older_provider_error(self):
        """Same order as `end_reason`, whose sentence then says "killed by the
        system" — a headline blaming the model over that would contradict
        it."""
        self.assertEqual(self.cause(exit_code=-signal.SIGKILL,
                                    error='litellm.Timeout: retried'), '')

    def test_nothing_known_is_no_cause(self):
        self.assertEqual(self.cause(), '')
        self.assertEqual(self.cause(exit_code=1), '')
        self.assertEqual(self.cause(scanner_reason='new_thing'), '')


class ExplainEndTests(unittest.TestCase):
    """`LocalStrixBackend.explain_end` over a real scan folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.handle = {'scan_dir': self.tmp.name, 'pid': 4242}

    def write_log(self, text):
        with open(os.path.join(self.tmp.name, scan_backends.LOG_NAME), 'w',
                  encoding='utf-8') as f:
            f.write(text)

    def write_end_record(self, exit_reason):
        run = os.path.join(self.tmp.name, scan_backends.RUNS_SUBDIR, 'run-1')
        os.makedirs(run)
        with open(os.path.join(run, scan_backends.END_RECORD), 'w') as f:
            json.dump({'completeness': {'complete': False,
                                        'exit_reason': exit_reason}}, f)
        return run

    def explain(self, *, exit_code=1, explainer=None, redactor=None):
        b = backend(runner=StubRunner(exit_code=exit_code),
                    explainer=explainer, redactor=redactor)
        return b.explain_end(self.handle)

    def test_a_provider_failure_reads_as_a_sentence_with_the_hint(self):
        self.write_log(scanner_log('llm_unavailable.log'))
        reason, detail, _ = self.explain(
            explainer=lambda e: 'The provider is busy right now.')
        self.assertTrue(reason.startswith('The provider is busy right now. '
                                          'The scanner said: This model'),
                        reason)

    def test_the_hint_table_is_not_applied_to_a_setup_error(self):
        """The provider table reads "not found" as a wrong model name; the
        docker CLI being missing is not that."""
        self.write_log(scanner_log('docker_missing.log'))
        reason, _, _ = self.explain(explainer=lambda e: 'Wrong model name.')
        self.assertNotIn('Wrong model name', reason)
        self.assertIn("The 'docker' CLI was not found", reason)

    def test_the_scanners_end_record_names_a_rate_limit(self):
        self.write_log(scanner_log('killed.log'))
        self.write_end_record('rate_limited')
        reason, detail, _ = self.explain(exit_code=0)
        self.assertIn('rate limit', reason)
        self.assertIn('scanner end reason: rate_limited', detail)

    def test_an_out_of_memory_kill_says_so(self):
        self.write_log(scanner_log('killed.log'))
        reason, detail, _ = self.explain(exit_code=-signal.SIGKILL)
        self.assertIn('ran out of memory', reason)
        self.assertIn('exit status: -9 (SIGKILL)', detail)

    def test_the_detail_says_where_everything_is_kept(self):
        """For the server log: whoever debugs this needs the files, not a
        paraphrase."""
        self.write_log(scanner_log('crash_quota.log'))
        run = self.write_end_record('')
        _, detail, _ = self.explain()
        self.assertIn(os.path.join(self.tmp.name, scan_backends.LOG_NAME),
                      detail)
        self.assertIn(os.path.join(run, scan_backends.RUN_LOG), detail)
        self.assertIn('exit status: 1', detail)
        self.assertIn('RESOURCE_EXHAUSTED', detail)

    def test_an_unknown_exit_status_is_said_to_be_unknown(self):
        self.write_log('')
        reason, detail, _ = self.explain(exit_code=None)
        self.assertEqual(reason, '')
        self.assertIn('exit status: unknown', detail)

    def test_the_credential_is_redacted_from_both_halves(self):
        fake = 'kc-test-credential-0001'
        self.write_log(f'Error during penetration test: litellm.AuthError: '
                       f'key {fake} rejected\n')
        reason, detail, _ = self.explain(
            redactor=lambda t: t.replace(fake, '[redacted]'))
        self.assertNotIn(fake, reason + detail)
        self.assertIn('[redacted]', reason)

    def test_the_cause_travels_with_the_reason(self):
        self.write_log(scanner_log('llm_unavailable.log'))
        self.assertEqual(self.explain()[2], 'provider')
        self.write_log(scanner_log('docker_missing.log'))
        self.assertEqual(self.explain()[2], 'scanner')

    def test_a_spent_budget_is_a_budget_cause(self):
        self.write_log(scanner_log('killed.log'))
        self.write_end_record('budget_exceeded')
        reason, _, cause = self.explain(exit_code=0)
        self.assertEqual(cause, 'budget')
        self.assertIn('spending limit', reason)

    def test_a_backend_that_cannot_explain_satisfies_the_contract(self):
        """Not abstract: the base answer is "nothing known"."""
        b = SeamConformanceTests.FakeRemoteBackend()
        self.assertEqual(b.explain_end({}), ('', '', ''))


class ProcessTrackingTests(unittest.TestCase):
    """The runner holds each scan's `Popen`, so its exit status survives the
    process and the process is reaped rather than left a zombie."""

    class FakeProc:
        def __init__(self, pid, returncode=None):
            self.pid = pid
            self.returncode = returncode

        def poll(self):
            return self.returncode

    def setUp(self):
        self.runner = scan_backends._SubprocessRunner()
        saved = dict(self.runner._children)
        self.runner._children.clear()

        def restore():
            self.runner._children.clear()
            self.runner._children.update(saved)
        self.addCleanup(restore)

    def test_a_tracked_scan_is_alive_until_its_process_exits(self):
        proc = self.FakeProc(90001)
        self.runner._children[90001] = proc
        self.assertTrue(self.runner.alive(90001))
        self.assertIsNone(self.runner.exit_status(90001))
        proc.returncode = -signal.SIGKILL
        self.assertFalse(self.runner.alive(90001))
        self.assertEqual(self.runner.exit_status(90001), -signal.SIGKILL)

    def test_a_scan_from_an_earlier_server_process_falls_back_to_a_signal(self):
        """A restart leaves no `Popen` to ask."""
        self.assertTrue(self.runner.alive(os.getpid()))
        self.assertIsNone(self.runner.exit_status(os.getpid()))

    def test_spawning_registers_the_child_and_drops_finished_ones(self):
        self.runner._children[90002] = self.FakeProc(90002, returncode=0)
        self.runner._children[90003] = self.FakeProc(90003)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(scan_backends.subprocess, 'Popen',
                                  return_value=self.FakeProc(90004)):
            pid = self.runner.spawn(['true'], cwd=tmp, env={},
                                    log_path=os.path.join(tmp, 'log'))
        self.assertEqual(pid, 90004)
        self.assertEqual(sorted(self.runner._children), [90003, 90004])


class SeamConformanceTests(unittest.TestCase):
    """A second backend, with nothing in common with the first.

    It has no process, no filesystem and no container runtime — it is a
    stand-in for the dedicated scan host this seam exists to allow. If this
    passes, the contract is a contract; if it needs a special case anywhere
    above the seam, the seam has leaked.
    """

    class FakeRemoteBackend(scan_backends.ScanBackend):
        name = 'fake-remote'

        def __init__(self):
            self.stopped = []
            self.state = {}

        def preflight(self):
            return {'ok': True, 'reason': '', 'detail': ''}

        def resolve_target(self, port):
            return f'https://runner.invalid/workspace/{port}'

        def start(self, spec):
            self.state = {'run': {'status': 'running', 'llm_usage': {}},
                          'findings': []}
            return {'backend': self.name, 'job': 'job-1'}

        def artifacts(self, handle):
            return self.state or None

        def is_alive(self, handle):
            return self.state.get('run', {}).get('status') == 'running'

        def stop(self, handle):
            self.stopped.append(handle.get('job'))
            self.state['run']['status'] = 'stopped'

    def test_it_satisfies_the_abstract_base_class(self):
        self.FakeRemoteBackend()   # ABC raises here if a method is missing

    def test_the_abc_refuses_an_incomplete_backend(self):
        class Partial(scan_backends.ScanBackend):
            name = 'partial'

            def preflight(self):
                return {'ok': True}

        with self.assertRaises(TypeError):
            Partial()

    def test_the_pure_layer_drives_it_with_no_change(self):
        """Every step the manager takes, against a backend that has no files,
        no process and a completely different target scheme."""
        b = self.FakeRemoteBackend()
        self.assertTrue(b.preflight()['ok'])
        target = b.resolve_target(3000)

        spec, err = scans.validate_create(
            {'port': 3000, 'mode': 'quick'},
            [{'port': 3000, 'name': 'shop', 'addr': '0.0.0.0',
              'reachable': True, 'reason': ''}],
            connected_model='prov/model')
        self.assertIsNone(err)
        spec['target_url'] = target

        handle = b.start(spec)
        record = {'status': 'running', 'findings': [], 'ended_at': None,
                  'target': {'port': 3000, 'name': 'shop'}, 'mode': 'quick'}

        b.state['findings'] = [{'id': 'r1', 'severity': 'high'}]
        added, _ = scans.apply_artifacts(record, b.artifacts(handle))
        self.assertEqual([f['id'] for f in added], ['r1'])
        self.assertTrue(b.is_alive(handle))

        b.stop(handle)
        scans.apply_artifacts(record, b.artifacts(handle))
        self.assertEqual(record['status'], 'stopped')
        self.assertFalse(b.is_alive(handle))
        self.assertIn('part', scans.result_summary(record))


if __name__ == '__main__':
    unittest.main()
