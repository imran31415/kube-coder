"""kc-open-url: the workspace's $BROWSER (issue #763).

The pod has no xdg-open / x-www-browser and no $BROWSER, so `gh auth login
--web` (and every other CLI that opens a login page) failed with a red
"Failed opening a web browser … executable file not found". start.sh now
writes kc-open-url, which prints the link and exits 0, and seeds the rc
files so an UNSET $BROWSER points at it.

These tests run the real script and rc block pulled out of start.sh under
bash, so they check behaviour, not just that the text is present (the
helm-unittest suite in rc_path_bootstrap_test.yaml covers the wiring).
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
INSTALL_PATH = "/home/dev/.local/bin/kc-open-url"
BASH = shutil.which("bash")


def _start_sh():
    with open(START_SH, encoding="utf-8") as f:
        return f.read()


def _script():
    m = re.search(r"<<'OPENURL'\n(.*?)\nOPENURL\n", _start_sh(), re.S)
    assert m, "kc-open-url heredoc not found in start.sh"
    return m.group(1) + "\n"


def _rc_block():
    m = re.search(r"bootstrap_rc 'kc-open-url' '(.*?)'\n", _start_sh(), re.S)
    assert m, "kc-open-url bootstrap_rc block not found in start.sh"
    return m.group(1)


@unittest.skipUnless(BASH, "bash not available")
class KcOpenUrlTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.script = os.path.join(self.tmp, "kc-open-url")
        with open(self.script, "w", encoding="utf-8", newline="\n") as f:
            f.write(_script())
        os.chmod(self.script, 0o755)

    def _run(self, *args):
        return subprocess.run([BASH, self.script, *args],
                              capture_output=True, text=True, timeout=10)

    def test_prints_the_url_and_succeeds(self):
        # gh treats a non-zero exit as "Failed opening a web browser", so the
        # whole point is exit 0 with the link visible.
        r = self._run("https://github.com/login/device")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("https://github.com/login/device", r.stderr)
        self.assertIn("Open this link on your computer", r.stderr)

    def test_keeps_stdout_clean(self):
        # A caller capturing stdout (e.g. `x=$($BROWSER url)`) gets nothing.
        self.assertEqual(self._run("https://example.com").stdout, "")

    def test_prints_every_url_on_its_own_line(self):
        r = self._run("https://a.example", "https://b.example")
        self.assertEqual(r.returncode, 0)
        lines = [l.strip() for l in r.stderr.splitlines()]
        self.assertIn("https://a.example", lines)
        self.assertIn("https://b.example", lines)

    def test_no_url_is_a_usage_error(self):
        r = self._run()
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage", r.stderr)


@unittest.skipUnless(BASH, "bash not available")
class BrowserRcBlockTest(unittest.TestCase):
    """The rc block, sourced the way .profile/.bashrc would source it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fake = os.path.join(self.tmp, "kc-open-url")

    def _browser_after_rc(self, preset=None, installed=True):
        if installed:
            with open(self.fake, "w", newline="\n") as f:
                f.write("#!/bin/sh\n")
            os.chmod(self.fake, 0o755)
        block = _rc_block().replace(INSTALL_PATH, self.fake)
        env = {k: v for k, v in os.environ.items() if k != "BROWSER"}
        if preset is not None:
            env["BROWSER"] = preset
        # Source it twice: .profile chains to .bashrc, so it really runs twice.
        prog = block + "\n" + block + '\nprintf %s "${BROWSER:-}"'
        r = subprocess.run([BASH, "-c", prog], env=env,
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_sets_browser_when_unset(self):
        self.assertEqual(self._browser_after_rc(), self.fake)

    def test_keeps_an_existing_browser(self):
        # code-server exports its own helper into its terminals; that one
        # opens the link in the user's real browser and must win.
        helper = "/usr/lib/code-server/lib/vscode/bin/helpers/browser.sh"
        self.assertEqual(self._browser_after_rc(preset=helper), helper)

    def test_noop_when_the_script_is_missing(self):
        # The SSH sidecar seeds the same block before the ide container may
        # have written the script; a dangling $BROWSER would recreate the bug.
        self.assertEqual(self._browser_after_rc(installed=False), "")


if __name__ == "__main__":
    unittest.main()
