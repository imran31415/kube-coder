"""One declarative catalog of the agent runtimes this workspace can launch (#604).

Adding an agent CLI used to mean editing the same assistant id into a handful of
unrelated places: an `if assistant ==` branch in server.py that built the
interactive command, a second branch in mcp_agent_orchestrator.py that built the
headless one, a hand-maintained `_HEADLESS_CAPABLE` set that had to agree with
both, and a per-runtime model-default helper next to it. Four encodings of one
fact, which is three opportunities to drift — and they had drifted (see the
DIVERGENCE notes on `codex` and `deepseek-harness` below, each frozen by
tests/runtime_command_matrix_test.py).

This module is the single place a runtime id exists. Everything else derives
from it:

    RUNTIMES[id]['launch_args']       interactive REPL   (server.assistant_command)
    RUNTIMES[id]['orch_launch_args']  attachable sub-agent (orchestrator)
    RUNTIMES[id]['headless_args']     one-shot sub-agent   (orchestrator)
    RUNTIMES[id]['headless_capable']  derived: `headless_args` is not None

`headless_capable` is no longer a separate set that can disagree with the
branch bodies — it IS the presence of a headless template, so a runtime cannot
claim a mode it has no command for. A runtime with no genuine one-shot mode
(kc-harness reads stdin) declares `headless_args: None` deliberately, and the
orchestrator falls back to pasting the prompt into its REPL.

## Template grammar

A template is a list of tokens joined with single spaces. Each token is either:

  * a literal string  — spliced verbatim, NEVER quoted. `"$PWD"` is a literal on
    purpose: the command is run under `bash -lc` and must expand at launch.
  * a placeholder     — MODEL / PROMPT / ARG resolve to a caller- or
    env-supplied value and are ALWAYS shell-quoted; SKIP / EFFORT splice
    pre-rendered argv fragments.
  * a nested list     — an optional group, emitted only when every placeholder
    inside it resolves non-empty. This is what makes `['--model', MODEL]`
    disappear entirely for a CLI that should pick its own default, instead of
    emitting a dangling flag.

Quoting is the renderer's job, once, rather than a per-branch obligation that
every new site has to remember against a hostile env var. `quote` is injected
rather than imported because the two callers ship different (both safe)
quoters — server.py uses `shlex.quote`, the orchestrator a narrower allowlist —
and #604 is a refactor that must not change a single emitted byte. Converging
them is a deliberate follow-up, not a side effect of this one.
"""

# ── Placeholders ─────────────────────────────────────────────────────────────
MODEL = '{{model}}'    # resolved model id (with `model_prefix`), shell-quoted
PROMPT = '{{prompt}}'  # the sub-agent's prompt, shell-quoted
ARG = '{{arg}}'        # one extra runtime-specific value (librefang's agent name)
SKIP = '{{skip}}'      # the CLI's skip-approvals flag, when the caller asked
EFFORT = '{{effort}}'  # pre-quoted argv from ClaudeTaskManager.effort_cli_args

_PLACEHOLDERS = (MODEL, PROMPT, ARG, SKIP, EFFORT)

# `librefang chat` / `librefang message` talk to the kernel daemon and panic
# ("there is no reactor running") when none is up, killing the tmux session
# instantly. `librefang start` self-daemonizes and is a no-op when already
# running; poll status briefly so the command doesn't run before the daemon's
# API binds. Declared once here because BOTH launch paths need it.
LIBREFANG_DAEMON_BOOTSTRAP = (
    'librefang status -q >/dev/null 2>&1 || { '
    'librefang start >/dev/null 2>&1 || true; '
    'for _ in 1 2 3 4 5 6 7 8 9 10; do '
    'librefang status -q >/dev/null 2>&1 && break; sleep 1; '
    'done; }; '
)

# Zen's free model ids (#395). Owned here rather than in server.py because the
# orchestrator needs the same default and used to carry a hand-copied literal
# with a "keep in sync with server.py" comment above it.
OPENCODE_ZEN_FREE_MODELS = (
    'deepseek-v4-flash-free',
    'big-pickle',
    'mimo-v2.5-free',
    'laguna-s-2.1-free',
    'ling-3.0-flash-free',
    'north-mini-code-free',
    'nemotron-3-ultra-free',
)
OPENCODE_ZEN_DEFAULT_MODEL = OPENCODE_ZEN_FREE_MODELS[0]

