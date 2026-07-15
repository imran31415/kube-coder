"""Unit tests for server.py's MetricsCollector and GitHubManager.

These two manager classes (system metrics from /proc + os.statvfs, and
git/gh/ssh identity helpers) had little coverage. They're pure static
methods over the filesystem and subprocess, so they test cleanly with
mocking — no HTTP handler needed.

Run with:    python3 -m unittest tests.server_managers_test
(from charts/workspace/)
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402

MC = server.MetricsCollector
GH = server.GitHubManager


def _proc(returncode=0, stdout='', stderr=''):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


# ───────────────────────── MetricsCollector ─────────────────────────

class CpuUsageTests(unittest.TestCase):
    def test_real_read_has_shape(self):
        with mock.patch.object(server.time, 'sleep'):  # skip the 0.5s delay
            out = MC.get_cpu_usage()
        self.assertIn('usage_percent', out)
        self.assertGreaterEqual(out['cores'], 1)
        self.assertIsInstance(out['usage_percent'], float)

    def test_error_path_returns_safe_default(self):
        with mock.patch('builtins.open', side_effect=OSError('boom')):
            out = MC.get_cpu_usage()
        self.assertEqual(out['usage_percent'], 0.0)
        self.assertIn('error', out)


class MemoryUsageTests(unittest.TestCase):
    def test_parses_meminfo(self):
        fake = 'MemTotal: 2000 kB\nMemAvailable: 500 kB\nMemFree: 400 kB\n'
        with mock.patch('builtins.open', mock.mock_open(read_data=fake)):
            out = MC.get_memory_usage()
        self.assertEqual(out['total_mb'], round(2000 / 1024, 1))
        # used = total - available = 1500 kB
        self.assertEqual(out['percent'], round(1500 / 2000 * 100, 1))

    def test_error_path(self):
        with mock.patch('builtins.open', side_effect=OSError('x')):
            out = MC.get_memory_usage()
        self.assertEqual(out['percent'], 0)
        self.assertIn('error', out)


class DiskUsageTests(unittest.TestCase):
    def test_real_read_has_shape(self):
        out = MC.get_disk_usage()
        for k in ('total_gb', 'used_gb', 'available_gb', 'percent', 'path'):
            self.assertIn(k, out)

    def test_error_path(self):
        with mock.patch.object(server.os, 'statvfs', side_effect=OSError('x')):
            out = MC.get_disk_usage()
        self.assertEqual(out['percent'], 0)
        self.assertIn('error', out)


class AlertsTests(unittest.TestCase):
    def setUp(self):
        self.T = server.ALERT_THRESHOLDS

    def test_no_alerts_when_below_warning(self):
        out = MC.get_alerts({'usage_percent': 0}, {'percent': 0}, {'percent': 0})
        self.assertEqual(out, [])

    def test_critical_cpu(self):
        out = MC.get_alerts({'usage_percent': self.T['cpu']['critical']},
                            {'percent': 0}, {'percent': 0})
        self.assertEqual(out[0]['type'], 'critical')
        self.assertEqual(out[0]['resource'], 'cpu')

    def test_warning_memory(self):
        warn = self.T['memory']['warning']
        crit = self.T['memory']['critical']
        # pick a value in [warning, critical)
        val = warn if warn < crit else warn
        out = MC.get_alerts({'usage_percent': 0}, {'percent': val}, {'percent': 0})
        mem_alerts = [a for a in out if a['resource'] == 'memory']
        self.assertTrue(mem_alerts)
        self.assertIn(mem_alerts[0]['type'], ('warning', 'critical'))

    def test_critical_disk(self):
        out = MC.get_alerts({'usage_percent': 0}, {'percent': 0},
                            {'percent': self.T['disk']['critical']})
        disk = [a for a in out if a['resource'] == 'disk']
        self.assertEqual(disk[0]['type'], 'critical')

    def test_get_all_metrics_shape(self):
        with mock.patch.object(server.time, 'sleep'):
            out = MC.get_all_metrics()
        for k in ('cpu', 'memory', 'disk', 'alerts', 'timestamp'):
            self.assertIn(k, out)


# ───────────────────────── GitHubManager ─────────────────────────

class SshStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = GH.SSH_DIR
        GH.SSH_DIR = self.tmp
        self.addCleanup(self._restore)

    def _restore(self):
        GH.SSH_DIR = self._orig

    def test_not_configured_when_no_pubkey(self):
        self.assertEqual(GH.get_ssh_status(), {'configured': False})

    def test_configured_reads_key_and_fingerprint(self):
        with open(os.path.join(self.tmp, 'id_ed25519.pub'), 'w') as f:
            f.write('ssh-ed25519 AAAAKEY user@host\n')
        with mock.patch.object(server.subprocess, 'run',
                               return_value=_proc(0, '256 SHA256:abc user (ED25519)')):
            out = GH.get_ssh_status()
        self.assertTrue(out['configured'])
        self.assertEqual(out['key_type'], 'ed25519')
        self.assertEqual(out['key_fingerprint'], 'SHA256:abc')
        self.assertIn('ssh-ed25519', out['public_key'])


class GhCliStatusTests(unittest.TestCase):
    def test_authenticated_parses_username(self):
        out_text = 'github.com\n  Logged in to github.com account octocat (keyring)\n'
        with mock.patch.object(server.subprocess, 'run', return_value=_proc(0, '', out_text)):
            out = GH.get_gh_cli_status()
        self.assertTrue(out['authenticated'])
        self.assertEqual(out['username'], 'octocat')

    def test_not_authenticated(self):
        with mock.patch.object(server.subprocess, 'run', return_value=_proc(1)):
            out = GH.get_gh_cli_status()
        self.assertTrue(out['installed'])
        self.assertFalse(out['authenticated'])

    def test_not_installed(self):
        with mock.patch.object(server.subprocess, 'run', side_effect=FileNotFoundError):
            out = GH.get_gh_cli_status()
        self.assertFalse(out['installed'])


class GitConfigTests(unittest.TestCase):
    def test_get_git_config_reads_name_email(self):
        def fake_run(argv, **kw):
            if argv[-1] == 'user.name':
                return _proc(0, 'Imran\n')
            if argv[-1] == 'user.email':
                return _proc(0, 'imran@example.com\n')
            return _proc(1)
        with mock.patch.object(server.subprocess, 'run', side_effect=fake_run):
            out = GH.get_git_config()
        self.assertEqual(out['user_name'], 'Imran')
        self.assertEqual(out['user_email'], 'imran@example.com')

    def test_get_git_config_empty_when_unset(self):
        with mock.patch.object(server.subprocess, 'run', return_value=_proc(1)):
            out = GH.get_git_config()
        self.assertEqual(out['user_name'], '')
        self.assertEqual(out['user_email'], '')

    def test_set_git_config_invokes_git_then_reads_back(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            if argv[-1] == 'user.name':
                return _proc(0, 'New Name\n')
            if argv[-1] == 'user.email':
                return _proc(0, 'new@example.com\n')
            return _proc(0)
        with mock.patch.object(server.subprocess, 'run', side_effect=fake_run):
            out = GH.set_git_config('New Name', 'new@example.com')
        self.assertEqual(out['user_name'], 'New Name')
        # two set calls + two read-back calls
        self.assertTrue(any('user.name' in c and 'New Name' in c for c in calls))


class MiscGitHubTests(unittest.TestCase):
    def test_start_device_flow_returns_instructions(self):
        out = GH.start_device_flow()
        self.assertIn('command', out)
        self.assertTrue(out['manual_steps'])

    def test_get_full_status_combines(self):
        with mock.patch.object(GH, 'get_ssh_status', return_value={'configured': False}), \
             mock.patch.object(GH, 'get_gh_cli_status', return_value={'authenticated': False}), \
             mock.patch.object(GH, 'get_git_config', return_value={'user_name': ''}), \
             mock.patch.object(GH, 'get_auth_mode', return_value='app'), \
             mock.patch.object(GH, 'app_available', return_value=False):
            out = GH.get_full_status()
        self.assertEqual(set(out), {'ssh', 'gh_cli', 'git_config', 'auth_mode', 'app_available'})


class AuthModeTests(unittest.TestCase):
    """GitHub auth mode: personal-vs-app switch (issue #256)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.mode_file = os.path.join(self.tmp, '.github-auth-mode')
        self.tok_file = os.path.join(self.tmp, '.github-token')
        self._orig_mode, self._orig_tok = GH.AUTH_MODE_FILE, GH.TOKEN_FILE
        GH.AUTH_MODE_FILE, GH.TOKEN_FILE = self.mode_file, self.tok_file

    def tearDown(self):
        GH.AUTH_MODE_FILE, GH.TOKEN_FILE = self._orig_mode, self._orig_tok
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read_mode(self):
        with open(self.mode_file) as f:
            return f.read().strip()

    def test_default_mode_is_app_when_missing(self):
        self.assertEqual(GH.get_auth_mode(), 'app')

    def test_reads_personal(self):
        with open(self.mode_file, 'w') as f:
            f.write('personal\n')
        self.assertEqual(GH.get_auth_mode(), 'personal')

    def test_garbage_falls_back_to_app(self):
        with open(self.mode_file, 'w') as f:
            f.write('bogus')
        self.assertEqual(GH.get_auth_mode(), 'app')

    def test_app_available_true_when_appid_env(self):
        with mock.patch.dict(server.os.environ, {'GITHUB_APP_ID': '123'}):
            self.assertTrue(GH.app_available())

    def test_app_available_true_when_token_file_exists(self):
        with open(self.tok_file, 'w') as f:
            f.write('ghs_x')
        with mock.patch.dict(server.os.environ, clear=False):
            server.os.environ.pop('GITHUB_APP_ID', None)
            self.assertTrue(GH.app_available())

    def test_set_mode_rejects_bad_value(self):
        with self.assertRaises(ValueError):
            GH.set_auth_mode('nope')
        self.assertFalse(os.path.exists(self.mode_file))

    def test_set_personal_removes_app_helper_and_hands_git_to_gh(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return _proc(0)
        with mock.patch.object(server.subprocess, 'run', side_effect=fake_run), \
             mock.patch.object(GH, 'get_full_status', return_value={'auth_mode': 'personal'}):
            out = GH.set_auth_mode('personal')
        self.assertEqual(self._read_mode(), 'personal')
        self.assertEqual(out['auth_mode'], 'personal')
        self.assertTrue(any('--unset-all' in c and 'credential.helper' in c for c in calls))
        self.assertTrue(any(c[:3] == ['gh', 'auth', 'setup-git'] for c in calls))

    def test_set_app_installs_token_helper(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return _proc(0)
        with mock.patch.object(server.subprocess, 'run', side_effect=fake_run), \
             mock.patch.object(GH, 'get_full_status', return_value={'auth_mode': 'app'}):
            GH.set_auth_mode('app')
        self.assertEqual(self._read_mode(), 'app')
        self.assertTrue(any('--replace-all' in c and 'credential.helper' in c for c in calls))


if __name__ == '__main__':
    unittest.main()
