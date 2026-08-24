#!/usr/bin/env python3
"""Unit tests for controller.py — the capacity rollup and the quantity parsers
it builds on. Pure-Python: Prometheus is faked by monkeypatching the query
helpers, so these run with no cluster and no network.

Run from charts/workspace-controller:
    python3 -m unittest discover -s tests -p '*_test.py' -v
"""
import base64
import io
import json
import os
import sys
import types
import unittest

# controller.py lives one dir up; import it without installing anything.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import controller  # noqa: E402


class QuantityParsingTest(unittest.TestCase):
    def test_parse_cpu(self):
        self.assertEqual(controller.parse_cpu('2'), 2.0)
        self.assertEqual(controller.parse_cpu('500m'), 0.5)
        self.assertEqual(controller.parse_cpu('3.89'), 3.89)
        self.assertIsNone(controller.parse_cpu(None))
        self.assertIsNone(controller.parse_cpu(''))

    def test_parse_bytes_binary_and_decimal(self):
        self.assertEqual(controller.parse_bytes('1Ki'), 1024)
        self.assertEqual(controller.parse_bytes('1Gi'), 1024 ** 3)
        # 'Gi' must win over 'G' (longest-suffix-first ordering).
        self.assertEqual(controller.parse_bytes('6Gi'), 6 * 1024 ** 3)
        self.assertEqual(controller.parse_bytes('1G'), 1e9)
        self.assertEqual(controller.parse_bytes('1000000'), 1000000)
        self.assertIsNone(controller.parse_bytes(None))


class ResourceBlockTest(unittest.TestCase):
    def test_percentages_of_allocatable(self):
        b = controller._resource_block(allocatable=4.0, workspace=1.0, cluster=2.0)
        self.assertEqual(b['workspacePct'], 25.0)
        self.assertEqual(b['clusterPct'], 50.0)
        self.assertEqual(b['other'], 1.0)  # cluster - workspace

    def test_unknown_allocatable_yields_null_pcts(self):
        b = controller._resource_block(allocatable=None, workspace=1.0, cluster=2.0)
        self.assertIsNone(b['workspacePct'])
        self.assertIsNone(b['clusterPct'])
        self.assertEqual(b['other'], 1.0)

    def test_zero_allocatable_does_not_divide(self):
        b = controller._resource_block(allocatable=0.0, workspace=1.0, cluster=1.0)
        self.assertIsNone(b['workspacePct'])

    def test_other_clamped_when_workspace_exceeds_cluster(self):
        # Scrape skew can momentarily put ws usage above the all-namespace total;
        # `other` must never go negative or the stacked bar breaks.
        b = controller._resource_block(allocatable=4.0, workspace=2.0, cluster=1.5)
        self.assertEqual(b['other'], 0.0)


class NodeRollupTest(unittest.TestCase):
    def _rollup(self):
        return controller._node_rollup(
            alloc_cpu={'n1': 4.0, 'n2': 4.0},
            alloc_mem={'n1': 8e9, 'n2': 8e9},
            alloc_pods={'n1': 110, 'n2': 110},
            ws_cpu={'n1': 1.0, 'n2': 0.5},
            ws_mem={'n1': 2e9, 'n2': 1e9},
            tot_cpu={'n1': 2.0, 'n2': 1.0},
            tot_mem={'n1': 4e9, 'n2': 3e9},
            pods_ws={'n1': 2, 'n2': 1},
            pods_tot={'n1': 10, 'n2': 8},
        )

    def test_cluster_equals_sum_of_nodes(self):
        nodes, cluster = self._rollup()
        self.assertEqual(cluster['nodeCount'], 2)
        self.assertEqual(cluster['cpu']['allocatable'], 8.0)
        self.assertEqual(cluster['cpu']['workspace'], 1.5)
        self.assertEqual(cluster['cpu']['cluster'], 3.0)
        self.assertEqual(cluster['memory']['allocatable'], 16e9)
        self.assertEqual(cluster['pods'], {'allocatable': 220, 'workspace': 3, 'cluster': 18})

    def test_nodes_sorted_by_name(self):
        nodes, _ = self._rollup()
        self.assertEqual([n['name'] for n in nodes], ['n1', 'n2'])

    def test_allocatable_defines_node_set_missing_usage_is_zero(self):
        # n2 has allocatable but no usage series yet (just joined / idle).
        nodes, cluster = controller._node_rollup(
            alloc_cpu={'n1': 4.0, 'n2': 4.0}, alloc_mem={'n1': 8e9, 'n2': 8e9},
            alloc_pods={}, ws_cpu={'n1': 1.0}, ws_mem={}, tot_cpu={'n1': 1.0},
            tot_mem={}, pods_ws={}, pods_tot={},
        )
        self.assertEqual(len(nodes), 2)
        n2 = next(n for n in nodes if n['name'] == 'n2')
        self.assertEqual(n2['cpu']['workspace'], 0.0)
        self.assertEqual(cluster['cpu']['workspace'], 1.0)

    def test_unknown_allocatable_propagates_to_cluster(self):
        # No allocatable series at all -> cluster allocatable is None, not 0,
        # so the UI shows "unknown capacity" rather than "0 cores".
        nodes, cluster = controller._node_rollup(
            alloc_cpu={}, alloc_mem={}, alloc_pods={}, ws_cpu={}, ws_mem={},
            tot_cpu={}, tot_mem={}, pods_ws={}, pods_tot={},
        )
        self.assertEqual(nodes, [])
        self.assertEqual(cluster['nodeCount'], 0)
        self.assertIsNone(cluster['cpu']['allocatable'])


class PerNodeUsageQueryTest(unittest.TestCase):
    def test_join_keys_on_namespace_and_pod(self):
        q = controller._per_node_usage('INNER', 'namespace="coder",pod=~"ws-.*"')
        self.assertIn('sum by (node)', q)
        self.assertIn('on (namespace, pod) group_left (node)', q)
        self.assertIn('kube_pod_info{namespace="coder",pod=~"ws-.*"}', q)
        self.assertIn('INNER', q)


class ClusterCapacityTest(unittest.TestCase):
    """End-to-end shape with Prometheus faked out."""

    def setUp(self):
        self._instant = controller.prom_instant_multi
        self._range = controller.prom_range

    def tearDown(self):
        controller.prom_instant_multi = self._instant
        controller.prom_range = self._range

    def test_happy_path_shape(self):
        def fake_instant(expr):
            if 'kube_node_status_allocatable{resource="cpu"}' in expr:
                return [({'node': 'n1'}, 4.0)]
            if 'kube_node_status_allocatable{resource="memory"}' in expr:
                return [({'node': 'n1'}, 8e9)]
            if 'kube_node_status_allocatable{resource="pods"}' in expr:
                return [({'node': 'n1'}, 110.0)]
            if 'count by (node) (kube_pod_info{namespace' in expr:
                return [({'node': 'n1'}, 2.0)]
            if 'count by (node) (kube_pod_info)' in expr:
                return [({'node': 'n1'}, 9.0)]
            if 'pod=~"ws-.*"' in expr and 'cpu' in expr:
                return [({'node': 'n1'}, 1.0)]
            if 'pod=~"ws-.*"' in expr:
                return [({'node': 'n1'}, 2e9)]
            if 'cpu' in expr:
                return [({'node': 'n1'}, 2.0)]
            return [({'node': 'n1'}, 4e9)]

        controller.prom_instant_multi = fake_instant
        controller.prom_range = lambda expr, s, st: [[1000, 1.0], [1060, 1.5]]

        cap = controller.cluster_capacity(range_seconds=600, step=60)
        self.assertIsNone(cap['metricsError'])
        self.assertEqual(cap['cluster']['nodeCount'], 1)
        self.assertEqual(cap['cluster']['cpu']['allocatable'], 4.0)
        self.assertEqual(cap['cluster']['cpu']['workspace'], 1.0)
        self.assertEqual(cap['cluster']['cpu']['clusterPct'], 50.0)
        self.assertEqual(len(cap['nodes']), 1)
        self.assertEqual(cap['history']['cpu']['workspace'], [[1000, 1.0], [1060, 1.5]])

    def test_prom_error_is_captured_not_raised(self):
        def boom(expr):
            raise controller.PromError('prometheus unreachable')
        controller.prom_instant_multi = boom

        cap = controller.cluster_capacity()
        self.assertEqual(cap['metricsError'], 'prometheus unreachable')
        self.assertIsNone(cap['cluster'])
        self.assertEqual(cap['nodes'], [])


class HealthStatusTest(unittest.TestCase):
    def test_worst_percentage_drives_the_light(self):
        ok, warn, crit = {'clusterPct': 50.0}, {'clusterPct': 80.0}, {'clusterPct': 95.0}
        self.assertEqual(controller._health_status(ok, ok), 'ok')
        self.assertEqual(controller._health_status(ok, warn), 'warn')   # worst wins
        self.assertEqual(controller._health_status(warn, crit), 'crit')

    def test_boundaries(self):
        self.assertEqual(controller._health_status({'clusterPct': 74.9}), 'ok')
        self.assertEqual(controller._health_status({'clusterPct': 75.0}), 'warn')
        self.assertEqual(controller._health_status({'clusterPct': 89.9}), 'warn')
        self.assertEqual(controller._health_status({'clusterPct': 90.0}), 'crit')

    def test_unknown_when_no_percentage(self):
        self.assertEqual(controller._health_status({'clusterPct': None}, None), 'unknown')


