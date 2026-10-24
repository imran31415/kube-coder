"""`persist_cred` executed against sandbox homes — the #795 contract.

WHY THIS RUNS THE SHELL INSTEAD OF GREPPING IT. `persist_cred` now links
credentials from **both** homes, and /home/dev is the persistent volume: the
paths it migrates are a user's real SSH keys, git identity and stored tokens.
A `matchRegex` on the rendered configmap proves the text shipped; it cannot
prove that a second boot doesn't nest a symlink, that a diverged key survives,
or that nothing gets deleted. So this suite lifts the function verbatim out of
start.sh, rebinds `/home/dev` and `/home/ubuntu` onto a throwaway directory,
and runs the real thing.

The invariant under test: a login shell in either home resolves the SAME
credential files, and getting there never destroys what was already on disk.

Background (issue #795). `persist_cred` used to loop over `/home/ubuntu`
alone, so an agent task — which server.py spawns with HOME=/home/dev — read
an entirely separate, unmanaged set of config. /home/dev/.gitconfig held the
issue #454 `helper =` chain reset, which broke `git push` for every agent
while the App token was perfectly healthy, and no self-heal reached it because
every heal writes the managed file that only /home/ubuntu linked to.

Run with:
    cd charts/workspace && python3 -m unittest tests.credential_home_mirror_test
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(HERE)
START_SH = os.path.join(WORKSPACE, 'start.sh')


def _start_sh():
    with open(START_SH, encoding='utf-8') as fh:
        return fh.read()


def _persist_cred_source():
    """The verbatim `persist_cred() { ... }` definition from start.sh."""
    text = _start_sh()
    match = re.search(r'^persist_cred\(\).*?^\}$', text, re.S | re.M)
    assert match, 'persist_cred() not found in start.sh'
    return match.group(0)


class PersistCredSandbox:
    """A throwaway pair of homes plus the credential store under them."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix='kc795-')
        self.ubuntu = os.path.join(self.root, 'home', 'ubuntu')
        self.dev = os.path.join(self.root, 'home', 'dev')
        self.store = os.path.join(self.dev, '.credentials')
        os.makedirs(self.ubuntu)
        os.makedirs(self.store)

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run(self, *calls, preamble=''):
        """Execute persist_cred against the sandbox and return its stderr log.

        The function hardcodes the two homes and the store path (deliberately —
        absolute paths are the #793/#795 lesson), so the sandbox is created by
        rewriting those two prefixes rather than by parameterising the script.
        `preamble` injects shell stubs (`date`, `mv`) for the branches a
        sandbox cannot provoke by arranging files alone.
        """
        body = _persist_cred_source()
        body = body.replace('/home/dev', self.dev).replace(
            '/home/ubuntu', self.ubuntu)
        script = ('log_stage() { echo "[stage] $*" >&2; }\n' + preamble +
                  '\n' + body + '\n' + '\n'.join(calls) + '\n')
        proc = subprocess.run(['bash', '-c', script], capture_output=True,
                              text=True)
        self.assert_no_failure(proc)
        return proc.stderr

    @staticmethod
    def assert_no_failure(proc):
        if proc.returncode != 0:
            raise AssertionError('persist_cred exited %d\n%s'
                                 % (proc.returncode, proc.stderr))

    # --- helpers the assertions read like prose with -----------------------

    def link(self, home, sub):
        return os.path.join(getattr(self, home), sub)

    def write(self, path, text, mode=0o600):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text)
        os.chmod(path, mode)

    def read(self, path):
        with open(path, encoding='utf-8') as fh:
            return fh.read()

    def aside_paths(self, home, sub):
        """Timestamped backups left next to <home>/<sub>."""
        base = os.path.join(getattr(self, home), sub)
        parent = os.path.dirname(base)
        name = os.path.basename(base)
        if not os.path.isdir(parent):
            return []
        return sorted(os.path.join(parent, entry)
                      for entry in os.listdir(parent)
                      if entry.startswith(name + '.pre795.'))


