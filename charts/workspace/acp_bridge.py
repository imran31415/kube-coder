#!/usr/bin/env python3
"""ACP (Agent Client Protocol) → line-delimited events bridge (issue #639).

Why this file exists
--------------------
Every other Hypervisor adapter drives a ONE-WAY CLI: build an argv, write the
prompt, read line-delimited JSON off stdout until the process exits. The
DeepSeek Harness has no such surface with structured output — `dsh --profile
headless` prints only the last assistant message (prose, no tool calls), and the
structured surface is `dsh --profile acp`, a **bidirectional** JSON-RPC server:
it streams updates AND asks the client questions mid-turn (tool permissions).
An unanswered question stalls the turn forever.

Rather than teach `hypervisor_session`'s process runner to talk back — which
would touch the code path every assistant shares — this bridge is a small
standalone client that converts the bidirectional protocol into exactly the
one-way shape the runner already consumes:

    stdin:  the user's prompt text
    stdout: one JSON object per line, our own small envelope (see EVENTS below)
    exit:   0 when the turn settled normally, non-zero otherwise

So `DeepseekHarnessAdapter` stays an ordinary `_StructuredCliAdapter`, and the
ACP-specific concerns — handshake, request/response correlation, answering the
agent's permission requests, merging tool-call updates, cancellation — all live
here, testable on their own against a stub agent.

EVENTS emitted on stdout (each on one line)
-------------------------------------------
    {"type":"session","sessionId":"<uuid>"}      once, before anything else
    {"type":"message","text":"..."}              a committed assistant message
    {"type":"thought","text":"..."}              reasoning, if the model emits it
    {"type":"tool_call","id":"..","name":"..","title":"..","kind":"..",
     "input":{...}}                              a tool started
    {"type":"tool_result","id":"..","is_error":bool,"text":"..."}
                                                 that tool finished
    {"type":"usage","used":N,"size":N}           context usage, if reported
    {"type":"error","text":"..."}                turn-fatal problem
    {"type":"done","stopReason":"end_turn"}      always last on a settled turn

Text chunks are ACCUMULATED here, not forwarded raw: ACP streams
`agent_message_chunk` deltas, and the Hypervisor renders one message per event,
so a raw forward would render a message per token. A buffer is flushed when the
message id changes, when a tool call interleaves (so the transcript keeps its
real order), or at turn end.

Auth: none at the protocol level (`initialize` advertises `authMethods: []`).
The harness reads `DEEPSEEK_API_KEY` from the environment; a missing or invalid
key surfaces as a JSON-RPC error on `session/prompt`, which becomes an `error`
event and a non-zero exit — never a silent empty turn.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

# The ACP protocol version we speak. The harness answered `1` in-pod; if it ever
# answers something else we still proceed (the spec says the agent replies with
# its own latest when it does not support ours) but we say so on stderr, so a
# protocol drift shows up in the task log instead of as mysterious silence.
PROTOCOL_VERSION = 1

# `dsh --profile acp`. Overridable so an operator can pin a different binary or
# profile without an image rebuild, and so a test can drive a stub agent.
DSH_BIN = os.environ.get('KC_DSH_BIN', 'dsh')
DSH_PROFILE = os.environ.get('KC_DSH_ACP_PROFILE', 'acp')
# Full escape hatch: a JSON array of strings replacing the whole agent argv.
# Wins over KC_DSH_BIN/KC_DSH_ACP_PROFILE when it parses.
DSH_ARGV_ENV = 'KC_DSH_ARGV'

# Total wall-clock budget for one turn. The Hypervisor's own turn timeout sits
# above this; this one exists so a wedged agent yields a readable error event
# rather than a killed process with no transcript.
DEFAULT_TIMEOUT = int(os.environ.get('KC_DSH_TURN_TIMEOUT', '900'))

# How long to wait for the handshake + session setup before giving up. Boot
# loads the whole plugin tree, so it is slower than a normal RPC.
HANDSHAKE_TIMEOUT = int(os.environ.get('KC_DSH_HANDSHAKE_TIMEOUT', '120'))

# ACP config option ids advertised by the harness (captured in-pod).
CONFIG_MODEL = 'model'
CONFIG_EFFORT = 'reasoning_effort'


def _log(msg: str) -> None:
    """Diagnostics go to stderr. stdout is the event stream and nothing else."""
    print(f'[acp-bridge] {msg}', file=sys.stderr, flush=True)


def default_argv() -> List[str]:
    """Command that starts the ACP agent."""
    raw = (os.environ.get(DSH_ARGV_ENV) or '').strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            _log(f'{DSH_ARGV_ENV} is not valid JSON; ignoring it')
        else:
            if (isinstance(parsed, list) and parsed
                    and all(isinstance(x, str) for x in parsed)):
                return parsed
            _log(f'{DSH_ARGV_ENV} must be a non-empty JSON array of strings; '
                 'ignoring it')
    return [DSH_BIN, '--profile', DSH_PROFILE]


class AcpBridge:
    def __init__(self, cwd: str, session_id: str = '', model: str = '',
                 effort: str = '', timeout: int = DEFAULT_TIMEOUT,
                 argv: Optional[List[str]] = None):
        self.cwd = cwd
        self.want_session = session_id
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self.argv = argv or default_argv()

        self.proc: Optional[subprocess.Popen] = None
        self._next_id = 0
        self._pending: Dict[int, Dict[str, Any]] = {}   # id → {'result'|'error'}
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._reader: Optional[threading.Thread] = None
        self._read_failed = False

        self.session_id = ''
        # Tool calls seen this turn: id → {'name','title','kind','input',
        # 'reported'} so a `tool_call_update` can complete a card the agent
        # opened earlier without us re-emitting the whole thing.
        self._tools: Dict[str, Dict[str, Any]] = {}
        # Accumulating assistant/thought text: (kind, message_id, [parts])
        self._buf_kind = ''
        self._buf_msg_id = ''
        self._buf: List[str] = []

    # ── plumbing ────────────────────────────────────────────────────────

    def emit(self, obj: Dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + '\n')
        sys.stdout.flush()

    def _send(self, obj: Dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin:
            return
        try:
            self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + '\n')
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as e:
            _log(f'write to agent failed: {e}')
            self._done.set()

    def request(self, method: str, params: Dict[str, Any],
                timeout: float) -> Dict[str, Any]:
        """Send a JSON-RPC request and block for its response.

        Returns the raw response envelope ({'result': ...} or {'error': ...}).
        A timeout or a dead agent is returned as an `error` rather than raised,
        so every call site handles one shape.
        """
        with self._lock:
            self._next_id += 1
            rid = self._next_id
        self._send({'jsonrpc': '2.0', 'id': rid, 'method': method,
                    'params': params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if rid in self._pending:
                    return self._pending.pop(rid)
            if self._done.is_set() or self._read_failed:
                break
            time.sleep(0.01)
        with self._lock:
            if rid in self._pending:
                return self._pending.pop(rid)
        if self._done.is_set() or self._read_failed:
            return {'error': {'message': f'{method}: agent connection closed'}}
        return {'error': {'message': f'{method}: timed out after {timeout:g}s'}}

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        self._send({'jsonrpc': '2.0', 'method': method, 'params': params})

    def respond(self, rid: Any, result: Dict[str, Any]) -> None:
        self._send({'jsonrpc': '2.0', 'id': rid, 'result': result})

    # ── reader thread ───────────────────────────────────────────────────

    def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # Protocol-only stdout is the agent's documented contract;
                    # anything else is a bug on their side, not ours. Log it and
                    # keep going rather than tearing down a working turn.
                    _log(f'non-JSON line from agent: {line[:200]}')
                    continue
                if not isinstance(msg, dict):
                    continue
                self._dispatch(msg)
        except (OSError, ValueError) as e:
            _log(f'read from agent failed: {e}')
        finally:
            self._read_failed = True
            self._done.set()

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        # A response to something we asked.
        if 'id' in msg and ('result' in msg or 'error' in msg):
            with self._lock:
                self._pending[msg['id']] = msg
            return
        method = msg.get('method') or ''
        # A request FROM the agent — this is the bidirectional half, and the
        # reason a plain subprocess+parse adapter cannot drive ACP.
        if 'id' in msg:
            self._handle_agent_request(msg['id'], method, msg.get('params') or {})
            return
        # A notification from the agent.
        if method == 'session/update':
            self._handle_update(msg.get('params') or {})

    def _handle_agent_request(self, rid: Any, method: str,
                              params: Dict[str, Any]) -> None:
        if method == 'session/request_permission':
            self.respond(rid, {'outcome': self._permission_outcome(params)})
            return
        # We advertise no fs/terminal/elicitation capabilities, so the agent
        # should never ask for those. Answer anything unexpected with an error
        # rather than silence: silence stalls the turn forever.
        self._send({'jsonrpc': '2.0', 'id': rid,
                    'error': {'code': -32601,
                              'message': f'method not supported by client: {method}'}})

    @staticmethod
    def _permission_outcome(params: Dict[str, Any]) -> Dict[str, Any]:
        """Auto-approve tool use.

        The Hypervisor drives the agent by text and has no way to answer an
        interactive approval prompt — the same reason every other assistant is
        launched with its skip-permissions flag (`ante --yolo`, claude
        `--dangerously-skip-permissions`). The pod is the sandbox.

        Prefer an `allow_always` option so a long turn is not re-asked for every
        call; fall back to any `allow_once`, then to the first non-reject option.
        If the agent offers nothing but rejections, say so honestly by picking a
        rejection rather than inventing an option id it would refuse.
        """
        options = params.get('options')
        options = options if isinstance(options, list) else []
        clean = [o for o in options if isinstance(o, dict) and o.get('optionId')]
        for want in ('allow_always', 'allow_once'):
            for o in clean:
                if o.get('kind') == want:
                    return {'outcome': 'selected', 'optionId': o['optionId']}
        for o in clean:
            if not str(o.get('kind') or '').startswith('reject'):
                return {'outcome': 'selected', 'optionId': o['optionId']}
        if clean:
            return {'outcome': 'selected', 'optionId': clean[0]['optionId']}
        return {'outcome': 'cancelled'}

    # ── session/update → our envelope ───────────────────────────────────

    def _handle_update(self, params: Dict[str, Any]) -> None:
        update = params.get('update')
        if not isinstance(update, dict):
            return
        kind = str(update.get('sessionUpdate') or '')
        if kind in ('agent_message_chunk', 'agent_thought_chunk'):
            out = 'message' if kind == 'agent_message_chunk' else 'thought'
            self._buffer(out, update)
            return
        if kind == 'tool_call':
            self._flush()
            self._on_tool_call(update)
            return
        if kind == 'tool_call_update':
            self._flush()
            self._on_tool_update(update)
            return
        if kind == 'usage_update':
            used, size = update.get('used'), update.get('size')
            if isinstance(used, (int, float)) and isinstance(size, (int, float)):
                self.emit({'type': 'usage', 'used': used, 'size': size})
            return
        # user_message_chunk (our own prompt echoed back), plan*, mode, config
        # and compaction updates carry nothing the transcript needs.

    def _buffer(self, out_kind: str, update: Dict[str, Any]) -> None:
        text = _block_text(update.get('content'))
        if not text:
            return
        msg_id = str(update.get('messageId') or '')
        if out_kind != self._buf_kind or msg_id != self._buf_msg_id:
            self._flush()
            self._buf_kind, self._buf_msg_id = out_kind, msg_id
        self._buf.append(text)

    def _flush(self) -> None:
        if not self._buf:
            self._buf_kind = self._buf_msg_id = ''
            return
        text = ''.join(self._buf).strip()
        kind = self._buf_kind or 'message'
        self._buf = []
        self._buf_kind = self._buf_msg_id = ''
        if text:
            self.emit({'type': kind, 'text': text})

    def _on_tool_call(self, update: Dict[str, Any]) -> None:
        tid = str(update.get('toolCallId') or '')
        if not tid:
            return
        rec = {
            'name': update.get('name') or update.get('title') or 'tool',
            'title': update.get('title') or '',
            'kind': update.get('kind') or 'other',
            'input': update.get('rawInput'),
        }
        self._tools[tid] = rec
        self.emit({'type': 'tool_call', 'id': tid, 'name': rec['name'],
                   'title': rec['title'], 'kind': rec['kind'],
                   'input': rec['input'] if isinstance(rec['input'], dict) else {}})
        # A tool call can arrive already finished; do not wait for an update
        # that will never come.
        self._maybe_result(tid, update)

    def _on_tool_update(self, update: Dict[str, Any]) -> None:
        tid = str(update.get('toolCallId') or '')
        if not tid:
            return
        if tid not in self._tools:
            # An update for a call we never saw open — the agent is allowed to
            # do this, and dropping it would lose the tool card entirely.
            self._on_tool_call(update)
            return
        rec = self._tools[tid]
        for field, key in (('name', 'name'), ('title', 'title'), ('kind', 'kind')):
            if update.get(key):
                rec[field] = update[key]
        self._maybe_result(tid, update)

    def _maybe_result(self, tid: str, update: Dict[str, Any]) -> None:
        status = str(update.get('status') or '')
        if status not in ('completed', 'failed'):
            return
        if self._tools.get(tid, {}).get('reported'):
            return
        self._tools.setdefault(tid, {})['reported'] = True
        text = _tool_content_text(update.get('content'))
        if not text:
            raw = update.get('rawOutput')
            text = raw if isinstance(raw, str) else (
                json.dumps(raw, ensure_ascii=False) if raw is not None else '')
        self.emit({'type': 'tool_result', 'id': tid,
                   'is_error': status == 'failed', 'text': text})

    # ── turn ────────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Spawn the agent and complete the ACP handshake + session setup."""
        env = dict(os.environ)
        # stdout must carry protocol traffic only (the agent's own contract);
        # make sure nothing we control adds colour codes to it.
        env.setdefault('NO_COLOR', '1')
        try:
            self.proc = subprocess.Popen(
                self.argv, cwd=self.cwd, env=env, text=True, bufsize=1,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True)
        except (OSError, ValueError) as e:
            self.emit({'type': 'error', 'text': f'cannot start {self.argv[0]}: {e}'})
            return False
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        r = self.request('initialize', {
            'protocolVersion': PROTOCOL_VERSION,
            # We answer permission requests and nothing else: no filesystem
            # proxying, no terminal, no elicitation. Understating capabilities
            # keeps the agent from asking for things we cannot serve — an
            # unanswerable question is a hung turn.
            'clientCapabilities': {
                'fs': {'readTextFile': False, 'writeTextFile': False},
                'terminal': False,
            },
            'clientInfo': {'name': 'kube-coder-hypervisor', 'version': '1'},
        }, HANDSHAKE_TIMEOUT)
        if 'error' in r:
            self.emit({'type': 'error',
                       'text': f'initialize failed: {_err_text(r)}'})
            return False
        got = (r.get('result') or {}).get('protocolVersion')
        if got != PROTOCOL_VERSION:
            _log(f'agent speaks protocol {got}, we asked for {PROTOCOL_VERSION}')

        return self._open_session()

    def _open_session(self) -> bool:
        cfg: List[Dict[str, Any]] = []
        if self.want_session:
            r = self.request('session/resume',
                             {'sessionId': self.want_session, 'cwd': self.cwd,
                              'mcpServers': []}, HANDSHAKE_TIMEOUT)
            if 'error' not in r:
                self.session_id = self.want_session
                cfg = (r.get('result') or {}).get('configOptions') or []
            else:
                # A session that cannot be resumed (pruned, different cwd,
                # older harness) must not kill the turn — start a fresh one and
                # say why. Losing history is much better than losing the turn.
                _log(f'resume of {self.want_session} failed ({_err_text(r)}); '
                     'starting a new session')
        if not self.session_id:
            r = self.request('session/new', {'cwd': self.cwd, 'mcpServers': []},
                             HANDSHAKE_TIMEOUT)
            if 'error' in r:
                self.emit({'type': 'error',
                           'text': f'session/new failed: {_err_text(r)}'})
                return False
            res = r.get('result') or {}
            self.session_id = str(res.get('sessionId') or '')
            cfg = res.get('configOptions') or []
            if not self.session_id:
                self.emit({'type': 'error',
                           'text': 'session/new returned no sessionId'})
                return False
        self.emit({'type': 'session', 'sessionId': self.session_id})
        self._apply_config(cfg)
        return True

    def _apply_config(self, options: Any) -> None:
        """Select model / reasoning effort from what this session advertises.

        Both are best-effort by design: an unknown or unavailable choice is
        logged and skipped, never fatal. The harness's own default is always a
        working configuration, so a bad pick must degrade to it rather than
        fail the turn.
        """
        opts = options if isinstance(options, list) else []
        by_id = {o.get('id'): o for o in opts if isinstance(o, dict)}
        if self.model:
            value = _match_config_value(by_id.get(CONFIG_MODEL), self.model)
            if value is None:
                _log(f'model {self.model!r} not offered by this session; '
                     'keeping the harness default')
            else:
                self._set_config(CONFIG_MODEL, value)
        if self.effort:
            value = _match_config_value(by_id.get(CONFIG_EFFORT), self.effort)
            if value is None:
                _log(f'effort {self.effort!r} not offered by this session; '
                     'keeping the harness default')
            else:
                self._set_config(CONFIG_EFFORT, value)

    def _set_config(self, config_id: str, value: str) -> None:
        r = self.request('session/set_config_option',
                         {'sessionId': self.session_id, 'configId': config_id,
                          'value': value}, 30)
        if 'error' in r:
            _log(f'set {config_id}={value!r} failed: {_err_text(r)}')

    def prompt(self, text: str) -> int:
        """Run one turn. Returns the process exit code to use."""
        r = self.request('session/prompt', {
            'sessionId': self.session_id,
            'prompt': [{'type': 'text', 'text': text}],
        }, self.timeout)
        self._flush()
        if 'error' in r:
            self.emit({'type': 'error', 'text': _err_text(r)})
            return 1
        stop = str((r.get('result') or {}).get('stopReason') or '')
        self.emit({'type': 'done', 'stopReason': stop or 'end_turn'})
        # `refusal` and `cancelled` are real outcomes, not failures: the
        # transcript already shows what happened, so exiting non-zero would
        # make the adapter print a spurious "exited with code 1" on top of it.
        return 0

    def cancel(self) -> None:
        if self.session_id:
            self.notify('session/cancel', {'sessionId': self.session_id})

    def close(self) -> None:
        if self.session_id:
            self.request('session/close', {'sessionId': self.session_id}, 10)
        p = self.proc
        if not p:
            return
        try:
            if p.stdin:
                p.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                p.terminate()
                p.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    p.kill()
                except OSError:
                    pass

    def _drain_stderr(self) -> None:
        """Forward the agent's stderr so its diagnostics reach the task log.

        REQUIRED, not cosmetic: an undrained stderr pipe fills and blocks the
        agent mid-turn.
        """
        p = self.proc
        if not p or not p.stderr:
            return
        try:
            for line in p.stderr:
                _log(f'dsh: {line.rstrip()}')
        except (OSError, ValueError):
            pass


