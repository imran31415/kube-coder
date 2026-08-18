"""Unit tests for server.ActivityBeacon — the activity signal behind auto-pause
(#612).

The controller scales a workspace to 0 when this says "not busy". A wrong
"false" here destroys a running agent's work, so the cases that matter most are
the ones where something is broken or unreadable: those must report BUSY, not
idle.

Run with:    python3 -m unittest tests.activity_beacon_test
(from charts/workspace/)
"""

import json
import os
import sys
import unittest
from unittest import mock

# Import server.py from the parent directory.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402

NOW = 1_000_000.0


def _task(status, last_activity=None):
    return {'status': status, 'last_activity_at': last_activity, 'created_at': 1.0}


class BeaconBusyTest(unittest.TestCase):
    """What counts as busy."""

    def setUp(self):
        # Isolate from the real workspace: no hypervisor threads, no tmux, no
        # terminal history. Each test opts its own signal back in.
        self._hv = server._HYPERVISOR_AVAILABLE
        server._HYPERVISOR_AVAILABLE = False
        server.ActivityBeacon._terminal_at = 0.0
        self._attached = mock.patch.object(
            server.ActivityBeacon, '_terminal_attached', classmethod(lambda cls: False))
        self._attached.start()
        # Keep the boot floor far in the past so it doesn't mask the timestamps
        # under test; its own behaviour is asserted separately below.
        self._started_at = server.ActivityBeacon._started_at
        server.ActivityBeacon._started_at = 0.0

    def tearDown(self):
        self._attached.stop()
        server._HYPERVISOR_AVAILABLE = self._hv
        server.ActivityBeacon._terminal_at = 0.0
        server.ActivityBeacon._started_at = self._started_at

    def _metas(self, metas):
        return mock.patch.object(server.ProjectsManager, '_scan_task_metas',
                                 staticmethod(lambda: metas))

    def test_idle_when_every_task_is_finished(self):
        with self._metas([_task('completed', 500.0), _task('error', 400.0)]):
            state = server.ActivityBeacon.compute(now=NOW)
        self.assertFalse(state['busy'])
        self.assertEqual(state['reasons'], [])
        self.assertEqual(state['last_activity'], 500.0)

    def test_a_running_build_is_busy(self):
        with self._metas([_task('running', 900.0)]):
            state = server.ActivityBeacon.compute(now=NOW)
        self.assertTrue(state['busy'])
        self.assertIn('build', state['reasons'])

    def test_waiting_for_input_is_busy(self):
        """The case CPU alone gets wrong.

        An agent parked at a permission prompt burns no CPU and looks perfectly
        idle from outside the pod — but its run is mid-flight and scaling to 0
        would throw it away. This is the reason the beacon exists at all.
        """
        with self._metas([_task('waiting-for-input', 100.0)]):
            state = server.ActivityBeacon.compute(now=NOW)
        self.assertTrue(state['busy'])
        self.assertIn('build', state['reasons'])

    def test_busy_matches_the_servers_own_definition(self):
        # Not a second opinion about what "live" means — the same tuple.
        for status in server.ClaudeTaskManager._LIVE_STATUSES:
            with self._metas([_task(status, 1.0)]):
                self.assertTrue(server.ActivityBeacon.compute(now=NOW)['busy'],
                                f'{status} must count as busy')

    def test_an_unreadable_tasks_dir_reports_busy(self):
        """Fail closed: we could not prove it is idle, so we do not claim it."""
        def boom():
            raise OSError('tasks dir gone')
        with mock.patch.object(server.ProjectsManager, '_scan_task_metas',
                               staticmethod(boom)):
            state = server.ActivityBeacon.compute(now=NOW)
        self.assertTrue(state['busy'])

    def test_malformed_task_metadata_does_not_raise(self):
        with self._metas([None, 'nonsense', {}, _task('completed', 'later')]):
            state = server.ActivityBeacon.compute(now=NOW)
        self.assertFalse(state['busy'])

    def test_an_attached_terminal_is_busy(self):
        with mock.patch.object(server.ActivityBeacon, '_terminal_attached',
                               classmethod(lambda cls: True)):
            with self._metas([]):
                state = server.ActivityBeacon.compute(now=NOW)
        self.assertTrue(state['busy'])
        self.assertIn('terminal', state['reasons'])
        self.assertEqual(state['last_activity'], NOW)

    def test_recent_terminal_traffic_is_busy_then_ages_out(self):
        server.ActivityBeacon._terminal_at = NOW - 10
        with self._metas([]):
            self.assertTrue(server.ActivityBeacon.compute(now=NOW)['busy'])
        server.ActivityBeacon._terminal_at = (
            NOW - server.ActivityBeacon.TERMINAL_WINDOW_SECONDS - 1)
        with self._metas([]):
            self.assertFalse(server.ActivityBeacon.compute(now=NOW)['busy'])

    def test_a_live_chat_turn_is_busy(self):
        server._HYPERVISOR_AVAILABLE = True
        session = mock.Mock()
        session.read_meta.return_value = {'status': 'running', 'updated_at': 900.0}
        with mock.patch.object(server, 'HypervisorSession') as hv, \
                mock.patch.object(server.os, 'listdir', lambda p: ['t1']):
            hv.get.return_value = session
            with self._metas([]):
                state = server.ActivityBeacon.compute(now=NOW)
        self.assertTrue(state['busy'])
        self.assertIn('hypervisor', state['reasons'])

    def test_an_idle_chat_is_not_busy(self):
        server._HYPERVISOR_AVAILABLE = True
        session = mock.Mock()
        session.read_meta.return_value = {'status': 'idle', 'updated_at': 900.0}
        with mock.patch.object(server, 'HypervisorSession') as hv, \
                mock.patch.object(server.os, 'listdir', lambda p: ['t1']):
            hv.get.return_value = session
            with self._metas([]):
                state = server.ActivityBeacon.compute(now=NOW)
        self.assertFalse(state['busy'])
        self.assertEqual(state['last_activity'], 900.0)

    def test_last_activity_never_predates_boot(self):
        """A workspace with no history at all is 'active since boot'.

        Otherwise a brand-new workspace reports last-activity 0, the controller
        computes an idle age of decades, and opting in would pause it instantly
        — at the exact moment someone provisioned it to use it.
        """
        server.ActivityBeacon._started_at = NOW - 5
        with self._metas([]):
            state = server.ActivityBeacon.compute(now=NOW)
        self.assertEqual(state['last_activity'], NOW - 5)