class PersistCredTestCase(unittest.TestCase):

    def setUp(self):
        self.box = PersistCredSandbox()
        self.addCleanup(self.box.cleanup)

    # ---- the headline invariant -------------------------------------------

    def test_both_homes_resolve_the_same_credential_files(self):
        """The whole point of #795: terminal and agent shell read one store."""
        self.box.run('persist_cred .ssh dir',
                     'persist_cred .gitconfig file .config/git/config',
                     'persist_cred .npmrc file')
        for sub in ('.ssh', '.gitconfig', '.npmrc'):
            with self.subTest(sub=sub):
                ubuntu = os.path.realpath(self.box.link('ubuntu', sub))
                dev = os.path.realpath(self.box.link('dev', sub))
                self.assertEqual(ubuntu, dev)
                self.assertTrue(ubuntu.startswith(self.box.store + os.sep),
                                '%s should resolve into the store' % sub)

    def test_fresh_homes_get_symlinks_not_copies(self):
        self.box.run('persist_cred .expo dir', 'persist_cred .npmrc file')
        for home in ('ubuntu', 'dev'):
            for sub in ('.expo', '.npmrc'):
                with self.subTest(home=home, sub=sub):
                    self.assertTrue(os.path.islink(self.box.link(home, sub)))

    def test_nested_subpaths_get_their_parent_created(self):
        self.box.run('persist_cred .config/gcloud dir')
        for home in ('ubuntu', 'dev'):
            link = self.box.link(home, '.config/gcloud')
            self.assertTrue(os.path.islink(link), link)
            self.assertEqual(os.path.realpath(link),
                             os.path.join(self.box.store, '.config', 'gcloud'))

    # ---- idempotency and the link-shaped edge cases -----------------------

    def test_repeated_boots_are_a_no_op(self):
        """Three boots: still a symlink, still one store, nothing moved."""
        call = 'persist_cred .ssh dir'
        self.box.write(os.path.join(self.box.store, '.ssh', 'id_ed25519'), 'k')
        for _ in range(3):
            self.box.run(call)
        for home in ('ubuntu', 'dev'):
            link = self.box.link(home, '.ssh')
            self.assertTrue(os.path.islink(link))
            self.assertEqual(os.path.realpath(link),
                             os.path.join(self.box.store, '.ssh'))
            self.assertEqual([], self.box.aside_paths(home, '.ssh'))
        self.assertEqual(['id_ed25519'],
                         os.listdir(os.path.join(self.box.store, '.ssh')))

    def test_an_existing_correct_link_is_not_treated_as_data(self):
        """`-L` has to come before `-e`, or a boot would 'migrate' the store
        into itself and then move the link aside."""
        self.box.write(os.path.join(self.box.store, '.ssh', 'id_ed25519'), 'k')
        os.symlink(os.path.join(self.box.store, '.ssh'),
                   self.box.link('dev', '.ssh'))
        self.box.run('persist_cred .ssh dir')
        self.assertEqual([], self.box.aside_paths('dev', '.ssh'))
        self.assertEqual(['id_ed25519'],
                         os.listdir(os.path.join(self.box.store, '.ssh')))

    def test_plain_ln_sf_nesting_does_not_happen(self):
        """`ln -sfn`, not `ln -sf`. With a link already in place, `ln -sf`
        resolves through it and creates <store>/.ssh/.ssh — the bug that
        leaves /home/ubuntu/.config/gh unpersisted to this day (#797)."""
        os.makedirs(os.path.join(self.box.store, '.ssh'))
        os.symlink(os.path.join(self.box.store, '.ssh'),
                   self.box.link('ubuntu', '.ssh'))
        self.box.run('persist_cred .ssh dir')
        self.assertFalse(
            os.path.exists(os.path.join(self.box.store, '.ssh', '.ssh')),
            'ln nested the link inside the credential store')

    def test_a_dangling_link_is_repointed(self):
        os.symlink(os.path.join(self.box.root, 'gone'),
                   self.box.link('dev', '.npmrc'))
        self.box.run('persist_cred .npmrc file')
        self.assertEqual(os.path.realpath(self.box.link('dev', '.npmrc')),
                         os.path.join(self.box.store, '.npmrc'))

    def test_a_link_into_the_other_home_is_repointed_at_the_store(self):
        os.makedirs(os.path.join(self.box.store, '.docker'))
        os.symlink(self.box.link('ubuntu', '.docker'),
                   self.box.link('dev', '.docker'))
        self.box.run('persist_cred .docker dir')
        self.assertEqual(os.path.realpath(self.box.link('dev', '.docker')),
                         os.path.join(self.box.store, '.docker'))

    def test_a_target_inside_its_own_link_is_refused(self):
        """The store is a subdirectory of /home/dev, so a wrong subpath could
        bury it under the link meant to reach it. Refuse and keep the data."""
        self.box.write(os.path.join(self.box.store, 'keep'), 'data')
        log = self.box.run('persist_cred .credentials dir .credentials/inner')
        self.assertIn('refusing', log)
        self.assertFalse(os.path.islink(self.box.store))
        self.assertEqual('data', self.box.read(
            os.path.join(self.box.store, 'keep')))

    # ---- migration: merge what is safe, never delete ----------------------

    def test_a_directory_only_the_pvc_home_has_is_merged_in(self):
        """/home/dev/.ssh really did hold a deploy key the managed store
        lacked. It has to end up in the store, and the original has to
        survive the boot that moved it."""
        self.box.write(os.path.join(self.box.store, '.ssh', 'id_ed25519'),
                       'managed')
        self.box.write(self.box.link('dev', '.ssh/id_ed25519'), 'shadowing')
        self.box.write(self.box.link('dev', '.ssh/bench_deploy'), 'unique-key')
        self.box.run('persist_cred .ssh dir')

        store_ssh = os.path.join(self.box.store, '.ssh')
        self.assertEqual('unique-key',
                         self.box.read(os.path.join(store_ssh, 'bench_deploy')))
        # no-clobber: the managed copy wins a name collision.
        self.assertEqual('managed',
                         self.box.read(os.path.join(store_ssh, 'id_ed25519')))
        # and the displaced directory is still there, under a new name.
        aside = self.box.aside_paths('dev', '.ssh')
        self.assertEqual(1, len(aside), aside)
        self.assertEqual('shadowing',
                         self.box.read(os.path.join(aside[0], 'id_ed25519')))
        self.assertTrue(os.path.islink(self.box.link('dev', '.ssh')))

    def test_a_configured_managed_file_wins_and_the_shadow_is_kept(self):
        """A .gitconfig is a unit, not a merge. Importing the shadowing copy
        is what would reinstate the #454 empty-`helper =` chain reset."""
        managed = os.path.join(self.box.store, '.config', 'git', 'config')
        self.box.write(managed, '[user]\n\temail = me@example.com\n')
        self.box.write(self.box.link('dev', '.gitconfig'),
                       '[credential "https://github.com"]\n\thelper = \n')
        self.box.run('persist_cred .gitconfig file .config/git/config')

        self.assertIn('me@example.com', self.box.read(managed))
        self.assertNotIn('helper', self.box.read(managed))
        aside = self.box.aside_paths('dev', '.gitconfig')
        self.assertEqual(1, len(aside), aside)
        self.assertIn('helper', self.box.read(aside[0]))
        self.assertEqual(os.path.realpath(self.box.link('dev', '.gitconfig')),
                         managed)

    def test_an_empty_managed_stub_adopts_the_real_login(self):
        """A zero-byte stub carries no information, so a real login found in
        either home is adopted onto the PVC rather than shadowed by nothing."""
        self.box.write(self.box.link('dev', '.npmrc'), '//reg/:_authToken=t\n')
        self.box.run('persist_cred .npmrc file')
        self.assertEqual('//reg/:_authToken=t\n',
                         self.box.read(os.path.join(self.box.store, '.npmrc')))
        self.assertEqual(1, len(self.box.aside_paths('dev', '.npmrc')))

    def test_nothing_is_ever_deleted_during_migration(self):
        """Rule one for a loop that now runs over the PVC. Every byte that was
        on disk before the boot is still reachable after it."""
        self.box.write(self.box.link('dev', '.docker/config.json'), '{"a":1}')
        self.box.write(self.box.link('dev', '.git-credentials'),
                       'https://x:ghs_stale@github.com\n')
        self.box.run('persist_cred .docker dir',
                     'persist_cred .git-credentials file')
        aside_dir = self.box.aside_paths('dev', '.docker')[0]
        aside_file = self.box.aside_paths('dev', '.git-credentials')[0]
        self.assertEqual('{"a":1}', self.box.read(
            os.path.join(aside_dir, 'config.json')))
        self.assertIn('ghs_stale', self.box.read(aside_file))

    def test_a_shadow_that_cannot_be_moved_is_left_alone(self):
        """If the rename fails, the honest outcome is to log and keep the
        user's data — not to force the link over it. `mv` is stubbed to fail
        because the real failure modes (a read-only home, an immovable mount)
        are not reproducible in a sandbox."""
        self.box.write(self.box.link('dev', '.expo/state.json'), '{}')
        stderr = self.box.run('persist_cred .expo dir',
                              preamble='mv() { return 1; }')
        self.assertIn('WARNING', stderr)
        self.assertFalse(os.path.islink(self.box.link('dev', '.expo')))
        self.assertEqual('{}', self.box.read(
            self.box.link('dev', '.expo/state.json')))
        # the ephemeral home is unaffected by the other home's problem
        self.assertTrue(os.path.islink(self.box.link('ubuntu', '.expo')))

    def test_a_colliding_backup_name_never_clobbers_an_older_backup(self):
        """Two migrations inside the same second (a restart loop, or two homes
        with the same subpath) must not rename one backup over the other."""
        self.box.write(self.box.link('dev', '.expo.pre795.7/older'), 'keep me')
        self.box.write(self.box.link('dev', '.expo/state.json'), '{}')
        self.box.run('persist_cred .expo dir', preamble='date() { echo 7; }')
        self.assertEqual('keep me', self.box.read(
            self.box.link('dev', '.expo.pre795.7/older')))
        new_aside = [p for p in self.box.aside_paths('dev', '.expo')
                     if not p.endswith('.pre795.7')]
        self.assertEqual(1, len(new_aside), self.box.aside_paths('dev', '.expo'))
        self.assertEqual('{}', self.box.read(
            os.path.join(new_aside[0], 'state.json')))

    def test_migration_happens_once_not_on_every_boot(self):
        self.box.write(self.box.link('dev', '.expo/state.json'), '{}')
        self.box.run('persist_cred .expo dir')
        self.box.run('persist_cred .expo dir')
        self.box.run('persist_cred .expo dir')
        self.assertEqual(1, len(self.box.aside_paths('dev', '.expo')))


