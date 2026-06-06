#!/usr/bin/env python3
"""MCP server exposing agent orchestration tools to running agents.

Allows any running agent (Claude, Ante, OpenCode) to spawn sub-agents,
monitor their progress, and collect results via MCP tools.

Protocol: JSON-RPC 2.0 over stdin/stdout (newline-delimited, same as
mcp_memory.py). Each request is one JSON line on stdin; each response
is one JSON line on stdout.

Lifecycle: read a line → dispatch → write a response line. EOF on stdin
exits cleanly.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
import traceback
import uuid
from typing import Any, Dict, List, Optional

# ───────────────────────────────────────────────────────────────────────────
# Constants
# ───────────────────────────────────────────────────────────────────────────
TASKS_DIR = '/home/dev/.claude-tasks'
_LOG = sys.stderr


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


# Resource guards. The agent-orchestrator MCP is available to every agent —
# including spawned ones — so without limits a recursive or looping agent
# could fork-bomb the (shared, multi-tenant) pod and burn unbounded API
# tokens. Overridable via env for ops tuning.
MAX_CONCURRENT_SUBAGENTS = _int_env('KC_MAX_SUBAGENTS', 8)
MAX_SPAWN_DEPTH = _int_env('KC_MAX_SPAWN_DEPTH', 3)
# Cap the output returned to the model when no explicit tail is requested,
# so a long-running agent's unbounded pane log can't blow up a response.
MAX_OUTPUT_BYTES = 256 * 1024


# ───────────────────────────────────────────────────────────────────────────
# Logging
# ───────────────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    try:
        _LOG.write(f'[mcp_agent_orch] {msg}\n')
        _LOG.flush()
    except Exception:
        pass


# ───────────────────────────────────────────────────────────────────────────
# JSON-RPC framing
# ───────────────────────────────────────────────────────────────────────────
_OUT = sys.stdout


def _send(msg: Dict[str, Any]) -> None:
    line = json.dumps(msg, ensure_ascii=False, separators=(',', ':'))
    _OUT.write(line)
    _OUT.write('\n')
    _OUT.flush()


def _error(id_val: Any, code: int, message: str, data: Any = None) -> None:
    err: Dict[str, Any] = {'code': code, 'message': message}
    if data is not None:
        err['data'] = data
    _send({'jsonrpc': '2.0', 'id': id_val, 'error': err})


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────

def _shell_quote(s: str) -> str:
    """Minimal safe quoting for bash -c argument injection."""
    if not s:
        return "''"
    if all(c.isalnum() or c in '/._-@:' for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


def _task_dir(task_id: str) -> str:
    return os.path.join(TASKS_DIR, task_id)


def _meta_path(task_dir: str) -> str:
    return os.path.join(task_dir, 'task.json')


def _read_meta(task_dir: str) -> Optional[Dict[str, Any]]:
    path = _meta_path(task_dir)
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_meta(task_dir: str, meta: Dict[str, Any]) -> bool:
    path = _meta_path(task_dir)
    try:
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(meta, f, indent=2)
        os.rename(tmp, path)
        return True
    except OSError as e:
        _log(f'write_meta failed: {e}')
        return False


def _reconcile_status(meta: Dict[str, Any], task_dir: str) -> None:
    """Update status from running→completed if tmux session is gone."""
    if meta.get('status') not in ('running', 'waiting-for-input'):
        return
    session = meta.get('tmux_session', '')
    if not session:
        return
    check = subprocess.run(
        ['tmux', 'has-session', '-t', session],
        capture_output=True, text=True,
    )
    if check.returncode != 0:
        # Session gone → the wrapped CLI exited. Read the exit code the
        # spawn wrapper persisted so a crash (non-zero) is reported as
        # 'error' instead of being indistinguishable from a clean finish.
        exit_code = _read_exit_code(task_dir)
        if exit_code is not None and exit_code != 0:
            meta['status'] = 'error'
            meta['error'] = f'agent exited with code {exit_code}'
        else:
            meta['status'] = 'completed'
        if exit_code is not None:
            meta['exit_code'] = exit_code
        meta['finished_at'] = time.time()
        meta.pop('waiting_for_input', None)
        meta.pop('last_input_prompt', None)
        _write_meta(task_dir, meta)


def _session_is_alive(session_name: str) -> bool:
    r = subprocess.run(
        ['tmux', 'has-session', '-t', session_name],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def _output_log_path(task_dir: str) -> str:
    return os.path.join(task_dir, 'output.log')


def _read_output(task_dir: str, tail: Optional[int] = None) -> str:
    log_path = _output_log_path(task_dir)
    if not os.path.isfile(log_path):
        return '(no output available)'
    try:
        with open(log_path, 'r', errors='replace') as f:
            if tail is not None:
                lines = f.readlines()
                return ''.join(lines[-tail:])
            # No tail requested: still cap the payload to the last
            # MAX_OUTPUT_BYTES so an unbounded log can't blow up the
            # response sent to the model.
            try:
                size = os.fstat(f.fileno()).st_size
                if size > MAX_OUTPUT_BYTES:
                    f.seek(size - MAX_OUTPUT_BYTES)
                    return '(…output truncated…)\n' + f.read()
            except OSError:
                pass
            return f.read()
    except OSError:
        return '(read error)'


def _exit_code_path(task_dir: str) -> str:
    return os.path.join(task_dir, 'exit_code')


def _read_exit_code(task_dir: str) -> Optional[int]:
    """Read the wrapped CLI's exit status, if it has been written yet."""
    try:
        with open(_exit_code_path(task_dir), 'r') as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _current_depth() -> int:
    """Nesting depth of the agent calling this MCP server (0 = top level)."""
    return _int_env('KC_AGENT_DEPTH', 0)