# DeepSeek Harness model ids, as advertised by a live `session/new`. The
# experimental vision model is left out of the default list — an operator who
# wants it sets KC_DSH_MODELS.
DSH_MODELS = ('deepseek-v4-flash', 'deepseek-v4-pro')
DSH_DEFAULT_MODEL = DSH_MODELS[0]

# The ACP bridge flags both DeepSeek Harness modes share. `--mcp default` is the
# curated dashboard+memory pair rather than the full boot-seeded set: ACP
# connects every declared server before publishing the session, so one slow npx
# server would be a dead session instead of a missing tool.
_ACP = ('--mcp', 'default')


RUNTIMES = {
    'claude': {
        'label': 'Claude Code',
        # No env default and no prefix: Claude Code picks its own model unless a
        # per-launch pick arrives. `default` is the "let the CLI decide"
        # sentinel from the model list, so it must never become a --model flag.
        'model_env': None,
        'model_default': None,
        'model_sentinels': ('default',),
        'skip_permissions_flag': '--dangerously-skip-permissions',
        'launch_args': ['claude', SKIP, ['--model', MODEL]],
        # Sub-agents run unattended, so headless always skips approvals.
        'headless_args': ['claude', '--dangerously-skip-permissions', '-p', PROMPT],
        # Session pinning (#574) and resume (#588) are stateful process control
        # — they depend on what THIS CLI build advertises — so they stay a
        # registered hook rather than a template the catalog pretends to own.
        'launch_hook': 'claude_session_args',
    },
    'ante': {
        'label': 'Ante CLI',
        'model_env': None,
        'model_default': None,
        'skip_permissions_flag': '--yolo',
        'launch_args': ['ante', SKIP],
        'headless_args': ['ante', '--yolo', '-p', PROMPT],
    },
    # Antigravity — Google's `agy` CLI. OAuth login (no API key), so it is
    # listed whenever its binary is resolvable.
    'antigravity': {
        'label': 'Antigravity',
        'model_env': 'KC_ANTIGRAVITY_MODEL',
        'model_default': None,
        'skip_permissions_flag': '--dangerously-skip-permissions',
        'launch_args': ['agy', SKIP, ['--model', MODEL]],
        'headless_args': ['agy', '--dangerously-skip-permissions',
                          ['--model', MODEL], '-p', PROMPT],
    },
    # Codex — OpenAI's CLI. The pod is externally sandboxed (k8s), so the
    # skip flag is the documented bypass rather than a permissions opt-out.
    'codex': {
        'label': 'Codex',
        'model_env': 'KC_CODEX_MODEL',
        'model_default': None,
        'skip_permissions_flag': '--dangerously-bypass-approvals-and-sandbox',
        'launch_args': ['codex', SKIP, ['--model', MODEL], EFFORT],
        # DIVERGENCE (pre-existing, frozen deliberately): the orchestrator's
        # interactive codex ALWAYS bypasses — a sub-agent has no human to answer
        # an approval menu — spells the model flag `-m` rather than `--model`
        # (a codex alias, same effect), and threads no reasoning effort. Stated
        # here as three template differences instead of being rediscovered from
        # two hand-written branches.
        'orch_launch_args': ['codex', '--dangerously-bypass-approvals-and-sandbox',
                             ['-m', MODEL]],
        # `codex exec` is the one-shot non-interactive mode. --skip-git-repo-check
        # so it runs in any workdir; no --json, because the orchestrator captures
        # the tmux pane as text and exec prints its final message to stdout.
        'headless_args': ['codex', 'exec',
                          '--dangerously-bypass-approvals-and-sandbox',
                          '--skip-git-repo-check', ['-m', MODEL], PROMPT],
    },
    # DeepSeek Harness (#639) — driven over its ACP JSON-RPC server via
    # acp_bridge.py, never the `dsh` CLI directly: `dsh` ships no usable REPL
    # (the `tui` profile is not among the bundles the npm package installs) and
    # `--profile headless` is one-shot prose with no tool output, so its only
    # real structured surface is the bidirectional JSON-RPC server.
    #
    #   headless    one prompt in, events out, exits when the turn settles —
    #               exactly the print-mode contract the orchestrator needs to
    #               detect completion by session death + exit code.
    #   interactive `--serve`, one long-lived ACP session fed by tmux paste.
    #
    # `--format stream-json` in BOTH, because both consumers capture the tmux
    # pane as TEXT: that renderer interleaves human-readable lines with the
    # JSONL, so a parent agent's get_agent_output returns something readable.
    #
    # `auto_approve` is deliberately a NO-OP (no skip_permissions_flag): ACP's
    # permission requests are JSON-RPC calls that must be answered within the
    # turn, and a tmux pane cannot put that question to a user. The bridge
    # always auto-approves — the pod is the sandbox. Documented in
    # docs/llm-setup.md so it isn't a surprise.
    'deepseek-harness': {
        'label': 'DeepSeek Harness',
        'model_env': 'KC_DSH_MODEL',
        'model_default': DSH_DEFAULT_MODEL,
        # DIVERGENCE (pre-existing, frozen deliberately): the orchestrator omits
        # the model default, so a sub-agent runs whatever the bridge falls back
        # to while a Build tab runs DSH_DEFAULT_MODEL. Declaring it as an
        # explicit override is the point — it was previously invisible, spread
        # across two files.
        'orch_model_default': None,
        'skip_permissions_flag': None,
        'launch_args': ['python3', '/tmp/browser/acp_bridge.py', '--serve',
                        '--format', 'stream-json', '--cwd', '"$PWD"', *_ACP,
                        ['--model', MODEL], EFFORT],
        # Same flags, different ORDER — the fingerprint of two hand-written
        # implementations of one fact, kept byte-identical for now.
        'orch_launch_args': ['python3', '/tmp/browser/acp_bridge.py', '--serve',
                             '--cwd', '"$PWD"', '--format', 'stream-json', *_ACP,
                             ['--model', MODEL]],
        # printf keeps the prompt off argv and out of `ps`, and its format string
        # is a literal '%s' so a prompt containing % is inert.
        'headless_args': ['printf', '%s', PROMPT, '|',
                          'python3', '/tmp/browser/acp_bridge.py',
                          '--cwd', '"$PWD"', '--format', 'stream-json', *_ACP,
                          ['--model', MODEL]],
    },
    # LibreFang — open-source agent OS. Tasks talk to its registry-bundled
    # "coder" agent; KC_LIBREFANG_AGENT overrides it for a user-supplied
    # manifest. The CLI picks up whatever provider key is in the environment.
    'librefang': {
        'label': 'LibreFang',
        'model_env': None,
        'model_default': None,
        'arg_env': 'KC_LIBREFANG_AGENT',
        'arg_default': 'coder',
        'skip_permissions_flag': None,
        'shell_prefix': LIBREFANG_DAEMON_BOOTSTRAP,
        'launch_args': ['librefang', 'chat', ARG],
        'headless_args': ['librefang', 'message', ARG, PROMPT],
    },
    'opencode-openrouter': {
        'label': 'OpenRouter',
        'model_env': 'KC_OPENROUTER_MODEL',
        'model_default': 'anthropic/claude-sonnet-4',
        'model_prefix': 'openrouter/',
        'skip_permissions_flag': None,
        'launch_args': ['opencode', '--model', MODEL],
        'headless_args': ['opencode', 'run', '--model', MODEL, PROMPT],
    },
    'opencode-deepseek': {
        'label': 'DeepSeek',
        'model_env': 'KC_DEEPSEEK_MODEL',
        'model_default': 'deepseek-chat',
        'model_prefix': 'deepseek/',
        'skip_permissions_flag': None,
        'launch_args': ['opencode', '--model', MODEL],
        'headless_args': ['opencode', 'run', '--model', MODEL, PROMPT],
    },
    # OpenCode Zen (#395) — OpenCode's hosted gateway of free coding models.
    # The provider id prefix must match the custom stanza start.sh writes into
    # opencode.json. `free` drives the "free" marker in the picker;
    # `training_disclosure` drives the UI note that Zen may train on submissions.
    'opencode-zen': {
        'label': 'OpenCode Zen',
        'free': True,
        'training_disclosure': True,
        'model_env': 'KC_OPENCODE_ZEN_MODEL',
        'model_default': OPENCODE_ZEN_DEFAULT_MODEL,
        'model_prefix': 'opencode-zen/',
        'skip_permissions_flag': None,
        'launch_args': ['opencode', '--model', MODEL],
        'headless_args': ['opencode', 'run', '--model', MODEL, PROMPT],
    },
    # kc-harness — thin in-pod LLM tool-call loop at /tmp/browser/harness.py.
    # `headless_args: None` is a truthful capability declaration, not a gap: the
    # harness reads its prompt from stdin (tmux paste), so there is no one-shot
    # argv form to offer. The orchestrator pastes instead of pretending.
    'kc-harness': {
        'label': 'Opensource GPU',
        'model_env': None,
        'model_default': None,
        'skip_permissions_flag': None,
        'launch_args': ['python3', '/tmp/browser/harness.py'],
        'headless_args': None,
    },
}


