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


class ProvisionPureLogicTest(unittest.TestCase):
    """Pure-logic provisioning helpers: state signing, the GitHub App manifest,
    cookie-secret shape, and values rendering. No network, no cluster."""

    def setUp(self):
        controller.PROVISION_STATE_SECRET = 'unit-test-hmac-key'
        controller.CONTROLLER_HOST = 'controller.dev.scalebase.io'
        controller.WORKSPACE_DOMAIN = 'dev.scalebase.io'
        controller.GITHUB_APP_ORG = ''

    def test_slugify_lowercases(self):
        self.assertEqual(controller.slugify('Chase-31415'), 'chase-31415')

    def test_login_regex_accepts_valid_rejects_invalid(self):
        self.assertTrue(controller._GH_LOGIN_RE.match('octocat'))
        self.assertTrue(controller._GH_LOGIN_RE.match('a-b-c1'))
        self.assertFalse(controller._GH_LOGIN_RE.match('-bad'))
        self.assertFalse(controller._GH_LOGIN_RE.match('bad-'))
        self.assertFalse(controller._GH_LOGIN_RE.match('has space'))
        self.assertFalse(controller._GH_LOGIN_RE.match('under_score'))

    def test_state_roundtrip(self):
        payload = {'login': 'Octo', 'host': 'octo.dev.scalebase.io'}
        token = controller.sign_state(payload)
        out = controller.verify_state(token)
        self.assertEqual(out['login'], 'Octo')
        self.assertEqual(out['host'], 'octo.dev.scalebase.io')

    def test_state_tamper_rejected(self):
        token = controller.sign_state({'login': 'octo'})
        raw, sig = token.split('.', 1)
        tampered = base64.urlsafe_b64encode(b'{"login":"admin","exp":9999999999}').rstrip(b'=').decode() + '.' + sig
        with self.assertRaises(ValueError):
            controller.verify_state(tampered)

    def test_state_expiry(self):
        # Sign with a TTL in the past so the token is born expired.
        orig = controller.STATE_TTL
        try:
            controller.STATE_TTL = -1
            token = controller.sign_state({'login': 'octo'})
            with self.assertRaises(ValueError):
                controller.verify_state(token)
        finally:
            controller.STATE_TTL = orig

    def test_app_manifest_callback_and_redirect(self):
        m = controller.build_app_manifest('Chase-31415', 'chase-31415.dev.scalebase.io')
        self.assertEqual(m['callback_urls'], ['https://chase-31415.dev.scalebase.io/oauth2/callback'])
        self.assertEqual(m['redirect_url'], 'https://controller.dev.scalebase.io/api/provision/github/callback')
        self.assertFalse(m['public'])
        self.assertLessEqual(len(m['name']), 34)

    def test_manifest_post_url_personal_vs_org(self):
        self.assertTrue(controller.manifest_post_url('S').startswith('https://github.com/settings/apps/new?state='))
        controller.GITHUB_APP_ORG = 'acme'
        self.assertIn('/organizations/acme/settings/apps/new', controller.manifest_post_url('S'))

    def test_cookie_secret_shape(self):
        s = controller.gen_cookie_secret()
        self.assertEqual(len(s), 32)
        self.assertTrue(s.isalnum())

    def test_render_values_yaml_is_valid_and_has_fields(self):
        opts = {'login': 'Octo', 'slug': 'octo', 'host': 'octo.dev.scalebase.io',
                'pvcSize': '30Gi', 'gitName': 'Octo Cat', 'gitEmail': 'octo@example.com',
                'imageTag': 'v9.9.9'}
        text = controller.render_values_yaml(opts, 'Iv1.clientid', 'cookiesecret32xxxxxxxxxxxxxxxxxxx')
        # Validate it parses as YAML and carries the access gate + host.
        try:
            import yaml  # PyYAML may not be installed in CI; fall back to substring checks.
            doc = yaml.safe_load(text)
            self.assertEqual(doc['user']['name'], 'octo')
            self.assertEqual(doc['user']['host'], 'octo.dev.scalebase.io')
            self.assertEqual(doc['user']['pvcSize'], '30Gi')
            self.assertEqual(doc['oauth2']['githubUsers'], 'Octo')   # login, case-preserved
            self.assertEqual(doc['oauth2']['clientId'], 'Iv1.clientid')
            self.assertEqual(doc['image']['tag'], 'devlaptop-v9.9.9')
            self.assertEqual(doc['ingress']['tls']['secretName'], 'octo-dev-scalebase-io-tls')
            self.assertEqual(doc['ingress']['auth']['type'], 'oauth2')
            # values.yaml carries only the placeholder; the real secret is
            # split out into secrets/oauth2.yaml (render_oauth_secret_yaml).
            self.assertEqual(doc['oauth2']['clientSecret'], 'OVERRIDE-IN-SECRETS-OAUTH2-YAML')
        except ImportError:
            self.assertIn('name: octo', text)
            self.assertIn('host: octo.dev.scalebase.io', text)
            self.assertIn('githubUsers: "Octo"', text)
            self.assertIn('devlaptop-v9.9.9', text)

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
        self.assertIn('ttlSecondsAfterFinished', job['spec'])
        self.assertEqual(job['spec']['template']['spec']['restartPolicy'], 'Never')


