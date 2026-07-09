#!/usr/bin/env python3
"""Unit tests for controller.py — the capacity rollup and the quantity parsers
it builds on. Pure-Python: Prometheus is faked by monkeypatching the query
helpers, so these run with no cluster and no network.

Run from charts/workspace-controller:
    python3 -m unittest discover -s tests -p '*_test.py' -v
"""
import base64
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
        self.assertIsNone(controller._SELF_SERVE_GET_RE.match('/api/workspaces/octo/version'))
        self.assertIsNone(controller._SELF_SERVE_POST_RE.match('/api/self/workspaces/octo/stop'))


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


if __name__ == '__main__':
    unittest.main()
