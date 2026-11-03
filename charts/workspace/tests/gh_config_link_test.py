"""~/.config/gh is a symlink, not a directory containing one (issue #797).

`gh auth login` did not survive a pod restart. start.sh pre-created
`~/.config/gh` with `mkdir -p` and then tried to link over it seven lines
later with `ln -sf` — and when ln's final argument already resolves to a
directory it treats it as a destination *directory* and drops the link
inside it. Exit 0, no diagnostic, and the real `gh` config stayed in the
ephemeral /home/ubuntu, wiped from skel on every boot.

These tests run the real `link_cred_dir` helper pulled out of start.sh under
bash against fake homes in /tmp, so they check behaviour rather than text
(the helm-unittest suite in credential_persist_test.yaml covers the wiring).
Nothing here touches a real credential path: every HOME and PVC path is a
tempdir, and chown is stubbed out.

The three states a boot has to cope with:

  fresh     — nothing at the link path (new workspace)
  broken    — a real directory holding the useless nested `gh/gh` link
              (every workspace that ever ran the buggy start.sh)
  correct   — the symlink already in place (second boot after the fix)

plus the one that costs a user something if it goes wrong: a real directory
with a live login in it, which must be migrated onto the PVC and parked,
never deleted.

Run with:
    cd charts/workspace && python3 -m unittest tests.gh_config_link_test
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

START_SH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "start.sh",
)
BASH = shutil.which("bash")


def _start_sh():
    with open(START_SH, encoding="utf-8") as f:
        return f.read()


def _helper():
    """The link_cred_dir function body, verbatim from start.sh."""
    m = re.search(r"^link_cred_dir\(\) \{.*?^\}$", _start_sh(), re.S | re.M)
    assert m, "link_cred_dir not found in start.sh"
    return m.group(0)


@unittest.skipUnless(BASH, "bash not available")
class LinkCredDirTest(unittest.TestCase):
    """Behaviour of the helper, driven through bash exactly as boot does."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # Fake ephemeral home and fake PVC credential store.
        self.home = os.path.join(self.tmp, "home", "ubuntu")
        self.target = os.path.join(self.tmp, "dev", ".credentials", ".config", "gh")
        self.link = os.path.join(self.home, ".config", "gh")
        os.makedirs(os.path.join(self.home, ".config"))
        os.makedirs(self.target)

    # -- helpers ----------------------------------------------------------

    def _run(self, times=1):
        """Run the extracted helper `times` times in one bash process."""
        script = "\n".join([
            "set -o pipefail",
            'log_stage() { echo "[stage] $*" >&2; }',
            "chown() { :; }",          # boot runs as root; the sandbox does not
            _helper(),
            *['link_cred_dir "$1" "$2"' for _ in range(times)],
        ])
        return subprocess.run([BASH, "-c", script, "bash", self.link, self.target],
                              capture_output=True, text=True, timeout=30)

    def _write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def _read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _parked(self):
        parent = os.path.dirname(self.link)
        return sorted(n for n in os.listdir(parent) if n.startswith("gh.pre-797."))

    def assertLinked(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.islink(self.link),
                        "%s is not a symlink (it is %s)" % (
                            self.link,
                            "a directory" if os.path.isdir(self.link) else "missing"))
        self.assertEqual(os.path.realpath(self.link), os.path.realpath(self.target))

    # -- the three states -------------------------------------------------

    def test_fresh_boot_creates_the_link(self):
        self.assertLinked(self._run())

    def test_broken_state_is_repaired(self):
        """A real directory holding the nested `gh/gh` link from the bug."""
        os.makedirs(self.link)
        os.symlink(self.target, os.path.join(self.link, "gh"))
        self.assertLinked(self._run())
        # The stray must not be dragged into the PVC as a link to its own
        # parent — that is how the bug propagates onto persistent storage.
        self.assertEqual(os.listdir(self.target), [])

    def test_existing_correct_link_stays_correct(self):
        os.symlink(self.target, self.link)
        self.assertLinked(self._run())
        self.assertEqual(self._parked(), [], "nothing to migrate, nothing parked")

    def test_double_boot_is_idempotent(self):
        """Two runs in a row — the re-run case `ln -sf` gets wrong by
        following the existing symlink into the target and writing a
        self-referential link inside the PVC."""
        proc = self._run(times=2)
        self.assertLinked(proc)
        self.assertEqual(os.listdir(self.target), [])

    # -- the data path ----------------------------------------------------

    def test_real_login_is_migrated_onto_the_pvc(self):
        self._write(os.path.join(self.link, "hosts.yml"), "github.com:\n  user: me\n")
        self.assertLinked(self._run())
        self.assertIn("user: me", self._read(os.path.join(self.target, "hosts.yml")))

    def test_original_directory_is_parked_not_deleted(self):
        self._write(os.path.join(self.link, "hosts.yml"), "ephemeral\n")
        proc = self._run()
        self.assertLinked(proc)
        parked = self._parked()
        self.assertEqual(len(parked), 1, "expected exactly one parked copy")
        kept = os.path.join(os.path.dirname(self.link), parked[0], "hosts.yml")
        self.assertEqual(self._read(kept), "ephemeral\n")
        self.assertIn("parked at", proc.stderr)

    def test_pvc_login_is_never_clobbered(self):
        """cp -an: a login already on the PVC outranks the ephemeral copy."""
        self._write(os.path.join(self.target, "hosts.yml"), "pvc\n")
        self._write(os.path.join(self.link, "hosts.yml"), "ephemeral\n")
        self.assertLinked(self._run())
        self.assertEqual(self._read(os.path.join(self.target, "hosts.yml")), "pvc\n")

    def test_writes_through_the_link_land_on_the_pvc(self):
        """The whole point: `gh auth login` after boot writes to the PVC."""
        self.assertLinked(self._run())
        self._write(os.path.join(self.link, "hosts.yml"), "after login\n")
        self.assertEqual(self._read(os.path.join(self.target, "hosts.yml")),
                         "after login\n")

    def test_refuses_to_link_rather_than_nest_when_the_dir_cannot_move(self):
        """If the salvage fails the helper must not fall through to a bare
        ln, which would silently nest again. Read-only parent => mv fails."""
        os.makedirs(self.link)
        parent = os.path.dirname(self.link)
        os.chmod(parent, 0o500)
        self.addCleanup(os.chmod, parent, 0o700)
        proc = self._run()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("WARNING", proc.stderr)
        self.assertFalse(os.path.islink(self.link))
        self.assertEqual(os.listdir(self.link), [], "no nested link created")


