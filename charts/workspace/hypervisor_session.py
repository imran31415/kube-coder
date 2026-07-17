#!/usr/bin/env python3
"""Structured agent sessions for the Hypervisor chat.

This replaces the old "run the CLI in a tmux pane and screen-scrape it"
mechanism. That approach was fundamentally fragile: a TUI has interactive
menus a paste-only chat can't answer (the bypass/permission/API-key dialogs),
and its rendered pane (box-drawing tables, ANSI, wrapping) can't be reliably
un-scraped into clean chat.

Instead, each Hypervisor thread is a **structured agent session**:

  * The selected CLI is run in its machine-readable streaming mode over pipes
    (no TTY, no tmux) — for Claude that's `claude -p --output-format
    stream-json`, which emits JSON events (assistant text, tool_use,
    tool_result, result) and renders no interactive dialogs by construction.
  * A small per-CLI **adapter** normalizes that native output into ONE
    canonical event schema (see EVENT SCHEMA below).
  * Canonical events are appended to an append-only `events.jsonl` per thread
    — the durable transcript. The frontend renders these events and never sees
    a terminal.

Adding a new assistant means writing one adapter; the server + frontend never
change. Claude and kc-harness are first-class; every other CLI gets a clean
non-TTY line fallback (plain prose, never a garbled pane).

EVENT SCHEMA (one JSON object per line in events.jsonl):
    {
      "seq":  int,          # monotonic per thread, starts at 1
      "ts":   float,        # unix seconds
      "role": "user" | "assistant" | "system",
      "type": "message" | "tool_call" | "tool_result" | "error" | "status",
      # by type:
      #   message      -> "text": str
      #   tool_call    -> "tool": {"name": str, "input": dict}, "tool_id": str
      #   tool_result  -> "tool_use_id": str, "text": str, "is_error": bool
      #   error        -> "text": str
      #   status       -> "status": "running"|"idle"|"error" ("turn" lifecycle)
      #   choice       -> "options": [str, ...], "question": str (optional)
    }

The `choice` event is how the agent asks the user to pick between a few options
(rendered as clickable buttons in the chat). It is NOT emitted by any adapter —
instead, when the agent ends an assistant message with a ```choice fenced block
(instructed via the preamble), _append() splits that message into a prose
`message` + a `choice` event centrally, downstream of every adapter. So the
picker works identically for Claude and every fallback CLI, and clients stay
pure renderers.

Persistence lives under /home/dev/.claude-tasks/hypervisor/<thread_id>/:
    thread.json   — metadata (title, assistant, workdir, status, session ids)
    events.jsonl  — canonical transcript (append-only)
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

# ───────────────────────────────────────────────────────────────────────────
# Paths / constants
# ───────────────────────────────────────────────────────────────────────────
# The workspace home is the PVC-mounted /home/dev, where the CLIs' config,
# credentials (Claude oauth in ~/.claude.json, ~/.ante, …) and the task store
# live. This is HARDCODED to match server.py's TASKS_DIR — the server process
# itself may run with a different $HOME (e.g. /home/ubuntu), so keying storage
# or the CLI subprocess env off os.environ['HOME'] would land in an ephemeral,
# config-less home. We store threads next to the tasks and force HOME=/home/dev
# on every spawned CLI so it finds its subscription/oauth + seeded MCP config.
WORKSPACE_HOME = '/home/dev'
HYPERVISOR_DIR = os.path.join(WORKSPACE_HOME, '.claude-tasks', 'hypervisor')

# User-set provider keys (managed by server.py's ProviderKeysManager) are stored
# as one JSON on the PVC and overlaid onto every CLI subprocess's env at spawn,
# so a key set in Settings takes effect on the next turn with no restart. This
# module can't import server.py (server imports us), so we read the same file
# directly — keep _PROVIDER_KEY_VARS in sync with ProviderKeysManager.ALLOWED.
_PROVIDER_KEYS_FILE = os.path.join(WORKSPACE_HOME, '.claude-tasks', 'provider-keys.json')
_PROVIDER_KEY_VARS = ('OPENROUTER_API_KEY', 'DEEPSEEK_API_KEY', 'ANTHROPIC_API_KEY')


def _provider_key_overlay() -> Dict[str, str]:
    """{VAR: value} for the provider keys the user has set, or {} if none."""
    try:
        with open(_PROVIDER_KEYS_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in _PROVIDER_KEY_VARS
            if isinstance(data.get(k), str) and data[k].strip()}

# Strip ANSI/VT escape sequences from fallback CLI output so a non-structured
# assistant still reads as clean prose, never raw terminal control codes.
_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|[\x00-\x08\x0b\x0c\x0e-\x1f]')

# Per-turn wall-clock ceiling for a fallback CLI (Claude manages its own).
FALLBACK_TURN_TIMEOUT = float(os.environ.get('KC_HYPERVISOR_FALLBACK_TIMEOUT', '180'))

# Size ceiling for a thread's runner.log — the bounded on-disk capture of the
# CLI subprocess's stderr + per-turn runner diagnostics (issue: hypervisor
# observability). Capped so the PVC can't grow unbounded; when exceeded we keep
# the tail (the recent, relevant lines) and drop the head. Env-overridable.
try:
    RUNNER_LOG_MAX_BYTES = int(os.environ.get('KC_HYPERVISOR_RUNNER_LOG_MAX', str(256 * 1024)))
except ValueError:
    RUNNER_LOG_MAX_BYTES = 256 * 1024

# The Hypervisor gives Claude a MINIMAL, curated MCP set — `dashboard` (the
# workspace UI actions: metrics/tasks/apps + create_task/pin_app/gated kill…)
# and `memory` — instead of the full seeded config (playwright,
# sequential-thinking, spine, …). A headless `claude -p` turn connects MCP
# servers asynchronously and doesn't wait long; with the full set the dashboard
# tools frequently weren't ready before the turn finished and Claude fell back
# to bash. Two lightweight stdio servers connect fast and reliably. Passed via
# --mcp-config/--strict-mcp-config so it overrides ~/.claude.json for this run
# only (the Build tab keeps the full set). The dashboard MCP reads the bearer
# token from $HOME/.claude-tasks/.api-token, so HOME=/home/dev (forced below) is
# what keeps its REST calls from 401-ing.
_HYPERVISOR_MCP_CONFIG = json.dumps({'mcpServers': {
    'dashboard': {'type': 'stdio', 'command': 'python3',
                  'args': ['/tmp/browser/mcp_dashboard.py']},
    'memory': {'type': 'stdio', 'command': 'python3',
               'args': [os.path.join(WORKSPACE_HOME, '.claude-memory', 'mcp_memory.py')]},
}})


def _now() -> float:
    return time.time()


def _log(msg: str) -> None:
    try:
        sys.stderr.write(f'[hypervisor_session] {msg}\n')
        sys.stderr.flush()
    except Exception:
        pass


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text or '')


# A ```choice fenced block turns into a clickable multiple-choice picker. We
# parse it centrally (see _expand_choices, called from _append) so it works for
# every harness, not per-adapter. First non-bullet line is an optional question;
# `-`/`*`/`1.`/`1)` lines are the options.
_CHOICE_FENCE_RE = re.compile(r'```choice[^\n]*\n(.*?)```', re.DOTALL)
_CHOICE_OPT_RE = re.compile(r'^(?:[-*]|\d+[.)])\s+(.+)$')


def _parse_choice_body(body: str) -> Optional[Dict[str, Any]]:
    options: List[str] = []
    question = ''
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _CHOICE_OPT_RE.match(line)
        if m:
            options.append(m.group(1).strip())
        elif not options:
            question = f'{question} {line}'.strip()
    if not options:
        return None
    ev: Dict[str, Any] = {'role': 'assistant', 'type': 'choice', 'options': options}
    if question:
        ev['question'] = question
    return ev


def _expand_choices(partial: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Split an assistant message containing a ```choice fence into a prose
    `message` + a canonical `choice` event. Non-message or fence-free partials
    pass through unchanged — so this is a safe no-op for everything else."""
    if partial.get('role') != 'assistant' or partial.get('type') != 'message':
        return [partial]
    text = partial.get('text') or ''
    if '```choice' not in text:
        return [partial]
    out: List[Dict[str, Any]] = []
    last = 0
    for m in _CHOICE_FENCE_RE.finditer(text):
        before = text[last:m.start()].strip()
        if before:
            out.append({'role': 'assistant', 'type': 'message', 'text': before})
        choice = _parse_choice_body(m.group(1))
        # Unparseable fence (no options) → keep the raw text so nothing is lost.
        out.append(choice or {'role': 'assistant', 'type': 'message',
                              'text': m.group(0)})
        last = m.end()
    rest = text[last:].strip()
    if rest:
        out.append({'role': 'assistant', 'type': 'message', 'text': rest})
    return out or [partial]