class ClusterHealthTest(unittest.TestCase):
    """The cheap landing-page summary: instant scalars only, no range/per-node."""

    def setUp(self):
        self._scalar = controller.prom_scalar

    def tearDown(self):
        controller.prom_scalar = self._scalar

    def test_summary_shape_is_cheap(self):
        def fake_scalar(expr):
            if 'kube_node_status_allocatable{resource="cpu"}' in expr:
                return 4.0
            if 'kube_node_status_allocatable{resource="memory"}' in expr:
                return 8e9
            if 'count by (node) (kube_node_status_allocatable)' in expr:
                return 1.0
            if 'pod=~"ws-.*"' in expr and 'container_cpu' in expr:
                return 1.0
            if 'pod=~"ws-.*"' in expr:
                return 2e9
            if 'container_cpu' in expr:
                return 2.0
            return 4e9

        controller.prom_scalar = fake_scalar
        h = controller.cluster_health()
        self.assertIsNone(h['metricsError'])
        self.assertEqual(h['cluster']['nodeCount'], 1)
        self.assertEqual(h['cluster']['cpu']['allocatable'], 4.0)
        self.assertEqual(h['cluster']['cpu']['clusterPct'], 50.0)  # 2.0 / 4.0
        self.assertEqual(h['cluster']['memory']['clusterPct'], 50.0)
        self.assertEqual(h['status'], 'ok')
        # The whole point: no range history and no per-node breakdown.
        self.assertNotIn('history', h)
        self.assertNotIn('nodes', h)

    def test_prom_error_captured_not_raised(self):
        def boom(expr):
            raise controller.PromError('prometheus unreachable')
        controller.prom_scalar = boom
        h = controller.cluster_health()
        self.assertEqual(h['metricsError'], 'prometheus unreachable')
        self.assertIsNone(h['cluster'])
        self.assertEqual(h['status'], 'unknown')


