"""herdr in the workspace terminal (#834).

herdr is a terminal multiplexer for coding agents. The image ships it pinned and
checksum-verified, and start.sh links its three state directories from the
ephemeral /home/ubuntu onto the PVC so a user's config and saved session
survive a pod restart. These tests cover each piece without building the image:

  * the Dockerfile pin, which Renovate must be able to track and re-pin;
  * the boot-time links, by running the real start.sh lines against throwaway
    homes, because a regex on the text cannot prove a second boot is a no-op
    or that a stray real directory is migrated rather than deleted;
  * the docs, which must give the detach key that works inside tmux.

Run with:
    cd charts/workspace && python3 -m unittest tests.herdr_test
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(WORKSPACE))

DOCKERFILE = os.path.join(REPO, 'devlaptop', 'Dockerfile')
RENOVATE_JSON = os.path.join(REPO, 'renovate.json')
RENOVATE_WORKFLOW = os.path.join(REPO, '.github', 'workflows', 'renovate.yml')
RELEASE_DIGESTS = os.path.join(REPO, 'scripts', 'renovate', 'release-digests.mjs')
START_SH = os.path.join(WORKSPACE, 'start.sh')
CLAUDE_MD = os.path.join(WORKSPACE, 'claude-md.txt')
HERDR_DOC = os.path.join(REPO, 'docs', 'in-app', 'herdr.md')

PINNED = 'v0.9.5'
STATE_DIRS = ('.config/herdr', '.local/state/herdr', '.herdr')
NODE = shutil.which('node')
BASH = shutil.which('bash')


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


class DockerfilePinTest(unittest.TestCase):
    """The image installs the pinned release, verified, for both arches."""

    @classmethod
    def setUpClass(cls):
        cls.src = _read(DOCKERFILE)
        m = re.search(r'^ARG HERDR_VERSION=.*?(?=^\n|^# Install GitHub CLI)',
                      cls.src, re.S | re.M)
        assert m, 'herdr block not found in devlaptop/Dockerfile'
        cls.block = m.group(0)

    def test_version_is_pinned_with_a_renovate_marker_directly_above(self):
        # The regex manager only matches the marker on the line right above the
        # ARG; anything in between silently untracks the pin (SUPPLY_CHAIN.md).
        self.assertIn('# renovate: datasource=github-releases depName=herdrdev/herdr\n'
                      'ARG HERDR_VERSION=%s\n' % PINNED, self.src)

    def test_renovate_custom_manager_extracts_the_pin(self):
        cfg = json.loads(_read(RENOVATE_JSON))
        manager = next(m for m in cfg['customManagers']
                       if 'devlaptop/Dockerfile' in m['managerFilePatterns']
                       and 'ARG' in m['matchStrings'][0])
        pattern = manager['matchStrings'][0].replace('(?<', '(?P<')
        found = [m.groupdict() for m in re.finditer(pattern, self.src)]
        herdr = [f for f in found if f['depName'] == 'herdrdev/herdr']
        self.assertEqual(len(herdr), 1, found)
        self.assertEqual(herdr[0]['datasource'], 'github-releases')
        self.assertEqual(herdr[0]['currentValue'], PINNED)

    def test_both_arches_have_a_pinned_sha256(self):
        for arch in ('AMD64', 'ARM64'):
            self.assertRegex(self.src, r'(?m)^ARG HERDR_SHA256_%s=[0-9a-f]{64}$' % arch)
        self.assertIn('amd64) HERDR_ARCH=x86_64;  HERDR_SHA256="$HERDR_SHA256_AMD64"', self.block)
        self.assertIn('arm64) HERDR_ARCH=aarch64; HERDR_SHA256="$HERDR_SHA256_ARM64"', self.block)
        # Unknown arch fails the build rather than installing unverified.
        self.assertIn('exit 1', self.block)

    def test_download_is_verified_before_install_and_version_is_checked(self):
        verify = self.block.index('sha256sum -c -')
        install = self.block.index('install -m 0755 /tmp/herdr /usr/local/bin/herdr')
        self.assertLess(verify, install, 'install must come after the checksum check')
        self.assertIn('releases/download/${HERDR_VERSION}/herdr-linux-${HERDR_ARCH}', self.block)
        # The build fails unless the installed binary reports the pinned version.
        self.assertIn('herdr --version | grep -qx "herdr ${HERDR_VERSION#v}"', self.block)
        self.assertNotRegex(self.block, r'curl[^\n]*\|\s*(ba)?sh', 'no curl | sh installer')


class RenovateRepinTest(unittest.TestCase):
    """A Renovate bump also re-pins the digests, and the command is allowed."""

    def test_herdr_rule_runs_the_repin_script(self):
        cfg = json.loads(_read(RENOVATE_JSON))
        rule = next(r for r in cfg['packageRules']
                    if r.get('matchDepNames') == ['herdrdev/herdr'])
        self.assertEqual(rule['postUpgradeTasks']['commands'],
                         ['node scripts/renovate/herdr-sha.mjs'])
        self.assertEqual(rule['postUpgradeTasks']['fileFilters'], ['devlaptop/Dockerfile'])
        self.assertTrue(os.path.isfile(os.path.join(REPO, 'scripts', 'renovate', 'herdr-sha.mjs')))

    def test_every_post_upgrade_command_is_allowed_by_the_workflow(self):
        # allowedCommands is global-only, so it lives in the workflow env. A
        # command it does not match is skipped and the PR ships stale digests.
        m = re.search(r"RENOVATE_ALLOWED_COMMANDS: '(\[.*\])'", _read(RENOVATE_WORKFLOW))
        self.assertIsNotNone(m)
        allowed = [re.compile(p) for p in json.loads(m.group(1))]
        cfg = json.loads(_read(RENOVATE_JSON))
        commands = [c for r in cfg['packageRules']
                    for c in r.get('postUpgradeTasks', {}).get('commands', [])]
        self.assertIn('node scripts/renovate/code-server-sha.mjs', commands)
        self.assertIn('node scripts/renovate/herdr-sha.mjs', commands)
        for cmd in commands:
            self.assertTrue(any(p.search(cmd) for p in allowed), cmd)
        self.assertFalse(any(p.search('node scripts/renovate/herdr-sha.mjs; rm -rf /')
                             for p in allowed), 'allowlist must stay anchored')


@unittest.skipUnless(NODE, 'node not available')
class ApplyDigestsTest(unittest.TestCase):
    """The pure re-pin step shared by the code-server and herdr scripts."""

    SRC = ('ARG HERDR_VERSION=v9.9.9\n'
           'ARG HERDR_SHA256_AMD64=' + '0' * 64 + '\n'
           'ARG HERDR_SHA256_ARM64=' + '1' * 64 + '\n')
    PINS = {'HERDR_SHA256_AMD64': 'herdr-linux-x86_64',
            'HERDR_SHA256_ARM64': 'herdr-linux-aarch64'}

    def _apply(self, src, assets, pins):
        script = (
            'import { applyDigests, readArg } from %s;\n'
            'const [src, assets, pins] = JSON.parse(process.argv[1]);\n'
            'try {\n'
            '  process.stdout.write(JSON.stringify({ out: applyDigests(src, assets, pins),\n'
            '    version: readArg(src, "HERDR_VERSION") }));\n'
            '} catch (e) { process.stdout.write(JSON.stringify({ error: e.message })); }\n'
        ) % json.dumps('file://' + RELEASE_DIGESTS)
        proc = subprocess.run([NODE, '--input-type=module', '-e', script,
                               json.dumps([src, assets, pins])],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_replaces_each_digest_from_its_asset(self):
        a, b = 'a' * 64, 'b' * 64
        res = self._apply(self.SRC, [
            {'name': 'herdr-linux-x86_64', 'digest': 'sha256:' + a},
            {'name': 'herdr-linux-aarch64', 'digest': 'sha256:' + b},
            {'name': 'herdr-macos-aarch64', 'digest': 'sha256:' + 'c' * 64},
        ], self.PINS)
        self.assertEqual(res['version'], 'v9.9.9')
        self.assertEqual(res['out'], 'ARG HERDR_VERSION=v9.9.9\n'
                         'ARG HERDR_SHA256_AMD64=%s\nARG HERDR_SHA256_ARM64=%s\n' % (a, b))

    def test_missing_asset_or_digest_fails_closed(self):
        res = self._apply(self.SRC, [
            {'name': 'herdr-linux-x86_64', 'digest': 'sha256:' + 'a' * 64},
            {'name': 'herdr-linux-aarch64', 'digest': None},
        ], self.PINS)
        self.assertIn('herdr-linux-aarch64', res['error'])

    def test_missing_arg_fails_closed(self):
        res = self._apply('ARG HERDR_VERSION=v9.9.9\n', [
            {'name': 'herdr-linux-x86_64', 'digest': 'sha256:' + 'a' * 64},
        ], {'HERDR_SHA256_AMD64': 'herdr-linux-x86_64'})
        self.assertIn('HERDR_SHA256_AMD64', res['error'])


def _boot_snippet():
    """link_cred_dir plus the herdr loop, verbatim from start.sh."""
    text = _read(START_SH)
    helper = re.search(r'^link_cred_dir\(\) \{.*?^\}$', text, re.S | re.M)
    loop = re.search(r'^for herdr_dir in .*?^done$', text, re.S | re.M)
    assert helper and loop, 'link_cred_dir or the herdr loop not found in start.sh'
    return helper.group(0) + '\n' + loop.group(0) + '\n'


@unittest.skipUnless(BASH, 'bash not available')
class BootLinksTest(unittest.TestCase):
    """Run the real boot lines against a sandbox pair of homes."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='kc834-')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.ubuntu = os.path.join(self.root, 'home', 'ubuntu')
        self.dev = os.path.join(self.root, 'home', 'dev')
        os.makedirs(self.ubuntu)
        os.makedirs(self.dev)

    def boot(self):
        body = _boot_snippet().replace('/home/dev', self.dev).replace('/home/ubuntu', self.ubuntu)
        script = ('log_stage() { echo "[stage] $*" >&2; }\nchown() { :; }\n' + body)
        proc = subprocess.run([BASH, '-c', script], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stderr

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text)

    def assert_linked(self):
        for sub in STATE_DIRS:
            link = os.path.join(self.ubuntu, sub)
            target = os.path.join(self.dev, sub)
            self.assertTrue(os.path.islink(link), '%s is not a symlink' % link)
            self.assertEqual(os.readlink(link), target)
            # A real directory on the PVC, so herdr can write through the link
            # and a HOME=/home/dev shell uses the very same files.
            self.assertTrue(os.path.isdir(target) and not os.path.islink(target), target)

    def test_fresh_boot_links_every_state_dir_onto_the_pvc(self):
        self.boot()
        self.assert_linked()

    def test_second_boot_is_a_no_op_and_keeps_saved_state(self):
        self.boot()
        session = os.path.join(self.dev, '.config/herdr/session.json')
        self.write(session, '{"workspaces": ["kept"]}')
        self.boot()
        self.assert_linked()
        # Nothing nested inside the target by re-running ln over the link.
        self.assertEqual(sorted(os.listdir(os.path.join(self.dev, '.config/herdr'))),
                         ['session.json'])
        self.assertEqual(_read(session), '{"workspaces": ["kept"]}')

    def test_pod_restart_reattaches_existing_pvc_state(self):
        config = os.path.join(self.dev, '.config/herdr/config.toml')
        self.write(config, 'onboarding = false\n')
        self.boot()
        # A restart rebuilds /home/ubuntu from skel; the PVC survives.
        shutil.rmtree(self.ubuntu)
        os.makedirs(self.ubuntu)
        self.boot()
        self.assert_linked()
        self.assertEqual(_read(os.path.join(self.ubuntu, '.config/herdr/config.toml')),
                         'onboarding = false\n')

    def test_stray_real_directory_is_migrated_no_clobber_and_parked(self):
        pvc_cfg = os.path.join(self.dev, '.config/herdr/config.toml')
        self.write(pvc_cfg, '[keys]\nprefix = "ctrl+b"\n')
        stray = os.path.join(self.ubuntu, '.config/herdr')
        self.write(os.path.join(stray, 'config.toml'), 'stale = true\n')
        self.write(os.path.join(stray, 'session.json'), '{"only": "here"}')
        log = self.boot()
        self.assert_linked()
        self.assertEqual(_read(pvc_cfg), '[keys]\nprefix = "ctrl+b"\n', 'PVC copy must win')
        self.assertEqual(_read(os.path.join(self.dev, '.config/herdr/session.json')),
                         '{"only": "here"}')
        parked = [n for n in os.listdir(os.path.dirname(stray)) if n.startswith('herdr.pre-797.')]
        self.assertEqual(len(parked), 1, 'the original is parked, never deleted')
        self.assertIn('migrated real', log)


class DocsTest(unittest.TestCase):
    """The shipped docs tell users and agents how to drive herdr here."""

    def test_claude_md_documents_start_detach_and_persistence(self):
        text = _read(CLAUDE_MD)
        self.assertIn('herdr', text)
        self.assertIn('Ctrl+B Ctrl+B q', text)
        self.assertIn('/home/dev/.config/herdr', text)

    def test_workspace_doc_covers_start_detach_reattach(self):
        text = _read(HERDR_DOC)
        for needle in ('herdr', 'Ctrl+B Ctrl+B q', 'herdr server stop',
                       '/home/dev/.config/herdr', 'integration install'):
            self.assertIn(needle, text)


if __name__ == '__main__':
    unittest.main()
