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
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import scan_backends  # noqa: E402
import scans  # noqa: E402


class StubRunner:
    """Records what the backend asked the OS to do, and answers as told."""

    def __init__(self, *, route='default via 172.17.0.1 dev eth0\n',
                 runtime=(True, '')):
        self.route = route
        self.runtime = runtime
        self.spawned = []
        self.terminated = []

    def default_route(self):
        return self.route

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

    def test_the_gateway_is_read_from_real_route_output(self):
        out = ('default via 172.17.0.1 dev eth0 \n'
               '172.17.0.0/16 dev eth0 scope link  src 172.17.0.2\n')
        self.assertEqual(scan_backends.parse_default_gateway(out), '172.17.0.1')

    def test_a_different_gateway_is_honoured_not_assumed(self):
        """172.17.0.1 is a convention the runtime may change. Assuming it is
        the silent-empty-result failure."""
        self.assertEqual(
            scan_backends.parse_default_gateway('default via 10.200.0.1 dev eth0'),
            '10.200.0.1')

    def test_output_without_a_default_route_yields_nothing(self):
        for out in ('', None, '172.17.0.0/16 dev eth0 scope link', 'garbage'):
            self.assertIsNone(scan_backends.parse_default_gateway(out))

    def test_a_non_address_is_refused_rather_than_interpolated(self):
        """The value lands in a URL, so anything unexpected is rejected."""
        for out in ('default via not-an-ip dev eth0',
                    'default via 172.17.0.1;rm -rf / dev eth0',
                    'default via 999.999.999.999.999 dev eth0'):
            self.assertIsNone(scan_backends.parse_default_gateway(out))

    def test_resolve_target_builds_the_url_from_what_it_read(self):
        b = backend(runner=StubRunner(route='default via 10.0.0.1 dev eth0'))
        self.assertEqual(b.resolve_target(3000), 'http://10.0.0.1:3000')

    def test_resolve_target_refuses_to_guess_when_it_cannot_tell(self):
        b = backend(runner=StubRunner(route=''))
        with self.assertRaises(scan_backends.BackendError) as ctx:
            b.resolve_target(3000)
        self.assertIn('check nothing', str(ctx.exception))

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

    def write_run(self, name='target-run', run=None, findings=None):
        d = os.path.join(self.tmp.name, scan_backends.RUNS_SUBDIR, name)
        os.makedirs(d, exist_ok=True)
        if run is not None:
            with open(os.path.join(d, scan_backends.RUN_RECORD), 'w') as f:
                json.dump(run, f)
        if findings is not None:
            with open(os.path.join(d, scan_backends.FINDINGS_RECORD), 'w') as f:
                json.dump(findings, f)
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
        """A path-shaped seam would be local-only in disguise."""
        self.write_run(run={'status': 'running'}, findings=[])
        got = backend().artifacts(self.handle)
        self.assertEqual(set(got), {'run', 'findings'})
        self.assertNotIn(self.tmp.name, json.dumps(got))

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
