#!/usr/bin/env python3
"""Stdio MCP server exposing the workspace's persistent memory to Claude Code.

Invoked per-Claude-session via the `mcpServers.memory` entry seeded into
~/.claude.json. Talks JSON-RPC 2.0 over stdin/stdout (newline-delimited, the
LSP-style Content-Length framing is not used by Claude Code's stdio
transport).

Design choices:
  - Stdlib-only. Memory access is via the `memory` package, which is copied
    next to this file at pod start so a single sqlite-vec-aware code path
    serves both the HTTP API (server.py) and this MCP process.
  - Each request runs synchronously; SQLite WAL plus BEGIN IMMEDIATE retries
    in memory.store handle concurrency with server.py.
  - Provenance for writes is derived from $KC_TASK_ID (exported by
    ClaudeTaskManager when it spawns the tmux session). Falls back to
    'mcp:unknown' if absent.

Lifecycle: read a line → dispatch → write a response line. EOF on stdin
exits cleanly. Errors are returned as JSON-RPC error responses or
tool-isError content, never as stdout noise outside the framing.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any, Dict, List, Optional

# Make the colocated `memory` package importable when this script is run
# from /home/dev/.claude-memory/mcp_memory.py.
_SELF_DIR = os.path.dirname(os.path.abspath(__file__))
if _SELF_DIR not in sys.path:
    sys.path.insert(0, _SELF_DIR)

from memory.manager import (  # noqa: E402
    MemoryManager,
    MemoryError as MemErr,
    NotFound,
    ValidationError,
    Conflict,
)

# ───────────────────────────────────────────────────────────────────────────
# Identity / provenance
# ───────────────────────────────────────────────────────────────────────────

def _actor() -> str:
    task_id = os.environ.get('KC_TASK_ID')
    if task_id:
        return f'task:{task_id}'
    return 'mcp:unknown'


# ───────────────────────────────────────────────────────────────────────────
# JSON-RPC framing (newline-delimited)
# ───────────────────────────────────────────────────────────────────────────

# Buffered writer so we control flushes precisely.
_OUT = sys.stdout
_LOG = sys.stderr


def _log(msg: str) -> None:
    try:
        _LOG.write(f'[mcp_memory] {msg}\n')
        _LOG.flush()
    except Exception:
        pass


def _send(msg: Dict[str, Any]) -> None:
    line = json.dumps(msg, ensure_ascii=False, separators=(',', ':'))
    _OUT.write(line)
    _OUT.write('\n')
    _OUT.flush()


def _reply(id_: Any, result: Any) -> None:
    _send({'jsonrpc': '2.0', 'id': id_, 'result': result})


def _error(id_: Any, code: int, message: str, data: Any = None) -> None:
    err: Dict[str, Any] = {'code': code, 'message': message}
    if data is not None:
        err['data'] = data
    _send({'jsonrpc': '2.0', 'id': id_, 'error': err})


# JSON-RPC error codes
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


# ───────────────────────────────────────────────────────────────────────────
# Tool definitions (what Claude sees)
# ───────────────────────────────────────────────────────────────────────────

# Descriptions are deliberately phrased to trigger the right tool on common
# user phrases ("remember…", "what did I tell you…", "forget…").

TOOLS: List[Dict[str, Any]] = [
    {
        'name': 'memory_remember',
        'description': (
            'Persist a fact in the workspace memory so it survives across '
            'tasks, browser tabs, and pod restarts. Call this whenever the '
            'user says "remember", "note that", "save this", or shares a '
            'stable preference. Pick a stable namespace.key '
            '(e.g. "user.preferences.editor"). Returns the stored row '
            'including its new version.'
        ),
        'inputSchema': {
            'type': 'object',
            'required': ['namespace', 'key', 'value'],
            'properties': {
                'namespace': {'type': 'string', 'description':
                    'Dotted scope, e.g. "user.preferences" or "project.kube-coder".'},
                'key': {'type': 'string', 'description':
                    'Stable identifier within the namespace.'},
                'value': {'type': 'string', 'description':
                    'The fact to remember. Keep concise; one fact per entry.'},
                'kind': {'type': 'string',
                    'enum': ['semantic', 'episodic', 'procedural', 'preference'],
                    'description': (
                        'semantic=fact, episodic=event, procedural=how-to, '
                        'preference=stable user preference. Default semantic.')},
                'tags': {'type': 'string', 'description':
                    'Optional comma-separated tags. Use "secret" to opt out '
                    'of auto-injection.'},
                'importance': {'type': 'number',
                    'description': '0..1; default 0.5. Raise for things you '
                    'want surfaced more often.'},
                'expires_in_days': {'type': 'number',
                    'description': 'Optional TTL for ephemeral notes.'},
            },
        },
    },
    {
        'name': 'memory_update',
        'description': (
            'Update an existing memory in place by (namespace, key). Bumps '
            'version and writes a history entry. Use when correcting a fact.'
        ),
        'inputSchema': {
            'type': 'object',
            'required': ['namespace', 'key'],
            'properties': {
                'namespace': {'type': 'string'},
                'key': {'type': 'string'},
                'value': {'type': 'string'},
                'tags': {'type': 'string'},
                'kind': {'type': 'string',
                    'enum': ['semantic', 'episodic', 'procedural', 'preference']},
                'importance': {'type': 'number'},
            },
        },
    },
    {
        'name': 'memory_recall',
        'description': (
            'Read a specific memory by exact (namespace, key). Use when you '
            'know the key — otherwise prefer memory_search.'
        ),
        'inputSchema': {
            'type': 'object',
            'required': ['namespace', 'key'],
            'properties': {
                'namespace': {'type': 'string'},
                'key': {'type': 'string'},
            },
        },
    },
    {
        'name': 'memory_search',
        'description': (
            'Search the workspace memory by free-form text. Use this '
            'whenever the user asks "what did I tell you about…", '
            '"do you remember…", or wants to retrieve something they may '
            'have remembered earlier. Returns ranked entries.'
        ),
        'inputSchema': {
            'type': 'object',
            'required': ['q'],
            'properties': {
                'q': {'type': 'string', 'description': 'Topic words.'},
                'namespaces': {'type': 'array', 'items': {'type': 'string'},
                    'description': 'Optional filter to specific namespaces.'},
                'kinds': {'type': 'array', 'items': {'type': 'string'},
                    'description': 'Optional filter: semantic|episodic|procedural|preference.'},
                'limit': {'type': 'integer', 'description': 'Default 10, max 100.'},
            },
        },
    },
    {
        'name': 'memory_list',
        'description': (
            'Enumerate memories, optionally filtered by namespace or kind. '
            'Use to discover what is in memory when you don\'t know the key.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'namespace': {'type': 'string'},
                'kind': {'type': 'string',
                    'enum': ['semantic', 'episodic', 'procedural', 'preference']},
                'limit': {'type': 'integer'},
            },
        },
    },
    {
        'name': 'memory_link',
        'description': (
            'Create a graph relation between two existing memories '
            '(e.g. "caused-by", "part-of", "related-to"). Useful for building '
            'a structured map of facts.'
        ),
        'inputSchema': {
            'type': 'object',
            'required': ['src_namespace', 'src_key',
                         'dst_namespace', 'dst_key'],
            'properties': {
                'src_namespace': {'type': 'string'},
                'src_key': {'type': 'string'},
                'dst_namespace': {'type': 'string'},
                'dst_key': {'type': 'string'},
                'kind': {'type': 'string', 'description':
                    'Relation kind, e.g. related-to (default), caused-by, part-of.'},
                'weight': {'type': 'number', 'description': '0..1, default 1.0.'},
            },
        },
    },
    {
        'name': 'memory_neighbors',
        'description': (
            'Walk the relation graph outward from a memory and return the '
            'reachable entries up to a given depth (max 4).'
        ),
        'inputSchema': {
            'type': 'object',
            'required': ['namespace', 'key'],
            'properties': {
                'namespace': {'type': 'string'},
                'key': {'type': 'string'},
                'depth': {'type': 'integer', 'description': '1..4, default 1.'},
                'kinds': {'type': 'array', 'items': {'type': 'string'}},
            },
        },
    },
    {
        'name': 'memory_unlink',
        'description': (
            'Remove a graph relation between two memories (the inverse of '
            'memory_link). Omit `kind` to remove every edge from src→dst, or '
            'pass it to remove just that one. Returns how many edges were '
            'removed (0 if none matched).'
        ),
        'inputSchema': {
            'type': 'object',
            'required': ['src_namespace', 'src_key',
                         'dst_namespace', 'dst_key'],
            'properties': {
                'src_namespace': {'type': 'string'},
                'src_key': {'type': 'string'},
                'dst_namespace': {'type': 'string'},
                'dst_key': {'type': 'string'},
                'kind': {'type': 'string', 'description':
                    'Optional: only remove this relation kind.'},
            },
        },
    },
    {
        'name': 'memory_forget',
        'description': (
            'Soft-delete a memory. The row stays in history (auditable) but '
            'no longer surfaces in search/list/auto-inject. Confirm with the '
            'user before calling.'
        ),
        'inputSchema': {
            'type': 'object',
            'required': ['namespace', 'key'],
            'properties': {
                'namespace': {'type': 'string'},
                'key': {'type': 'string'},
            },
        },
    },
    {
        'name': 'memory_stats',
        'description': (
            'Return counts by kind/namespace and store health information.'
        ),
        'inputSchema': {'type': 'object', 'properties': {}},
    },
]


# ───────────────────────────────────────────────────────────────────────────
# Tool implementations
# ───────────────────────────────────────────────────────────────────────────

def _expires_at_from_days(days: Optional[float]) -> Optional[float]:
    if days is None:
        return None
    try:
        import time
        return time.time() + float(days) * 86400.0
    except (TypeError, ValueError):
        return None


def _tool_remember(args: Dict[str, Any]) -> Dict[str, Any]:
    row = MemoryManager.upsert(
        namespace=args['namespace'],
        key=args['key'],
        value=args['value'],
        kind=args.get('kind', 'semantic'),
        tags=args.get('tags', ''),
        importance=float(args.get('importance', 0.5)),
        source=_actor(),
        expires_at=_expires_at_from_days(args.get('expires_in_days')),
    )
    MemoryManager.log_ref(
        namespace=row['namespace'], key=row['key'],
        ref_kind='task' if _actor().startswith('task:') else 'api',
        ref_id=_actor().split(':', 1)[1], access_kind='write',
    )
    return row


def _tool_update(args: Dict[str, Any]) -> Dict[str, Any]:
    row = MemoryManager.update_partial(
        namespace=args['namespace'],
        key=args['key'],
        value=args.get('value'),
        tags=args.get('tags'),
        kind=args.get('kind'),
        importance=args.get('importance'),
        source=_actor(),
    )
    MemoryManager.log_ref(
        namespace=row['namespace'], key=row['key'],
        ref_kind='task' if _actor().startswith('task:') else 'api',
        ref_id=_actor().split(':', 1)[1], access_kind='write',
    )
    return row


def _tool_recall(args: Dict[str, Any]) -> Dict[str, Any]:
    row = MemoryManager.get(namespace=args['namespace'], key=args['key'])
    if row is None:
        raise NotFound(f"no memory at {args['namespace']}/{args['key']}")
    MemoryManager.log_ref(
        namespace=row['namespace'], key=row['key'],
        ref_kind='task' if _actor().startswith('task:') else 'api',
        ref_id=_actor().split(':', 1)[1], access_kind='read',
    )
    return row


def _tool_search(args: Dict[str, Any]) -> Dict[str, Any]:
    q = args.get('q', '')
    rows = MemoryManager.search(
        q=q,
        namespaces=args.get('namespaces'),
        kinds=args.get('kinds'),
        limit=int(args.get('limit', 10)),
    )
    actor = _actor()
    actor_kind = 'task' if actor.startswith('task:') else 'api'
    actor_id = actor.split(':', 1)[1]
    for r in rows[:5]:  # only log the top few to avoid ref-table churn
        MemoryManager.log_ref(
            namespace=r['namespace'], key=r['key'],
            ref_kind=actor_kind, ref_id=actor_id, access_kind='read',
        )
    return {'results': rows, 'query': q, 'count': len(rows)}


def _tool_list(args: Dict[str, Any]) -> Dict[str, Any]:
    rows = MemoryManager.list(
        namespace=args.get('namespace'),
        kind=args.get('kind'),
        limit=int(args.get('limit', 100)),
    )
    return {'memories': rows, 'count': len(rows)}


def _tool_link(args: Dict[str, Any]) -> Dict[str, Any]:
    return MemoryManager.link(
        src_namespace=args['src_namespace'], src_key=args['src_key'],
        dst_namespace=args['dst_namespace'], dst_key=args['dst_key'],
        kind=args.get('kind', 'related-to'),
        weight=float(args.get('weight', 1.0)),
        created_by=_actor(),
    )


def _tool_neighbors(args: Dict[str, Any]) -> Dict[str, Any]:
    rows = MemoryManager.neighbors(
        namespace=args['namespace'], key=args['key'],
        depth=int(args.get('depth', 1)),
        kinds=args.get('kinds'),
    )
    return {'neighbors': rows, 'count': len(rows)}


def _tool_unlink(args: Dict[str, Any]) -> Dict[str, Any]:
    removed = MemoryManager.unlink(
        src_namespace=args['src_namespace'], src_key=args['src_key'],
        dst_namespace=args['dst_namespace'], dst_key=args['dst_key'],
        kind=args.get('kind'),
    )
    return {'removed': removed}


def _tool_forget(args: Dict[str, Any]) -> Dict[str, Any]:
    row = MemoryManager.soft_delete(
        namespace=args['namespace'], key=args['key'],
        source=_actor(),
    )
    return row


def _tool_stats(_args: Dict[str, Any]) -> Dict[str, Any]:
    return MemoryManager.stats()


_TOOL_IMPLS = {
    'memory_remember':  _tool_remember,
    'memory_update':    _tool_update,
    'memory_recall':    _tool_recall,
    'memory_search':    _tool_search,
    'memory_list':      _tool_list,
    'memory_link':      _tool_link,
    'memory_neighbors': _tool_neighbors,
    'memory_unlink':    _tool_unlink,
    'memory_forget':    _tool_forget,
    'memory_stats':     _tool_stats,
}


def _tool_call(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    fn = _TOOL_IMPLS.get(name)
    if fn is None:
        raise ValueError(f'unknown tool: {name}')
    return fn(args or {})


# ───────────────────────────────────────────────────────────────────────────
# MCP method dispatch
# ───────────────────────────────────────────────────────────────────────────

SERVER_INFO = {
    'name': 'kube-coder-memory',
    'version': '1.0.0',
}
PROTOCOL_VERSION = '2024-11-05'


def _content_text(payload: Any) -> List[Dict[str, Any]]:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return [{'type': 'text', 'text': text}]


def _handle_initialize(_params: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'protocolVersion': PROTOCOL_VERSION,
        'capabilities': {'tools': {'listChanged': False}},
        'serverInfo': SERVER_INFO,
    }


def _handle_tools_list(_params: Dict[str, Any]) -> Dict[str, Any]:
    return {'tools': TOOLS}


def _handle_tools_call(params: Dict[str, Any]) -> Dict[str, Any]:
    name = params.get('name')
    args = params.get('arguments') or {}
    if not isinstance(name, str):
        raise ValueError('tools/call requires string "name"')
    try:
        result = _tool_call(name, args)
        return {'content': _content_text(result), 'isError': False}
    except (ValidationError, NotFound, Conflict, MemErr) as e:
        return {
            'content': _content_text({'error': str(e), 'code': e.code}),
            'isError': True,
        }
    except Exception as e:  # surface failures to the model, not the transport
        _log(f'tool {name} failed: {e!r}\n{traceback.format_exc()}')
        return {
            'content': _content_text({'error': str(e), 'code': 'internal'}),
            'isError': True,
        }


_METHODS = {
    'initialize': _handle_initialize,
    'tools/list': _handle_tools_list,
    'tools/call': _handle_tools_call,
}


def _serve() -> None:
    """Read newline-delimited JSON-RPC messages until EOF."""
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            _error(None, INVALID_REQUEST, 'invalid JSON')
            continue

        if isinstance(req, list):
            # We don't support batch requests; reply per-element so callers
            # that send a batch by accident at least see something.
            for r in req:
                _dispatch(r)
            continue
        _dispatch(req)


def _dispatch(req: Dict[str, Any]) -> None:
    if not isinstance(req, dict):
        _error(None, INVALID_REQUEST, 'request must be an object')
        return
    id_ = req.get('id')
    method = req.get('method')
    params = req.get('params') or {}

    # Notifications (no id) get no response per JSON-RPC spec. Specifically
    # `notifications/initialized` arrives after initialize and must be silently
    # swallowed.
    if id_ is None:
        return

    handler = _METHODS.get(method)
    if handler is None:
        _error(id_, METHOD_NOT_FOUND, f'unknown method: {method}')
        return
    try:
        result = handler(params)
    except ValueError as e:
        _error(id_, INVALID_PARAMS, str(e))
        return
    except Exception as e:
        _log(f'method {method} crashed: {e!r}\n{traceback.format_exc()}')
        _error(id_, INTERNAL_ERROR, str(e))
        return
    _reply(id_, result)


def main() -> int:
    try:
        _serve()
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        pass
    except Exception as e:
        _log(f'fatal: {e!r}\n{traceback.format_exc()}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
