"""Regression tests for the durable state and external-effect boundaries."""
import copy
import datetime
import os
import sys
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from provision_store import KubeStore, Conflict
from provision_backend import Backend
from dex_client import DexClient
from signup_errors import SignupError


class StoreTest(unittest.TestCase):
    def test_release_preserves_cas_and_expires_positive_lease(self):
        store = KubeStore('state')
        held = {'metadata': {'resourceVersion': '7'}, 'spec': {}}
        store.request = Mock(side_effect=[None, held, {}])
        with store.leader() as acquired:
            self.assertTrue(acquired)
        released = store.request.call_args.args[2]
        self.assertEqual(released['metadata']['resourceVersion'], '7')
        self.assertGreater(released['spec']['leaseDurationSeconds'], 0)
        self.assertEqual(released['spec']['renewTime'], '1970-01-01T00:00:00.000000Z')

    def test_acquire_sends_microtime_even_on_whole_second(self):
        # The API server rejects MicroTime without six fractional digits (400),
        # which left the lease held for its full 600s after every step.
        import provision_store
        store = KubeStore('state')
        store.request = Mock(side_effect=[None, {'metadata': {}, 'spec': {}}, {}])
        whole = datetime.datetime(2026, 11, 21, 3, 53, 25, 0, tzinfo=datetime.timezone.utc)
        with patch.object(provision_store.datetime, 'datetime', wraps=datetime.datetime) as dt:
            dt.now.return_value = whole
            with store.leader():
                pass
        acquired = store.request.call_args_list[1].args[2]
        self.assertEqual(acquired['spec']['renewTime'], '2026-11-21T03:53:25.000000Z')

    def test_live_lease_prevents_second_worker(self):
        store = KubeStore('state')
        store.request = Mock(return_value={'spec': {'renewTime': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'leaseDurationSeconds': 600}})
        with store.leader() as acquired:
            self.assertFalse(acquired)
        store.request.assert_called_once()

    def test_creation_race_uses_winning_credentials(self):
        import base64
        store = KubeStore('state')
        store.request = Mock(side_effect=[None, Conflict(), {'data': {'secret': base64.b64encode(b'winner').decode()}}])
        self.assertEqual(store.credentials('123', lambda: {'secret': 'loser'}), {'secret': 'winner'})


class DexTest(unittest.TestCase):
    def client(self):
        client = DexClient.__new__(DexClient)
        client.messages = types.SimpleNamespace(Client=lambda **kw: types.SimpleNamespace(**kw),
            CreateClientReq=lambda **kw: kw, GetClientReq=lambda **kw: kw)
        return client

    def test_existing_client_must_match_secret_redirect_and_trust(self):
        valid = dict(secret='secret', redirect_uris=['https://owner.test/oauth2/callback'], public=False, trusted_peers=[])
        for change in ({}, {'secret': 'different'}, {'redirect_uris': ['https://attacker.test']}, {'public': True}, {'trusted_peers': ['other']}):
            with self.subTest(change=change):
                client = self.client()
                client.call = Mock(side_effect=[types.SimpleNamespace(already_exists=True), types.SimpleNamespace(client=types.SimpleNamespace(**(valid | change)))])
                if change:
                    with self.assertRaises(SignupError) as caught:
                        client.ensure('workspace-123', 'secret', valid['redirect_uris'][0])
                    self.assertEqual(caught.exception.code, 'identity_conflict')
                else:
                    client.ensure('workspace-123', 'secret', valid['redirect_uris'][0])

    def test_rpc_failure_is_safe_and_bounded(self):
        client = self.client()
        class RpcFailure(RuntimeError):
            def code(self):
                return types.SimpleNamespace(name='UNAVAILABLE')
        client.grpc = types.SimpleNamespace(RpcError=RpcFailure)
        client.api = Mock()
        client.api.DeleteClient.side_effect = RpcFailure('private upstream message')
        with self.assertRaises(SignupError) as caught:
            client.call('DeleteClient', {})
        self.assertEqual(caught.exception.code, 'dex_unavailable')
        client.api.DeleteClient.assert_called_once_with({}, timeout=10)


