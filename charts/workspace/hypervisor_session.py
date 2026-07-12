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
    }

Persistence lives under ~/.claude-tasks/hypervisor/<thread_id>/:
    thread.json   — metadata (title, assistant, workdir, status, session ids)
    events.jsonl  — canonical transcript (append-only)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

# ───────────────────────────────────────────────────────────────────────────
# Paths / constants
# ───────────────────────────────────────────────────────────────────────────
HOME = os.environ.get('HOME', '/home/dev')
HYPERVISOR_DIR = os.path.join(HOME, '.claude-tasks', 'hypervisor')

# Strip ANSI/VT escape sequences from fallback CLI output so a non-structured
# assistant still reads as clean prose, never raw terminal control codes.
_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|[\x00-\x08\x0b\x0c\x0e-\x1f]')

# Per-turn wall-clock ceiling for a fallback CLI (Claude manages its own).
FALLBACK_TURN_TIMEOUT = float(os.environ.get('KC_HYPERVISOR_FALLBACK_TIMEOUT', '180'))


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
        return {'argv': argv, 'cwd': ctx.get('workdir') or HOME, 'env': env}

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
        env.update({'TERM': 'dumb', 'NO_COLOR': '1', 'CI': '1'})
        return {'argv': ['bash', '-lc', cli_cmd], 'cwd': ctx.get('workdir') or HOME,
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


_ADAPTERS: Dict[str, Adapter] = {
    'claude': ClaudeAdapter(),
    'fallback': FallbackAdapter(),
}


def _adapter_for(assistant: str) -> Adapter:
    return _ADAPTERS.get(assistant, _ADAPTERS['fallback'])


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
    def list(cls) -> List[Dict[str, Any]]:
        if not os.path.isdir(HYPERVISOR_DIR):
            return []
        out = []
        for tid in os.listdir(HYPERVISOR_DIR):
            s = cls.get(tid)
            if s:
                m = s.read_meta()
                if m:
                    out.append(s.summary(m))
        out.sort(key=lambda t: t.get('updated_at') or 0, reverse=True)
        return out

    def delete(self) -> None:
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    # ── meta ───────────────────────────────────────────────────────────────
    def read_meta(self) -> Optional[Dict[str, Any]]:
        try:
            with open(self.meta_path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def _write_meta(self, meta: Dict[str, Any]) -> None:
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

    def summary(self, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        m = meta or self.read_meta() or {}
        return {
            'id': self.id,
            'title': m.get('title', 'New chat'),
            'assistant': m.get('assistant'),
            'status': self.status(),
            'created_at': m.get('created_at'),
            'updated_at': m.get('updated_at'),
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
        with self._append_lock:
            seq = self._next_seq()
            with open(self.events_path, 'a') as f:
                for p in partials:
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
        # Title from the first user message.
        if meta.get('title', 'New chat') in ('New chat', '') and first:
            meta['title'] = text[:80]
        self._append([{'role': 'user', 'type': 'message', 'text': text}])
        meta['status'] = 'running'
        self._write_meta(meta)
        with _RUNLOCK:
            _RUNNING[self.id] = True
        threading.Thread(target=self._run_turn, args=(text, first, meta),
                         daemon=True).start()

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
            proc = subprocess.Popen(
                spec['argv'],
                cwd=spec.get('cwd') or HOME,
                env=spec.get('env') or os.environ,
                stdin=subprocess.PIPE if spec.get('stdin') is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                bufsize=1,
            )
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
                self._append(adapter.finalize_buffered(ctx, proc.returncode, out))
            else:
                # Streaming path: parse each stdout line into canonical events.
                for line in proc.stdout:
                    self._append(adapter.parse(ctx, line))
                proc.wait()
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
            m = self.read_meta() or meta
            m['adapter'] = ctx  # persist any session id the adapter captured
            m['status'] = 'idle'
            self._write_meta(m)