class ResourceLimitTest(unittest.TestCase):
    """Validation + strategic-merge patch construction for in-place limit edits."""

    def setUp(self):
        controller.MAX_CPU_LIMIT_CORES = 16.0
        controller.MAX_MEM_LIMIT = '64Gi'
        controller.WORKSPACE_CONTAINER = 'ide'
        controller.WORKSPACE_PREFIX = 'ws-'
        # Pretend the workspace exists so the existence guard passes.
        controller.list_workspaces = lambda: {'namespace': 'coder', 'workspaces': [{'deployment': 'ws-octo'}]}

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
        controller._kubectl_run = lambda args: captured.setdefault('args', args)
        limits = controller.set_workspace_resources('octo', '2', '4Gi')
        self.assertEqual(limits, {'cpu': '2', 'memory': '4Gi'})
        args = captured['args']
        self.assertEqual(args[0], 'patch')
        self.assertEqual(args[1], 'deployment/ws-octo')
        self.assertIn('--type=strategic', args)
        patch = json.loads(args[args.index('-p') + 1])
        container = patch['spec']['template']['spec']['containers'][0]
        self.assertEqual(container['name'], 'ide')
        self.assertEqual(container['resources']['limits'], {'cpu': '2', 'memory': '4Gi'})
        # requests must not be touched by the patch.
        self.assertNotIn('requests', container['resources'])

    def test_set_resources_requires_at_least_one(self):
        controller._kubectl_run = lambda args: None
        with self.assertRaises(ValueError):
            controller.set_workspace_resources('octo', None, None)

    def test_set_resources_unknown_workspace_raises_lookup(self):
        controller.list_workspaces = lambda: {'namespace': 'coder', 'workspaces': []}
        with self.assertRaises(LookupError):
            controller.set_workspace_resources('octo', '2', None)


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
        self._orig_list = controller.list_workspaces
        self._orig_run = controller._kubectl_run
        self._orig_latest = controller.latest_version
        self._orig_gitops_repo = controller.GITOPS_REPO
        self._orig_gitops_token = controller.GITOPS_TOKEN
        controller.GITOPS_REPO = ''       # persistence off by default in tests
        controller.GITOPS_TOKEN = ''
        controller.latest_version = lambda: 'v1.4.0'
        controller.list_workspaces = lambda: {'namespace': 'coder', 'workspaces': [
            {'deployment': 'ws-octo', 'version': 'v1.3.0',
             'image': 'registry/coder:devlaptop-v1.3.0', 'imageTag': 'devlaptop-v1.3.0'}]}

    def tearDown(self):
        controller.list_workspaces = self._orig_list
        controller._kubectl_run = self._orig_run
        controller.latest_version = self._orig_latest
        controller.GITOPS_REPO = self._orig_gitops_repo
        controller.GITOPS_TOKEN = self._orig_gitops_token

    def test_patches_image_to_latest(self):
        captured = {}
        controller._kubectl_run = lambda args: captured.setdefault('args', args)
        result = controller.set_workspace_image('octo')
        args = captured['args']
        self.assertEqual(args[0], 'patch')
        self.assertEqual(args[1], 'deployment/ws-octo')
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
        controller._kubectl_run = lambda args: captured.setdefault('args', args)
        controller.set_workspace_image('octo', 'v1.3.5')
        patch = json.loads(captured['args'][captured['args'].index('-p') + 1])
        self.assertEqual(patch['spec']['template']['spec']['containers'][0]['image'],
                         'registry/coder:devlaptop-v1.3.5')

    def test_noop_when_already_on_target(self):
        ran = {'called': False}
        controller._kubectl_run = lambda args: ran.update(called=True)
        result = controller.set_workspace_image('octo', 'v1.3.0')  # already on v1.3.0
        self.assertFalse(ran['called'])   # no patch issued
        self.assertFalse(result['rolled'])

    def test_unknown_target_raises(self):
        controller.latest_version = lambda: None
        with self.assertRaises(ValueError):
            controller.set_workspace_image('octo')          # no version anywhere

    def test_unknown_workspace_raises_lookup(self):
        controller.list_workspaces = lambda: {'namespace': 'coder', 'workspaces': []}
        with self.assertRaises(LookupError):
            controller.set_workspace_image('octo', 'v1.4.0')

    def test_persist_path_invoked_when_gitops_configured(self):
        controller._kubectl_run = lambda args: None
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


if __name__ == '__main__':
    unittest.main()
