"""Drift guard for what the shipped docs say about $HOME and PATH (#793).

claude-md.txt is copied into every workspace as CLAUDE.md and auto-loaded
into every agent's context, so a wrong environment fact there is not a
cosmetic bug — agents act on it. Two claims had drifted out of true:

  * "$HOME is /home/ubuntu" — true for PID 1, terminals and sshd, but NOT
    for the CLIs server.py spawns (agent tasks, hooks, the hypervisor),
    which are forced to HOME=/home/dev so they find their config on the
    PVC; tmux then hands that environment to every session it creates
    later. Following the doc's advice — rm -rf "$HOME/<path>" and symlink
    it at /home/dev/<path> — deleted ~1GB of freshly extracted data from
    the PVC and left a self-referential symlink.
    (#815 refined this: only hooks and the Hypervisor are forced. Builds
    and sub-agents are tmux sessions and inherit the tmux server's HOME,
    normally /home/ubuntu, so "agent tasks are forced to /home/dev" was
    wrong too, and it hid that each home loads its own .claude.json.)
  * "/home/dev/.local/bin is already on PATH in every shell" — it comes
    from an rc block, so only *login* shells get it. Scripts, `bash -c`
    and anything server.py execs directly read no rc file.

These tests assert the corrected framing survives later edits. They check
for the absence of the specific stale sentences rather than grepping for
"$HOME", so ordinary prose about the variable stays legal.

Run with:
    cd charts/workspace && python3 -m unittest tests.home_path_docs_test
"""

import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(WORKSPACE))

CLAUDE_MD = os.path.join(WORKSPACE, 'claude-md.txt')
START_SH = os.path.join(WORKSPACE, 'start.sh')
FILES_DOC = os.path.join(REPO, 'docs', 'in-app', 'files.md')

# Sentences that were wrong. Any of them coming back means the doc is
# telling agents something the pod does not do.
STALE_CLAIMS = (
    '`$HOME` is `/home/ubuntu`, which is **ephemeral**',
    'it is already on `PATH` in every shell',
    'Already on `PATH` in every shell',
    # #815: Builds are NOT forced to /home/dev; they inherit tmux's HOME.
    'agent tasks, hooks, the hypervisor — which are forced there',
    '`/home/dev` for the CLIs the dashboard spawns (agent tasks, hooks)',
)


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


class ClaudeMdHomeFactsTestCase(unittest.TestCase):

    def setUp(self):
        self.text = _read(CLAUDE_MD)

    def test_does_not_restate_the_wrong_claims(self):
        for claim in STALE_CLAIMS:
            with self.subTest(claim=claim):
                self.assertNotIn(claim, self.text)

    def test_says_home_varies_and_names_both_values(self):
        self.assertIn('`$HOME` varies by context', self.text)
        self.assertIn('/home/ubuntu', self.text)
        self.assertIn('/home/dev', self.text)

    def test_says_agent_tasks_inherit_tmux_home(self):
        """#815: a task's HOME comes from tmux, not from the server."""
        self.assertIn('**Agent tasks are not forced:**', self.text)
        self.assertIn('tmux show-environment -g HOME', self.text)

    def test_says_mcp_servers_differ_by_home(self):
        """#815: `claude mcp add` in one home was invisible to a task in the
        other, and lost on restart. The doc points at the registry."""
        self.assertIn('Each home has its own `.claude.json`', self.text)
        self.assertIn('Settings → MCP servers', self.text)
        self.assertIn('written into both homes', self.text)

    def test_warns_against_destructive_use_of_home(self):
        """The specific shape that cost a user ~1GB."""
        self.assertIn('rm -rf "$HOME', self.text)
        self.assertIn('issue #793', self.text)

    def test_distinguishes_login_from_non_login_shells_for_path(self):
        self.assertIn('Non-login shells do not read any rc file', self.text)

    def test_credential_symlinks_are_spelled_out_not_tilde(self):
        """`~/.ssh` is a *different* directory when $HOME is /home/dev, so
        the Storage table has to name /home/ubuntu explicitly."""
        for line in ('- **`/home/ubuntu/.ssh`**',
                     '- **`/home/ubuntu/.gitconfig`**',
                     '- **`/home/ubuntu/.config/gh`**'):
            with self.subTest(line=line):
                self.assertIn(line, self.text)

    def test_probe_recipe_does_not_hardcode_one_home(self):
        """The old verify-your-PATH recipe pinned HOME=/home/ubuntu, which
        is exactly the assumption under test."""
        self.assertIn("env -i HOME=/home/ubuntu TERM=xterm bash -lc", self.text)
        self.assertIn('HOME=/home/dev', self.text)


class FilesDocHomeFactsTestCase(unittest.TestCase):

    def setUp(self):
        self.text = _read(FILES_DOC)

    def test_does_not_restate_the_wrong_claims(self):
        for claim in STALE_CLAIMS:
            with self.subTest(claim=claim):
                self.assertNotIn(claim, self.text)

    def test_table_has_a_row_for_the_varying_home(self):
        self.assertIn('| `~`, `$HOME` |', self.text)


class StartShBootstrapTestCase(unittest.TestCase):
    """The doc claim "login shells get it on PATH" is only true because
    bootstrap_rc seeds both homes. Asserted here too (not just in the Helm
    suite) so the doc and the code that backs it fail together."""

    def setUp(self):
        self.text = _read(START_SH)

    def test_bootstrap_rc_seeds_both_homes(self):
        self.assertIn('for HOME_DIR in /home/ubuntu /home/dev; do', self.text)

    def test_blocks_use_absolute_paths_not_home(self):
        """A $HOME inside a seeded block would resolve to a different
        directory in each home — the bug this issue is about."""
        self.assertNotIn('export PATH="$HOME/.local/bin', self.text)


if __name__ == '__main__':
    unittest.main()
