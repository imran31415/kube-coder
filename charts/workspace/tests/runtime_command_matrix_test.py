"""Golden characterization matrix for every runtime's launch command (#604).

This is the SAFETY NET the runtime-catalog refactor is built on, and it is
deliberately written before the catalog lands: the catalog is only worth having
if it provably changes nothing, and "provably" means an exact-string assertion
for every runtime in every mode, not a spot check.

Four modes are pinned per runtime — the full cross-product of the two command
builders that exist today:

    interactive       server.assistant_command(a)                 Build tab REPL
    interactive_auto  server.assistant_command(a, auto_approve=1) Hypervisor REPL
    orch_interactive  orchestrator _assistant_command(headless=0) attachable sub-agent
    orch_headless     orchestrator _assistant_command(headless=1) one-shot sub-agent

Two builders reconstructing the same knowledge is the bug #604 is about, so the
matrix records BOTH sides even where they disagree. Where they do, the golden
value below is the CURRENT behaviour with the divergence called out in a
comment — this file's job is to freeze what is, so a refactor can be shown to
preserve it. Fixing a divergence is a separate, deliberate diff that edits an
entry here and says why.

Env is scrubbed to the pod-default state (no KC_*/provider keys) so the golden
strings are the out-of-the-box commands, and the two Claude CLI capability
probes are pinned off — they shell out to `claude --help`, which is neither
present nor deterministic in CI.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402
import mcp_agent_orchestrator as orch  # noqa: E402
import runtimes  # noqa: E402

CTM = server.ClaudeTaskManager

_LIBREFANG_DAEMON = (
    'librefang status -q >/dev/null 2>&1 || { '
    'librefang start >/dev/null 2>&1 || true; '
    'for _ in 1 2 3 4 5 6 7 8 9 10; do '
    'librefang status -q >/dev/null 2>&1 && break; sleep 1; '
    'done; }; '
)

# runtime id -> mode -> exact command string.
GOLDEN = {
    'ante': {
        'interactive': 'ante',
        'interactive_auto': 'ante --yolo',
        'orch_interactive': 'ante',
        'orch_headless': "ante --yolo -p 'hi there'",
    },
    'antigravity': {
        # No KC_ANTIGRAVITY_MODEL set => no --model flag at all (agy picks).
        'interactive': 'agy',
        'interactive_auto': 'agy --dangerously-skip-permissions',
        'orch_interactive': 'agy',
        'orch_headless': "agy --dangerously-skip-permissions -p 'hi there'",
    },
    'claude': {
        'interactive': 'claude',
        'interactive_auto': 'claude --dangerously-skip-permissions',
        'orch_interactive': 'claude',
        'orch_headless': "claude --dangerously-skip-permissions -p 'hi there'",
    },
    'codex': {
        # DIVERGENCE: only the server side threads reasoning effort through
        # (_EFFORT_DELIVERY -> `-c model_reasoning_effort=<level>`); the
        # orchestrator has no effort plumbing, so a codex sub-agent runs at the
        # CLI's own default rather than the workspace's configured effort.
        'interactive': 'codex -c model_reasoning_effort=high',
        'interactive_auto': ('codex --dangerously-bypass-approvals-and-sandbox '
                             '-c model_reasoning_effort=high'),
        'orch_interactive': 'codex --dangerously-bypass-approvals-and-sandbox',
        'orch_headless': ('codex exec --dangerously-bypass-approvals-and-sandbox '
                          "--skip-git-repo-check 'hi there'"),
    },
    'deepseek-harness': {
        # auto_approve is a documented NO-OP here: ACP permission requests are
        # answered programmatically by the bridge, never by a tmux pane.
        #
        # THREE server/orchestrator divergences, all visible in these strings:
        #   1. --model: the server falls back to _DSH_DEFAULT_MODEL when
        #      KC_DSH_MODEL is unset; the orchestrator omits --model entirely,
        #      so a sub-agent silently runs whatever the bridge defaults to.
        #   2. --effort: server only (same gap as codex above).
        #   3. flag ORDER differs (--format/--cwd swapped) — harmless, but it is
        #      the fingerprint of two hand-written implementations of one fact.
        'interactive': ('python3 /tmp/browser/acp_bridge.py --serve '
                        '--format stream-json --cwd "$PWD" --mcp default '
                        '--model deepseek-v4-flash --effort high'),
        'interactive_auto': ('python3 /tmp/browser/acp_bridge.py --serve '
                             '--format stream-json --cwd "$PWD" --mcp default '
                             '--model deepseek-v4-flash --effort high'),
        'orch_interactive': ('python3 /tmp/browser/acp_bridge.py --serve '
                             '--cwd "$PWD" --format stream-json --mcp default'),
        'orch_headless': ("printf %s 'hi there' | "
                          'python3 /tmp/browser/acp_bridge.py '
                          '--cwd "$PWD" --format stream-json --mcp default'),
    },
    'kc-harness': {
        # Deliberately absent from _HEADLESS_CAPABLE: the harness reads stdin,
        # so "headless" falls back to the interactive REPL and the orchestrator
        # pastes the prompt. Identical in all four modes, by design.
        'interactive': 'python3 /tmp/browser/harness.py',
        'interactive_auto': 'python3 /tmp/browser/harness.py',
        'orch_interactive': 'python3 /tmp/browser/harness.py',
        'orch_headless': 'python3 /tmp/browser/harness.py',
    },
    'librefang': {
        # Both sides prepend the kernel-daemon bootstrap: `librefang chat` /
        # `message` panic ("there is no reactor running") without it.
        'interactive': _LIBREFANG_DAEMON + 'librefang chat coder',
        'interactive_auto': _LIBREFANG_DAEMON + 'librefang chat coder',
        'orch_interactive': _LIBREFANG_DAEMON + 'librefang chat coder',
        'orch_headless': _LIBREFANG_DAEMON + "librefang message coder 'hi there'",
    },
    'opencode-deepseek': {
        'interactive': 'opencode --model deepseek/deepseek-chat',
        'interactive_auto': 'opencode --model deepseek/deepseek-chat',
        'orch_interactive': 'opencode --model deepseek/deepseek-chat',
        'orch_headless': "opencode run --model deepseek/deepseek-chat 'hi there'",
    },
    'opencode-openrouter': {
        'interactive': 'opencode --model openrouter/anthropic/claude-sonnet-4',
        'interactive_auto': 'opencode --model openrouter/anthropic/claude-sonnet-4',
        'orch_interactive': 'opencode --model openrouter/anthropic/claude-sonnet-4',
        'orch_headless': ('opencode run --model openrouter/anthropic/claude-sonnet-4 '
                          "'hi there'"),
    },
    'opencode-zen': {
        'interactive': 'opencode --model opencode-zen/deepseek-v4-flash-free',
        'interactive_auto': 'opencode --model opencode-zen/deepseek-v4-flash-free',
        'orch_interactive': 'opencode --model opencode-zen/deepseek-v4-flash-free',
        'orch_headless': ('opencode run --model opencode-zen/deepseek-v4-flash-free '
                          "'hi there'"),
    },
}

# Every env var that steers a launch command. Scrubbed for the golden run so the
# matrix describes the out-of-the-box pod, and listed explicitly (rather than by
# KC_ prefix) so adding a new steering var to a builder without adding it here
# shows up as a matrix that quietly depends on the developer's shell.
_STEERING_ENV = (
    'KC_ANTIGRAVITY_MODEL', 'KC_CODEX_MODEL', 'KC_DSH_MODEL',
    'KC_DSH_EFFORT', 'KC_CODEX_EFFORT', 'KC_CLAUDE_EFFORT', 'KC_HARNESS_EFFORT',
    'KC_LIBREFANG_AGENT', 'KC_OPENROUTER_MODEL', 'KC_DEEPSEEK_MODEL',
    'KC_OPENCODE_ZEN_MODEL', 'KC_HARNESS_MODEL', 'KC_FALLBACK_MODEL',
)

_PROMPT = 'hi there'


def _commands(assistant):
    return {
        'interactive': CTM.assistant_command(assistant),
        'interactive_auto': CTM.assistant_command(assistant, auto_approve=True),
        'orch_interactive': orch._assistant_command(assistant, headless=False),
        'orch_headless': orch._assistant_command(assistant, prompt=_PROMPT,
                                                 headless=True),
    }


class RuntimeCommandMatrixTest(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in _STEERING_ENV}
        # `claude --help` is neither installed nor stable in CI; pin both
        # capability probes off so the golden claude strings are deterministic.
        self._probes = (CTM._CLAUDE_SESSION_ID_SUPPORTED,
                        CTM._CLAUDE_RESUME_SUPPORTED)
        CTM._CLAUDE_SESSION_ID_SUPPORTED = False
        CTM._CLAUDE_RESUME_SUPPORTED = False

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        (CTM._CLAUDE_SESSION_ID_SUPPORTED,
         CTM._CLAUDE_RESUME_SUPPORTED) = self._probes

    def test_every_runtime_matches_its_golden_command(self):
        for assistant, modes in sorted(GOLDEN.items()):
            got = _commands(assistant)
            for mode, expected in sorted(modes.items()):
                with self.subTest(assistant=assistant, mode=mode):
                    self.assertEqual(
                        got[mode], expected,
                        msg=(f'{assistant}/{mode} launch command changed.\n'
                             f'  was: {expected!r}\n'
                             f'  now: {got[mode]!r}\n'
                             'If this change is intended, edit GOLDEN and say '
                             'why in the entry comment.'))

    def test_matrix_covers_every_declared_runtime(self):
        """`Done looks like`: a test fails if a catalog entry is added without a
        corresponding smoke check. ASSISTANTS is the id registry the dropdown is
        built from, so a new runtime that never gets a golden command lands here
        as a failure rather than as an untested launch path."""
        self.assertEqual(sorted(GOLDEN), sorted(CTM.ASSISTANTS),
                         msg='every runtime in ClaudeTaskManager.ASSISTANTS needs '
                             'a GOLDEN entry (and vice versa) — add its four '
                             'launch commands above.')

    def test_headless_capable_matches_the_launch_builders(self):
        """Headless capability must be the SAME fact as the headless command,
        not a second copy of it. Before #604 this was a hand-maintained
        `_HEADLESS_CAPABLE` set that could disagree with the branch bodies —
        a runtime launchable interactively silently falling back in headless
        mode. It is now derived from the catalog; this pins the derivation
        against what the builders actually emit, so a template added without a
        capability (or the reverse) still fails here."""
        for assistant in sorted(CTM.ASSISTANTS):
            with self.subTest(assistant=assistant):
                cmds = _commands(assistant)
                differs = cmds['orch_headless'] != cmds['orch_interactive']
                self.assertEqual(
                    differs, runtimes.is_headless_capable(assistant),
                    msg=(f'{assistant}: catalog says headless_capable='
                         f'{runtimes.is_headless_capable(assistant)}, but its '
                         f'headless command '
                         f'{"differs from" if differs else "is identical to"} '
                         'its interactive one.'))

    def test_catalog_is_the_only_runtime_id_registry(self):
        """#604's core rule: the id exists in ONE place. ASSISTANTS carries the
        dropdown's presentation metadata, but its key set must be exactly the
        catalog's — a runtime that can be launched but never listed (or listed
        but never launchable) is the drift this issue is about."""
        self.assertEqual(sorted(runtimes.RUNTIMES), sorted(CTM.ASSISTANTS))

    def test_empty_prompt_stays_a_present_but_empty_argument(self):
        """An empty prompt must still render as `''`, not vanish. A dropped
        argument turns `claude --dangerously-skip-permissions -p ''` into a bare
        `-p`, which makes the CLI read the prompt from stdin — a hung tmux
        session rather than a fast no-op. Caught by differential-testing the
        catalog against the pre-#604 builders; pinned here so the renderer's
        "empty placeholders disappear" rule never grows to cover PROMPT."""
        for assistant in sorted(CTM.ASSISTANTS):
            if not runtimes.is_headless_capable(assistant):
                continue
            with self.subTest(assistant=assistant):
                cmd = orch._assistant_command(assistant, prompt='', headless=True)
                self.assertIn("''", cmd,
                              msg=f'{assistant} dropped its empty prompt: {cmd!r}')

    def test_unknown_id_gets_an_interactive_claude_repl(self):
        """An id this workspace doesn't know (a retired one like
        `opencode-fallback`, or a typo from a free-form caller) falls back to
        Claude's REPL with the prompt PASTED — not to a headless Claude run the
        caller never asked for. Capability is judged on the id as requested,
        before the fallback."""
        for unknown in ('opencode-fallback', 'garbage', ''):
            with self.subTest(unknown=unknown):
                self.assertEqual(
                    orch._assistant_command(unknown, prompt='x', headless=True),
                    'claude')
                self.assertEqual(CTM.assistant_command(unknown), 'claude')

    def test_no_runtime_falls_back_to_another_cli(self):
        """The drift failure #604 names: an id that is launchable interactively
        but silently becomes `claude` headlessly. Every declared runtime must
        launch something of its own in every mode."""
        for assistant in sorted(CTM.ASSISTANTS):
            if assistant == 'claude':
                continue
            for mode, cmd in sorted(_commands(assistant).items()):
                with self.subTest(assistant=assistant, mode=mode):
                    self.assertFalse(
                        cmd == 'claude' or cmd.startswith('claude '),
                        msg=f'{assistant}/{mode} silently launches claude: {cmd!r}')


if __name__ == '__main__':
    unittest.main()