class BeaconTerminalDetectionTest(unittest.TestCase):
    """tmux clients are how an attached web terminal is seen at all."""

    def test_attached_when_tmux_lists_a_client(self):
        with mock.patch.object(server.subprocess, 'run',
                               return_value=mock.Mock(returncode=0, stdout='/dev/pts/1\n')):
            self.assertTrue(server.ActivityBeacon._terminal_attached())

    def test_not_attached_when_no_clients(self):
        with mock.patch.object(server.subprocess, 'run',
                               return_value=mock.Mock(returncode=0, stdout='\n')):
            self.assertFalse(server.ActivityBeacon._terminal_attached())

    def test_not_attached_when_tmux_server_is_not_running(self):
        with mock.patch.object(server.subprocess, 'run',
                               return_value=mock.Mock(returncode=1, stdout='')):
            self.assertFalse(server.ActivityBeacon._terminal_attached())

    def test_missing_tmux_is_not_fatal(self):
        with mock.patch.object(server.subprocess, 'run', side_effect=OSError('no tmux')):
            self.assertFalse(server.ActivityBeacon._terminal_attached())


class BeaconPublishTest(unittest.TestCase):
    """What actually lands on the pod."""

    def setUp(self):
        os.environ['POD_NAME'] = 'ws-octo-abc-123'

    def tearDown(self):
        os.environ.pop('POD_NAME', None)

    def _patch_args(self, run):
        return run.call_args.args[0]

    def test_publishes_all_three_annotations(self):
        state = {'busy': False, 'last_activity': 950.0, 'reasons': []}
        with mock.patch.object(server.subprocess, 'run',
                               return_value=mock.Mock(returncode=0, stdout='', stderr='')) as run, \
                mock.patch.object(server.CronManager, 'detect_namespace',
                                  staticmethod(lambda: 'ws-octo')):
            server.ActivityBeacon.publish(state, now=NOW)
        args = self._patch_args(run)
        self.assertEqual(args[:4], ['kubectl', 'patch', 'pod', 'ws-octo-abc-123'])
        self.assertIn('ws-octo', args)
        body = json.loads(args[args.index('-p') + 1])
        ann = body['metadata']['annotations']
        self.assertEqual(ann[server.ActivityBeacon.BUSY_ANNOTATION], 'false')
        self.assertEqual(ann[server.ActivityBeacon.LAST_ACTIVITY_ANNOTATION], '950')
        self.assertEqual(ann[server.ActivityBeacon.BEACON_AT_ANNOTATION], '1000000')

    def test_busy_is_published_as_true(self):
        state = {'busy': True, 'last_activity': NOW, 'reasons': ['build']}
        with mock.patch.object(server.subprocess, 'run',
                               return_value=mock.Mock(returncode=0, stdout='', stderr='')) as run, \
                mock.patch.object(server.CronManager, 'detect_namespace',
                                  staticmethod(lambda: 'ws-octo')):
            server.ActivityBeacon.publish(state, now=NOW)
        args = self._patch_args(run)
        body = json.loads(args[args.index('-p') + 1])
        self.assertEqual(body['metadata']['annotations'][
            server.ActivityBeacon.BUSY_ANNOTATION], 'true')

    def test_only_annotations_are_patched(self):
        """The beacon must never be able to change the pod's spec."""
        state = {'busy': False, 'last_activity': 1.0, 'reasons': []}
        with mock.patch.object(server.subprocess, 'run',
                               return_value=mock.Mock(returncode=0, stdout='', stderr='')) as run, \
                mock.patch.object(server.CronManager, 'detect_namespace',
                                  staticmethod(lambda: 'ws-octo')):
            server.ActivityBeacon.publish(state, now=NOW)
        args = self._patch_args(run)
        body = json.loads(args[args.index('-p') + 1])
        self.assertEqual(list(body.keys()), ['metadata'])
        self.assertEqual(list(body['metadata'].keys()), ['annotations'])

    def test_a_failed_patch_raises_so_the_annotation_goes_stale(self):
        # Going stale is the safe outcome: the controller reads a stale beacon
        # as busy and leaves the workspace running.
        state = {'busy': False, 'last_activity': 1.0, 'reasons': []}
        with mock.patch.object(server.subprocess, 'run',
                               return_value=mock.Mock(returncode=1, stdout='',
                                                      stderr='forbidden')), \
                mock.patch.object(server.CronManager, 'detect_namespace',
                                  staticmethod(lambda: 'ws-octo')):
            with self.assertRaises(RuntimeError):
                server.ActivityBeacon.publish(state, now=NOW)

    def test_pod_name_falls_back_to_the_hostname(self):
        os.environ.pop('POD_NAME', None)
        with mock.patch.object(server.socket, 'gethostname', lambda: 'ws-octo-xyz'):
            self.assertEqual(server.ActivityBeacon.pod_name(), 'ws-octo-xyz')


if __name__ == '__main__':
    unittest.main()
