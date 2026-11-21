"""Render security boundaries with the real Helm binary; no cluster required."""
import json
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]


@unittest.skipUnless(shutil.which('helm'), 'Helm not installed')
class HelmSignupTest(unittest.TestCase):
    def render(self, chart, *args, success=True):
        result = subprocess.run(['helm', 'template', 'test', str(ROOT / chart), *args],
                                capture_output=True, text=True, encoding='utf-8', timeout=60)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result.stdout + result.stderr

    def workspace(self, *extra, success=True):
        args = ['--set', 'user.name=octocat,user.host=octocat.example.test,ingress.auth.type=oauth2',
                '--set', 'oauth2.clientId=test,oauth2.clientSecret=test,oauth2.cookieSecret=AAAAAAAAAAAAAAAAAAAAAA==']
        return self.render('charts/workspace', *args, *extra, success=success)

    def test_legacy_provider_unchanged(self):
        text = self.workspace('--set', 'oauth2.githubUsers=octocat')
        self.assertIn('--provider=github', text)
        self.assertIn('--github-user=octocat', text)
        self.assertNotIn('--provider=oidc', text)

    def test_oidc_requires_numeric_owner(self):
        text = self.workspace('--set', 'oauth2.provider=oidc,oauth2.oidcIssuerUrl=https://auth.example.test', success=False)
        self.assertIn('ownerGithubId', text)
        self.workspace('--set', 'oauth2.provider=oidc,oauth2.ownerGithubId=octocat,oauth2.oidcIssuerUrl=https://auth.example.test', success=False)

    def test_oidc_owner_gate_and_shared_tls(self):
        text = self.workspace('--set', 'oauth2.provider=oidc,oauth2.ownerGithubId=123,oauth2.oidcIssuerUrl=https://auth.example.test,ingress.tls.sharedCertificate=true')
        for expected in ('--allowed-group=123', '--oidc-groups-claim=federated_claims.user_id', '--insecure-oidc-skip-nonce=false', '--code-challenge-method=S256'):
            self.assertIn(expected, text)
        self.assertNotIn('--github-user=', text)
        self.assertNotIn('cert-manager.io/cluster-issuer:', text)

    def test_signup_chart_is_opt_in(self):
        base = ['-f', str(ROOT / 'charts/workspace-controller/tests/test-values.yaml')]
        text = self.render('charts/workspace-controller', *base)
        self.assertNotIn('name: workspace-signup-oauth2', text)
        self.render('charts/workspace-controller', *base, '--set', 'signup.enabled=true', success=False)

    def test_signup_listener_and_csrf_secret(self):
        text = self.render('charts/workspace-controller', '-f', str(ROOT / 'charts/workspace-controller/tests/test-values.yaml'),
                           '-f', str(ROOT / 'charts/workspace-controller/tests/signup-values.yaml'))
        self.assertIn('--upstream=http://workspace-controller:8082/', text)
        self.assertIn('key: csrf-secret', text)
        self.assertIn('namespace: kube-coder-signup', text)
        self.assertIn('--cookie-name=__Host-kc_signup', text)
        self.assertIn('--api-route=^/api/.*', text)

    def test_dex_private_admin_api_and_shared_storage(self):
        text = self.render('charts/dex', '-f', str(ROOT / 'charts/dex/tests/test-values.yaml'))
        for expected in ('replicas: 2', 'type: kubernetes', 'tlsClientCA: /grpc/ca.crt', 'useLoginAsID: false', 'app: workspace-controller'):
            self.assertIn(expected, text)
        self.assertNotIn('kind: ClusterRole', text)
        self.assertIn('service: {name: dex, port: {number: 5556}}', text)


if __name__ == '__main__':
    unittest.main()