@unittest.skipUnless(BASH, "bash not available")
class PlainLnNestsTest(unittest.TestCase):
    """Why the helper exists at all. These assert the ln(1) behaviour the
    old two lines relied on — if a future coreutils ever stops nesting,
    these fail and the helper can be reconsidered."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.target = os.path.join(self.tmp, "target")
        self.link = os.path.join(self.tmp, "gh")
        os.makedirs(self.target)

    def _ln(self, *flags):
        return subprocess.run(["ln", *flags, self.target, self.link],
                              capture_output=True, text=True, timeout=30)

    def test_ln_sf_nests_inside_an_existing_directory(self):
        os.makedirs(self.link)
        proc = self._ln("-sf")
        self.assertEqual(proc.returncode, 0, "it does not even fail")
        self.assertFalse(os.path.islink(self.link))
        self.assertEqual(os.listdir(self.link), ["target"])

    def test_n_alone_does_not_help_against_a_real_directory(self):
        """-n only un-dereferences a *symlink* to a directory, which is why
        `mkdir -p` + `ln -sfn` would still have been broken."""
        os.makedirs(self.link)
        proc = self._ln("-sfn")
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(os.path.islink(self.link))
        self.assertEqual(os.listdir(self.link), ["target"])

    def test_ln_sf_onto_an_existing_symlink_writes_into_the_target(self):
        """The re-run hazard: without -n, ln follows the link it is meant to
        replace and litters the PVC with a link pointing at its own parent."""
        os.symlink(self.target, self.link)
        self.assertEqual(self._ln("-sf").returncode, 0)
        self.assertEqual(os.listdir(self.target), ["target"])

    def test_ln_sfn_replaces_an_existing_symlink_in_place(self):
        os.symlink(self.target, self.link)
        self.assertEqual(self._ln("-sfn").returncode, 0)
        self.assertTrue(os.path.islink(self.link))
        self.assertEqual(os.listdir(self.target), [])


if __name__ == "__main__":
    unittest.main()