# ── helpers ─────────────────────────────────────────────────────────────

def _err_text(envelope: Dict[str, Any]) -> str:
    err = envelope.get('error')
    if isinstance(err, dict):
        msg = err.get('message')
        if isinstance(msg, str) and msg:
            data = err.get('data')
            extra = _stringify(data)
            return f'{msg}: {extra}' if extra and extra not in msg else msg
        return _stringify(err) or 'unknown error'
    return _stringify(err) or 'unknown error'


def _stringify(v: Any) -> str:
    if v is None:
        return ''
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(v)


def _block_text(block: Any) -> str:
    """Text out of one ACP ContentBlock. Non-text blocks render as a marker so
    the transcript shows that something was there rather than dropping it."""
    if not isinstance(block, dict):
        return ''
    t = block.get('type')
    if t == 'text':
        return block.get('text') or ''
    if t == 'resource_link':
        name = block.get('name') or block.get('uri') or 'resource'
        return f'[resource_link {name}]'
    if t == 'resource':
        res = block.get('resource')
        if isinstance(res, dict):
            return res.get('text') or f"[resource {res.get('uri') or ''}]"
        return '[resource]'
    if t in ('image', 'audio'):
        return f'[{t}]'
    return ''


def _tool_content_text(content: Any) -> str:
    """Flatten ACP ToolCallContent[] into readable text for a tool result."""
    if not isinstance(content, list):
        return ''
    parts: List[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        t = item.get('type')
        if t == 'content':
            txt = _block_text(item.get('content'))
            if txt:
                parts.append(txt)
        elif t == 'diff':
            path = item.get('path') or ''
            parts.append(f'[diff {path}]'.strip())
        elif t == 'terminal':
            parts.append(f"[terminal {item.get('terminalId') or ''}]".strip())
    return '\n'.join(p for p in parts if p)


def _match_config_value(option: Any, wanted: str) -> Optional[str]:
    """Resolve a human-friendly pick against one advertised config option.

    The harness's model values are JSON-encoded provider/model pairs, e.g.
    `["deepseek-official","deepseek-v4-pro"]`, which is not something a picker
    entry or a `KC_*_MODEL` env var should have to spell. Match, in order, the
    exact value, the option's display name, and any element of a JSON-array
    value — so `deepseek-v4-pro`, `DeepSeek-V4-Pro` and the full pair all work.
    Returns None when nothing matches, meaning "keep the default".
    """
    if not isinstance(option, dict) or not wanted:
        return None
    want = wanted.strip()
    want_lc = want.lower()
    candidates: List[Dict[str, Any]] = []

    def collect(items: Any) -> None:
        if not isinstance(items, list):
            return
        for it in items:
            if not isinstance(it, dict):
                continue
            if 'value' in it:
                candidates.append(it)
            # Grouped selects nest their real entries one level down.
            collect(it.get('options'))

    collect(option.get('options'))
    for c in candidates:
        if str(c.get('value')) == want:
            return str(c.get('value'))
    for c in candidates:
        if str(c.get('name') or '').lower() == want_lc:
            return str(c.get('value'))
    for c in candidates:
        raw = str(c.get('value') or '')
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, list) and any(
                isinstance(p, str) and p.lower() == want_lc for p in parsed):
            return raw
    return None