def is_headless_capable(assistant):
    """True when the runtime declares a real one-shot command.

    Replaces the hand-maintained `_HEADLESS_CAPABLE` set: the capability is now
    the presence of the template that implements it, so the two cannot drift.
    An unknown id is not headless-capable — the caller falls back safely.
    """
    entry = RUNTIMES.get(assistant)
    return bool(entry and entry.get('headless_args'))


def resolve_model(assistant, override='', *, env=None, orchestrator=False):
    """The model id to splice into a command, prefix included ('' for none).

    Precedence: an explicit per-launch `override`, then the runtime's env var,
    then its declared default. A value matching one of the runtime's
    `model_sentinels` (Claude's `default`) resolves to '' so it never becomes a
    flag. `orchestrator=True` honours an `orch_model_default` override where the
    two launch paths genuinely disagree today.
    """
    import os
    env = os.environ if env is None else env
    entry = RUNTIMES.get(assistant)
    if not entry:
        return ''
    default = entry.get('model_default')
    if orchestrator and 'orch_model_default' in entry:
        default = entry['orch_model_default']
    model = (override or '').strip()
    if not model:
        env_key = entry.get('model_env')
        model = (env.get(env_key) if env_key else '') or default or ''
    if not model or model in (entry.get('model_sentinels') or ()):
        return ''
    return (entry.get('model_prefix') or '') + model