def _count_live_agent_sessions() -> int:
    """Count live tmux sessions named like agent tasks (claude-<id>).

    Covers both dashboard-created and spawned agents, giving a conservative
    pod-wide cap rather than a per-parent one.
    """
    r = subprocess.run(
        ['tmux', 'list-sessions', '-F', '#{session_name}'],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return 0
    return sum(1 for name in r.stdout.splitlines() if name.startswith('claude-'))


def _append_sub_task_id(parent_task_id: str, child_task_id: str) -> None:
    """Record child lineage on the parent's task.json (best-effort)."""
    parent_dir = _task_dir(parent_task_id)
    meta = _read_meta(parent_dir)
    if meta is None:
        return
    subs = meta.get('sub_task_ids') or []
    if child_task_id not in subs:
        subs.append(child_task_id)
        meta['sub_task_ids'] = subs
        _write_meta(parent_dir, meta)


# Assistants with a non-interactive one-shot "print" mode that exits when
# the task is done. Anything not listed has no reliable headless interface
# (kc-harness) and is always run interactively (prompt pasted into the REPL).
_HEADLESS_CAPABLE = {'claude', 'ante', 'opencode-openrouter', 'opencode-deepseek'}


def _opencode_model(assistant: str) -> str:
    if assistant == 'opencode-deepseek':
        return 'deepseek/' + os.environ.get('KC_DEEPSEEK_MODEL', 'deepseek-chat')
    return 'openrouter/' + os.environ.get('KC_OPENROUTER_MODEL', 'anthropic/claude-sonnet-4')


def _assistant_command(assistant: str, prompt: str = '', headless: bool = True) -> str:
    """Build the shell command that launches an assistant.

    headless=True → a one-shot 'print' invocation that takes the prompt on
    the command line and EXITS when finished, so completion is detectable via
    session death + exit code (the orchestration default). headless=False →
    the bare interactive REPL; the caller pastes the prompt afterwards. Used
    for long-lived sessions a human will attach to.

    Sub-agents run autonomously with no human to approve tool calls, so the
    headless commands include each CLI's skip-approval flag. They run inside
    the same isolated per-user workspace pod the dashboard already trusts.
    """
    if not headless or assistant not in _HEADLESS_CAPABLE:
        # Interactive REPL — prompt delivered via tmux paste by the caller.
        if assistant in ('opencode-openrouter', 'opencode-deepseek'):
            return f'opencode --model {_shell_quote(_opencode_model(assistant))}'
        if assistant == 'kc-harness':
            return 'python3 /tmp/browser/harness.py'
        return assistant if assistant in ('claude', 'ante') else 'claude'

    q = _shell_quote(prompt)
    if assistant == 'claude':
        return f'claude --dangerously-skip-permissions -p {q}'
    if assistant == 'ante':
        return f'ante --yolo -p {q}'
    # OpenCode one-shot: `opencode run <message>` is non-interactive.
    return f'opencode run --model {_shell_quote(_opencode_model(assistant))} {q}'


def _wait_pane_ready(session_name: str, min_delay: float = 1.5,
                     timeout: float = 15.0, interval: float = 1.0) -> None:
    """Wait until an interactive REPL pane looks initialized before pasting.

    Heuristic: after a short minimum delay, sample the pane and consider it
    ready once it's non-empty and unchanged across two consecutive samples
    (settled), bailing at `timeout`. Replaces a brittle fixed sleep that
    dropped prompts when CLI init was slow (first run / model download).
    """
    time.sleep(min_delay)
    deadline = time.time() + timeout
    prev = None
    while time.time() < deadline:
        r = subprocess.run(['tmux', 'capture-pane', '-p', '-t', session_name],
                           capture_output=True, text=True)
        cur = r.stdout.strip()
        if cur and cur == prev:
            return
        prev = cur
        time.sleep(interval)


# ───────────────────────────────────────────────────────────────────────────
# Tool implementations
# ───────────────────────────────────────────────────────────────────────────

_ASSISTANTS_LIST = [
    {'id': 'claude', 'label': 'Claude Code'},
    {'id': 'ante', 'label': 'Ante CLI'},
    {'id': 'opencode-openrouter', 'label': 'OpenRouter'},
    {'id': 'opencode-deepseek', 'label': 'DeepSeek'},
    {'id': 'kc-harness', 'label': 'Opensource GPU'},
]


def _tool_spawn_agent(args: Dict[str, Any]) -> Dict[str, Any]:
    """Spawn a sub-agent task in a new tmux session."""
    prompt = (args.get('prompt') or '').strip()
    if not prompt:
        return {'isError': True, 'content': [{'type': 'text', 'text': 'prompt is required'}]}

    assistant = args.get('assistant', 'ante')
    workdir = args.get('workdir', '/home/dev')
    parent_task_id = args.get('parent_task_id') or os.environ.get('KC_TASK_ID')

    # Headless one-shot is the orchestration default: the agent exits when
    # done, so completion is detectable. 'interactive' keeps a long-lived
    # REPL for human attach. Assistants without a headless mode (kc-harness)
    # fall back to interactive regardless.
    requested_interactive = args.get('mode') == 'interactive'
    use_headless = (not requested_interactive) and assistant in _HEADLESS_CAPABLE
    mode = 'headless' if use_headless else 'interactive'

    # Depth guard: refuse to spawn beyond MAX_SPAWN_DEPTH so a recursive
    # agent (a spawned agent that itself spawns) can't fork-bomb the pod
    # or burn unbounded API tokens. Depth is inherited via KC_AGENT_DEPTH.
    depth = _current_depth()
    if depth >= MAX_SPAWN_DEPTH:
        return {'isError': True, 'content': [{'type': 'text', 'text':
            f'spawn refused: max nesting depth {MAX_SPAWN_DEPTH} reached '
            f'(current depth {depth}). Do the work in this agent instead.'}]}

    # Concurrency guard: cap simultaneously-live agent sessions to protect
    # the shared pod's CPU/memory.
    live = _count_live_agent_sessions()
    if live >= MAX_CONCURRENT_SUBAGENTS:
        return {'isError': True, 'content': [{'type': 'text', 'text':
            f'spawn refused: {live} agent sessions already running '
            f'(max {MAX_CONCURRENT_SUBAGENTS}). Wait for some to finish.'}]}

    task_id = f"{int(time.time())}-{secrets.token_hex(4)}"
    session_name = f'claude-{task_id}'
    task_dir = _task_dir(task_id)
    os.makedirs(task_dir, mode=0o700, exist_ok=True)

    meta: Dict[str, Any] = {
        'task_id': task_id,
        'session_id': str(uuid.uuid4()),
        'prompt': prompt,
        # A recognizable name so spawned agents read as sub-agents (not
        # human tasks) in the flat dashboard list; the prompt drops to the
        # row subtitle. The dashboard also badges these via parent_task_id.
        'name': f'↳ sub-agent · {assistant}',
        'workdir': workdir,
        'status': 'running',
        'created_at': time.time(),
        'tmux_session': session_name,
        'assistant': assistant,
        'mode': mode,
        'parent_task_id': parent_task_id,
        'depth': depth + 1,
        'sub_task_ids': [],
    }

    if not _write_meta(task_dir, meta):
        # Nothing to clean up — the tmux session isn't created until below.
        return {'isError': True, 'content': [{'type': 'text', 'text': 'Failed to write task metadata'}]}

    # Write prompt file
    prompt_file = os.path.join(task_dir, 'prompt.txt')
    try:
        with open(prompt_file, 'w') as f:
            f.write(prompt)
    except OSError as e:
        return {'isError': True, 'content': [{'type': 'text', 'text': f'Failed to write prompt: {e}'}]}

    # Build the CLI command. In headless mode the prompt is on the command
    # line and the CLI exits when done; interactive mode launches the bare
    # REPL and the prompt is pasted below. Either way we wrap so the CLI's
    # exit status is persisted when the session ends — lets _reconcile_status
    # tell a crash (non-zero) apart from a clean finish.
    cli_cmd = _assistant_command(assistant, prompt, headless=use_headless)
    exit_file = _exit_code_path(task_dir)
    shell_cmd = (f'cd {_shell_quote(workdir)} && {cli_cmd}; '
                 f'echo $? > {_shell_quote(exit_file)}')

    # Spawn tmux session. KC_AGENT_DEPTH is bumped so the spawned agent's
    # own orchestrator MCP enforces the depth cap one level deeper.
    tmux_result = subprocess.run(
        ['tmux', 'new-session', '-d',
         '-s', session_name,
         '-x', '220', '-y', '50',
         '-e', f'KC_TASK_ID={task_id}',
         '-e', f'KC_AGENT_DEPTH={depth + 1}',
         'bash', '-lc', shell_cmd],
        capture_output=True, text=True,
    )
    if tmux_result.returncode != 0:
        meta['status'] = 'error'
        meta['error'] = tmux_result.stderr.strip()
        _write_meta(task_dir, meta)
        return {'isError': True, 'content': [{'type': 'text', 'text': f'tmux failed: {tmux_result.stderr.strip()}'}]}

    # Pipe pane output to log file
    output_log = _output_log_path(task_dir)
    subprocess.run(
        ['tmux', 'pipe-pane', '-o', '-t', session_name,
         f'cat >> {_shell_quote(output_log)}'],
        capture_output=True, text=True,
    )

    # Record lineage on the parent so list_subagents and the dashboard can
    # surface the tree directly from the parent's task.json.
    if parent_task_id:
        _append_sub_task_id(parent_task_id, task_id)

    # Interactive mode only: paste the prompt once the REPL has initialized.
    # (Headless mode already carries the prompt on the command line.)
    if not use_headless:
        def _send_prompt():
            _wait_pane_ready(session_name)
            try:
                buf = f'prompt-{task_id}'
                subprocess.run(['tmux', 'load-buffer', '-b', buf, prompt_file],
                               capture_output=True, text=True, check=True)
                subprocess.run(['tmux', 'paste-buffer', '-b', buf, '-t', session_name],
                               capture_output=True, text=True, check=True)
                subprocess.run(['tmux', 'send-keys', '-t', session_name, 'Enter'],
                               capture_output=True, text=True)
                subprocess.run(['tmux', 'delete-buffer', '-b', buf],
                               capture_output=True, text=True)
            except Exception as e:
                _log(f'prompt paste failed for {task_id}: {e}')

        threading.Thread(target=_send_prompt, daemon=True).start()

    return {
        'content': [{'type': 'text', 'text': json.dumps({
            'task_id': task_id,
            'tmux_session': session_name,
            'status': 'running',
            'assistant': assistant,
            'mode': mode,
        })}],
    }


def _tool_get_agent_status(args: Dict[str, Any]) -> Dict[str, Any]:
    """Get the current status of a spawned agent task."""
    task_id = args.get('task_id', '')
    if not task_id:
        return {'isError': True, 'content': [{'type': 'text', 'text': 'task_id is required'}]}
    task_dir = _task_dir(task_id)
    meta = _read_meta(task_dir)
    if meta is None:
        return {'isError': True, 'content': [{'type': 'text', 'text': f'Task {task_id} not found'}]}
    _reconcile_status(meta, task_dir)
    return {
        'content': [{'type': 'text', 'text': json.dumps({
            'task_id': meta['task_id'],
            'status': meta.get('status', 'unknown'),
            'assistant': meta.get('assistant', 'claude'),
            'mode': meta.get('mode', 'interactive'),
            'exit_code': meta.get('exit_code'),
            'created_at': meta.get('created_at'),
            'finished_at': meta.get('finished_at'),
            'prompt': (meta.get('prompt', '') or '')[:200],
        })}],
    }


def _tool_list_subagents(args: Dict[str, Any]) -> Dict[str, Any]:
    """List all sub-agents spawned by a parent task."""
    parent_task_id = args.get('parent_task_id') or os.environ.get('KC_TASK_ID')
    if not parent_task_id:
        return {'isError': True, 'content': [{'type': 'text', 'text': 'parent_task_id is required'}]}

    sub_tasks: List[Dict[str, Any]] = []
    try:
        entries = sorted(os.listdir(TASKS_DIR), reverse=True)
    except OSError:
        entries = []

    for entry in entries:
        task_dir = os.path.join(TASKS_DIR, entry)
        meta_path = _meta_path(task_dir)
        if not os.path.isfile(meta_path):
            continue
        meta = _read_meta(task_dir)
        if meta is None:
            continue
        if meta.get('parent_task_id') != parent_task_id:
            continue
        _reconcile_status(meta, task_dir)
        sub_tasks.append({
            'task_id': meta['task_id'],
            'status': meta.get('status', 'unknown'),
            'assistant': meta.get('assistant', 'claude'),
            'created_at': meta.get('created_at'),
            'finished_at': meta.get('finished_at'),
            'prompt': (meta.get('prompt', '') or '')[:200],
        })

    return {
        'content': [{'type': 'text', 'text': json.dumps({
            'sub_tasks': sub_tasks,
            'count': len(sub_tasks),
        })}],
    }


def _tool_get_agent_output(args: Dict[str, Any]) -> Dict[str, Any]:
    """Get the output log from a spawned agent task."""
    task_id = args.get('task_id', '')
    if not task_id:
        return {'isError': True, 'content': [{'type': 'text', 'text': 'task_id is required'}]}
    task_dir = _task_dir(task_id)
    meta = _read_meta(task_dir)
    if meta is None:
        return {'isError': True, 'content': [{'type': 'text', 'text': f'Task {task_id} not found'}]}
    _reconcile_status(meta, task_dir)
    tail = args.get('tail')
    output = _read_output(task_dir, tail=tail)
    return {
        'content': [{'type': 'text', 'text': json.dumps({
            'task_id': task_id,
            'status': meta.get('status', 'unknown'),
            'output': output,
        })}],
    }


def _tool_wait_for_agent(args: Dict[str, Any]) -> Dict[str, Any]:
    """Block until a spawned agent task completes, then return its output.

    Note: this blocks the server's single-threaded request loop for up to
    `timeout` seconds, so no other tool call on the same MCP connection is
    serviced meanwhile. That's fine for the usual one-call-at-a-time MCP
    client, but avoid very long timeouts.
    """
    task_id = args.get('task_id', '')
    if not task_id:
        return {'isError': True, 'content': [{'type': 'text', 'text': 'task_id is required'}]}
    poll_interval = args.get('poll_interval', 2.0)
    timeout = args.get('timeout', 300.0)
    tail = args.get('tail', 100)
    task_dir = _task_dir(task_id)
    deadline = time.time() + timeout

    while time.time() < deadline:
        meta = _read_meta(task_dir)
        if meta is None:
            return {'isError': True, 'content': [{'type': 'text', 'text': f'Task {task_id} not found'}]}
        _reconcile_status(meta, task_dir)
        status = meta.get('status', 'unknown')
        if status not in ('running', 'waiting-for-input'):
            output = _read_output(task_dir, tail=tail)
            return {
                'content': [{'type': 'text', 'text': json.dumps({
                    'task_id': task_id,
                    'status': status,
                    'output': output,
                })}],
            }
        time.sleep(poll_interval)

    return {
        'content': [{'type': 'text', 'text': json.dumps({
            'task_id': task_id,
            'status': 'timeout',
            'error': f'Task did not complete within {timeout}s timeout',
        })}],
    }


def _tool_kill_agent(args: Dict[str, Any]) -> Dict[str, Any]:
    """Kill a spawned agent task."""
    task_id = args.get('task_id', '')
    if not task_id:
        return {'isError': True, 'content': [{'type': 'text', 'text': 'task_id is required'}]}
    task_dir = _task_dir(task_id)
    meta = _read_meta(task_dir)
    if meta is None:
        return {'isError': True, 'content': [{'type': 'text', 'text': f'Task {task_id} not found'}]}

    session_name = meta.get('tmux_session', f'claude-{task_id}')
    subprocess.run(['tmux', 'kill-session', '-t', session_name],
                   capture_output=True, text=True)
    meta['status'] = 'killed'
    meta['killed_at'] = time.time()
    _write_meta(task_dir, meta)
    return {
        'content': [{'type': 'text', 'text': json.dumps({
            'task_id': task_id,
            'status': 'killed',
        })}],
    }


# ───────────────────────────────────────────────────────────────────────────
# Tool registry
# ───────────────────────────────────────────────────────────────────────────
TOOLS: Dict[str, Any] = {
    'spawn_agent': {
        'handler': _tool_spawn_agent,
        'schema': {
            'name': 'spawn_agent',
            'description': 'Spawn a new sub-agent task in a tmux session. '
                           'Returns immediately with a task_id; use '
                           'get_agent_status / wait_for_agent to monitor. '
                           'Defaults to headless mode: the agent runs the '
                           'prompt to completion and exits, so wait_for_agent '
                           'returns its result. Capped by depth and concurrency.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'prompt': {'type': 'string', 'description': 'The task prompt/instructions'},
                    'assistant': {
                        'type': 'string',
                        'description': 'Which agent to spawn',
                        'enum': ['ante', 'claude', 'opencode-openrouter', 'opencode-deepseek', 'kc-harness'],
                        'default': 'ante',
                    },
                    'mode': {
                        'type': 'string',
                        'description': 'headless (default): one-shot, agent exits when done '
                                       'so completion is detectable. interactive: long-lived '
                                       'REPL for human attach. kc-harness is always interactive.',
                        'enum': ['headless', 'interactive'],
                        'default': 'headless',
                    },
                    'workdir': {'type': 'string', 'description': 'Working directory', 'default': '/home/dev'},
                    'parent_task_id': {'type': 'string', 'description': 'Parent task for lineage tracking (auto-inherited from env)'},
                },
                'required': ['prompt'],
            },
        },
    },
    'get_agent_status': {
        'handler': _tool_get_agent_status,
        'schema': {
            'name': 'get_agent_status',
            'description': 'Get the current status of a spawned agent task.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'task_id': {'type': 'string', 'description': 'The task ID from spawn_agent'},
                },
                'required': ['task_id'],
            },
        },
    },
    'list_subagents': {
        'handler': _tool_list_subagents,
        'schema': {
            'name': 'list_subagents',
            'description': 'List all sub-agents spawned by a parent task.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'parent_task_id': {'type': 'string', 'description': 'Parent task ID (defaults to current task from env)'},
                },
            },
        },
    },
    'get_agent_output': {
        'handler': _tool_get_agent_output,
        'schema': {
            'name': 'get_agent_output',
            'description': 'Get the full output log from a spawned agent task.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'task_id': {'type': 'string', 'description': 'The task ID from spawn_agent'},
                    'tail': {'type': 'number', 'description': 'Only return last N lines', 'default': None},
                },
                'required': ['task_id'],
            },
        },
    },
    'wait_for_agent': {
        'handler': _tool_wait_for_agent,
        'schema': {
            'name': 'wait_for_agent',
            'description': 'Block until a spawned agent completes, then return its output. '
                           'Use for sequential orchestration patterns.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'task_id': {'type': 'string', 'description': 'The task ID from spawn_agent'},
                    'timeout': {'type': 'number', 'description': 'Max seconds to wait', 'default': 300},
                    'poll_interval': {'type': 'number', 'description': 'Poll frequency in seconds', 'default': 2},
                    'tail': {'type': 'number', 'description': 'Lines of output to return', 'default': 100},
                },
                'required': ['task_id'],
            },
        },
    },
    'kill_agent': {
        'handler': _tool_kill_agent,
        'schema': {
            'name': 'kill_agent',
            'description': 'Kill a running spawned agent task.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'task_id': {'type': 'string', 'description': 'The task ID from spawn_agent'},
                },
                'required': ['task_id'],
            },
        },
    },
}


