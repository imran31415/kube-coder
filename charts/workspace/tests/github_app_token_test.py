"""Behavioural tests for the GitHub App token script (issue #558).

The script ships inside a Helm ConfigMap, so `helm unittest` can only assert on
its *text*. The container split this issue introduced is a runtime protocol —
the sidecar mints, the ide container asks and waits — and the failure mode of
getting it wrong is a boot with no git credential, which no regex catches. So
load the script the way the pod does and exercise it against a temp dir.

Extraction is dependency-free (CI's python job installs nothing): the template's
only Go templating is the outer `{{- if }}` / `{{- end }}` guard, and the script
is a plain YAML block scalar under `github-app-token.py: |`.
"""

import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

TEMPLATE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "templates", "github-app-token-refresh.yaml",
)

# Paths the script hardcodes at import time; every one is rebound onto the
# sandbox so a test can never touch the real workspace credentials.
PATH_CONSTANTS = (
    "TOKEN_FILE", "ENV_FILE", "AUTH_MODE_FILE", "GIT_STORE_FILE",
    "REQUEST_FILE", "ACK_FILE", "PROFILE_HOOK",
)


def extract_script():
    """Pull `github-app-token.py` out of the ConfigMap template."""
    with open(TEMPLATE) as f:
        lines = f.read().splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if ln.strip() == "github-app-token.py: |")
    body = []
    for ln in lines[start + 1:]:
        if ln.strip() and not ln.startswith("    "):
            break                      # the closing {{- end }}
        body.append(ln[4:])
    return "\n".join(body) + "\n"


def load_module(sandbox):
    """Exec the script as a module with its credential paths in `sandbox`."""
    mod = types.ModuleType("github_app_token")
    exec(compile(extract_script(), "github-app-token.py", "exec"), mod.__dict__)
    mod.CREDENTIALS_DIR = sandbox
    for name in PATH_CONSTANTS:
        mod.__dict__[name] = os.path.join(
            sandbox, os.path.basename(getattr(mod, name)))
    # Keep the loops fast; every test that spins one sets its own budget too.
    mod.POLL_INTERVAL = 0.02
    return mod


class TokenScriptTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mod = load_module(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def stub_mint(self, token="ghs_stub"):
        """Replace the two network/openssl calls with fakes."""
        self.mod.generate_jwt = lambda app_id, key: "jwt-for-%s" % app_id
        self.mod.get_installation_token = lambda jwt, inst: token

    def read(self, const):
        with open(getattr(self.mod, const)) as f:
            return f.read()


class MintingTests(TokenScriptTestCase):
    """The sidecar's half: mint, write files, touch nothing $HOME-relative."""

    def test_refresh_writes_the_files_the_ide_container_reads(self):
        self.stub_mint("ghs_minted")
        self.mod.refresh_token("app1", "inst1", "KEY")

        self.assertEqual(self.read("TOKEN_FILE"), "ghs_minted")
        self.assertIn('export GH_TOKEN="ghs_minted"', self.read("ENV_FILE"))
        self.assertIn('export GITHUB_TOKEN="ghs_minted"', self.read("ENV_FILE"))
        # The per-shell hook stays mode-guarded (#256).
        hook = self.read("PROFILE_HOOK")
        self.assertIn(".github-auth-mode", hook)
        self.assertIn('export GH_TOKEN="ghs_minted"', hook)
        self.assertEqual(os.stat(getattr(self.mod, "TOKEN_FILE")).st_mode & 0o777,
                         0o600)

    def test_refresh_never_shells_out(self):
        """Minting must not run git/gh: those resolve against $HOME, and the
        sidecar has its own ephemeral one. Configuring git is the ide's job."""
        self.stub_mint()
        with mock.patch.object(self.mod.subprocess, "run") as run:
            self.mod.refresh_token("app1", "inst1", "KEY")
        run.assert_not_called()


class AtomicWriteTests(TokenScriptTestCase):
    """Nobody may ever observe one of these files half-written.

    Every one of them is read by a *different* process while this daemon
    rewrites it: TOKEN_FILE by the git credential helper on every git
    operation, PROFILE_HOOK by every new shell, GIT_STORE_FILE by git itself.
    `open(path, "w")` truncates on entry, so all of them had a window in which
    they existed and were empty — which is how two unrelated contributor PRs
    ended up red with `'' != 'ghs_boot'`.
    """

    def test_a_reader_never_sees_an_empty_file_mid_write(self):
        """The regression, reproduced as a race the old code loses.

        The reader uses exactly the condition a credential helper does: the
        path exists, so read it. With truncate-then-write it observes ''.
        """
        target = os.path.join(self.tmp.name, "raced")
        with open(target, "w") as f:
            f.write("old-token")

        seen = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    with open(target) as f:
                        seen.append(f.read())
                except OSError:
                    pass

        th = threading.Thread(target=reader, daemon=True)
        th.start()
        try:
            for i in range(200):
                self.mod.atomic_write(target, f"token-{i}")
        finally:
            stop.set()
            th.join(timeout=5)

        self.assertTrue(seen, "reader never managed a read")
        # Every observation must be a WHOLE value — never '' and never a prefix.
        bad = [v for v in seen if not (v == "old-token" or
                                       (v.startswith("token-") and v[6:].isdigit()))]
        self.assertEqual(bad, [], f"torn or empty reads: {bad[:5]}")

    def test_the_secret_is_never_briefly_world_readable(self):
        """Permissions are set at creation, not chmod-ed after the write.

        The old order wrote the token first and fixed the mode second, so under
        the usual umask the file sat at 0644 with a live credential in it.
        """
        target = os.path.join(self.tmp.name, "secret")
        modes = []
        stop = threading.Event()

        def watcher():
            while not stop.is_set():
                try:
                    modes.append(os.stat(target).st_mode & 0o777)
                except OSError:
                    pass

        th = threading.Thread(target=watcher, daemon=True)
        th.start()
        try:
            for i in range(200):
                self.mod.atomic_write(target, f"ghs_{i}")
        finally:
            stop.set()
            th.join(timeout=5)

        self.assertTrue(modes, "watcher never saw the file")
        self.assertEqual([m for m in modes if m != 0o600], [],
                         f"observed non-0600 modes: {sorted(set(modes))}")

    def test_a_failed_write_leaves_no_temp_file_behind(self):
        target = os.path.join(self.tmp.name, "boom")
        real_replace = os.replace

        def failing_replace(a, b):
            raise OSError("disk gone")

        os.replace = failing_replace
        try:
            with self.assertRaises(OSError):
                self.mod.atomic_write(target, "never-lands")
        finally:
            os.replace = real_replace

        leftovers = [n for n in os.listdir(self.tmp.name) if ".tmp-" in n]
        self.assertEqual(leftovers, [], f"temp files left: {leftovers}")

    def test_the_old_value_survives_a_failed_write(self):
        """Truncate-then-write destroys the old file before it knows the new
        one is good. GIT_STORE_FILE holds other hosts' credentials."""
        target = os.path.join(self.tmp.name, "store")
        with open(target, "w") as f:
            f.write("https://other-host.example\n")

        real_replace = os.replace
        os.replace = lambda a, b: (_ for _ in ()).throw(OSError("disk gone"))
        try:
            with self.assertRaises(OSError):
                self.mod.atomic_write(target, "replacement")
        finally:
            os.replace = real_replace

        with open(target) as f:
            self.assertEqual(f.read(), "https://other-host.example\n")


class HandshakeTests(TokenScriptTestCase):
    """The cross-container protocol that replaces the synchronous `--once`."""

    def run_serve(self, budget=5.0):
        """Run the sidecar loop in a daemon thread for the test's duration."""
        stop = threading.Event()
        real_sleep = time.sleep

        def loop():
            # serve() is an infinite loop by design; run it until the test ends.
            def sleep(n):
                if stop.is_set():
                    raise SystemExit
                real_sleep(n)
            self.mod.time = types.SimpleNamespace(
                sleep=sleep, time=time.time)
            try:
                self.mod.serve("app1", "inst1", "KEY")
            except SystemExit:
                pass

        t = threading.Thread(target=loop, daemon=True)
        t.start()
        self.addCleanup(lambda: (stop.set(), t.join(timeout=budget)))
        return t

    def test_requester_gets_a_fresh_token_and_an_ack(self):
        self.stub_mint("ghs_minted")
        self.run_serve()

        self.assertTrue(self.mod.request_refresh(timeout=10))
        self.assertEqual(self.read("TOKEN_FILE"), "ghs_minted")
        # The request is consumed, so it cannot re-trigger forever.
        self.assertFalse(os.path.exists(self.mod.REQUEST_FILE))

    def test_serve_mints_on_entry_without_being_asked(self):
        """The sidecar must produce a token at boot even if nobody requests
        one — a warm pod's PVC token may already be expired.

        Waits for CONTENT, not for the path to exist. Waiting on existence and
        then asserting on content is a race the test loses roughly one run in
        a few hundred, and it failed that way on two unrelated contributor PRs
        (#634, #635) with `'' != 'ghs_boot'`. The write is atomic now, so the
        file cannot be seen empty — but a test that only passes because of a
        guarantee somewhere else should not be phrased as though it is racing.
        """
        self.stub_mint("ghs_boot")
        self.run_serve()
        self.assertEqual(self._await_content("TOKEN_FILE"), "ghs_boot")

    def _await_content(self, const, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                value = self.read(const)
            except OSError:
                value = ""
            if value:
                return value
            time.sleep(0.02)
        self.fail(f"{const} still had no content after {timeout}s")

    def test_request_times_out_loudly_when_no_sidecar_answers(self):
        """No sidecar running: bounded wait, False, and the caller decides.
        start.sh turns this into a WARNING rather than a silent boot."""
        started = time.time()
        self.assertFalse(self.mod.request_refresh(timeout=1))
        self.assertLess(time.time() - started, 5)

    def test_a_stale_ack_does_not_satisfy_a_new_request(self):
        with open(self.mod.ACK_FILE, "w") as f:
            f.write("some-older-nonce\n")
        self.assertFalse(self.mod.request_refresh(timeout=1))

    def test_take_request_claims_the_nonce_exactly_once(self):
        with open(self.mod.REQUEST_FILE, "w") as f:
            f.write("nonce-1\n")
        self.assertEqual(self.mod.take_request(), "nonce-1")
        self.assertIsNone(self.mod.take_request())

    def test_failed_refresh_retries_well_before_the_full_interval(self):
        """A mint that fails at the 50-minute mark must not wait another 50:
        the live token expires in 60."""
        calls = []

        def boom(*a):
            calls.append(a)
            raise RuntimeError("GitHub is down")

        self.mod.refresh_token = boom
        self.mod.REFRESH_INTERVAL = 10_000
        self.mod.RETRY_INTERVAL = 0.05
        self.run_serve()
        with open(self.mod.REQUEST_FILE, "w") as f:
            f.write("kick\n")

        deadline = time.time() + 3
        while len(calls) < 3 and time.time() < deadline:
            time.sleep(0.02)
        # Only the first attempt was requested; the rest are the retry backoff.
        self.assertGreaterEqual(len(calls), 3)


class GitConfigTests(TokenScriptTestCase):
    """The ide container's half: no key needed, purely $HOME-relative work."""

    def git_calls(self, mode):
        with open(self.mod.AUTH_MODE_FILE, "w") as f:
            f.write(mode + "\n")
        with mock.patch.object(self.mod.subprocess, "run") as run:
            self.mod.configure_git(self.mod.get_auth_mode())
        return [list(c.args[0]) for c in run.call_args_list]

    def test_app_mode_owns_the_github_com_helper_chain(self):
        calls = self.git_calls("app")
        key = self.mod.HOST_HELPER_KEY
        # reset the host-scoped chain, then append the live-token reader (#454)
        self.assertIn(["git", "config", "--global", "--replace-all", key, ""],
                      calls)
        added = [c for c in calls if c[3:5] == ["--add", key]]
        self.assertTrue(added)
        self.assertIn("/home/dev/.credentials/.github-token", added[0][-1])

    def test_personal_mode_withdraws_only_the_app_helper(self):
        calls = self.git_calls("personal")
        key = self.mod.HOST_HELPER_KEY
        self.assertIn(
            ["git", "config", "--global", "--unset-all", key, "github-token"],
            calls)
        self.assertIn(["gh", "auth", "setup-git", "--hostname", "github.com"],
                      calls)
        # It must NOT install the App reader over a personal login.
        self.assertFalse([c for c in calls if "--add" in c])

    def test_configure_git_needs_no_private_key(self):
        """The whole point of the split: this runs where the key is absent."""
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(self.mod.subprocess, "run"):
                self.mod.configure_git("app")


class EntryPointTests(TokenScriptTestCase):
    """`--once` stays a working re-mint command from the ide container."""

    def main(self, argv_mode, env):
        with mock.patch.object(sys, "argv", ["github-app-token.py", argv_mode]):
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(SystemExit) as ctx:
                    self.mod.main()
        return ctx.exception.code

    def test_once_without_a_key_delegates_to_the_sidecar(self):
        seen = []
        self.mod.request_refresh = lambda *a, **k: seen.append(True) or True
        with mock.patch.object(self.mod.subprocess, "run"):
            code = self.main("--once", {"GITHUB_APP_ID": "1",
                                        "GITHUB_APP_INSTALLATION_ID": "2"})
        self.assertEqual(seen, [True])
        self.assertEqual(code, 0)

    def test_once_also_reasserts_git_config(self):
        """`--once` had two documented uses — re-mint after a permissions
        change, and repair the helper chain `gh auth setup-git` clobbered
        (#454). Minting used to do both; keep it that way."""
        self.mod.request_refresh = lambda *a, **k: True
        with mock.patch.object(self.mod.subprocess, "run") as run:
            self.main("--once", {"GITHUB_APP_ID": "1",
                                 "GITHUB_APP_INSTALLATION_ID": "2"})
        calls = [list(c.args[0]) for c in run.call_args_list]
        self.assertTrue([c for c in calls
                         if c[:2] == ["git", "config"]
                         and self.mod.HOST_HELPER_KEY in c])

    def test_once_reports_failure_when_the_sidecar_never_answers(self):
        self.mod.request_refresh = lambda *a, **k: False
        with mock.patch.object(self.mod.subprocess, "run"):
            code = self.main("--once", {"GITHUB_APP_ID": "1",
                                        "GITHUB_APP_INSTALLATION_ID": "2"})
        self.assertEqual(code, 1)

    def test_daemon_without_a_key_fails_loudly(self):
        """--daemon belongs to the sidecar; it must never silently no-op in a
        container that cannot mint."""
        code = self.main("--daemon", {"GITHUB_APP_ID": "1",
                                      "GITHUB_APP_INSTALLATION_ID": "2"})
        self.assertEqual(code, 1)

    def test_unknown_mode_is_rejected(self):
        self.assertEqual(self.main("--nope", {}), 1)


if __name__ == "__main__":
    unittest.main()