def resolve_arg(assistant, *, env=None):
    """The runtime's one extra declared value (librefang's agent name), '' if it
    declares none."""
    import os
    env = os.environ if env is None else env
    entry = RUNTIMES.get(assistant) or {}
    key = entry.get('arg_env')
    return ((env.get(key) if key else '') or entry.get('arg_default') or '')


def _resolve(token, values):
    """A placeholder's rendered text, or None when `token` is a literal."""
    return values.get(token) if token in _PLACEHOLDERS else None


def _render_tokens(template, values):
    out = []
    for token in template:
        if isinstance(token, (list, tuple)):
            # Optional group: every placeholder inside must resolve non-empty,
            # else the whole group (flag included) disappears.
            inner = _render_tokens(token, values)
            if all(_resolve(t, values) for t in token if t in _PLACEHOLDERS):
                out.extend(inner)
            continue
        resolved = _resolve(token, values)
        if resolved is None:
            out.append(token)          # literal — never quoted
        elif resolved:
            out.append(resolved)
    return out


def render(assistant, template, *, quote, model='', prompt='', arg='',
           skip=(), effort=()):
    """Render one template into a shell command string.

    `quote` is the caller's shell-quoting function, applied to every
    placeholder-derived value and never to a literal token. `skip` and `effort`
    are already-rendered argv fragments (the CLI's skip-approvals flag, and
    ClaudeTaskManager.effort_cli_args' pre-quoted pair) and are spliced as-is.
    A runtime's declared `shell_prefix` (LibreFang's daemon bootstrap) is
    prepended to the finished command.
    """
    values = {
        MODEL: quote(model) if model else '',
        # PROMPT is quoted unconditionally, so an EMPTY prompt still emits `''`
        # and stays a present-but-empty argument. Dropping it would turn
        # `claude -p ''` into a bare `claude -p`, which reads the prompt from
        # stdin instead — a hang, not a no-op. Every other placeholder is
        # allowed to vanish, because for those "absent" is the intended meaning.
        PROMPT: quote(prompt),
        ARG: quote(arg) if arg else '',
        SKIP: ' '.join(skip),
        EFFORT: ' '.join(effort),
    }
    cmd = ' '.join(_render_tokens(template, values))
    prefix = (RUNTIMES.get(assistant) or {}).get('shell_prefix') or ''
    return prefix + cmd