class ProvisionPureLogicTest(unittest.TestCase):
    """Pure-logic provisioning helpers: OAuth-cred validation, cookie-secret
    shape, and values rendering. No network, no cluster."""

    def setUp(self):
        controller.WORKSPACE_DOMAIN = 'dev.scalebase.io'

    def test_slugify_lowercases(self):
        self.assertEqual(controller.slugify('Chase-31415'), 'chase-31415')

    def test_login_regex_accepts_valid_rejects_invalid(self):
        self.assertTrue(controller._GH_LOGIN_RE.match('octocat'))
        self.assertTrue(controller._GH_LOGIN_RE.match('a-b-c1'))
        self.assertFalse(controller._GH_LOGIN_RE.match('-bad'))
        self.assertFalse(controller._GH_LOGIN_RE.match('bad-'))
        self.assertFalse(controller._GH_LOGIN_RE.match('has space'))
        self.assertFalse(controller._GH_LOGIN_RE.match('under_score'))

    def test_oauth_callback_url(self):
        self.assertEqual(controller.oauth_callback_url('octo.dev.scalebase.io'),
                         'https://octo.dev.scalebase.io/oauth2/callback')

    def test_validate_oauth_creds_accepts_oauth_app(self):
        cid, secret = controller.validate_oauth_creds('  Ov23liExampleId  ', '  shh-secret ')
        self.assertEqual(cid, 'Ov23liExampleId')   # trimmed
        self.assertEqual(secret, 'shh-secret')

    def test_validate_oauth_creds_requires_both(self):
        with self.assertRaises(ValueError):
            controller.validate_oauth_creds('', 'secret')
        with self.assertRaises(ValueError):
            controller.validate_oauth_creds('Ov23li', '   ')

    def test_validate_oauth_creds_rejects_github_app_id(self):
        # The exact misconfig that 404s oauth2-proxy: a GitHub App client id.
        with self.assertRaises(ValueError) as ctx:
            controller.validate_oauth_creds('Iv23liO7CFQE11YsmG0N', 'secret')
        self.assertIn('OAuth App', str(ctx.exception))

    def test_cookie_secret_shape(self):
        s = controller.gen_cookie_secret()
        self.assertEqual(len(s), 32)
        self.assertTrue(s.isalnum())

    def test_render_values_defaults_to_latest_release_when_no_tag_given(self):
        # No explicit imageTag => the workspace is pinned to the latest release,
        # not a stale WORKSPACE_IMAGE_TAG. Regression: new workspaces were coming
        # up on an old version because the static env pin won over the release.
        opts = {'login': 'octo', 'slug': 'octo', 'host': 'octo.dev.scalebase.io'}
        orig_latest, orig_env = controller.latest_version, controller.WORKSPACE_IMAGE_TAG
        controller.NAMESPACE = 'coder'
        controller.latest_version = lambda: 'v1.11.0'
        controller.WORKSPACE_IMAGE_TAG = 'v1.6.0'   # stale pin must NOT win
        try:
            text = controller.render_values_yaml(opts, 'Ov23liclientid', 'cookiesecret32xxxxxxxxxxxxxxxxxxx')
        finally:
            controller.latest_version, controller.WORKSPACE_IMAGE_TAG = orig_latest, orig_env
        self.assertIn('tag: devlaptop-v1.11.0', text)
        self.assertNotIn('devlaptop-v1.6.0', text)

    def test_render_values_falls_back_to_env_when_release_lookup_fails(self):
        # If the release lookup is unavailable, WORKSPACE_IMAGE_TAG is the fallback.
        opts = {'login': 'octo', 'slug': 'octo', 'host': 'octo.dev.scalebase.io'}
        orig_latest, orig_env = controller.latest_version, controller.WORKSPACE_IMAGE_TAG
        controller.NAMESPACE = 'coder'
        controller.latest_version = lambda: None
        controller.WORKSPACE_IMAGE_TAG = 'v1.6.0'
        try:
            text = controller.render_values_yaml(opts, 'Ov23liclientid', 'cookiesecret32xxxxxxxxxxxxxxxxxxx')
        finally:
            controller.latest_version, controller.WORKSPACE_IMAGE_TAG = orig_latest, orig_env
        self.assertIn('tag: devlaptop-v1.6.0', text)

    def test_render_values_yaml_is_valid_and_has_fields(self):
        opts = {'login': 'Octo', 'slug': 'octo', 'host': 'octo.dev.scalebase.io',
                'pvcSize': '30Gi', 'gitName': 'Octo Cat', 'gitEmail': 'octo@example.com',
                'imageTag': 'v9.9.9'}
        # Shared-secret projection (parity with the hand-scaffolded template).
        controller.WORKSPACE_SELF_SERVE_SECRET = 'kc-self-serve'
        controller.WORKSPACE_ASSISTANT_SECRET = 'coder-shared-assistant'
        controller.NAMESPACE = 'coder'   # control-plane namespace the controller runs in
        text = controller.render_values_yaml(opts, 'Ov23liexampleclientid', 'cookiesecret32xxxxxxxxxxxxxxxxxxx')
        # Validate it parses as YAML and carries the access gate + host.
        try:
            import yaml  # PyYAML may not be installed in CI; fall back to substring checks.
            doc = yaml.safe_load(text)
            self.assertEqual(doc['user']['name'], 'octo')
            # Per-workspace namespace (#103): lands in its own ws-<slug> namespace,
            # and points back at the control-plane namespace for controller RBAC +
            # self-serve URL resolution.
            self.assertEqual(doc['namespace'], 'ws-octo')
            self.assertEqual(doc['controller']['namespace'], 'coder')
            self.assertEqual(doc['update']['controllerNamespace'], 'coder')
            self.assertEqual(doc['user']['host'], 'octo.dev.scalebase.io')
            self.assertEqual(doc['user']['pvcSize'], '30Gi')
            self.assertEqual(doc['oauth2']['githubUsers'], 'Octo')   # login, case-preserved
            self.assertEqual(doc['oauth2']['clientId'], 'Ov23liexampleclientid')
            self.assertEqual(doc['image']['tag'], 'devlaptop-v9.9.9')
            self.assertEqual(doc['ingress']['tls']['secretName'], 'octo-dev-scalebase-io-tls')
            self.assertEqual(doc['ingress']['auth']['type'], 'oauth2')
            # values.yaml carries only the placeholder; the real secret is
            # split out into secrets/oauth2.yaml (render_oauth_secret_yaml).
            self.assertEqual(doc['oauth2']['clientSecret'], 'OVERRIDE-IN-SECRETS-OAUTH2-YAML')
            # Parity blocks: self-serve updates + shared OpenRouter projected in.
            self.assertEqual(doc['update']['selfServeSecretName'], 'kc-self-serve')
            self.assertEqual(doc['assistant']['openrouter']['sharedSecretName'], 'coder-shared-assistant')
        except ImportError:
            self.assertIn('name: octo', text)
            self.assertIn('host: octo.dev.scalebase.io', text)
            self.assertIn('githubUsers: "Octo"', text)
            self.assertIn('devlaptop-v9.9.9', text)
            self.assertIn('selfServeSecretName: "kc-self-serve"', text)
            self.assertIn('sharedSecretName: "coder-shared-assistant"', text)
            self.assertIn('namespace: ws-octo', text)
            self.assertIn('controllerNamespace: coder', text)

    def test_oauth_secret_yaml_holds_only_secret(self):
        text = controller.render_oauth_secret_yaml('supersecret')
        self.assertIn('clientSecret: "supersecret"', text)
        self.assertNotIn('clientId', text)

    def test_job_manifest_uses_provisioner_sa_and_slug(self):
        controller.PROVISIONER_IMAGE = 'example/img:1'
        controller.PROVISIONER_SA = 'workspace-provisioner'
        controller.NAMESPACE = 'coder'
        # Provisioning now requires an immutable pinned chart ref (finding 7).
        self.addCleanup(setattr, controller, 'CHART_REF', controller.CHART_REF)
        controller.CHART_REF = 'v1.40.1'
        job = controller.build_job_manifest('octo')
        self.assertEqual(job['kind'], 'Job')
        self.assertEqual(job['spec']['template']['spec']['serviceAccountName'], 'workspace-provisioner')
        self.assertEqual(job['metadata']['labels']['provisionUser'], 'octo')
        env = {e['name']: e.get('value') for e in job['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual(env['SLUG'], 'octo')
        # The Job runs in the control-plane namespace (regcred source) but deploys
        # the workspace into its own ws-<slug> namespace (#103).
        self.assertEqual(job['metadata']['namespace'], 'coder')
        self.assertEqual(env['NAMESPACE'], 'coder')
        self.assertEqual(env['WS_NAMESPACE'], 'ws-octo')
        self.assertIn('ttlSecondsAfterFinished', job['spec'])
        self.assertEqual(job['spec']['template']['spec']['restartPolicy'], 'Never')

    def test_job_manifest_conforms_to_admission_policy_invariants(self):
        """The real provisioner Job must satisfy every invariant the
        ValidatingAdmissionPolicy (templates/provisioner-vap.yaml) enforces on
        provisioner-SA Jobs — otherwise deploying the VAP would reject the
        controller's own legitimate Job (finding 4). Keep code + policy in sync."""
        controller.PROVISIONER_IMAGE = 'test-registry/coder:tag'
        controller.PROVISIONER_SA = 'workspace-provisioner'
        controller.NAMESPACE = 'coder'
        self.addCleanup(setattr, controller, 'CHART_REF', controller.CHART_REF)
        controller.CHART_REF = 'v1.40.1'   # immutable pinned ref (finding 7)
        pod = controller.build_job_manifest('octo')['spec']['template']['spec']
        # Exactly one container named 'provision'; no init/ephemeral containers.
        self.assertEqual(len(pod['containers']), 1)
        self.assertEqual(pod['containers'][0]['name'], 'provision')
        self.assertNotIn('initContainers', pod)
        self.assertNotIn('ephemeralContainers', pod)
        # No command/args override — the image's baked entrypoint is the program
        # (#422). The VAP denies these outright, so the controller's own Job must
        # not set them or it would reject itself.
        self.assertNotIn('command', pod['containers'][0])
        self.assertNotIn('args', pod['containers'][0])
        # Approved image repository.
        self.assertTrue(pod['containers'][0]['image'].startswith('test-registry/coder'))
        # No privileged securityContext, no host namespaces, no hostPath volumes.
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


class ChartRefSupplyChainTest(unittest.TestCase):
    """Finding 7: the provisioner Job clones CHART_REF and runs its make deploy
    under the cluster-privileged provisioner SA, so the ref must be immutable.
    Mutable/floating refs are rejected fail-closed unless the operator opts in
    via ALLOW_MUTABLE_CHART_REF, and every provision logs the ref it used."""

    def setUp(self):
        self.addCleanup(setattr, controller, 'CHART_REF', controller.CHART_REF)
        self.addCleanup(setattr, controller, 'ALLOW_MUTABLE_CHART_REF',
                        controller.ALLOW_MUTABLE_CHART_REF)
        self.addCleanup(setattr, controller, 'PROVISIONER_IMAGE',
                        controller.PROVISIONER_IMAGE)
        self.addCleanup(setattr, controller, 'PROVISIONER_SA', controller.PROVISIONER_SA)
        self.addCleanup(setattr, controller, 'NAMESPACE', controller.NAMESPACE)
        controller.PROVISIONER_IMAGE = 'example/img:1'
        controller.PROVISIONER_SA = 'workspace-provisioner'
        controller.NAMESPACE = 'coder'
        controller.ALLOW_MUTABLE_CHART_REF = False

    def test_classify_chart_ref(self):
        self.assertEqual(controller.classify_chart_ref('a' * 40), 'commit-sha')
        self.assertEqual(controller.classify_chart_ref('b' * 64), 'commit-sha')
        self.assertEqual(controller.classify_chart_ref('v1.40.1'), 'release-tag')
        for mut in ('main', 'latest', 'HEAD', 'feature/x', 'develop', 'abc1234'):
            self.assertEqual(controller.classify_chart_ref(mut), 'mutable', mut)

    def test_rejects_mutable_refs_by_default(self):
        for mut in ('main', 'latest', 'HEAD', 'feature/x'):
            controller.CHART_REF = mut
            with self.assertRaises(controller.ProvisionError):
                controller.validate_chart_ref(mut)
            # create_provision_job -> build_job_manifest must fail closed too.
            with self.assertRaises(controller.ProvisionError):
                controller.build_job_manifest('octo')

    def test_error_message_names_the_escape_hatch(self):
        with self.assertRaises(controller.ProvisionError) as ctx:
            controller.validate_chart_ref('main')
        msg = str(ctx.exception)
        self.assertIn('allowMutableRef', msg)
        self.assertIn('immutable', msg)

    def test_accepts_immutable_sha_and_release_tag(self):
        for ref in ('a' * 40, 'c' * 64, 'v1.40.1'):
            controller.CHART_REF = ref
            self.assertTrue(controller.chart_ref_is_immutable(ref))
            job = controller.build_job_manifest('octo')   # must not raise
            env = {e['name']: e.get('value')
                   for e in job['spec']['template']['spec']['containers'][0]['env']}
            self.assertEqual(env['CHART_REF'], ref)

    def test_escape_hatch_permits_mutable_ref(self):
        controller.ALLOW_MUTABLE_CHART_REF = True
        controller.CHART_REF = 'main'
        self.assertEqual(controller.validate_chart_ref('main'), 'mutable')
        job = controller.build_job_manifest('octo')       # must not raise
        env = {e['name']: e.get('value')
               for e in job['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual(env['CHART_REF'], 'main')

    def test_resolved_ref_is_logged(self):
        controller.CHART_REF = 'v1.40.1'
        buf = io.StringIO()
        orig = sys.stderr
        sys.stderr = buf
        try:
            controller.build_job_manifest('octo')
        finally:
            sys.stderr = orig
        out = buf.getvalue()
        self.assertIn('v1.40.1', out)
        self.assertIn('release-tag', out)
        self.assertIn('octo', out)

    def test_escape_hatch_decision_is_logged(self):
        controller.ALLOW_MUTABLE_CHART_REF = True
        controller.CHART_REF = 'main'
        buf = io.StringIO()
        orig = sys.stderr
        sys.stderr = buf
        try:
            controller.build_job_manifest('octo')
        finally:
            sys.stderr = orig
        out = buf.getvalue()
        self.assertIn('allowMutableRef=True', out)
        self.assertIn('(mutable)', out)


# Repo root, for the provisioner image sources the Job now runs FROM rather than
# being handed. tests/ -> workspace-controller/ -> charts/ -> repo root.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
PROVISION_SH = os.path.join(REPO_ROOT, 'provisioner', 'provision.sh')
PROVISIONER_DOCKERFILE = os.path.join(REPO_ROOT, 'provisioner', 'Dockerfile')


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


class ProvisionNoRuntimeToolDownloadTest(unittest.TestCase):
    """Finding 7: the privileged provisioner must NOT download tools at runtime.
    helm/kubectl/git/make are baked into the dedicated, checksum-verified
    provisioner image; the Job fails closed if any is missing rather than
    reaching out to the internet under the cluster-privileged provisioner SA.

    The script moved out of this module into the image (#422 item 1), so these
    now assert against provisioner/provision.sh — the file that is COPYed in as
    the entrypoint. Same invariants, new home."""

    def test_script_does_not_download_helm_at_runtime(self):
        script = _read(PROVISION_SH)
        # The old runtime install is gone: no fetch from get.helm.sh, no tarball
        # unpack/install of helm inside the privileged Job.
        self.assertNotIn('get.helm.sh', script)
        self.assertNotIn('helm.tgz', script)
        self.assertNotIn('curl', script)
        # No unfilled template placeholders survived the move into the image.
        self.assertNotIn('__HELM', script)

    def test_script_fails_closed_when_tools_missing(self):
        script = _read(PROVISION_SH)
        # Presence check for every baked-in tool, failing closed (exit 1).
        for tool in ('helm', 'kubectl', 'git', 'make'):
            self.assertIn(tool, script)
        self.assertIn('command -v', script)
        self.assertIn('exit 1', script)
        self.assertIn('refusing to download tools at runtime', script)

    def test_helm_version_has_exactly_one_source_of_truth(self):
        """#422: the controller used to carry PROVISION_HELM_VERSION and string-
        substitute it into the script, mirroring provisioner/Dockerfile's ARG —
        two declarations that could drift, policed by a test that only ever
        checked the controller side. With the script in the image the ARG is the
        sole declaration: it is baked to /etc/provisioner/helm-version at build
        time and read back at run time, so there is nothing left to keep in sync.
        Assert the drift cannot be reintroduced."""
        self.assertFalse(hasattr(controller, 'PROVISION_HELM_VERSION'),
                         'helm version belongs to provisioner/Dockerfile alone now; a copy '
                         'in the controller reintroduces the drift #422 removed')
        self.assertFalse(hasattr(controller, 'PROVISION_HELM_SHA256'),
                         'runtime helm sha256 is obsolete once helm is baked into the image')
        dockerfile = _read(PROVISIONER_DOCKERFILE)
        # Renovate owns the bump; it must stay attached to the ARG it annotates.
        self.assertIn('# renovate: datasource=github-releases depName=helm/helm', dockerfile)
        self.assertRegex(dockerfile, r'ARG HELM_VERSION=v\d+\.\d+\.\d+')
        # Build-time bake + run-time read: the two halves of the single source.
        self.assertIn('/etc/provisioner/helm-version', dockerfile)
        self.assertIn('/etc/provisioner/helm-version', _read(PROVISION_SH))

    def test_script_logs_resolved_commit(self):
        # Provenance inside the Job: the exact commit the privileged deploy runs.
        self.assertIn('rev-parse HEAD', _read(PROVISION_SH))


class ProvisionScriptIsBakedIntoImageTest(unittest.TestCase):
    """#422 item 1 — the provisioning script is the IMAGE's, not the manifest's.

    The Job used to carry `command: [bash, -c, <the whole script>]`, so anything
    that could shape the manifest chose the code running under the cluster-
    privileged provisioner SA. Now the image's ENTRYPOINT is the program and the
    Job passes env only; admission (provisioner-vap.yaml) denies command/args.
    That is what lets #421 treat the Job template as immutable."""

    def test_dockerfile_bakes_the_script_as_entrypoint(self):
        dockerfile = _read(PROVISIONER_DOCKERFILE)
        self.assertIn('COPY --chmod=0755 provisioner/provision.sh /usr/local/bin/provision.sh',
                      dockerfile)
        self.assertIn('ENTRYPOINT ["/usr/local/bin/provision.sh"]', dockerfile)

    def test_script_file_is_executable_and_a_bash_script(self):
        self.assertTrue(os.access(PROVISION_SH, os.X_OK),
                        'provision.sh must be committed executable')
        self.assertTrue(_read(PROVISION_SH).startswith('#!/usr/bin/env bash'))

    def test_controller_no_longer_carries_the_script(self):
        self.assertFalse(hasattr(controller, 'PROVISION_JOB_SCRIPT'),
                         'the script lives in provisioner/provision.sh; a copy here is code '
                         'the Job manifest could inject again')

    def test_job_container_supplies_no_command_or_args(self):
        controller.PROVISIONER_IMAGE = 'example/provisioner@sha256:abc'
        controller.PROVISIONER_SA = 'workspace-provisioner'
        controller.NAMESPACE = 'coder'
        self.addCleanup(setattr, controller, 'CHART_REF', controller.CHART_REF)
        controller.CHART_REF = 'v1.40.1'
        container = controller.build_job_manifest('octo')['spec']['template']['spec']['containers'][0]
        self.assertNotIn('command', container)
        self.assertNotIn('args', container)

    def test_job_still_passes_the_full_env_contract(self):
        """Env is the whole input surface now, so it must stay complete — the
        script fails closed on any empty one."""
        controller.PROVISIONER_IMAGE = 'example/provisioner@sha256:abc'
        controller.PROVISIONER_SA = 'workspace-provisioner'
        controller.NAMESPACE = 'coder'
        self.addCleanup(setattr, controller, 'CHART_REF', controller.CHART_REF)
        self.addCleanup(setattr, controller, 'GITOPS_REPO', controller.GITOPS_REPO)
        self.addCleanup(setattr, controller, 'GITOPS_TOKEN', controller.GITOPS_TOKEN)
        controller.CHART_REF = 'v1.40.1'
        controller.GITOPS_REPO = 'github.com/x/y.git'
        controller.GITOPS_TOKEN = 'tok'
        container = controller.build_job_manifest('octo')['spec']['template']['spec']['containers'][0]
        names = {e['name'] for e in container['env']}
        required = {'SLUG', 'NAMESPACE', 'WS_NAMESPACE', 'CHART_REPO', 'CHART_REF',
                    'GITOPS_REPO', 'GITOPS_BRANCH', 'GITOPS_TOKEN'}
        self.assertEqual(required, names & required)
        # …and the script validates exactly that set, so neither side can drop
        # one silently.
        script = _read(PROVISION_SH)
        for var in required:
            self.assertIn(var, script)

    def test_provisioning_fails_closed_without_a_provisioner_image(self):
        """No fallback to the controller image any more. With the script baked
        in, a Job with no command on the controller image would run ubuntu's
        default shell — exit 0, nothing provisioned. Refuse at build time."""
        self.addCleanup(setattr, controller, 'PROVISIONER_IMAGE',
                        controller.PROVISIONER_IMAGE)
        self.addCleanup(setattr, controller, 'CHART_REF', controller.CHART_REF)
        controller.CHART_REF = 'v1.40.1'
        controller.PROVISIONER_IMAGE = ''
        with self.assertRaises(controller.ProvisionError) as ctx:
            controller.build_job_manifest('octo')
        self.assertIn('provision.image', str(ctx.exception))


class ResourceLimitTest(unittest.TestCase):
    """Validation + strategic-merge patch construction for in-place limit edits."""

    def setUp(self):
        controller.MAX_CPU_LIMIT_CORES = 16.0
        controller.MAX_MEM_LIMIT = '64Gi'
        controller.WORKSPACE_CONTAINER = 'ide'
        controller.WORKSPACE_PREFIX = 'ws-'
        controller.GITOPS_REPO = ''        # persistence off by default in tests
        self._orig_find = controller.find_workspace
        # Pretend the workspace exists so the existence guard passes. Under
        # per-workspace namespaces (#103) it lives in its own ws-octo namespace.
        controller.find_workspace = lambda user: {
            'deployment': f'ws-{user}', 'namespace': f'ws-{user}'}

    def tearDown(self):
        controller.find_workspace = self._orig_find

    def test_validate_cpu_accepts_cores_and_millicores(self):
        self.assertEqual(controller._validate_cpu('2'), '2')
        self.assertEqual(controller._validate_cpu('500m'), '500m')

    def test_validate_cpu_rejects_bad_and_over_cap(self):
        with self.assertRaises(ValueError):
            controller._validate_cpu('2x')
        with self.assertRaises(ValueError):
            controller._validate_cpu('99')        # over 16-core cap

    def test_validate_mem_accepts_units_rejects_over_cap(self):
        self.assertEqual(controller._validate_mem('4Gi'), '4Gi')
        self.assertEqual(controller._validate_mem('512Mi'), '512Mi')
        with self.assertRaises(ValueError):
            controller._validate_mem('4 gigs')
        with self.assertRaises(ValueError):
            controller._validate_mem('128Gi')     # over 64Gi cap

    def test_set_resources_builds_strategic_patch_for_ide(self):
        captured = {}
        controller._kubectl_run = lambda args, namespace=None: captured.update(args=args, namespace=namespace)
        result = controller.set_workspace_resources('octo', '2', '4Gi')
        self.assertEqual(result['limits'], {'cpu': '2', 'memory': '4Gi'})
        self.assertFalse(result['persisted'])        # GITOPS_REPO unset → no write-back
        self.assertIsNone(result['persistError'])
        args = captured['args']
        self.assertEqual(args[0], 'patch')
        self.assertEqual(args[1], 'deployment/ws-octo')
        # Patch must target the workspace's own namespace (#103), not the controller's.
        self.assertEqual(captured['namespace'], 'ws-octo')
        self.assertIn('--type=strategic', args)
        patch = json.loads(args[args.index('-p') + 1])
        container = patch['spec']['template']['spec']['containers'][0]
        self.assertEqual(container['name'], 'ide')
        self.assertEqual(container['resources']['limits'], {'cpu': '2', 'memory': '4Gi'})
        # requests must not be touched by the patch.
        self.assertNotIn('requests', container['resources'])

    def test_set_resources_requires_at_least_one(self):
        controller._kubectl_run = lambda args, namespace=None: None
        with self.assertRaises(ValueError):
            controller.set_workspace_resources('octo', None, None)

    def test_set_resources_unknown_workspace_raises_lookup(self):
        def _absent(user):
            raise LookupError(user)
        controller.find_workspace = _absent
        with self.assertRaises(LookupError):
            controller.set_workspace_resources('octo', '2', None)

    def test_set_resources_persists_to_gitops_when_configured(self):
        controller._kubectl_run = lambda args, namespace=None: None
        controller.GITOPS_REPO = 'github.com/x/y.git'
        controller.GITOPS_TOKEN = 'tok'
        self.addCleanup(setattr, controller, 'GITOPS_TOKEN', controller.GITOPS_TOKEN)
        self.addCleanup(setattr, controller, 'gitops_update_resources',
                        controller.gitops_update_resources)
        seen = {}
        controller.gitops_update_resources = lambda slug, limits: seen.update(slug=slug, limits=limits) or True
        result = controller.set_workspace_resources('octo', '4', '8Gi')
        self.assertEqual(seen, {'slug': 'octo', 'limits': {'cpu': '4', 'memory': '8Gi'}})
        self.assertTrue(result['persisted'])

    _RES_YAML = (
        'resources:\n'
        '  requests:\n'
        '    cpu: "250m"\n'
        '    memory: 1Gi\n'
        '  limits:\n'
        '    cpu: "2"\n'
        '    memory: 4Gi\n'
        '\nbuild:\n  mode: buildkit\n'
    )

    def test_swap_resource_limits_edits_only_limits(self):
        out, changed = controller._swap_resource_limits(self._RES_YAML, {'cpu': '4', 'memory': '8Gi'})
        self.assertTrue(changed)
        # limits updated …
        self.assertIn('  limits:\n    cpu: "4"\n    memory: "8Gi"\n', out)
        # … requests left exactly as-is.
        self.assertIn('  requests:\n    cpu: "250m"\n    memory: 1Gi\n', out)

    def test_swap_resource_limits_partial_and_noop(self):
        out, changed = controller._swap_resource_limits(self._RES_YAML, {'cpu': '8'})
        self.assertTrue(changed)
        self.assertIn('  limits:\n    cpu: "8"\n    memory: 4Gi\n', out)  # memory untouched
        # No limits block → no change.
        out2, changed2 = controller._swap_resource_limits('user:\n  name: octo\n', {'cpu': '8'})
        self.assertFalse(changed2)
        self.assertEqual(out2, 'user:\n  name: octo\n')


class VersionParsingTest(unittest.TestCase):
    def test_parse_version(self):
        self.assertEqual(controller.parse_version('v1.4.0'), (1, 4, 0))
        self.assertEqual(controller.parse_version('1.4.0'), (1, 4, 0))
        self.assertEqual(controller.parse_version(' v2.10.3 '), (2, 10, 3))
        self.assertIsNone(controller.parse_version('latest'))
        self.assertIsNone(controller.parse_version('v1.4'))
        self.assertIsNone(controller.parse_version(None))

    def test_version_from_image(self):
        tag, ver = controller.version_from_image(
            'registry.digitalocean.com/resourceloop/coder:devlaptop-v1.4.0')
        self.assertEqual(tag, 'devlaptop-v1.4.0')
        self.assertEqual(ver, 'v1.4.0')
        # Non-semver tag => tag returned, version None.
        tag, ver = controller.version_from_image('repo/coder:latest')
        self.assertEqual(tag, 'latest')
        self.assertIsNone(ver)
        # No tag / empty.
        self.assertEqual(controller.version_from_image('repo/coder'), (None, None))
        self.assertEqual(controller.version_from_image(''), (None, None))

    def test_update_available(self):
        self.assertTrue(controller.update_available('v1.3.0', 'v1.4.0'))
        self.assertTrue(controller.update_available('1.3.9', '1.4.0'))
        self.assertFalse(controller.update_available('v1.4.0', 'v1.4.0'))
        self.assertFalse(controller.update_available('v1.5.0', 'v1.4.0'))
        # Unknown either side => not offered.
        self.assertFalse(controller.update_available(None, 'v1.4.0'))
        self.assertFalse(controller.update_available('v1.4.0', None))


class LatestVersionCacheTest(unittest.TestCase):
    def setUp(self):
        self._orig_api = controller._github_api
        controller._latest_cache = {'ts': 0.0, 'version': None}
        controller.RELEASE_CHECK_TTL = 600

    def tearDown(self):
        controller._github_api = self._orig_api

    def test_fetches_then_caches(self):
        calls = []
        controller._github_api = lambda m, p, token=None: (calls.append(p) or {'tag_name': 'v1.4.0'})
        self.assertEqual(controller.latest_version(), 'v1.4.0')
        self.assertEqual(controller.latest_version(), 'v1.4.0')
        self.assertEqual(len(calls), 1)  # second call served from cache
        self.assertIn('/repos/', calls[0])

    def test_api_failure_returns_cached_or_none(self):
        def boom(m, p, token=None):
            raise controller.GithubError('down', 503)
        controller._github_api = boom
        self.assertIsNone(controller.latest_version())  # no prior cache
        # Now seed a cached value, expire it, and confirm failure keeps it.
        controller._latest_cache = {'ts': 0.0, 'version': 'v1.3.0'}
        controller.RELEASE_CHECK_TTL = 0
        self.assertEqual(controller.latest_version(), 'v1.3.0')

    def test_non_semver_tag_ignored(self):
        controller._github_api = lambda m, p, token=None: {'tag_name': 'nightly'}
        self.assertIsNone(controller.latest_version())


class DecorateUpdatesTest(unittest.TestCase):
    def setUp(self):
        self._orig = controller.latest_version
        controller.latest_version = lambda: 'v1.4.0'

    def tearDown(self):
        controller.latest_version = self._orig

    def test_adds_latest_and_flags(self):
        resp = {'workspaces': [
            {'user': 'a', 'version': 'v1.3.0'},
            {'user': 'b', 'version': 'v1.4.0'},
            {'user': 'c', 'version': None},
        ]}
        out = controller.decorate_with_updates(resp)
        self.assertEqual(out['latestVersion'], 'v1.4.0')
        flags = {w['user']: w['updateAvailable'] for w in out['workspaces']}
        self.assertEqual(flags, {'a': True, 'b': False, 'c': False})


class SwapImageTagTest(unittest.TestCase):
    def test_swaps_only_devlaptop_tag_line(self):
        content = (
            'image:\n'
            '  repository: registry/coder\n'
            '  tag: devlaptop-v1.3.0\n'
            '  pullPolicy: Always\n'
            'somethingElse:\n'
            '  tag: keep-me\n'  # unrelated tag: must be left alone
        )
        new, changed = controller._swap_image_tag(content, 'devlaptop-v1.4.0')
        self.assertTrue(changed)
        self.assertIn('  tag: devlaptop-v1.4.0\n', new)
        self.assertIn('  tag: keep-me\n', new)  # untouched
        self.assertNotIn('devlaptop-v1.3.0', new)

    def test_no_change_when_already_current(self):
        content = 'image:\n  tag: devlaptop-v1.4.0\n'
        new, changed = controller._swap_image_tag(content, 'devlaptop-v1.4.0')
        self.assertFalse(changed)
        self.assertEqual(new, content)

    def test_no_devlaptop_tag_present(self):
        content = 'image:\n  tag: latest\n'
        _, changed = controller._swap_image_tag(content, 'devlaptop-v1.4.0')
        self.assertFalse(changed)


class SetWorkspaceImageTest(unittest.TestCase):
    def setUp(self):
        controller.WORKSPACE_CONTAINER = 'ide'
        controller.WORKSPACE_PREFIX = 'ws-'
        controller.IMAGE_TAG_PREFIX = 'devlaptop-'
        self._orig_find = controller.find_workspace
        self._orig_run = controller._kubectl_run
        self._orig_latest = controller.latest_version
        self._orig_gitops_repo = controller.GITOPS_REPO
        self._orig_gitops_token = controller.GITOPS_TOKEN
        controller.GITOPS_REPO = ''       # persistence off by default in tests
        controller.GITOPS_TOKEN = ''
        controller.latest_version = lambda: 'v1.4.0'
        controller.find_workspace = lambda user: {
            'deployment': f'ws-{user}', 'namespace': f'ws-{user}', 'version': 'v1.3.0',
            'image': 'registry/coder:devlaptop-v1.3.0', 'imageTag': 'devlaptop-v1.3.0'}

    def tearDown(self):
        controller.find_workspace = self._orig_find
        controller._kubectl_run = self._orig_run
        controller.latest_version = self._orig_latest
        controller.GITOPS_REPO = self._orig_gitops_repo
        controller.GITOPS_TOKEN = self._orig_gitops_token

    def test_patches_image_to_latest(self):
        captured = {}
        controller._kubectl_run = lambda args, namespace=None: captured.update(args=args, namespace=namespace)
        result = controller.set_workspace_image('octo')
        args = captured['args']
        self.assertEqual(args[0], 'patch')
        self.assertEqual(args[1], 'deployment/ws-octo')
        # Patch targets the workspace's own namespace (#103).
        self.assertEqual(captured['namespace'], 'ws-octo')
        self.assertIn('--type=strategic', args)
        patch = json.loads(args[args.index('-p') + 1])
        container = patch['spec']['template']['spec']['containers'][0]
        self.assertEqual(container['name'], 'ide')
        self.assertEqual(container['image'], 'registry/coder:devlaptop-v1.4.0')
        self.assertEqual(result['fromVersion'], 'v1.3.0')
        self.assertEqual(result['toVersion'], 'v1.4.0')
        self.assertTrue(result['rolled'])
        self.assertFalse(result['persisted'])

    def test_explicit_version_overrides_latest(self):
        captured = {}
        controller._kubectl_run = lambda args, namespace=None: captured.setdefault('args', args)
        controller.set_workspace_image('octo', 'v1.3.5')
        patch = json.loads(captured['args'][captured['args'].index('-p') + 1])
        self.assertEqual(patch['spec']['template']['spec']['containers'][0]['image'],
                         'registry/coder:devlaptop-v1.3.5')

    def test_noop_when_already_on_target(self):
        ran = {'called': False}
        controller._kubectl_run = lambda args, namespace=None: ran.update(called=True)
        result = controller.set_workspace_image('octo', 'v1.3.0')  # already on v1.3.0
        self.assertFalse(ran['called'])   # no patch issued
        self.assertFalse(result['rolled'])

    def test_unknown_target_raises(self):
        controller.latest_version = lambda: None
        with self.assertRaises(ValueError):
            controller.set_workspace_image('octo')          # no version anywhere

    def test_unknown_workspace_raises_lookup(self):
        def _absent(user):
            raise LookupError(user)
        controller.find_workspace = _absent
        with self.assertRaises(LookupError):
            controller.set_workspace_image('octo', 'v1.4.0')

    def test_persist_path_invoked_when_gitops_configured(self):
        controller._kubectl_run = lambda args, namespace=None: None
        controller.GITOPS_REPO = 'github.com/o/r.git'
        controller.GITOPS_TOKEN = 'tok'
        seen = {}
        self.addCleanup(setattr, controller, 'gitops_update_image_tag',
                        controller.gitops_update_image_tag)
        controller.gitops_update_image_tag = lambda slug, tag: seen.update(slug=slug, tag=tag) or True
        result = controller.set_workspace_image('octo')
        self.assertEqual(seen, {'slug': 'octo', 'tag': 'devlaptop-v1.4.0'})
        self.assertTrue(result['persisted'])

    def _enable_provisioning(self):
        """Turn on the wiring provisioning_enabled() checks + stub the Job launch
        so no real kubectl is spawned. Returns the dict the stub records into."""
        controller.GITOPS_REPO = 'github.com/o/r.git'
        controller.GITOPS_TOKEN = 'tok'
        self.addCleanup(setattr, controller, 'WORKSPACE_DOMAIN', controller.WORKSPACE_DOMAIN)
        controller.WORKSPACE_DOMAIN = 'dev.example.com'
        self.addCleanup(setattr, controller, 'gitops_update_image_tag',
                        controller.gitops_update_image_tag)
        controller.gitops_update_image_tag = lambda slug, tag: True
        launched = {}
        self.addCleanup(setattr, controller, 'create_provision_job',
                        controller.create_provision_job)
        controller.create_provision_job = lambda slug: launched.update(slug=slug) or 'job/x'
        return launched

    def test_config_reconcile_job_launched_on_update(self):
        # A real update refreshes the ConfigMaps (server.py) via a helm-upgrade
        # Job, not just the image tag.
        controller._kubectl_run = lambda args, namespace=None: None
        launched = self._enable_provisioning()
        result = controller.set_workspace_image('octo')
        self.assertEqual(launched, {'slug': 'octo'})
        self.assertEqual(result['reconcile'], 'launched')

    def test_config_reconcile_runs_even_when_already_on_target(self):
        # The umi bug: image already latest but the ConfigMap (server.py) stale —
        # "update" must still reconcile config, even with no image roll.
        controller._kubectl_run = lambda args, namespace=None: None
        launched = self._enable_provisioning()
        result = controller.set_workspace_image('octo', 'v1.3.0')  # already on v1.3.0
        self.assertFalse(result['rolled'])            # no image patch
        self.assertEqual(launched, {'slug': 'octo'})  # but config still reconciled
        self.assertEqual(result['reconcile'], 'launched')

    def test_no_reconcile_job_when_provisioning_disabled(self):
        # No GitOps/domain wiring → legacy image-tag-only update, no Job.
        controller._kubectl_run = lambda args, namespace=None: None
        result = controller.set_workspace_image('octo')  # GITOPS off (setUp default)
        self.assertIsNone(result['reconcile'])


class RestartWorkspaceTest(unittest.TestCase):
    """#352 — a plain restart rolls the pod on its current image, with no
    release version required and no GitOps/provisioning side effects."""

    def setUp(self):
        controller.WORKSPACE_PREFIX = 'ws-'
        self._orig_find = controller.find_workspace
        self._orig_run = controller._kubectl_run
        controller.find_workspace = lambda user: {
            'deployment': f'ws-{user}', 'namespace': f'ws-{user}',
            'version': 'v1.3.0', 'desiredReplicas': 1}

    def tearDown(self):
        controller.find_workspace = self._orig_find
        controller._kubectl_run = self._orig_run

    def test_rollout_restarts_in_own_namespace(self):
        captured = {}
        controller._kubectl_run = lambda args, namespace=None: captured.update(
            args=args, namespace=namespace)
        result = controller.restart_workspace('octo')
        self.assertEqual(captured['args'],
                         ['rollout', 'restart', 'deployment/ws-octo'])
        self.assertEqual(captured['namespace'], 'ws-octo')
        self.assertTrue(result['rolled'])
        self.assertEqual(result['user'], 'octo')
        self.assertEqual(result['version'], 'v1.3.0')

    def test_no_release_version_needed(self):
        # Unlike set_workspace_image, restart must work when no release is
        # resolvable at all (the exact gap that motivated #352).
        self.addCleanup(setattr, controller, 'latest_version', controller.latest_version)
        controller.latest_version = lambda: None
        controller._kubectl_run = lambda args, namespace=None: None
        self.assertTrue(controller.restart_workspace('octo')['rolled'])

    def test_stopped_workspace_refused(self):
        controller.find_workspace = lambda user: {
            'deployment': f'ws-{user}', 'namespace': f'ws-{user}',
            'version': 'v1.3.0', 'desiredReplicas': 0}
        controller._kubectl_run = lambda args, namespace=None: self.fail(
            'must not touch a stopped workspace')
        with self.assertRaises(ValueError):
            controller.restart_workspace('octo')

    def test_invalid_name_raises(self):
        with self.assertRaises(ValueError):
            controller.restart_workspace('Bad User!')

    def test_unknown_workspace_raises_lookup(self):
        def _absent(user):
            raise LookupError(user)
        controller.find_workspace = _absent
        with self.assertRaises(LookupError):
            controller.restart_workspace('octo')


class RestrictedListenerTest(unittest.TestCase):
    """The self-serve listener must 404 every admin/header-trusting route so a
    workspace pod that can reach it can never drive the admin API."""

    class _Stub:
        def __init__(self, restricted):
            self.server = types.SimpleNamespace(restricted=restricted)
            self.sent = None

        def send_json(self, body, code):
            self.sent = (code, body)

    def block(self, restricted, path, allowed_re):
        stub = self._Stub(restricted)
        handled = controller.Handler._restricted_block(stub, path, allowed_re)
        return handled, stub.sent

    def test_unrestricted_never_blocks(self):
        handled, sent = self.block(False, '/api/workspaces', controller._SELF_SERVE_GET_RE)
        self.assertFalse(handled)
        self.assertIsNone(sent)

    def test_restricted_blocks_admin_routes(self):
        for path in ('/api/workspaces', '/api/workspaces/octo/stop',
                     '/api/insights', '/'):
            handled, sent = self.block(True, path, controller._SELF_SERVE_GET_RE)
            self.assertTrue(handled, f'{path} should be blocked')
            self.assertEqual(sent[0], 404)

    def test_restricted_allows_self_serve_and_health(self):
        for path in ('/api/self/workspaces/octo/version', '/health'):
            handled, _ = self.block(True, path, controller._SELF_SERVE_GET_RE)
            self.assertFalse(handled, f'{path} should pass through')

    def test_self_serve_route_regexes(self):
        self.assertTrue(controller._SELF_SERVE_GET_RE.match('/api/self/workspaces/octo/version'))
        self.assertTrue(controller._SELF_SERVE_POST_RE.match('/api/self/workspaces/octo-1/update'))
        self.assertTrue(controller._SELF_SERVE_POST_RE.match('/api/self/workspaces/octo/restart'))
        self.assertIsNone(controller._SELF_SERVE_GET_RE.match('/api/workspaces/octo/version'))
        self.assertIsNone(controller._SELF_SERVE_POST_RE.match('/api/self/workspaces/octo/stop'))
        self.assertIsNone(controller._SELF_SERVE_POST_RE.match('/api/workspaces/octo/restart'))


class PerWorkspaceNamespaceTest(unittest.TestCase):
    """#103 — the controller must discover + address workspaces across their
    own per-user namespaces, not one shared namespace. kubectl is faked."""

    def setUp(self):
        controller.NAMESPACE = 'coder'
        controller.WORKSPACE_PREFIX = 'ws-'
        self._orig_json = controller._kubectl_json

    def tearDown(self):
        controller._kubectl_json = self._orig_json

    def test_ns_for_user_matches_workspace_name(self):
        self.assertEqual(controller.ns_for_user('octo'), 'ws-octo')

    def test_prom_ns_selector_spans_workspace_and_control_plane(self):
        sel = controller._ws_prom_ns_selector()
        self.assertIn('ws', sel)           # per-user namespaces (ws-<user>)
        self.assertIn('coder', sel)        # + not-yet-migrated fallback
        self.assertTrue(sel.startswith('namespace=~'))
        # Regression: the hyphen must NOT be backslash-escaped. Prometheus/RE2
        # rejects `\-` outside a character class with an HTTP 400, which broke
        # the whole cluster-capacity panel. re.escape() produced `ws\-`; the
        # selector must embed a bare `ws-`.
        self.assertIn('ws-', sel)
        self.assertNotIn('\\-', sel)

    def test_re2_literal_leaves_hyphen_but_escapes_metachars(self):
        # Hyphen stays literal (RE2-safe outside a char class); real
        # metacharacters are escaped so a crafted prefix can't break the query.
        self.assertEqual(controller._re2_literal('ws-'), 'ws-')
        self.assertEqual(controller._re2_literal('a.b+c'), 'a\\.b\\+c')

    def test_discover_namespaces_filters_to_ws_and_includes_own(self):
        controller._kubectl_json = lambda args, namespace=None: {'items': [
            {'metadata': {'name': 'ws-alice'}},
            {'metadata': {'name': 'ws-bob'}},
            {'metadata': {'name': 'kube-system'}},   # not a workspace
            {'metadata': {'name': 'ingress-nginx'}},
        ]} if args == ['get', 'namespaces'] else {'items': []}
        found = controller.discover_workspace_namespaces()
        self.assertIn('ws-alice', found)
        self.assertIn('ws-bob', found)
        self.assertIn('coder', found)      # the controller's own namespace, always
        self.assertNotIn('kube-system', found)

    def test_discover_namespaces_degrades_to_own_when_list_denied(self):
        def denied(args, namespace=None):
            raise controller.KubectlError('forbidden')
        controller._kubectl_json = denied
        self.assertEqual(controller.discover_workspace_namespaces(), ['coder'])

    def test_list_workspaces_reports_each_workspaces_own_namespace(self):
        # One deployment per tenant namespace; the payload must carry that ns so
        # start/stop/patch target the right place.
        def fake(args, namespace=None):
            if args == ['get', 'namespaces']:
                return {'items': [{'metadata': {'name': 'ws-alice'}},
                                  {'metadata': {'name': 'ws-bob'}}]}
            if args == ['get', 'deployments']:
                if namespace == 'ws-alice':
                    return {'items': [_dep('ws-alice')]}
                if namespace == 'ws-bob':
                    return {'items': [_dep('ws-bob')]}
            return {'items': []}
        controller._kubectl_json = fake
        out = controller.list_workspaces()
        by_user = {w['user']: w for w in out['workspaces']}
        self.assertEqual(set(by_user), {'alice', 'bob'})
        self.assertEqual(by_user['alice']['namespace'], 'ws-alice')
        self.assertEqual(by_user['bob']['namespace'], 'ws-bob')

    def test_find_workspace_reads_only_its_own_namespace(self):
        # Targeted O(1) lookup: reads ws-<user> directly, never the
        # all-namespaces fan-out (no 'get namespaces', no foreign-tenant reads).
        # Regression: workspace_version_info() used list_workspaces() and grew to
        # ~12s across the fleet, past the workspace's controller-call timeout, so
        # the self-serve update option silently vanished from Settings.
        seen = []
        def fake(args, namespace=None):
            seen.append((list(args), namespace))
            if args[:2] == ['get', 'deployments'] and namespace == 'ws-alice':
                return {'items': [_dep('ws-alice')]}
            return {'items': []}
        controller._kubectl_json = fake
        ws = controller.find_workspace('alice')
        self.assertEqual(ws['deployment'], 'ws-alice')
        self.assertEqual(ws['namespace'], 'ws-alice')
        self.assertEqual(ws['version'], 'v1.0.0')
        self.assertNotIn(['get', 'namespaces'], [a for a, _ in seen])   # never enumerated
        self.assertTrue(all(ns in ('ws-alice', 'coder') for _, ns in seen))

    def test_find_workspace_absent_raises_lookup(self):
        controller._kubectl_json = lambda args, namespace=None: {'items': []}
        with self.assertRaises(LookupError):
            controller.find_workspace('ghost')

    def test_list_workspaces_reads_no_pods_or_ingress(self):
        # The fleet listing must be deployments-only — no per-namespace pod or
        # ingress fan-out (that was the OOM + latency source). Regression guard.
        resources = []
        def fake(args, namespace=None):
            if args == ['get', 'namespaces']:
                return {'items': [{'metadata': {'name': 'ws-alice'}}]}
            if len(args) >= 2 and args[0] == 'get':
                resources.append(args[1])
            if args == ['get', 'deployments']:
                return {'items': [_dep('ws-alice')]}
            return {'items': []}
        controller._kubectl_json = fake
        controller.list_workspaces()
        self.assertIn('deployments', resources)
        self.assertNotIn('pods', resources)
        self.assertNotIn('ingress', resources)

    def test_ws_item_derives_url_from_domain_not_ingress(self):
        orig = controller.WORKSPACE_DOMAIN
        controller.WORKSPACE_DOMAIN = 'dev.example.io'
        try:
            item = controller._ws_item(_dep('ws-alice'))
        finally:
            controller.WORKSPACE_DOMAIN = orig
        self.assertEqual(item['url'], 'https://alice.dev.example.io/')
        self.assertEqual(item['pods'], [])           # no per-pod detail in the list
        self.assertEqual(item['state'], 'running')   # readyReplicas==desired in _dep

    def test_classify_degraded_from_progress_deadline_condition(self):
        # Pods-free degraded signal: a wedged rollout surfaces as a Deployment
        # Progressing=ProgressDeadlineExceeded condition.
        wedged = [{'type': 'Progressing', 'reason': 'ProgressDeadlineExceeded'}]
        self.assertEqual(controller._classify(1, 0, 2, 2, wedged), 'degraded')
        self.assertEqual(controller._classify(1, 0, 2, 2, []), 'transitioning')  # still starting
        self.assertEqual(controller._classify(0, 0, 2, 2, []), 'stopped')
        self.assertEqual(controller._classify(1, 1, 2, 2, []), 'running')

    def test_collect_aggregates_items_across_namespaces(self):
        # _collect fans out one read per workspace namespace (now concurrently)
        # and concatenates the items, each keeping its own namespace.
        def fake(args, namespace=None):
            if args == ['get', 'namespaces']:
                return {'items': [{'metadata': {'name': 'ws-alice'}},
                                  {'metadata': {'name': 'ws-bob'}}]}
            if args == ['get', 'pods']:
                return {'items': [{'metadata': {'name': f'p-{namespace}',
                                                'namespace': namespace}}]}
            return {'items': []}
        controller._kubectl_json = fake
        got = {i['metadata']['namespace'] for i in controller._collect('pods')}
        # coder (own ns) + the two ws-* namespaces
        self.assertEqual(got, {'coder', 'ws-alice', 'ws-bob'})

    def test_collect_skips_a_failing_namespace(self):
        # One unreadable tenant namespace must not blank the whole listing.
        def fake(args, namespace=None):
            if args == ['get', 'namespaces']:
                return {'items': [{'metadata': {'name': 'ws-alice'}},
                                  {'metadata': {'name': 'ws-bob'}}]}
            if args == ['get', 'pods']:
                if namespace == 'ws-bob':
                    raise controller.KubectlError('forbidden')
                return {'items': [{'metadata': {'name': f'p-{namespace}',
                                                'namespace': namespace}}]}
            return {'items': []}
        controller._kubectl_json = fake
        got = {i['metadata']['namespace'] for i in controller._collect('pods')}
        self.assertNotIn('ws-bob', got)          # the failing ns is dropped
        self.assertIn('ws-alice', got)           # the others still come through
        self.assertIn('coder', got)

    def test_workspace_exists_checks_the_per_user_namespace(self):
        seen = {}
        def fake(args, namespace=None):
            seen.setdefault('ns', []).append(namespace)
            if namespace == 'ws-octo':
                return {'metadata': {'name': 'ws-octo'}}
            raise controller.KubectlError('not found')
        controller._kubectl_json = fake
        self.assertTrue(controller.workspace_exists('octo'))
        self.assertIn('ws-octo', seen['ns'])


def _dep(name):
    """Minimal workspace Deployment object as kubectl -o json would return it."""
    return {
        'metadata': {'name': name, 'namespace': name, 'generation': 1},
        'spec': {'replicas': 1, 'template': {'spec': {'containers': [
            {'name': 'ide', 'image': 'registry/coder:devlaptop-v1.0.0'}]}}},
        'status': {'readyReplicas': 1, 'observedGeneration': 1},
    }


class SelfServeTokenBindingTest(unittest.TestCase):
    """Finding 2 (July 2026 security review): the self-serve token must be
    bound to one workspace. A token read out of Alice's workspace pod may
    drive /api/self/workspaces/alice/* only — presenting it while naming bob
    in the path must be rejected. The legacy shared master is accepted only
    while SELF_SERVE_ALLOW_SHARED_TOKEN is on (migration mode)."""

    MASTER = 'master-secret-for-tests'

    class _Req:
        def __init__(self, token, path='/api/self/workspaces/alice/update'):
            self.headers = {} if token is None else {'X-KC-Service-Token': token}
            self.path = path

    def setUp(self):
        self._saved = (controller.SELF_SERVE_TOKEN,
                       controller.SELF_SERVE_ALLOW_SHARED_TOKEN)
        controller.SELF_SERVE_TOKEN = self.MASTER
        controller.SELF_SERVE_ALLOW_SHARED_TOKEN = True

    def tearDown(self):
        (controller.SELF_SERVE_TOKEN,
         controller.SELF_SERVE_ALLOW_SHARED_TOKEN) = self._saved

    def check(self, token, user):
        import contextlib, io
        with contextlib.redirect_stderr(io.StringIO()):
            return controller.Handler.check_service_token(self._Req(token), user)

    def test_derivation_is_stable_hex_and_per_user(self):
        alice = controller.self_serve_token_for('alice')
        self.assertEqual(alice, controller.self_serve_token_for('alice'))
        self.assertEqual(len(alice), 64)
        int(alice, 16)  # raises if not hex
        self.assertNotEqual(alice, controller.self_serve_token_for('bob'))
        self.assertNotEqual(alice, self.MASTER)

    def test_derived_token_authorizes_its_own_workspace(self):
        self.assertTrue(self.check(controller.self_serve_token_for('alice'), 'alice'))
        # Strict mode changes nothing for correctly-bound tokens.
        controller.SELF_SERVE_ALLOW_SHARED_TOKEN = False
        self.assertTrue(self.check(controller.self_serve_token_for('alice'), 'alice'))

    def test_derived_token_cannot_name_another_workspace(self):
        # The core of finding 2 — Alice's credential naming bob must fail,
        # legacy migration mode or not.
        alice_tok = controller.self_serve_token_for('alice')
        self.assertFalse(self.check(alice_tok, 'bob'))
        controller.SELF_SERVE_ALLOW_SHARED_TOKEN = False
        self.assertFalse(self.check(alice_tok, 'bob'))

    def test_shared_master_only_accepted_in_migration_mode(self):
        self.assertTrue(self.check(self.MASTER, 'alice'))
        controller.SELF_SERVE_ALLOW_SHARED_TOKEN = False
        self.assertFalse(self.check(self.MASTER, 'alice'))

    def test_missing_empty_or_wrong_token_rejected(self):
        self.assertFalse(self.check(None, 'alice'))
        self.assertFalse(self.check('', 'alice'))
        self.assertFalse(self.check('not-a-token', 'alice'))

    def test_disabled_when_master_unset(self):
        derived = controller.self_serve_token_for('alice')
        controller.SELF_SERVE_TOKEN = ''
        self.assertFalse(self.check(derived, 'alice'))
        self.assertFalse(self.check('', 'alice'))

    def test_no_user_rejected(self):
        self.assertFalse(self.check(self.MASTER, ''))
        self.assertFalse(self.check(self.MASTER, None))


NOW = 1_000_000.0


def _ws(**over):
    """An opted-in, running workspace that IS eligible to pause. Each test
    breaks exactly one thing, so a failure names its own cause."""
    ws = {
        'user': 'octo', 'deployment': 'ws-octo', 'namespace': 'ws-octo',
        'state': 'running', 'desiredReplicas': 1,
        'autoPause': {'enabled': True, 'idleMinutes': 120, 'autoPausedAt': None},
    }
    ws.update(over)
    return ws


def _beacon(busy='false', last=NOW - 7200, at=NOW - 10):
    b = {}
    if busy is not None:
        b[controller.ANN_BUSY] = busy
    if last is not None:
        b[controller.ANN_LAST_ACTIVITY] = str(last)
    if at is not None:
        b[controller.ANN_BEACON_AT] = str(at)
    return b


class ShouldPauseTest(unittest.TestCase):
    """The auto-pause decision (#612).

    This is the highest-stakes pure function in the controller: a wrong `True`
    scales a workspace to 0 while an agent is mid-run and destroys that work.
    Every case below that is not the happy path must come back False.
    """

    def test_pauses_an_idle_opted_in_workspace(self):
        ok, why = controller.should_pause(_ws(), _beacon(), NOW)
        self.assertTrue(ok, why)

    def test_never_pauses_a_workspace_that_did_not_opt_in(self):
        ws = _ws(autoPause={'enabled': False, 'idleMinutes': 120})
        ok, why = controller.should_pause(ws, _beacon(), NOW)
        self.assertFalse(ok)
        self.assertIn('not opted in', why)

    def test_never_pauses_a_busy_workspace(self):
        ok, why = controller.should_pause(_ws(), _beacon(busy='true'), NOW)
        self.assertFalse(ok)
        self.assertIn('busy', why)

    def test_unrecognised_busy_value_counts_as_busy(self):
        # Fail closed: only the literal "false" is permission to pause.
        for value in ('TRUE', 'maybe', '', 'yes', '1'):
            ok, _ = controller.should_pause(_ws(), _beacon(busy=value), NOW)
            self.assertFalse(ok, f'busy={value!r} must not pause')

    def test_busy_is_case_insensitive_for_false(self):
        ok, why = controller.should_pause(_ws(), _beacon(busy='False'), NOW)
        self.assertTrue(ok, why)

    def test_never_pauses_before_the_threshold(self):
        ok, why = controller.should_pause(_ws(), _beacon(last=NOW - 60), NOW)
        self.assertFalse(ok)
        self.assertIn('threshold', why)

    def test_threshold_comes_from_the_workspace_not_the_default(self):
        ws = _ws(autoPause={'enabled': True, 'idleMinutes': 5})
        ok, why = controller.should_pause(ws, _beacon(last=NOW - 600), NOW)
        self.assertTrue(ok, why)          # 10m idle, 5m threshold

    def test_never_pauses_on_a_stale_beacon(self):
        # A dead beacon thread must not read as "quiet".
        ok, why = controller.should_pause(
            _ws(), _beacon(at=NOW - 9999), NOW, beacon_max_age=300)
        self.assertFalse(ok)
        self.assertIn('stale', why)

    def test_never_pauses_without_a_beacon(self):
        # An older workspace image publishes nothing at all.
        for beacon in (None, {}):
            ok, why = controller.should_pause(_ws(), beacon, NOW)
            self.assertFalse(ok)
            self.assertIn('beacon', why)

    def test_never_pauses_on_malformed_annotations(self):
        for bad in ({controller.ANN_BEACON_AT: 'soon',
                     controller.ANN_BUSY: 'false',
                     controller.ANN_LAST_ACTIVITY: '1'},
                    {controller.ANN_BEACON_AT: str(NOW - 10),
                     controller.ANN_BUSY: 'false',
                     controller.ANN_LAST_ACTIVITY: 'ages ago'}):
            ok, why = controller.should_pause(_ws(), bad, NOW)
            self.assertFalse(ok, why)

    def test_never_pauses_an_already_stopped_workspace(self):
        ok, why = controller.should_pause(_ws(desiredReplicas=0), _beacon(), NOW)
        self.assertFalse(ok)
        self.assertIn('already stopped', why)

    def test_never_pauses_a_workspace_that_is_not_settled(self):
        for state in ('transitioning', 'degraded', 'stopped'):
            ok, why = controller.should_pause(_ws(state=state), _beacon(), NOW)
            self.assertFalse(ok, f'{state} must not pause')


class AutoPauseConfigTest(unittest.TestCase):
    """Reading the opt-in off the Deployment's annotations."""

    def _dep(self, ann):
        return {'metadata': {'name': 'ws-octo', 'annotations': ann}}

    def test_absent_annotations_mean_disabled(self):
        cfg = controller.auto_pause_config({'metadata': {'name': 'ws-octo'}})
        self.assertFalse(cfg['enabled'])

    def test_reads_enabled_and_threshold(self):
        cfg = controller.auto_pause_config(self._dep({
            controller.ANN_AUTO_PAUSE: 'true',
            controller.ANN_AUTO_PAUSE_IDLE: '45'}))
        self.assertTrue(cfg['enabled'])
        self.assertEqual(cfg['idleMinutes'], 45)

    def test_bad_threshold_falls_back_to_the_default(self):
        for bad in ('nonsense', '0', '-5'):
            cfg = controller.auto_pause_config(self._dep({
                controller.ANN_AUTO_PAUSE: 'true',
                controller.ANN_AUTO_PAUSE_IDLE: bad}))
            self.assertEqual(cfg['idleMinutes'],
                             controller.AUTOPAUSE_DEFAULT_IDLE_MINUTES)


class AutoPauseValuesEditTest(unittest.TestCase):
    """values.yaml write-back, so a toggle survives the next reconcile."""

    BLOCK = 'autoPause:\n  enabled: false\n  idleMinutes: 120\n'

    def test_flips_an_existing_block(self):
        out, changed = controller._swap_auto_pause(self.BLOCK, True, 30)
        self.assertTrue(changed)
        self.assertIn('enabled: true', out)
        self.assertIn('idleMinutes: 30', out)

    def test_appends_the_block_when_missing(self):
        # Every workspace provisioned before #612 has no autoPause block; the
        # toggle has to persist for those too or the next reconcile undoes it.
        out, changed = controller._swap_auto_pause('image:\n  tag: v1\n', True, 60)
        self.assertTrue(changed)
        self.assertIn('autoPause:', out)
        self.assertIn('enabled: true', out)
        self.assertIn('idleMinutes: 60', out)

    def test_no_change_is_reported_as_no_change(self):
        # _gitops_edit_values skips the commit entirely when nothing changed.
        _, changed = controller._swap_auto_pause(self.BLOCK, False, 120)
        self.assertFalse(changed)

    def test_only_touches_its_own_block(self):
        content = ('resources:\n  limits:\n    cpu: "2"\n\n' + self.BLOCK +
                   '\nssh:\n  enabled: true\n')
        out, changed = controller._swap_auto_pause(content, True, 15)
        self.assertTrue(changed)
        self.assertIn('cpu: "2"', out)
        self.assertIn('ssh:\n  enabled: true', out)


class AutoPauseSweepTest(unittest.TestCase):
    """The fleet sweep: what it scales, and what it must never touch."""

    def setUp(self):
        self.calls = []
        self._orig = (controller.list_workspaces, controller._kubectl_run,
                      controller._pod_beacon, controller._cpu_is_idle,
                      controller.find_workspace)
        controller._kubectl_run = lambda args, namespace=None: self.calls.append(
            (list(args), namespace))
        controller.find_workspace = lambda user: {
            'deployment': f'ws-{user}', 'namespace': f'ws-{user}'}
        controller._cpu_is_idle = lambda ns, dep: True
        controller._pod_beacon = lambda ns, dep: _beacon()
        controller.list_workspaces = lambda: {'workspaces': [_ws()]}

    def tearDown(self):
        (controller.list_workspaces, controller._kubectl_run,
         controller._pod_beacon, controller._cpu_is_idle,
         controller.find_workspace) = self._orig

    def test_pauses_an_eligible_workspace(self):
        decisions = controller.autopause_pass(now=NOW)
        self.assertEqual(len(decisions), 1)
        self.assertTrue(decisions[0]['paused'], decisions[0]['reason'])
        verbs = [c[0][0] for c in self.calls]
        self.assertIn('scale', verbs)

    def test_pausing_never_touches_the_pvc(self):
        """Criterion 4 — PVC preservation asserted, not assumed.

        Pausing is `kubectl scale` and an annotation patch. Nothing in this
        path may name a PVC or delete anything: the whole point of pause over
        teardown is that the home volume survives it.
        """
        controller.autopause_pass(now=NOW)
        self.assertTrue(self.calls)
        for args, _ns in self.calls:
            self.assertNotIn('delete', args)
            flat = ' '.join(args).lower()
            self.assertNotIn('pvc', flat)
            self.assertNotIn('persistentvolumeclaim', flat)

    def test_scales_to_zero_not_to_something_else(self):
        controller.autopause_pass(now=NOW)
        scale = next(c[0] for c in self.calls if c[0][0] == 'scale')
        self.assertIn('--replicas=0', scale)

    def test_marks_the_workspace_as_auto_paused(self):
        controller.autopause_pass(now=NOW)
        patch = next(c[0] for c in self.calls if c[0][0] == 'patch')
        body = json.loads(patch[patch.index('-p') + 1])
        self.assertIn(controller.ANN_AUTO_PAUSED_AT,
                      body['metadata']['annotations'])

    def test_skips_workspaces_that_did_not_opt_in(self):
        controller.list_workspaces = lambda: {
            'workspaces': [_ws(autoPause={'enabled': False, 'idleMinutes': 120})]}
        decisions = controller.autopause_pass(now=NOW)
        self.assertEqual(decisions, [])
        self.assertEqual(self.calls, [])

    def test_unmeasurable_cpu_blocks_the_pause(self):
        # Prometheus down => we cannot confirm idleness => leave it running.
        controller._cpu_is_idle = lambda ns, dep: None
        decisions = controller.autopause_pass(now=NOW)
        self.assertFalse(decisions[0]['paused'])
        self.assertEqual(self.calls, [])

    def test_busy_cpu_blocks_the_pause(self):
        # A compile or dev server left running is invisible to the beacon and
        # very visible here.
        controller._cpu_is_idle = lambda ns, dep: False
        decisions = controller.autopause_pass(now=NOW)
        self.assertFalse(decisions[0]['paused'])
        self.assertEqual(self.calls, [])

    def test_one_failing_workspace_does_not_stop_the_sweep(self):
        controller.list_workspaces = lambda: {
            'workspaces': [_ws(user='a', deployment='ws-a', namespace='ws-a'),
                           _ws(user='b', deployment='ws-b', namespace='ws-b')]}
        boom = {'n': 0}

        def flaky(args, namespace=None):
            boom['n'] += 1
            if boom['n'] == 1:
                raise controller.KubectlError('conflict')
            self.calls.append((list(args), namespace))
        controller._kubectl_run = flaky
        decisions = controller.autopause_pass(now=NOW)
        self.assertEqual(len(decisions), 2)
        self.assertTrue(any(d['paused'] for d in decisions))


class SetAutoPauseTest(unittest.TestCase):
    """The per-workspace toggle endpoint's business logic."""

    def setUp(self):
        controller.GITOPS_REPO = ''        # no write-back in tests
        self.calls = []
        self._orig = (controller.find_workspace, controller._kubectl_run)
        controller.find_workspace = lambda user: {
            'deployment': f'ws-{user}', 'namespace': f'ws-{user}'}
        controller._kubectl_run = lambda args, namespace=None: self.calls.append(
            (list(args), namespace))

    def tearDown(self):
        controller.find_workspace, controller._kubectl_run = self._orig

    def test_enabling_patches_the_deployment_annotations(self):
        result = controller.set_workspace_auto_pause('octo', True, 45)
        self.assertEqual(result['autoPause'], {'enabled': True, 'idleMinutes': 45})
        args, ns = self.calls[0]
        self.assertEqual(args[0], 'patch')
        self.assertEqual(args[1], 'deployment/ws-octo')
        self.assertEqual(ns, 'ws-octo')
        body = json.loads(args[args.index('-p') + 1])
        ann = body['metadata']['annotations']
        self.assertEqual(ann[controller.ANN_AUTO_PAUSE], 'true')
        self.assertEqual(ann[controller.ANN_AUTO_PAUSE_IDLE], '45')

    def test_toggling_never_rolls_the_pod(self):
        # The annotation is on the Deployment's own metadata, not the pod
        # template — restarting a workspace to change a pause policy would
        # interrupt the very work the policy protects.
        controller.set_workspace_auto_pause('octo', True, 45)
        body = json.loads(self.calls[0][0][self.calls[0][0].index('-p') + 1])
        self.assertNotIn('spec', body)

    def test_rejects_a_nonsense_threshold(self):
        for bad in ('soon', '-1', '0', '99999999'):
            with self.assertRaises(ValueError):
                controller.set_workspace_auto_pause('octo', True, bad)

    def test_rejects_an_invalid_workspace_name(self):
        with self.assertRaises(ValueError):
            controller.set_workspace_auto_pause('../etc', True, 60)


if __name__ == '__main__':
    unittest.main()