class BackendTest(unittest.TestCase):
    def test_lookup_tells_an_unknown_user_from_a_github_outage(self):
        import controller as real
        controller = Mock(GithubError=real.GithubError, _GH_LOGIN_RE=real._GH_LOGIN_RE, GITOPS_TOKEN='t')
        backend = Backend(controller, 'https://auth.test', True)
        for status, code in ((404, 'github_user_not_found'), (401, 'github_unavailable'),
                             (500, 'github_unavailable'), (0, 'github_unavailable')):
            controller._github_api.side_effect = real.GithubError('failed', status)
            with self.subTest(status=status), self.assertRaises(SignupError) as raised:
                backend.lookup('octocat')
            self.assertEqual(raised.exception.code, code)
        with self.assertRaises(SignupError) as raised:
            backend.lookup('not a login')
        self.assertEqual(raised.exception.code, 'invalid_request')

    def test_migration_rejects_shared_namespace_and_custom_hosts(self):
        controller = Mock()
        controller.WORKSPACE_DOMAIN = 'example.test'
        controller.ns_for_user.return_value = 'ws-octocat'
        backend = Backend(controller, 'https://auth.test', True)
        for namespace, url in [('coder', 'https://octocat.example.test'), ('ws-octocat', 'https://custom.test')]:
            controller.find_workspace.return_value = {'namespace': namespace, 'url': url}
            with self.assertRaises(SignupError):
                backend.validate_migration('octocat')
        controller.find_workspace.return_value = {'namespace': 'ws-octocat', 'url': 'https://octocat.example.test/'}
        backend.validate_migration('octocat')

    def test_missing_namespace_does_not_query_tenant_resources(self):
        controller = Mock(NAMESPACE='coder', WORKSPACE_PREFIX='ws-')
        controller.ns_for_user.return_value = 'ws-octocat'
        backend = Backend(controller, 'https://auth.test', True)
        backend.resource = Mock(return_value=None)
        self.assertTrue(backend.absent('octocat'))
        self.assertEqual([call.args[0] for call in backend.resource.call_args_list], ['namespace', 'namespace'])

    def test_job_is_pinned_to_published_commit_and_keeps_recovery_evidence(self):
        manifest = {'metadata': {'labels': {}}, 'spec': {'ttlSecondsAfterFinished': 3600,
            'template': {'spec': {'containers': [{'env': []}]}}}}
        controller = Mock()
        controller.build_job_manifest.return_value = copy.deepcopy(manifest)
        backend = Backend(controller, 'https://auth.test', True)
        backend.job = Mock(return_value=None)
        backend.deploy({'slug': 'octocat', 'operationId': 'abc', 'attempt': 1, 'gitCommit': 'a' * 40})
        created = controller._kubectl_apply.call_args.args[0]
        self.assertNotIn('ttlSecondsAfterFinished', created['spec'])
        self.assertEqual(created['spec']['template']['spec']['containers'][0]['env'], [{'name': 'GITOPS_COMMIT', 'value': 'a' * 40}])


class MigrationFilesTest(unittest.TestCase):
    def test_migration_preserves_values_and_original_credentials(self):
        import json
        import pathlib
        import tempfile
        import controller
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            user = root / 'users-private/octocat'
            (user / 'secrets').mkdir(parents=True)
            (user / 'values.yaml').write_text('custom: keep-me\n', encoding='utf-8')
            (user / 'secrets/oauth2.yaml').write_text('legacy: keep-me\n', encoding='utf-8')
            opts = {'slug': 'octocat', 'migrate': True, 'oidcIssuer': 'https://auth.test', 'ownerGithubId': '123'}
            with patch.object(controller.tempfile, 'mkdtemp', return_value=tmp), patch.object(controller.shutil, 'rmtree'), patch.object(controller, '_git', return_value='a' * 40), patch.object(controller, 'render_values_yaml', return_value='unused'):
                self.assertEqual(controller.gitops_publish(opts, 'workspace-123', 'new-secret', 'new-cookie'), 'a' * 40)
                self.assertEqual((user / 'values.yaml').read_text(), 'custom: keep-me\n')
                self.assertEqual((user / 'secrets/oauth2.yaml').read_text(), 'legacy: keep-me\n')
                overlay = json.loads((user / 'secrets/zz-dex.yaml').read_text())
                self.assertEqual(set(overlay), {'oauth2'})
                self.assertEqual(overlay['oauth2']['ownerGithubId'], '123')
                (user / 'secrets/zzz.yaml').write_text('oauth2: {}')
                with self.assertRaises(controller.ProvisionError):
                    controller.gitops_publish(opts, 'workspace-123', 'secret', 'cookie')