# ───────────────────────────────────────────────────────────────────────────
# MCP dispatch
# ───────────────────────────────────────────────────────────────────────────

def _handle_initialize(id_val: Any, params: Any) -> None:
    _send({
        'jsonrpc': '2.0',
        'id': id_val,
        'result': {
            'protocolVersion': '2024-11-05',
            'capabilities': {'tools': {}},
            'serverInfo': {'name': 'agent-orchestrator', 'version': '0.1.0'},
        },
    })


def _handle_list_tools(id_val: Any, params: Any) -> None:
    _send({
        'jsonrpc': '2.0',
        'id': id_val,
        'result': {
            'tools': [t['schema'] for t in TOOLS.values()],
        },
    })


def _handle_call_tool(id_val: Any, params: Any) -> None:
    if not isinstance(params, dict):
        _error(id_val, -32602, 'params must be an object')
        return
    name = params.get('name', '')
    args = params.get('arguments', {})
    tool = TOOLS.get(name)
    if tool is None:
        _error(id_val, -32601, f'Unknown tool: {name}')
        return
    try:
        result = tool['handler'](args)
        _send({'jsonrpc': '2.0', 'id': id_val, 'result': result})
    except Exception as e:
        _log(f'tool {name} error: {traceback.format_exc()}')
        _send({
            'jsonrpc': '2.0',
            'id': id_val,
            'result': {'isError': True, 'content': [{'type': 'text', 'text': f'{type(e).__name__}: {e}'}]},
        })


_HANDLERS = {
    'initialize': _handle_initialize,
    'listTools': _handle_list_tools,
    'tools/list': _handle_list_tools,
    'tools/call': _handle_call_tool,
}


# ───────────────────────────────────────────────────────────────────────────
# Main loop
# ───────────────────────────────────────────────────────────────────────────

def main() -> int:
    _log('started')
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            _log(f'invalid JSON: {e} — line={line[:200]!r}')
            continue
        if not isinstance(msg, dict):
            continue
        method = msg.get('method', '')
        id_val = msg.get('id')
        params = msg.get('params')
        handler = _HANDLERS.get(method)
        if handler:
            handler(id_val, params)
        else:
            _error(id_val, -32601, f'Method not found: {method}')
    _log('exiting on EOF')
    return 0


if __name__ == '__main__':
    sys.exit(main())