def _read_prompt() -> str:
    """The prompt arrives on stdin, not argv: it is arbitrary user text and can
    be long, multi-line, and full of shell metacharacters."""
    try:
        return sys.stdin.read()
    except (OSError, ValueError):
        return ''


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description='Drive `dsh --profile acp` and emit line-delimited events.')
    ap.add_argument('--cwd', default=os.environ.get('KC_WORKSPACE_HOME', '/home/dev'),
                    help='workspace root for the session (absolute)')
    ap.add_argument('--session', default='',
                    help='resume this ACP session id instead of opening a new one')
    ap.add_argument('--model', default='',
                    help='model to select, e.g. deepseek-v4-pro (best-effort)')
    ap.add_argument('--effort', default='',
                    help='reasoning effort in the harness vocabulary '
                         '(off/low/high/max), best-effort')
    ap.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT,
                    help='seconds to allow one turn to settle')
    args = ap.parse_args(argv)

    text = _read_prompt()
    if not text.strip():
        print(json.dumps({'type': 'error', 'text': 'empty prompt'}), flush=True)
        return 2

    bridge = AcpBridge(cwd=args.cwd, session_id=args.session, model=args.model,
                       effort=args.effort, timeout=args.timeout)

    def on_signal(_signum, _frame):
        # A user-issued stop should cancel the TURN, giving the agent a chance
        # to emit its final updates, rather than killing the process and losing
        # everything it had already done.
        bridge.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, on_signal)
        except (ValueError, OSError):
            pass

    if not bridge.start():
        bridge.close()
        return 1
    try:
        return bridge.prompt(text)
    finally:
        bridge.close()


if __name__ == '__main__':
    sys.exit(main())
