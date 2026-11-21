"""Real local GitOps publishing through Helm rendering, without network credentials."""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'charts/workspace-controller'))
import controller


@unittest.skipUnless(shutil.which('git') and shutil.which('helm'), 'Git and Helm required')
class GitOpsIntegrationTest(unittest.TestCase):
    def git(self, *args):
        return subprocess.run(['git', *args], check=True, capture_output=True, text=True, encoding='utf-8').stdout.strip()

    def test_publish_replay_migrate_and_render_preserve_owner_and_custom_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            remote, seed = root / 'remote.git', root / 'seed'
            self.git('init', '--bare', str(remote))
            self.git('init', '-b', 'main', str(seed))
            (seed / 'README').write_text('private fixture repository')
            self.git('-C', str(seed), 'add', '.')
            self.git('-C', str(seed), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-m', 'init')
            self.git('-C', str(seed), 'remote', 'add', 'origin', str(remote))
            self.git('-C', str(seed), 'push', 'origin', 'main')
            real_git, mkdtemp = controller._git, tempfile.mkdtemp
            def local_git(args):
                args = list(args)
                if args[0] == 'clone':
                    args[-2] = remote.as_uri()
                return real_git(args)
            opts = {'slug': 'octocat', 'login': 'octocat', 'host': 'octocat.example.test',
                    'ownerGithubId': '123', 'oidcIssuer': 'https://auth.example.test', 'sharedTls': True}
            with patch.object(controller, '_git', side_effect=local_git), patch.object(controller.tempfile, 'mkdtemp', side_effect=lambda **kw: mkdtemp(prefix=kw['prefix'], dir=tmp)), patch.object(controller, 'GITOPS_BRANCH', 'main'), patch.object(controller, 'WORKSPACE_DOMAIN', 'example.test'), patch.object(controller, 'latest_version', return_value='v1.2.3'):
                first = controller.gitops_publish(opts, 'workspace-123', 'fixture-secret', 'A' * 32)
                self.assertEqual(controller.gitops_publish(opts, 'workspace-123', 'fixture-secret', 'A' * 32), first)
                self.git('-C', str(seed), 'pull', 'origin', 'main')
                values = seed / 'users-private/octocat/values.yaml'
                secrets = seed / 'users-private/octocat/secrets/oauth2.yaml'
                values.write_text(values.read_text(encoding='utf-8') + '\ncustomSetting: keep-me\n', encoding='utf-8')
                self.git('-C', str(seed), 'add', '.')
                self.git('-C', str(seed), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-m', 'customize')
                self.git('-C', str(seed), 'push', 'origin', 'main')
                preserved_values, preserved_secrets = values.read_bytes(), secrets.read_bytes()
                migrated = controller.gitops_publish(opts | {'migrate': True}, 'workspace-123', 'rotated', 'B' * 32)
                self.git('-C', str(seed), 'pull', 'origin', 'main')
                self.assertEqual(self.git('-C', str(seed), 'rev-parse', 'HEAD'), migrated)
                self.assertEqual(values.read_bytes(), preserved_values)
                self.assertEqual(secrets.read_bytes(), preserved_secrets)
                overlay = secrets.parent / 'zz-dex.yaml'
                self.assertEqual(json.loads(overlay.read_text())['oauth2']['clientSecret'], 'rotated')
                result = subprocess.run(['helm', 'template', 'test', str(ROOT / 'charts/workspace'), '-f', str(values), '-f', str(secrets), '-f', str(overlay)], capture_output=True, text=True, encoding='utf-8')
                self.assertEqual(result.returncode, 0, result.stderr)
                for flag in ('--provider=oidc', '--allowed-group=123', '--oidc-issuer-url=https://auth.example.test'):
                    self.assertIn(flag, result.stdout)
                self.assertNotIn('--github-user=', result.stdout)
