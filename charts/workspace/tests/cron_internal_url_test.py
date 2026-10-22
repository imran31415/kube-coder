"""Where a scheduled trigger sends its fire, and how it gets re-pointed (#728 4b).

The always-on keeper only helps a cron if the CronJob is actually curling the
keeper. Turning the keeper on changes that address, and the CronJobs already on
the cluster have the old one baked into their command line — nothing rewrites a
CronJob except an edit to its trigger. So a workspace whose crons all predate
the keeper would go on firing straight at a workspace that is asleep, which is
the precise failure step 4b exists to fix.

Hence the two halves here:

  * the manifest records the address it was written against, as an annotation,
    because that is the only thing cheap enough to check at every boot;
  * reconcile_internal_url() re-applies on a mismatch and on a missing
    annotation, and on nothing else — a boot step that re-applied every CronJob
    every time would be a kubectl storm on a workspace with twenty crons.

kubectl is stubbed. What is asserted is which commands would run and what the
manifest says, which is what the behaviour consists of; whether kubectl works
is not this suite's question.

Run:  python3 -m unittest tests.cron_internal_url_test   (from charts/workspace/)
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

# server.py imports fcntl at module load, which does not exist on Windows.
# Same shim as tests/trigger_runs_test.py, so this suite runs on a dev laptop.
try:
    import fcntl  # noqa: F401
except ImportError:  # pragma: no cover - platform shim
    import types
    _shim = types.ModuleType('fcntl')
    _shim._kube_coder_shim = True
    _shim.flock = lambda *a, **k: None
    _shim.lockf = lambda *a, **k: None
    _shim.LOCK_EX = 2
    _shim.LOCK_UN = 8
    _shim.LOCK_NB = 4
    sys.modules['fcntl'] = _shim

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import server  # noqa: E402

ANNOTATION = 'kube-coder.dev/internal-url'
KEEPER_URL = 'http://keeper-alice-api.ws-alice.svc.cluster.local'
WORKSPACE_URL = 'http://ws-alice.ws-alice.svc.cluster.local:6080'


class _Kubectl:
    """Stands in for subprocess.run and records every kubectl invocation.

    `annotations` maps a CronJob name to the annotation value a `kubectl get`
    should report; a name that is absent answers like a missing object, which
    the reconciler has to treat differently from an empty annotation.
    """

    def __init__(self, annotations=None, missing=()):
        self.annotations = annotations or {}
        self.missing = set(missing)
        self.gets = []
        self.applies = []

    def __call__(self, argv, **kw):
        class R:
            returncode = 0
            stdout = ''
            stderr = ''
        r = R()
        if argv[:2] == ['kubectl', 'get']:
            name = argv[3]
            self.gets.append(name)
            if name in self.missing:
                r.returncode = 1
                r.stderr = 'Error from server (NotFound)'
                return r
            r.stdout = self.annotations.get(name, '')
            return r
        if argv[:2] == ['kubectl', 'apply']:
            self.applies.append(kw.get('input', ''))
            return r
        raise AssertionError(f'unexpected command: {argv}')


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for mgr, attr, sub in (
                (server.CronManager, 'CRONS_DIR', 'crons'),
                (server.PageWatchManager, 'PAGE_WATCHES_DIR', 'page-watches')):
            d = os.path.join(self.tmp, sub)
            os.makedirs(d, mode=0o700)
            p = mock.patch.object(mgr, attr, d)
            p.start()
            self.addCleanup(p.stop)
        # A stable identity, so the k8s object names in the assertions are
        # predictable rather than derived from this laptop's hostname.
        for p in (mock.patch.dict(os.environ, {'WORKSPACE_USER': 'alice'}),
                  mock.patch.object(server.CronManager, 'detect_namespace',
                                    staticmethod(lambda: 'ws-alice'))):
            p.start()
            self.addCleanup(p.stop)

    def write_cron(self, cron_id='nightly', **extra):
        cfg = {'id': cron_id, 'schedule': '0 3 * * *', 'timezone': 'UTC',
               'prompt_template': 'tidy up', 'fire_token': 'fire-' + cron_id,
               'created_at': '2026-10-20T00:00:00Z'}
        cfg.update(extra)
        with open(os.path.join(server.CronManager.CRONS_DIR, cron_id + '.json'), 'w') as f:
            json.dump(cfg, f)
        return cfg

    def write_watch(self, watch_id='docs', **extra):
        cfg = {'id': watch_id, 'schedule': '*/10 * * * *', 'timezone': 'UTC',
               'url': 'https://example.com/status', 'selector': '.state',
               'prompt_template': 'something changed',
               'fire_token': 'fire-' + watch_id,
               'created_at': '2026-10-20T00:00:00Z'}
        cfg.update(extra)
        with open(os.path.join(server.PageWatchManager.PAGE_WATCHES_DIR,
                               watch_id + '.json'), 'w') as f:
            json.dump(cfg, f)
        return cfg


class InternalUrlTests(_Base):
    def test_defaults_to_the_workspace_service(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('WORKSPACE_INTERNAL_URL', None)
            self.assertEqual(server.CronManager.internal_url(), WORKSPACE_URL)

    def test_env_wins_so_the_chart_can_point_it_at_the_keeper(self):
        with mock.patch.dict(os.environ, {'WORKSPACE_INTERNAL_URL': KEEPER_URL}):
            self.assertEqual(server.CronManager.internal_url(), KEEPER_URL)

    def test_both_managers_read_the_same_address(self):
        """A page-watch that fires at the workspace while a cron fires at the
        keeper would be a sleeping workspace that half works."""
        kube = _Kubectl()
        with mock.patch.dict(os.environ, {'WORKSPACE_INTERNAL_URL': KEEPER_URL}), \
                mock.patch.object(server.subprocess, 'run', kube):
            server.CronManager._apply_k8s(self.write_cron())
            server.PageWatchManager._apply_k8s(self.write_watch())
        self.assertEqual(len(kube.applies), 2)
        for manifest in kube.applies:
            self.assertIn(f'{ANNOTATION}: {KEEPER_URL}', manifest)
            self.assertIn(f'"{KEEPER_URL}/api/triggers/', manifest)


class AnnotationTests(_Base):
    def test_cron_manifest_records_the_address_it_was_written_against(self):
        kube = _Kubectl()
        with mock.patch.dict(os.environ, {'WORKSPACE_INTERNAL_URL': KEEPER_URL}), \
                mock.patch.object(server.subprocess, 'run', kube):
            server.CronManager._apply_k8s(self.write_cron())
        manifest = kube.applies[0]
        self.assertIn(f'{ANNOTATION}: {KEEPER_URL}', manifest)
        # The annotation has to agree with the command, or the reconciler would
        # happily confirm a CronJob that curls the wrong place.
        self.assertIn(f'"{KEEPER_URL}/api/triggers/cron-fire/nightly"', manifest)
        self.assertNotIn(WORKSPACE_URL, manifest)

    def test_the_annotation_does_not_carry_the_fire_token(self):
        """The manifest needs the token in its Secret, and nowhere else: an
        annotation is world-readable to anyone with get on the CronJob."""
        kube = _Kubectl()
        with mock.patch.object(server.subprocess, 'run', kube):
            server.CronManager._apply_k8s(self.write_cron())
        manifest = kube.applies[0]
        annotations = manifest.split('annotations:')[1].split('labels:')[0]
        self.assertNotIn('fire-nightly', annotations)


class ReconcileTests(_Base):
    def reconcile(self, kube, url=KEEPER_URL):
        with mock.patch.dict(os.environ, {'WORKSPACE_INTERNAL_URL': url}), \
                mock.patch.object(server.subprocess, 'run', kube):
            return server.CronManager.reconcile_internal_url()

    def test_a_cronjob_written_before_step_4b_self_heals_once(self):
        """The migration case: a CronJob with no annotation at all, which is
        every CronJob on every cluster the moment this ships."""
        self.write_cron()
        kube = _Kubectl(annotations={'cron-alice-nightly': ''})
        self.assertEqual(self.reconcile(kube), 1)
        self.assertEqual(len(kube.applies), 1)
        self.assertIn(f'{ANNOTATION}: {KEEPER_URL}', kube.applies[0])

        # And then it stops: the second boot sees a matching annotation and
        # touches nothing. A boot step that re-applied every time would be a
        # kubectl storm on a workspace with twenty crons.
        kube2 = _Kubectl(annotations={'cron-alice-nightly': KEEPER_URL})
        self.assertEqual(self.reconcile(kube2), 0)
        self.assertEqual(kube2.applies, [])

    def test_a_stale_address_is_replaced(self):
        self.write_cron()
        kube = _Kubectl(annotations={'cron-alice-nightly': WORKSPACE_URL})
        self.assertEqual(self.reconcile(kube), 1)
        self.assertIn(f'"{KEEPER_URL}/api/triggers/cron-fire/nightly"', kube.applies[0])

    def test_turning_the_keeper_off_points_crons_back_at_the_workspace(self):
        """The reverse migration has to work too, or disabling the keeper leaves
        every cron firing at a Service that no longer exists."""
        self.write_cron()
        kube = _Kubectl(annotations={'cron-alice-nightly': KEEPER_URL})
        self.assertEqual(self.reconcile(kube, url=WORKSPACE_URL), 1)
        self.assertIn(f'"{WORKSPACE_URL}/api/triggers/cron-fire/nightly"', kube.applies[0])

    def test_page_watches_are_reconciled_too(self):
        self.write_watch()
        kube = _Kubectl(annotations={'page-watch-alice-docs': WORKSPACE_URL})
        self.assertEqual(self.reconcile(kube), 1)
        self.assertIn('/api/triggers/page-watch-check/docs', kube.applies[0])

    def test_a_missing_cronjob_is_left_alone(self):
        """A config whose apply failed, or a cluster this pod cannot read. Not
        the same as a CronJob with a stale annotation: there is nothing to
        repair, and guessing would mean creating objects at boot."""
        self.write_cron()
        kube = _Kubectl(missing={'cron-alice-nightly'})
        self.assertEqual(self.reconcile(kube), 0)
        self.assertEqual(kube.applies, [])

    def test_a_config_without_a_token_is_skipped_not_reapplied_broken(self):
        """_apply_k8s dereferences cfg['fire_token']; a hand-edited config
        missing it must be skipped rather than raising inside a boot step."""
        self.write_cron(fire_token='')
        kube = _Kubectl(annotations={'cron-alice-nightly': WORKSPACE_URL})
        self.assertEqual(self.reconcile(kube), 0)
        self.assertEqual(kube.applies, [])

    def test_one_broken_apply_does_not_stop_the_others(self):
        self.write_cron('aaa')
        self.write_cron('bbb')

        class Flaky(_Kubectl):
            def __call__(self, argv, **kw):
                if argv[:2] == ['kubectl', 'apply'] and 'aaa' in kw.get('input', ''):
                    raise RuntimeError('apply exploded')
                return super().__call__(argv, **kw)

        kube = Flaky(annotations={'cron-alice-aaa': '', 'cron-alice-bbb': ''})
        self.assertEqual(self.reconcile(kube), 1)
        self.assertEqual(len(kube.applies), 1)
        self.assertIn('bbb', kube.applies[0])

    def test_nothing_to_do_on_a_workspace_with_no_schedules(self):
        kube = _Kubectl()
        self.assertEqual(self.reconcile(kube), 0)
        self.assertEqual(kube.gets, [])

    def test_the_annotation_is_read_with_a_quoted_jsonpath(self):
        """The key contains dots and a slash. The dotted jsonpath form would
        read `kube-coder.dev/internal-url` as four nested fields and come back
        empty — which looks exactly like "no annotation" and would re-apply
        every CronJob on every boot, for ever."""
        self.write_cron()
        captured = []

        class Spy(_Kubectl):
            def __call__(self, argv, **kw):
                captured.append(argv)
                return super().__call__(argv, **kw)

        spy = Spy(annotations={'cron-alice-nightly': KEEPER_URL})
        self.assertEqual(self.reconcile(spy), 0, 'the quoted form must have matched')
        self.assertTrue(captured, 'nothing was read')
        jsonpath = [a for a in captured[0] if a.startswith('jsonpath=')]
        self.assertEqual(
            jsonpath, ["jsonpath={.metadata.annotations['kube-coder.dev/internal-url']}"])


if __name__ == '__main__':
    unittest.main()
