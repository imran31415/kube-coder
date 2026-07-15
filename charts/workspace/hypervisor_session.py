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
            for b in (o.get('message', {}) or {}).get('content', []) or []:
                bt = b.get('type')
                if bt == 'text' and b.get('text', '').strip():
                    out.append({'role': 'assistant', 'type': 'message',
                                'text': b['text']})
                elif bt == 'tool_use':
                    out.append({'role': 'assistant', 'type': 'tool_call',
                                'tool_id': b.get('id', ''),
                                'tool': {'name': b.get('name', 'tool'),
                                         'input': b.get('input', {})}})
            return out
        if t == 'user':
            for b in (o.get('message', {}) or {}).get('content', []) or []:
                if b.get('type') == 'tool_result':
                    out.append({'role': 'system', 'type': 'tool_result',
                                'tool_use_id': b.get('tool_use_id', ''),
                                'is_error': bool(b.get('is_error')),
                                'text': _stringify(b.get('content'))})
            return out
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
        # Reuse the model the workspace configured (assistant_command builds
        # `opencode --model '<provider>/<model>'`).
        m = re.search(r"--model\s+'?([^'\s]+)", ctx.get('cli_cmd', '') or '')
        if m:
            argv += ['--model', m.group(1)]
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
        model = os.environ.get('KC_CODEX_MODEL', '')
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

    # ── lifecycle ──────────────────────────────────────────────────────────
    @classmethod
    def create(cls, assistant: str, workdir: str, cli_cmd: str,
               preamble: str = '', title: str = '') -> 'HypervisorSession':
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
            # adapter ctx — carries per-thread state (session ids, preamble).
            'adapter': {
                'assistant': assistant,
                'workdir': workdir,
                'cli_cmd': cli_cmd,
                'preamble': preamble,
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

    def summary(self, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        m = meta or self.read_meta() or {}
        return {
            'id': self.id,
            'title': m.get('title', 'New chat'),
            'assistant': m.get('assistant'),
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
            if spec.get('stdin') is not None:
                try:
                    proc.stdin.write(spec['stdin'])
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass

            if spec.get('buffer_stdout'):
                # Fallback path: collect stdout, then emit once.
                try:
                    out, _ = proc.communicate(timeout=spec.get('timeout'))
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out, _ = proc.communicate()
                    self._append([{'role': 'system', 'type': 'error',
                                   'text': 'assistant timed out'}])
                # A user stop kills the process; its partial/garbled output isn't
                # a real answer, so skip finalize and let the stopped marker land.
                if not self._stop_requested():
                    self._append(adapter.finalize_buffered(ctx, proc.returncode, out))
            else:
                # Streaming path: parse each stdout line into canonical events.
                for line in proc.stdout:
                    self._append(adapter.parse(ctx, line))
                proc.wait()
                if not self._stop_requested():
                    self._append(adapter.finalize(ctx, proc.returncode))
        except FileNotFoundError:
            self._append([{'role': 'system', 'type': 'error',
                           'text': f'assistant binary not found: {meta.get("assistant")}'}])
        except Exception as e:  # never crash the server thread
            _log(f'turn error: {type(e).__name__}: {e}')
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