# ───────────────────────────────────────────────────────────────────────────
# Adapters — translate a CLI's native turn into canonical events.
#
# An adapter is three callables sharing a per-thread `ctx` dict (persisted in
# thread.json under "adapter"):
#   build(ctx, text, first) -> {argv, cwd, env, stdin, shell}
#   parse(ctx, line)        -> list[partial canonical events]   (per stdout line)
#   finalize(ctx, rc)       -> list[partial canonical events]   (on process exit)
# "partial" = the event dict without seq/ts (the session stamps those).
# ───────────────────────────────────────────────────────────────────────────


class Adapter:
    kind = 'base'

    def build(self, ctx: Dict[str, Any], text: str, first: bool) -> Dict[str, Any]:
        raise NotImplementedError

    def parse(self, ctx: Dict[str, Any], line: str) -> List[Dict[str, Any]]:
        return []

    def finalize(self, ctx: Dict[str, Any], rc: int) -> List[Dict[str, Any]]:
        return []


class ClaudeAdapter(Adapter):
    """`claude -p --output-format stream-json` — full structured transport.

    Multi-turn continuity uses `--resume <session_id>`: turn 1 has no session
    id; we capture it from the stream's init/result event and resume on every
    later turn. Restart-safe — the id lives in thread.json and Claude persists
    the session on disk.
    """

    kind = 'claude'

    def build(self, ctx, text, first):
        argv = [
            'claude', '-p', text,
            '--output-format', 'stream-json',
            '--verbose',
            '--permission-mode', 'bypassPermissions',
            # Curated 2-server MCP set that connects fast enough for a headless
            # turn (see _HYPERVISOR_MCP_CONFIG). --strict-mcp-config makes it the
            # ONLY set for this run, overriding ~/.claude.json.
            '--mcp-config', _HYPERVISOR_MCP_CONFIG,
            '--strict-mcp-config',
        ]
        sid = ctx.get('claude_session_id')
        if sid:
            argv += ['--resume', sid]
        elif ctx.get('preamble'):
            # Role/context note goes into the system prompt, not the user turn,
            # so it never shows up as a chat bubble or pollutes the title.
            argv += ['--append-system-prompt', ctx['preamble']]
        # Per-thread model (#308): read fresh each turn so an in-chat switch
        # takes effect on the next turn and carries across --resume. `default`
        # (and '') mean "let Claude Code pick" — omit the flag.
        model = (ctx.get('model') or '').strip()
        if model and model != 'default':
            argv += ['--model', model]
        # Headless `claude -p` silently prefers ANTHROPIC_API_KEY when it's set,
        # which routes to pay-per-use API billing (and fails outright when that
        # balance is empty). Drop it so the session uses the workspace's Claude
        # subscription (oauth) — the same credential the interactive Build tab
        # uses, where the "use this API key?" prompt defaults to No.
        env = {k: v for k, v in os.environ.items()
               if k not in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN')}
        env['HOME'] = WORKSPACE_HOME
        return {'argv': argv, 'cwd': ctx.get('workdir') or WORKSPACE_HOME, 'env': env}

    def parse(self, ctx, line):
        line = line.strip()
        if not line:
            return []
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            return []
        t = o.get('type')
        out: List[Dict[str, Any]] = []
        if t == 'system' and o.get('subtype') == 'init':
            if o.get('session_id'):
                ctx['claude_session_id'] = o['session_id']
            return []
        if t == 'assistant':
            # Shared with the session-log parser so the live stream and the
            # durable log normalize to byte-identical canonical events.
            return _claude_assistant_events((o.get('message', {}) or {}).get('content'))
        if t == 'user':
            return _claude_user_events((o.get('message', {}) or {}).get('content'))
        if t == 'result':
            if o.get('session_id'):
                ctx['claude_session_id'] = o['session_id']
            if o.get('subtype') not in (None, 'success'):
                out.append({'role': 'system', 'type': 'error',
                            'text': _stringify(o.get('result')
                                               or o.get('subtype'))})
            return out
        return []

    def finalize(self, ctx, rc):
        if rc not in (0, None):
            return [{'role': 'system', 'type': 'error',
                     'text': f'claude exited with code {rc}'}]
        return []


class FallbackAdapter(Adapter):
    """Best-effort non-TTY adapter for CLIs without a structured mode.

    Runs the assistant's shell command with the user's text on stdin, no TTY
    (TERM=dumb, NO_COLOR), captures stdout, strips ANSI, and emits it as one
    assistant message. Not multi-turn aware (each turn is stateless), but the
    output is always clean prose — never a scraped/garbled pane. Deepen into a
    real structured adapter per CLI over time.
    """

    kind = 'fallback'

    def build(self, ctx, text, first):
        cli_cmd = ctx.get('cli_cmd') or 'cat'
        stdin = text if not (first and ctx.get('preamble')) \
            else (ctx['preamble'] + '\n\n' + text)
        env = dict(os.environ)
        env.update({'TERM': 'dumb', 'NO_COLOR': '1', 'CI': '1',
                    'HOME': WORKSPACE_HOME})
        return {'argv': ['bash', '-lc', cli_cmd],
                'cwd': ctx.get('workdir') or WORKSPACE_HOME,
                'env': env, 'stdin': stdin + '\n', 'timeout': FALLBACK_TURN_TIMEOUT,
                'buffer_stdout': True}

    def finalize_buffered(self, ctx, rc, stdout):
        text = strip_ansi(stdout or '').strip()
        if not text:
            if rc not in (0, None):
                return [{'role': 'system', 'type': 'error',
                         'text': f'{ctx.get("assistant", "assistant")} exited '
                                 f'with code {rc} and no output'}]
            return [{'role': 'assistant', 'type': 'message',
                     'text': '_(no output)_'}]
        return [{'role': 'assistant', 'type': 'message', 'text': text}]


# ── Structured-CLI adapters (ante, opencode) ────────────────────────────────
# Both emit line-delimited JSON with a resumable session id, so they slot into
# the same pattern as Claude — the correct headless flow, NOT the interactive
# REPL the fallback would run. Schemas validated live in-pod (2026-07-13):
#   ante:     assistant text arrives as event.AgentMessage (inner = a string).
#   opencode: text arrives as a `text` event's part.text; step_start/step_finish
#             are noise; a `type:"error"` event carries provider errors (e.g.
#             "Key limit exceeded"), which we surface cleanly.
# Parsing stays permissive with a raw-stdout fallback so a version bump can't
# blank the chat.
_TEXT_FIELDS = ('text', 'content', 'message', 'output_text', 'response')


def _lift_text(inner: Any) -> str:
    if isinstance(inner, str):
        return inner
    if isinstance(inner, dict):
        for f in _TEXT_FIELDS:
            v = inner.get(f)
            if isinstance(v, str) and v.strip():
                return v
            if isinstance(v, list):  # Claude-style content blocks
                t = _stringify(v)
                if t.strip():
                    return t
    return ''


def _dig_session_id(o: Any) -> Optional[str]:
    """Find a session id in common shapes: session_id / sessionID / session.id."""
    if not isinstance(o, dict):
        return None
    for k in ('session_id', 'sessionID', 'sessionId'):
        if isinstance(o.get(k), str):
            return o[k]
    sess = o.get('session')
    if isinstance(sess, dict) and isinstance(sess.get('id'), str):
        return sess['id']
    return None


class _StructuredCliAdapter(Adapter):
    """Base for line-delimited-JSON CLIs: permissive parse + raw-text fallback."""

    kind = 'structured'

    def _reset_turn(self, ctx):
        ctx['_emitted'] = False
        ctx['_raw'] = []

    def parse(self, ctx, line):
        if not line.strip():
            return []
        try:
            o = json.loads(line.strip())
        except json.JSONDecodeError:
            ctx.setdefault('_raw', []).append(line.rstrip('\n'))  # → raw fallback
            return []
        return self._parse_obj(ctx, o)

    def _parse_obj(self, ctx, o):
        raise NotImplementedError

    def finalize(self, ctx, rc):
        # If structured content was recognized this turn, we're done. Otherwise
        # surface the raw stdout so the response is never lost to a schema miss.
        if ctx.get('_emitted'):
            return []
        raw = strip_ansi('\n'.join(ctx.get('_raw', []) or [])).strip()
        if raw:
            return [{'role': 'assistant', 'type': 'message', 'text': raw}]
        if rc not in (0, None):
            return [{'role': 'system', 'type': 'error',
                     'text': f'{self.kind} exited with code {rc}'}]
        return []


class AnteAdapter(_StructuredCliAdapter):
    """`ante -p --output-format json --permission-mode yolo` (headless).

    Ante wraps each event as {"event": {"<Type>": <inner>}}; assistant text is
    an `AgentMessage` (inner is the text string). Multi-turn via `-r <id>`.
    """

    kind = 'ante'
    _NOISE = frozenset({'ExtensionRefreshed', 'InfoBlockStart', 'InfoBlockAppend',
                        'InfoBlockEnd', 'UserInput', 'TurnStart', 'TurnEnd',
                        'SessionEnd', 'Usage', 'UsageUpdate', 'TokenUsage',
                        'Reasoning'})

    def build(self, ctx, text, first):
        self._reset_turn(ctx)
        argv = ['ante', '-p', text, '--output-format', 'json',
                '--permission-mode', 'yolo']
        sid = ctx.get('ante_session_id')
        if sid:
            argv += ['-r', sid]
        elif ctx.get('preamble'):
            argv += ['--append-system-prompt', ctx['preamble']]
        return {'argv': argv, 'cwd': ctx.get('workdir') or WORKSPACE_HOME}

    def _parse_obj(self, ctx, o):
        ev = o.get('event')
        if not isinstance(ev, dict) or not ev:
            return []
        k = next(iter(ev))
        inner = ev[k] if isinstance(ev[k], dict) else ev[k]
        if k == 'SessionStart':
            sid = _dig_session_id(inner if isinstance(inner, dict) else {})
            if sid:
                ctx['ante_session_id'] = sid
            return []
        if k in self._NOISE:
            return []
        lk = k.lower()
        d = inner if isinstance(inner, dict) else {}
        if 'error' in lk:
            ctx['_emitted'] = True
            return [{'role': 'system', 'type': 'error',
                     'text': _lift_text(inner) or _stringify(inner) or 'ante error'}]
        if 'tool' in lk and 'result' in lk:
            ctx['_emitted'] = True
            return [{'role': 'system', 'type': 'tool_result',
                     'tool_use_id': d.get('id') or d.get('tool_call_id') or '',
                     'is_error': bool(d.get('is_error') or d.get('error')),
                     'text': _stringify(d.get('output') or d.get('result')
                                        or d.get('content'))}]
        if 'tool' in lk and (d.get('name') or d.get('tool')):
            ctx['_emitted'] = True
            return [{'role': 'assistant', 'type': 'tool_call',
                     'tool_id': d.get('id', ''),
                     'tool': {'name': d.get('name') or d.get('tool') or 'tool',
                              'input': d.get('input') or d.get('arguments') or {}}}]
        if 'delta' in lk:  # skip partial-text spam; full events carry the text
            return []
        txt = _lift_text(inner)
        if txt:
            ctx['_emitted'] = True
            return [{'role': 'assistant', 'type': 'message', 'text': txt}]
        return []


class OpencodeAdapter(_StructuredCliAdapter):
    """`opencode run --format json` (headless); multi-turn via `-s <session_id>`."""

    kind = 'opencode'
    _NOISE = frozenset({'step_start', 'step_finish'})

    def build(self, ctx, text, first):
        self._reset_turn(ctx)
        msg = text if not (first and ctx.get('preamble')) \
            else ctx['preamble'] + '\n\n' + text
        argv = ['opencode', 'run', msg, '--format', 'json']
        # Model resolution (#308). The workspace-configured default lives in
        # cli_cmd (assistant_command builds `opencode --model '<provider>/<model>'`,
        # e.g. `openrouter/anthropic/claude-sonnet-4` or `deepseek/deepseek-chat`).
        # A per-thread switch stores just the provider-native model id in
        # ctx['model'] (e.g. `deepseek/deepseek-chat-v3-0324:free` for OpenRouter,
        # `deepseek-reasoner` for native DeepSeek); we keep the same opencode
        # provider prefix and swap the model, so the switcher never has to know
        # the prefix. Read fresh each turn so a mid-session switch takes effect.
        base = None
        m = re.search(r"--model\s+'?([^'\s]+)", ctx.get('cli_cmd', '') or '')
        if m:
            base = m.group(1)
        selected = (ctx.get('model') or '').strip()
        if selected:
            prefix = base.split('/', 1)[0] if base else 'openrouter'
            argv += ['--model', f'{prefix}/{selected}']
        elif base:
            argv += ['--model', base]
        sid = ctx.get('opencode_session_id')
        if sid:
            argv += ['-s', sid]
        return {'argv': argv, 'cwd': ctx.get('workdir') or WORKSPACE_HOME}

    def _parse_obj(self, ctx, o):
        sid = _dig_session_id(o)
        if sid:
            ctx['opencode_session_id'] = sid
        t = str(o.get('type') or o.get('event') or '').lower()
        if t in self._NOISE:
            return []
        if t == 'error':
            err = o.get('error') or {}
            msg = (err.get('data') or {}).get('message') or err.get('message') \
                or _stringify(err) or 'opencode error'
            ctx['_emitted'] = True
            return [{'role': 'system', 'type': 'error', 'text': msg}]
        part = o.get('part') if isinstance(o.get('part'), dict) else {}
        if 'tool' in t:
            name = o.get('tool') or o.get('name') or part.get('tool')
            if 'result' in t or o.get('state') == 'completed':
                out = o.get('output') or o.get('result') or part.get('output')
                if out is not None:
                    ctx['_emitted'] = True
                    return [{'role': 'system', 'type': 'tool_result',
                             'tool_use_id': o.get('callID') or o.get('id') or '',
                             'is_error': bool(o.get('error')),
                             'text': _stringify(out)}]
            if name:
                ctx['_emitted'] = True
                return [{'role': 'assistant', 'type': 'tool_call',
                         'tool_id': o.get('callID') or o.get('id') or '',
                         'tool': {'name': name,
                                  'input': o.get('input') or o.get('args') or {}}}]
            return []
        if t == 'text':
            txt = _lift_text(part) or _lift_text(o)
            if txt:
                ctx['_emitted'] = True
                return [{'role': 'assistant', 'type': 'message', 'text': txt}]
        return []


class CodexAdapter(_StructuredCliAdapter):
    """`codex exec --json` (headless); multi-turn via `codex exec resume <id>`.

    Codex (OpenAI) emits JSONL, captured empirically:
      {"type":"thread.started","thread_id":"<uuid>"}   → session id (for resume)
      {"type":"turn.started"} / {"type":"item.started",...}   → in-progress noise
      {"type":"item.completed","item":{"id":..,"type":"<t>",..}}   → the payload
      {"type":"turn.completed"|"turn.failed",...}      → turn outcome
    Transient top-level {"type":"error","message":"Reconnecting..."} lines are
    connection-retry notices, NOT turn outcomes — only `turn.failed` is terminal,
    so we deliberately drop the top-level error chatter.

    Auth is ChatGPT OAuth (`codex login` once in the pod; creds under $CODEX_HOME
    which start.sh persists on the PVC) — no API key. The pod is externally
    sandboxed (k8s), so we pass --dangerously-bypass-approvals-and-sandbox (its
    documented use is exactly an externally-sandboxed environment).

    NOTE: item rendering is intentionally minimal (agent message + error) pending
    an in-pod capture of the full item.type vocabulary (reasoning / command /
    file-change items) — same empirical-refinement path ante/opencode took.
    """

    kind = 'codex'
    # Shared exec options for both the first turn and a resume.
    def _opts(self, ctx):
        opts = ['--json', '--skip-git-repo-check',
                '--dangerously-bypass-approvals-and-sandbox',
                '-C', ctx.get('workdir') or WORKSPACE_HOME]
        # A per-thread model (#308) wins over the pod default (KC_CODEX_MODEL);
        # read fresh each turn so a mid-session switch takes effect. Codex has no
        # in-chat model list by default (its ids move fast) — this only fires
        # when an operator populates KC_CODEX_MODELS.
        model = (ctx.get('model') or os.environ.get('KC_CODEX_MODEL', '')).strip()
        if model:
            opts += ['--model', model]
        return opts

    def build(self, ctx, text, first):
        self._reset_turn(ctx)
        sid = ctx.get('codex_session_id')
        if sid:
            argv = ['codex', 'exec', 'resume', *self._opts(ctx), sid, text]
        else:
            prompt = (ctx['preamble'] + '\n\n' + text) \
                if (first and ctx.get('preamble')) else text
            argv = ['codex', 'exec', *self._opts(ctx), prompt]
        return {'argv': argv, 'cwd': ctx.get('workdir') or WORKSPACE_HOME}

    def _parse_obj(self, ctx, o):
        t = str(o.get('type') or '')
        if t == 'thread.started':
            tid = o.get('thread_id')
            if isinstance(tid, str):
                ctx['codex_session_id'] = tid
            return []
        if t == 'turn.failed':
            err = o.get('error') if isinstance(o.get('error'), dict) else {}
            ctx['_emitted'] = True
            return [{'role': 'system', 'type': 'error',
                     'text': err.get('message') or _stringify(o.get('error'))
                             or 'codex turn failed'}]
        if t == 'item.completed':
            item = o.get('item') if isinstance(o.get('item'), dict) else {}
            it = str(item.get('type') or '')
            if 'error' in it:
                ctx['_emitted'] = True
                return [{'role': 'system', 'type': 'error',
                         'text': _lift_text(item) or item.get('message')
                                 or 'codex error'}]
            # The agent's chat message (agent_message / assistant_message /
            # message / text). Internal-step items (reasoning, command_execution,
            # file_change, …) carry no such marker and are skipped for now.
            if any(s in it for s in ('message', 'text', 'agent', 'assistant')):
                txt = _lift_text(item)
                if txt:
                    ctx['_emitted'] = True
                    return [{'role': 'assistant', 'type': 'message', 'text': txt}]
            return []
        return []  # turn.started / item.started / turn.completed / retry chatter


_ADAPTERS: Dict[str, Adapter] = {
    'claude': ClaudeAdapter(),
    'ante': AnteAdapter(),
    'opencode': OpencodeAdapter(),
    'codex': CodexAdapter(),
    'fallback': FallbackAdapter(),
}


def _adapter_for(assistant: str) -> Adapter:
    if assistant == 'claude':
        return _ADAPTERS['claude']
    if assistant == 'ante':
        return _ADAPTERS['ante']
    if assistant == 'codex':
        return _ADAPTERS['codex']
    if assistant.startswith('opencode'):
        return _ADAPTERS['opencode']
    return _ADAPTERS['fallback']


def _stringify(v: Any) -> str:
    if v is None:
        return ''
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        parts = []
        for b in v:
            if isinstance(b, dict) and b.get('type') == 'text':
                parts.append(b.get('text', ''))
            elif isinstance(b, dict):
                parts.append(_stringify(b.get('content') or b.get('text') or ''))
            else:
                parts.append(str(b))
        return '\n'.join(p for p in parts if p)
    try:
        return json.dumps(v, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(v)


# ───────────────────────────────────────────────────────────────────────────
# Activity view — a normalized observability timeline over events.jsonl
# ───────────────────────────────────────────────────────────────────────────

# Map a tool name to a semantic category so the activity view can call out the
# high-signal side-effects — sub-builds, sub-agents, apps, memory — as
# first-class entries instead of undifferentiated "tool" rows. Built-in tools
# (Task, Bash, Edit…) arrive bare; the curated dashboard/memory MCP tools arrive
# namespaced as mcp__<server>__<tool>, so we classify on the un-namespaced base.
_MCP_NAME_RE = re.compile(r'^mcp__[^_]+__(.+)$')
_TOOL_CATEGORIES = {
    'create_task': 'build',        # spins up a background task/build
    'Task': 'subagent',            # Claude's built-in sub-agent spawn
    'pin_app': 'app',
    'show_app_preview': 'app',
    'add_memory': 'memory',
    'search_memory': 'memory',
    'list_memory': 'memory',
    'delete_memory': 'memory',
    'send_task_message': 'task',
    'kill_task': 'task',
    'get_task': 'task',
    'get_task_output': 'task',
    'list_tasks': 'task',
}


def _tool_base_name(name: Optional[str]) -> str:
    """The un-namespaced tool name: mcp__dashboard__create_task -> create_task,
    Bash -> Bash."""
    if not name:
        return ''
    m = _MCP_NAME_RE.match(name)
    return m.group(1) if m else name


def _classify_tool(name: Optional[str]) -> str:
    """Semantic category for a tool call: build | subagent | app | memory |
    task | tool. Drives the activity view's grouping/badges."""
    return _TOOL_CATEGORIES.get(_tool_base_name(name), 'tool')


def _extract_task_id(text: Optional[str]) -> Optional[str]:
    """Pull the created task_id out of a create_task tool result. The result is
    the (pretty-printed) JSON body of POST /api/claude/tasks, which carries
    task_id; fall back to a regex if it isn't cleanly parseable."""
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get('task_id'), str):
            return data['task_id']
    except (json.JSONDecodeError, ValueError):
        pass
    m = re.search(r'"task_id"\s*:\s*"([^"]+)"', text)
    return m.group(1) if m else None


def build_activity(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure transform of an events.jsonl list into a user-facing activity view
    (issue: hypervisor observability). Deterministic and side-effect free so
    it's trivially unit-testable.

    Produces:
      * timeline — ordered entries (seq-sorted) of the operationally interesting
        events: tool calls (each paired with its result + duration + ok/error
        status, and a semantic `category`), standalone errors, and status
        transitions. Plain chat messages are the transcript itself, so they're
        counted but not repeated here.
      * counts   — {tool_calls, tool_results, tool_errors, errors, messages,
        builds, subagents}.

    Each tool entry carries a `category` (build | subagent | app | memory |
    task | tool) so the UI can surface the high-signal side-effects. A `build`
    entry additionally carries the created `task_id` (for a deep-link); a
    `subagent` entry carries `subagent_type` + `description` from its input.

    A tool_call is matched to its tool_result by tool_id == tool_use_id. An
    unmatched call stays 'pending' (turn still running or the CLI never
    returned a result); an unmatched result is surfaced as its own orphan entry
    so nothing is silently dropped.
    """
    timeline: List[Dict[str, Any]] = []
    pending: Dict[str, int] = {}  # tool_id -> index into timeline
    counts = {'tool_calls': 0, 'tool_results': 0, 'tool_errors': 0,
              'errors': 0, 'messages': 0, 'builds': 0, 'subagents': 0}

    for e in sorted(events, key=lambda x: x.get('seq', 0)):
        etype = e.get('type')
        seq = e.get('seq')
        ts = e.get('ts')
        if etype == 'message':
            counts['messages'] += 1
        elif etype == 'tool_call':
            counts['tool_calls'] += 1
            tool = e.get('tool') or {}
            tool_id = e.get('tool_id')
            name = tool.get('name')
            tool_input = tool.get('input')
            category = _classify_tool(name)
            entry = {
                'kind': 'tool',
                'seq': seq,
                'ts': ts,
                'tool': name,
                'label': _tool_base_name(name),
                'category': category,
                'input': tool_input,
                'tool_id': tool_id,
                'status': 'pending',
                'result_text': None,
                'result_seq': None,
                'duration_ms': None,
                # Filled per-category below / at result time.
                'task_id': None,
                'subagent_type': None,
                'description': None,
            }
            if category == 'build':
                counts['builds'] += 1
            elif category == 'subagent':
                counts['subagents'] += 1
                if isinstance(tool_input, dict):
                    st = tool_input.get('subagent_type')
                    desc = tool_input.get('description')
                    entry['subagent_type'] = st if isinstance(st, str) else None
                    entry['description'] = desc if isinstance(desc, str) else None
            timeline.append(entry)
            if tool_id is not None:
                pending[tool_id] = len(timeline) - 1
        elif etype == 'tool_result':
            counts['tool_results'] += 1
            is_error = bool(e.get('is_error'))
            if is_error:
                counts['tool_errors'] += 1
            use_id = e.get('tool_use_id')
            idx = pending.pop(use_id, None) if use_id is not None else None
            if idx is not None:
                entry = timeline[idx]
                entry['status'] = 'error' if is_error else 'ok'
                entry['result_text'] = e.get('text')
                entry['result_seq'] = seq
                if isinstance(ts, (int, float)) and isinstance(entry.get('ts'), (int, float)):
                    entry['duration_ms'] = max(0, round((ts - entry['ts']) * 1000))
                # A successful sub-build carries the created task_id in its
                # result — lift it so the UI can deep-link to the task.
                if entry.get('category') == 'build' and not is_error:
                    entry['task_id'] = _extract_task_id(e.get('text'))
            else:
                # Result with no matching call — keep it visible.
                timeline.append({
                    'kind': 'tool_result_orphan',
                    'seq': seq,
                    'ts': ts,
                    'tool_use_id': use_id,
                    'status': 'error' if is_error else 'ok',
                    'result_text': e.get('text'),
                })
        elif etype == 'error':
            counts['errors'] += 1
            timeline.append({
                'kind': 'error', 'seq': seq, 'ts': ts, 'text': e.get('text'),
            })
        elif etype == 'status':
            timeline.append({
                'kind': 'status', 'seq': seq, 'ts': ts,
                'status': e.get('status'),
            })
        # 'choice' and any unknown types are chat-surface concerns, not activity.

    return {'timeline': timeline, 'counts': counts}


def hypervisor_health() -> Dict[str, Any]:
    """Global hypervisor runner health: how many turns are live right now, which
    threads they belong to and whether their subprocess is still alive, plus a
    per-thread last-status + recent-error snapshot. Read-only; safe to call from
    an auth-gated GET. Bounded — only scans thread listing metadata, not full
    transcripts."""
    with _RUNLOCK:
        running_ids = [tid for tid, v in _RUNNING.items() if v]
        proc_alive = {tid: (p.poll() is None)
                      for tid, p in _PROCS.items()}
    threads = []
    try:
        listed = HypervisorSession.list()
    except Exception:
        listed = []
    total_recent_errors = 0
    for summ in listed:
        tid = summ.get('id')
        errs = 0
        try:
            evs = HypervisorSession(tid).read_events()
            errs = sum(1 for e in evs[-100:] if e.get('type') == 'error')
        except Exception:
            errs = 0
        total_recent_errors += errs
        threads.append({
            'id': tid,
            'title': summ.get('title'),
            'status': summ.get('status'),
            'running': tid in running_ids,
            'subprocess_alive': proc_alive.get(tid, False),
            'recent_errors': errs,
        })
    return {
        'running_count': len(running_ids),
        'subprocess_count': sum(1 for a in proc_alive.values() if a),
        'recent_error_count': total_recent_errors,
        'threads': threads,
    }


# ───────────────────────────────────────────────────────────────────────────
# Claude Code JSONL session log — locate + parse into canonical events
#
# Every Claude turn (both the interactive Build-tab tasks AND our headless
# `claude -p` threads) is durably recorded by Claude Code itself, one JSON
# object per line, under ~/.claude/projects/<escaped-cwd>/<session_id>.jsonl.
# That log is a cleaner, complete source than the live pipe capture: it never
# drops a turn to a server restart or a truncated stream, and it carries the
# full assistant text + every tool_use / tool_result.
#
# We normalize it into the SAME canonical event shape the live ClaudeAdapter
# emits (below helpers are shared by both), so the frontend renders it with no
# special-casing. `HypervisorSession.transcript()` prefers this log when the
# thread is idle and falls back to the live events.jsonl capture otherwise.
# ───────────────────────────────────────────────────────────────────────────
# Content-record types in the log that carry no chat content (titles, mode
# switches, file snapshots, queue bookkeeping, …) — skipped by the parser.
_LOG_CONTENT_TYPES = frozenset({'user', 'assistant'})


def _claude_assistant_events(content: Any) -> List[Dict[str, Any]]:
    """Assistant content blocks → canonical `message` / `tool_call` events.
    `thinking` and any other block types are intentionally dropped. Shared by
    the live ClaudeAdapter stream parser and the session-log parser so both
    produce byte-identical event shapes."""
    out: List[Dict[str, Any]] = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        bt = b.get('type')
        if bt == 'text' and b.get('text', '').strip():
            out.append({'role': 'assistant', 'type': 'message', 'text': b['text']})
        elif bt == 'tool_use':
            out.append({'role': 'assistant', 'type': 'tool_call',
                        'tool_id': b.get('id', ''),
                        'tool': {'name': b.get('name', 'tool'),
                                 'input': b.get('input', {})}})
    return out


def _claude_user_events(content: Any) -> List[Dict[str, Any]]:
    """User content → canonical events. A string is a real user turn; a block
    list is Claude feeding tool results back (each → a `tool_result` event)."""
    if isinstance(content, str):
        text = content.strip()
        return [{'role': 'user', 'type': 'message', 'text': content}] if text else []
    out: List[Dict[str, Any]] = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        bt = b.get('type')
        if bt == 'tool_result':
            out.append({'role': 'system', 'type': 'tool_result',
                        'tool_use_id': b.get('tool_use_id', ''),
                        'is_error': bool(b.get('is_error')),
                        'text': _stringify(b.get('content'))})
        elif bt == 'text' and b.get('text', '').strip():
            out.append({'role': 'user', 'type': 'message', 'text': b['text']})
    return out


def claude_project_dir(workdir: str) -> str:
    """~/.claude/projects/<escaped-cwd> for a working directory. Claude Code
    slugifies the cwd by replacing every non-alphanumeric character with '-'
    (verified against on-disk dirs, e.g. /home/dev/.worktrees/kc →
    -home-dev--worktrees-kc)."""
    slug = re.sub(r'[^A-Za-z0-9]', '-', workdir or '')
    return os.path.join(WORKSPACE_HOME, '.claude', 'projects', slug)


def locate_claude_session_log(workdir: str,
                              session_id: Optional[str] = None) -> Optional[str]:
    """Path to a Claude Code JSONL session log, or None if there isn't one.

    Prefers the exact <session_id>.jsonl (deterministic — we know a thread's
    Claude session id). Absent an id, falls back to the most-recently-modified
    .jsonl in the project dir (the last session Claude opened for that cwd)."""
    proj = claude_project_dir(workdir)
    if session_id:
        p = os.path.join(proj, f'{session_id}.jsonl')
        return p if os.path.isfile(p) else None
    try:
        logs = [os.path.join(proj, n) for n in os.listdir(proj)
                if n.endswith('.jsonl')]
    except OSError:
        return None
    if not logs:
        return None
    return max(logs, key=lambda p: os.path.getmtime(p))


def parse_claude_session_log(path: str) -> List[Dict[str, Any]]:
    """Parse a Claude Code JSONL session log into an ordered list of canonical
    partial events (the same schema every adapter emits — role/type/text/tool/
    tool_use_id/…). ```choice fences in assistant prose are expanded into
    `choice` events, exactly as the live append path does, so quick-reply
    pickers still render. Non-conversational record types, sub-agent
    (`isSidechain`) lines, and synthetic (`isMeta`) lines are skipped. A
    malformed line is tolerated, never fatal — a partial log still renders."""
    out: List[Dict[str, Any]] = []
    try:
        f = open(path)
    except OSError:
        return out
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(o, dict):
                continue
            if o.get('isSidechain') or o.get('isMeta'):
                continue
            t = o.get('type')
            if t not in _LOG_CONTENT_TYPES:
                continue
            content = (o.get('message') or {}).get('content')
            partials = (_claude_assistant_events(content) if t == 'assistant'
                        else _claude_user_events(content))
            for p in partials:
                out.extend(_expand_choices(p))
    return out


# ───────────────────────────────────────────────────────────────────────────
# HypervisorSession — one chat thread backed by a runner + events.jsonl
# ───────────────────────────────────────────────────────────────────────────

# In-process registry of live runner threads, keyed by thread_id, so status
# reflects a running turn even before its events land.
_RUNNING: Dict[str, bool] = {}
# Live subprocess handle for the turn currently running in each thread, so a
# user-issued stop can terminate it. Registered once Popen succeeds and cleared
# in _run_turn's finally. Guarded by _RUNLOCK.
_PROCS: Dict[str, subprocess.Popen] = {}
# Thread ids the user asked to stop; lets _run_turn record a clean "stopped"
# marker (instead of a spurious error) and skip the adapter's finalize.
_STOPPING: set = set()
_RUNLOCK = threading.Lock()


class HypervisorSession:
    def __init__(self, thread_id: str):
        self.id = thread_id
        self.dir = os.path.join(HYPERVISOR_DIR, thread_id)
        self.meta_path = os.path.join(self.dir, 'thread.json')
        self.events_path = os.path.join(self.dir, 'events.jsonl')
        # Bounded capture of the CLI subprocess's stderr + per-turn runner
        # diagnostics, so a stalled/failed turn is debuggable from the UI
        # without attaching to stderr. Serialized by _runner_log_lock because
        # the stderr-drain thread and the runner thread both write to it.
        self.runner_log_path = os.path.join(self.dir, 'runner.log')
        self._runner_log_lock = threading.Lock()

    # ── lifecycle ──────────────────────────────────────────────────────────
    @classmethod
    def create(cls, assistant: str, workdir: str, cli_cmd: str,
               preamble: str = '', title: str = '',
               model: str = '') -> 'HypervisorSession':
        os.makedirs(HYPERVISOR_DIR, exist_ok=True)
        thread_id = f'{int(time.time())}-{uuid.uuid4().hex[:8]}'
        self = cls(thread_id)
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        adapter = _adapter_for(assistant)
        meta = {
            'id': thread_id,
            'title': (title or 'New chat')[:80],
            'assistant': assistant,
            'adapter_kind': adapter.kind,
            'workdir': workdir,
            'status': 'idle',
            'created_at': _now(),
            'updated_at': _now(),
            # adapter ctx — carries per-thread state (session ids, preamble,
            # and the selected model when the adapter honours one — #308).
            'adapter': {
                'assistant': assistant,
                'workdir': workdir,
                'cli_cmd': cli_cmd,
                'preamble': preamble,
                'model': model or '',
            },
        }
        self._write_meta(meta)
        open(self.events_path, 'a').close()
        return self

    @classmethod
    def get(cls, thread_id: str) -> Optional['HypervisorSession']:
        self = cls(thread_id)
        if not os.path.isfile(self.meta_path):
            return None
        return self

    @classmethod
    def list(cls, include_deleted: bool = False,
             only_deleted: bool = False) -> List[Dict[str, Any]]:
        """List threads, newest first.

        Soft-deleted threads (meta carrying ``deleted_at``) are hidden by
        default. ``only_deleted`` returns just the tombstones (the "Recently
        deleted" view); ``include_deleted`` returns both.
        """
        if not os.path.isdir(HYPERVISOR_DIR):
            return []
        out = []
        for tid in os.listdir(HYPERVISOR_DIR):
            s = cls.get(tid)
            if not s:
                continue
            m = s.read_meta()
            if not m:
                continue
            is_deleted = m.get('deleted_at') is not None
            if only_deleted and not is_deleted:
                continue
            if is_deleted and not (include_deleted or only_deleted):
                continue
            out.append(s.summary(m))
        # Tombstones sort by when they were deleted; live threads by activity.
        key = 'deleted_at' if only_deleted else 'updated_at'
        out.sort(key=lambda t: t.get(key) or 0, reverse=True)
        return out

    def delete(self) -> None:
        """Soft-delete: stamp ``deleted_at`` so the thread drops out of the
        default listing but its files (thread.json + events.jsonl) survive for
        `revive()`. A separate `purge_deleted()` hard-removes old tombstones so
        the PVC doesn't grow unbounded. Mirrors MemoryManager.soft_delete."""
        m = self.read_meta()
        if m is None:
            return
        m['deleted_at'] = int(_now())
        # Preserve the thread's original activity ordering across a
        # delete→restore round-trip: don't let _write_meta bump updated_at.
        self._write_meta(m, touch=False)

    def revive(self) -> bool:
        """Clear ``deleted_at`` so a soft-deleted thread reappears in the
        default listing. Returns False if the thread is missing or wasn't
        deleted. Leaves ``updated_at`` untouched so restore preserves order."""
        m = self.read_meta()
        if m is None or m.get('deleted_at') is None:
            return False
        m.pop('deleted_at', None)
        self._write_meta(m, touch=False)
        return True

    def hard_delete(self) -> None:
        """Irreversibly remove the thread directory. Used by `purge_deleted`
        for old tombstones — the pre-soft-delete behaviour."""
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    @classmethod
    def purge_deleted(cls, older_than_days: Optional[float] = None) -> Dict[str, Any]:
        """Hard-delete soft-deleted threads to bound disk use.

        Without this, `delete()` tombstones dirs forever. Removes every thread
        whose ``deleted_at`` is set and (if given) older than the cutoff.
        Idempotent — a call with nothing to purge is a cheap no-op. Mirrors
        MemoryManager.purge_deleted (server.py). Returns a count."""
        if not os.path.isdir(HYPERVISOR_DIR):
            return {'purged': 0}
        cutoff = None
        if older_than_days is not None:
            cutoff = _now() - float(older_than_days) * 86400.0
        purged = 0
        for tid in list(os.listdir(HYPERVISOR_DIR)):
            s = cls.get(tid)
            if not s:
                continue
            m = s.read_meta()
            if not m:
                continue
            deleted_at = m.get('deleted_at')
            if deleted_at is None:
                continue
            if cutoff is not None and float(deleted_at) >= cutoff:
                continue
            s.hard_delete()
            purged += 1
        return {'purged': purged}

    # ── meta ───────────────────────────────────────────────────────────────
    def read_meta(self) -> Optional[Dict[str, Any]]:
        try:
            with open(self.meta_path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def _write_meta(self, meta: Dict[str, Any], touch: bool = True) -> None:
        # `touch=False` persists a metadata change (soft-delete/revive) without
        # bumping updated_at, so a delete→restore round-trip keeps the thread's
        # place in the activity-sorted list.
        if touch:
            meta['updated_at'] = _now()
        tmp = self.meta_path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(meta, f, indent=2)
        os.replace(tmp, self.meta_path)

    def status(self) -> str:
        with _RUNLOCK:
            if _RUNNING.get(self.id):
                return 'running'
        m = self.read_meta() or {}
        return m.get('status', 'idle')

    def set_title(self, title: str) -> Optional[Dict[str, Any]]:
        """Rename the chat. Marks the title as user-set (`title_custom`) so the
        first-message auto-title in send() never clobbers a manual rename.
        Returns the updated summary, or None if the thread is gone."""
        meta = self.read_meta()
        if meta is None:
            return None
        meta['title'] = (title or '').strip()[:80] or 'New chat'
        meta['title_custom'] = True
        self._write_meta(meta)
        return self.summary(meta)

    def set_model(self, model: str) -> Optional[Dict[str, Any]]:
        """Switch the thread's model (#308). Stored in the adapter ctx so the
        next turn's build() reads it; takes effect from the next turn on (a
        running turn already spawned keeps its model). Returns the updated
        summary, or None if the thread is gone. Left untouched (updated_at not
        bumped) so a mid-session model tweak doesn't reorder the chat list."""
        meta = self.read_meta()
        if meta is None:
            return None
        ctx = meta.setdefault('adapter', {})
        ctx['model'] = (model or '').strip()
        self._write_meta(meta, touch=False)
        return self.summary(meta)

    def summary(self, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        m = meta or self.read_meta() or {}
        return {
            'id': self.id,
            'title': m.get('title', 'New chat'),
            'assistant': m.get('assistant'),
            # The per-thread model when the adapter honours one ('' otherwise),
            # so the switcher reflects a reopened thread's choice (#308).
            'model': (m.get('adapter') or {}).get('model') or '',
            'status': self.status(),
            'created_at': m.get('created_at'),
            'updated_at': m.get('updated_at'),
            # Present (unix seconds) only on soft-deleted threads — lets the UI
            # render/sort the "Recently deleted" section.
            'deleted_at': m.get('deleted_at'),
        }

    # ── events ─────────────────────────────────────────────────────────────
    def read_events(self, since_seq: int = 0) -> List[Dict[str, Any]]:
        out = []
        try:
            with open(self.events_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if e.get('seq', 0) > since_seq:
                        out.append(e)
        except OSError:
            pass
        return out

    def transcript(self, since_seq: int = 0) -> Dict[str, Any]:
        """The thread's render-ready transcript + its `source`.

        Prefers Claude Code's own JSONL **session log** (canonical, complete,
        restart-proof) for an idle Claude thread, re-stamped with contiguous
        seq/ts so the frontend's cursor still works. Falls back to the live
        `events.jsonl` **capture** (the successor to tmux pane scraping) while a
        turn is streaming, for a non-Claude adapter, or whenever the log can't
        be located/parsed — so a transcript always renders.

        Hypervisor-only system notices (turn errors, "⏹ Stopped by user")
        can't appear in Claude's log, so any trailing ones from the capture are
        carried over onto the log-sourced transcript."""
        capture = self.read_events()
        log_events = self._session_log_events()
        if log_events is None:
            return {'events': [e for e in capture if e.get('seq', 0) > since_seq],
                    'source': 'capture'}
        # Carry over trailing hypervisor-synthetic notices (errors / stop
        # markers) the log has no record of — tool_results ARE in the log, so
        # only plain system messages + errors are appended.
        extras = [e for e in capture if e.get('role') == 'system'
                  and e.get('type') in ('error', 'message')]
        stamped = self._stamp(log_events + [dict(e) for e in extras])
        return {'events': [e for e in stamped if e.get('seq', 0) > since_seq],
                'source': 'session_log'}

    def _session_log_events(self) -> Optional[List[Dict[str, Any]]]:
        """Parsed Claude session-log events for this thread, or None when the
        log shouldn't/can't be used (non-Claude, still streaming, no id, or the
        file is missing/empty). None means 'fall back to the live capture'."""
        meta = self.read_meta() or {}
        if meta.get('adapter_kind') != 'claude':
            return None
        # A live turn is still being written; the capture streams it best.
        if self.status() == 'running':
            return None
        ctx = meta.get('adapter', {}) or {}
        sid = ctx.get('claude_session_id')
        if not sid:
            return None
        path = locate_claude_session_log(ctx.get('workdir') or WORKSPACE_HOME, sid)
        if not path:
            return None
        events = parse_claude_session_log(path)
        return events or None

    @staticmethod
    def _stamp(partials: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Assign contiguous seq (1..N) + a ts to partial events for delivery.
        The log has no seq of its own; the frontend re-fetches the full
        transcript each poll (since=0), so simple positional numbering is
        correct and stable."""
        out = []
        for i, p in enumerate(partials, start=1):
            e = dict(p)
            e['seq'] = i
            e.setdefault('ts', _now())
            out.append(e)
        return out

    def _next_seq(self) -> int:
        last = 0
        try:
            with open(self.events_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            last = max(last, json.loads(line).get('seq', 0))
                        except json.JSONDecodeError:
                            pass
        except OSError:
            pass
        return last + 1

    _append_lock = threading.Lock()

    def _append(self, partials: List[Dict[str, Any]]) -> None:
        if not partials:
            return
        # Expand any ```choice fence in an assistant message into a prose
        # message + a canonical choice event (harness-agnostic — every adapter's
        # output funnels through here).
        expanded: List[Dict[str, Any]] = []
        for p in partials:
            expanded.extend(_expand_choices(p))
        if not expanded:
            return
        with self._append_lock:
            seq = self._next_seq()
            with open(self.events_path, 'a') as f:
                for p in expanded:
                    e = dict(p)
                    e['seq'] = seq
                    e['ts'] = _now()
                    f.write(json.dumps(e, ensure_ascii=False) + '\n')
                    seq += 1

    # ── turns ──────────────────────────────────────────────────────────────
    def send(self, text: str) -> None:
        """Record a user turn and spawn the runner for the assistant's reply."""
        text = (text or '').strip()
        if not text:
            return
        meta = self.read_meta()
        if not meta:
            return
        first = not self._has_assistant_turn()
        # Title from the first user message — unless the user already renamed
        # the chat by hand (title_custom), in which case leave it alone.
        if (first and not meta.get('title_custom')
                and meta.get('title', 'New chat') in ('New chat', '')):
            meta['title'] = text[:80]
        self._append([{'role': 'user', 'type': 'message', 'text': text}])
        meta['status'] = 'running'
        self._write_meta(meta)
        with _RUNLOCK:
            _RUNNING[self.id] = True
        threading.Thread(target=self._run_turn, args=(text, first, meta),
                         daemon=True).start()

    def stop(self) -> bool:
        """Terminate the turn currently running in this thread, if any.

        Returns True if a running turn was found and signalled, False if the
        thread was already idle (a safe no-op). Sends SIGTERM to the CLI's
        process group, escalating to SIGKILL after a short grace period; the
        runner thread's finally clause records the "stopped" marker and resets
        status to idle.
        """
        with _RUNLOCK:
            proc = _PROCS.get(self.id)
            running = bool(_RUNNING.get(self.id))
            if not running and proc is None:
                return False
            _STOPPING.add(self.id)
        if proc is None:
            # Turn is registered but its Popen hasn't been created yet (tiny
            # window in _run_turn); the stop flag above makes the runner skip
            # finalize once it lands.
            return True
        self._signal_group(proc, signal.SIGTERM)
        # Give it a moment to exit on SIGTERM, then force-kill. Bounded so the
        # request handler never hangs.
        for _ in range(30):  # ~3s total
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        if proc.poll() is None:
            self._signal_group(proc, signal.SIGKILL)
        return True

    @staticmethod
    def _signal_group(proc: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            # Group gone or unavailable — fall back to the direct child.
            try:
                proc.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass

    def _stop_requested(self) -> bool:
        with _RUNLOCK:
            return self.id in _STOPPING

    def _has_assistant_turn(self) -> bool:
        for e in self.read_events():
            if e.get('role') == 'assistant':
                return True
        return False

    # ── runner diagnostics (bounded runner.log) ─────────────────────────────
    def append_runner_log(self, text: str) -> None:
        """Append `text` to the bounded runner.log, keeping only the tail once
        it grows past RUNNER_LOG_MAX_BYTES. Never raises — best-effort logging
        must not perturb a turn."""
        if not text:
            return
        line = text if text.endswith('\n') else text + '\n'
        try:
            with self._runner_log_lock:
                with open(self.runner_log_path, 'a', encoding='utf-8',
                          errors='replace') as f:
                    f.write(line)
                self._cap_runner_log_locked()
        except OSError:
            pass

    def _cap_runner_log_locked(self) -> None:
        """Trim runner.log back to the last RUNNER_LOG_MAX_BYTES when it grows
        past 1.5x the cap. The 1.5x hysteresis is important: the streaming path
        can drive one append per stderr line, and trimming on every append once
        over the cap would make each append O(n) (an O(n^2) chatty turn). We
        instead let it overshoot by 50%, then rewrite once — O(1) amortized.
        Drops the now-partial leading line and prepends a truncation marker so
        the retained tail stays line-valid and honest. Caller must hold
        _runner_log_lock. File size stays bounded at ~1.5x the cap."""
        try:
            if os.path.getsize(self.runner_log_path) <= int(RUNNER_LOG_MAX_BYTES * 1.5):
                return
            with open(self.runner_log_path, 'rb') as f:
                f.seek(-RUNNER_LOG_MAX_BYTES, os.SEEK_END)
                tail = f.read()
        except OSError:
            return
        nl = tail.find(b'\n')
        if nl != -1:
            tail = tail[nl + 1:]
        marker = b'[runner.log truncated - older lines dropped]\n'
        try:
            with open(self.runner_log_path, 'wb') as f:
                f.write(marker + tail)
        except OSError:
            pass

    def read_runner_log(self, tail_bytes: int = 16384) -> str:
        """Return the last `tail_bytes` bytes of runner.log as text (empty if
        absent). Trims a partial leading line so the result starts on a line
        boundary — used by the activity endpoint's raw-log tail."""
        try:
            size = os.path.getsize(self.runner_log_path)
            with open(self.runner_log_path, 'rb') as f:
                if size > tail_bytes:
                    f.seek(-tail_bytes, os.SEEK_END)
                data = f.read()
        except OSError:
            return ''
        if size > tail_bytes:
            nl = data.find(b'\n')
            if nl != -1:
                data = data[nl + 1:]
        return data.decode('utf-8', errors='replace')

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        """Continuously drain the CLI subprocess's stderr into runner.log.

        REQUIRED for the streaming path: the caller consumes proc.stdout in a
        loop and never reads stderr, so a chatty CLI that writes past the
        stderr pipe buffer (~64 KB) would block on write while we wait on
        stdout that never comes — a real deadlock that surfaced as a 'hung
        chat'. Draining stderr here removes that, and persists the diagnostics
        the UI needs to explain a failed/stalled turn."""
        try:
            for line in proc.stderr:
                self.append_runner_log(line.rstrip('\n'))
        except (OSError, ValueError):
            pass

    def _run_turn(self, text: str, first: bool, meta: Dict[str, Any]) -> None:
        adapter = _adapter_for(meta.get('assistant', ''))
        ctx = meta.get('adapter', {})
        try:
            spec = adapter.build(ctx, text, first)
            # Force HOME=/home/dev on every spawned CLI so it finds its config /
            # credentials regardless of the server process's own $HOME.
            env = dict(spec.get('env') or os.environ)
            # Overlay any user-set provider keys (store wins over pod env). For
            # Claude this only re-adds ANTHROPIC_API_KEY if the user explicitly
            # set one — the adapter's default env drops it so oauth is used.
            env.update(_provider_key_overlay())
            env['HOME'] = WORKSPACE_HOME
            proc = subprocess.Popen(
                spec['argv'],
                cwd=spec.get('cwd') or WORKSPACE_HOME,
                env=env,
                stdin=subprocess.PIPE if spec.get('stdin') is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                bufsize=1,
                # Own process group so stop() can signal the whole tree (the CLI
                # may spawn children of its own) with a single killpg.
                start_new_session=True,
            )
            with _RUNLOCK:
                _PROCS[self.id] = proc
            self.append_runner_log(
                f'--- turn start ({time.strftime("%Y-%m-%d %H:%M:%S")}) '
                f'assistant={meta.get("assistant")} cwd={spec.get("cwd") or WORKSPACE_HOME} ---')
            if spec.get('stdin') is not None:
                try:
                    proc.stdin.write(spec['stdin'])
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass

            if spec.get('buffer_stdout'):
                # Fallback path: collect stdout, then emit once. communicate()
                # drains BOTH pipes, so stderr can't deadlock here — capture it
                # into runner.log instead of discarding it.
                try:
                    out, err = proc.communicate(timeout=spec.get('timeout'))
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out, err = proc.communicate()
                    self._append([{'role': 'system', 'type': 'error',
                                   'text': 'assistant timed out'}])
                    self.append_runner_log('runner: assistant timed out; process killed')
                if err:
                    self.append_runner_log(err)
                # A user stop kills the process; its partial/garbled output isn't
                # a real answer, so skip finalize and let the stopped marker land.
                if not self._stop_requested():
                    self._append(adapter.finalize_buffered(ctx, proc.returncode, out))
            else:
                # Streaming path: parse each stdout line into canonical events.
                # We consume stdout here and NEVER read stderr in this loop, so a
                # chatty CLI could fill the stderr pipe and deadlock — drain it
                # concurrently into runner.log (see _drain_stderr).
                stderr_thread = threading.Thread(
                    target=self._drain_stderr, args=(proc,), daemon=True)
                stderr_thread.start()
                for line in proc.stdout:
                    self._append(adapter.parse(ctx, line))
                proc.wait()
                stderr_thread.join(timeout=2)
                if not self._stop_requested():
                    self._append(adapter.finalize(ctx, proc.returncode))
        except FileNotFoundError:
            self._append([{'role': 'system', 'type': 'error',
                           'text': f'assistant binary not found: {meta.get("assistant")}'}])
            self.append_runner_log(f'runner: assistant binary not found: {meta.get("assistant")}')
        except Exception as e:  # never crash the server thread
            _log(f'turn error: {type(e).__name__}: {e}')
            self.append_runner_log(f'runner error: {type(e).__name__}: {e}')
            self._append([{'role': 'system', 'type': 'error',
                           'text': f'{type(e).__name__}: {e}'}])
        finally:
            with _RUNLOCK:
                _RUNNING.pop(self.id, None)
                _PROCS.pop(self.id, None)
                stopped = self.id in _STOPPING
                _STOPPING.discard(self.id)
            if stopped:
                self._append([{'role': 'system', 'type': 'message',
                               'text': '⏹ Stopped by user.'}])
            m = self.read_meta() or meta
            m['adapter'] = ctx  # persist any session id the adapter captured
            m['status'] = 'idle'
            self._write_meta(m)
