"""Run the production checkout blocks against real local Git repositories."""
import os
import pathlib
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
BASH = 'C:/Program Files/Git/bin/bash.exe' if os.name == 'nt' else shutil.which('bash')


@unittest.skipUnless(BASH and pathlib.Path(BASH).exists() and shutil.which('git'), 'Git and Bash required')
class ProvisionerRefTest(unittest.TestCase):
    def git(self, *args):
        return subprocess.run(['git', *args], check=True, capture_output=True, text=True).stdout.strip()

    def test_chart_sha_tag_and_gitops_commit_use_exact_revision(self):
        script = (ROOT / 'provisioner/provision.sh').read_text(encoding='utf-8')
        chart_block = script[script.index('if [[ "$CHART_REF"'):script.index('# Provenance: record')]
        config_block = script[script.index('if [ -n "${GITOPS_COMMIT'):script.index('mkdir -p /tmp/kc/users-private')]
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            repo = root / 'repo'
            self.git('init', '-b', 'main', str(repo))
            (repo / 'version').write_text('old')
            self.git('-C', str(repo), 'add', '.')
            self.git('-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-m', 'old')
            old = self.git('-C', str(repo), 'rev-parse', 'HEAD')
            self.git('-C', str(repo), 'tag', 'v1.2.3')
            (repo / 'version').write_text('new')
            self.git('-C', str(repo), 'add', '.')
            self.git('-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-m', 'new')
            for index, ref in enumerate((old, 'v1.2.3')):
                dest = root / str(index)
                command = 'set -euo pipefail\n' + chart_block.replace('/tmp/kc', shlex.quote(dest.as_posix()))
                result = subprocess.run([BASH, '-c', command], env=os.environ | {'CHART_REF': ref, 'CHART_REPO': repo.as_posix()}, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.git('-C', str(dest), 'rev-parse', 'HEAD'), old)
            cfg = root / 'cfg'
            self.git('clone', str(repo), str(cfg))
            command = 'set -euo pipefail\n' + config_block.replace('/tmp/cfg', shlex.quote(cfg.as_posix()))
            for ref, code in ((old, 0), ('not-a-commit', 1)):
                result = subprocess.run([BASH, '-c', command], env=os.environ | {'GITOPS_COMMIT': ref}, capture_output=True, text=True)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(self.git('-C', str(cfg), 'rev-parse', 'HEAD'), old)