class PersistCredSourceTestCase(unittest.TestCase):
    """Text-level guards for the properties execution cannot show."""

    def setUp(self):
        self.source = _persist_cred_source()
        self.start_sh = _start_sh()

    def test_loops_over_both_homes(self):
        self.assertIn('for HOME_DIR in /home/ubuntu /home/dev; do',
                      self.source)

    def test_contains_no_recursive_delete(self):
        """#795's own headline symptom was a destructive `rm -rf`. This
        function now runs over PVC paths, so it must not contain one at all —
        displaced data is renamed aside instead."""
        for forbidden in ('rm -rf', 'rm -r ', 'rm -f '):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.source)

    def test_moves_displaced_data_aside_with_a_timestamp(self):
        self.assertIn('aside="$link.pre795.$(date +%s)"', self.source)
        self.assertIn('mv -T "$link" "$aside"', self.source)

    def test_ssh_and_gitconfig_are_persisted_from_both_homes(self):
        self.assertRegex(self.start_sh, r'persist_cred \.ssh\s+dir')
        self.assertRegex(
            self.start_sh,
            r'persist_cred \.gitconfig\s+file \.config/git/config')

    def test_credential_links_do_not_go_through_a_bare_tilde(self):
        """`~` resolves to a different home per context, so a credential
        symlink written through it can land on the PVC (#793/#795). The last
        one — gh's — was replaced by link_cred_dir's spelled-out calls (#797),
        so there are now none at all."""
        tilde_links = [line for line in self.start_sh.splitlines()
                       if line.startswith('ln -s') and '~' in line]
        self.assertEqual([], tilde_links)


if __name__ == '__main__':
    unittest.main()
