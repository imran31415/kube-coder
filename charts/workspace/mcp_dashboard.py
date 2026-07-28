#!/usr/bin/env python3
"""MCP server exposing the kube-coder dashboard as tools to running agents.

This is what gives the **Hypervisor** chat (and any agent that has this MCP
seeded) its "do anything you can do in the UI" powers: a curated set of tools
that read live workspace state (metrics, tasks, health, memory, apps, triggers)
and take safe actions (create a task, add a memory, pin an app), plus gated
destructive actions (kill a task, delete a memory).

Design: every tool is a thin wrapper over the dashboard's OWN local HTTP API
(http://127.0.0.1:6080/...), authenticated with the workspace bearer token at
~/.claude-tasks/.api-token. That reuses every existing request handler — its
auth, validation, and business logic — so there is zero duplicated logic here
and the tool surface can never drift from the REST surface it mirrors.

Protocol: JSON-RPC 2.0 over stdin/stdout (newline-delimited), identical to
mcp_memory.py / mcp_agent_orchestrator.py.

Safety:
  * Destructive tools (kill_task, delete_memory) refuse to run unless called
    with confirm=true. The first call returns CONFIRMATION_REQUIRED telling the
    agent to get the user's explicit approval in chat, then call again with
    confirm=true. This makes "destructive needs an in-chat confirm" work with
    any CLI agent, no special UI plumbing.
  * When READONLY_MODE is set, write + destructive tools are omitted entirely —
    the agent can report on the workspace but never mutate it, matching how the
    rest of the server gates writes.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

# ───────────────────────────────────────────────────────────────────────────
# Constants / config
# ───────────────────────────────────────────────────────────────────────────
_LOG = sys.stderr
TOKEN_FILE = os.path.join(
    os.environ.get('HOME', '/home/dev'), '.claude-tasks', '.api-token'
)
BASE_URL = os.environ.get('KC_DASHBOARD_URL', 'http://127.0.0.1:6080').rstrip('/')
HTTP_TIMEOUT = float(os.environ.get('KC_DASHBOARD_MCP_TIMEOUT', '30'))


def _readonly() -> bool:
    return os.environ.get('READONLY_MODE', '').strip().lower() in (
        '1', 'true', 'yes', 'on',
    )


def _log(msg: str) -> None:
    try:
        _LOG.write(f'[mcp_dashboard] {msg}\n')
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


def _ok(text: str) -> Dict[str, Any]:
    return {'content': [{'type': 'text', 'text': text}]}


def _err(text: str) -> Dict[str, Any]:
    return {'isError': True, 'content': [{'type': 'text', 'text': text}]}


# ───────────────────────────────────────────────────────────────────────────
# HTTP helper — call the dashboard's own REST API with the bearer token
# ───────────────────────────────────────────────────────────────────────────

def _token() -> str:
    """The task-API bearer token, creating it if the file is absent.

    The server materializes this at boot (issue #528), so the create path is
    belt-and-braces for a pod where the file went missing — without it an empty
    Bearer means every dispatch 401s, which is exactly how the first-win build
    broke on fresh pods. O_EXCL so two racing MCP processes can't each mint a
    token and leave one of them holding a stale value: the loser falls through
    to reading whatever landed on disk.
    """
    try:
        with open(TOKEN_FILE, 'r') as f:
            token = f.read().strip()
        if token:
            return token
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(TOKEN_FILE), mode=0o700, exist_ok=True)
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            minted = secrets.token_urlsafe(36)
            os.write(fd, minted.encode('utf-8'))
        finally:
            os.close(fd)
        _log(f'minted task-API token at {TOKEN_FILE}')
        return minted
    except FileExistsError:
        pass
    except OSError as e:
        _log(f'could not create {TOKEN_FILE}: {e}')
        return ''
    try:
        with open(TOKEN_FILE, 'r') as f:
            return f.read().strip()
    except OSError:
        return ''


def _api(method: str, path: str, body: Optional[Dict[str, Any]] = None,
         query: Optional[Dict[str, Any]] = None):
    """Call the local dashboard API. Returns (status_code, parsed_json_or_text).

    Raises nothing — network/HTTP errors are returned as (status, {'error': ...})
    so tool handlers can format a clean message for the model.
    """
    url = BASE_URL + path
    if query:
        clean = {k: v for k, v in query.items() if v is not None}
        if clean:
            url += '?' + urllib.parse.urlencode(clean)
    data = None
    headers = {'Authorization': f'Bearer {_token()}'}
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', errors='replace') if e.fp else ''
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw or str(e)
    except urllib.error.URLError as e:
        return 0, {'error': f'dashboard API unreachable: {e.reason}'}
    except Exception as e:  # pragma: no cover - defensive
        return 0, {'error': f'{type(e).__name__}: {e}'}


def _pretty(value: Any, limit: int = 8000) -> str:
    try:
        text = json.dumps(value, indent=2, ensure_ascii=False) \
            if not isinstance(value, str) else value
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > limit:
        text = text[:limit] + f'\n… (truncated, {len(text)} chars total)'
    return text


def _call(method: str, path: str, ok_status=(200, 201, 202),
          body=None, query=None) -> Dict[str, Any]:
    """Run an API call and format the result as an MCP tool response."""
    status, payload = _api(method, path, body=body, query=query)
    if status in ok_status:
        return _ok(_pretty(payload))
    detail = payload.get('error') if isinstance(payload, dict) else payload
    return _err(f'dashboard API {method} {path} returned HTTP {status}: {detail}')


# ───────────────────────────────────────────────────────────────────────────
# Tool handlers — read
# ───────────────────────────────────────────────────────────────────────────

def _t_get_metrics(a):
    return _call('GET', '/metrics')


def _t_list_tasks(a):
    return _call('GET', '/api/claude/tasks')


def _t_get_task(a):
    tid = (a.get('task_id') or '').strip()
    if not tid:
        return _err('task_id is required')
    return _call('GET', f'/api/claude/tasks/{urllib.parse.quote(tid)}')


def _t_get_task_output(a):
    tid = (a.get('task_id') or '').strip()
    if not tid:
        return _err('task_id is required')
    tail = a.get('tail')
    q = {'tail': int(tail)} if isinstance(tail, (int, float)) else None
    return _call('GET', f'/api/claude/tasks/{urllib.parse.quote(tid)}/output', query=q)


def _t_get_service_health(a):
    return _call('GET', '/health')


def _t_get_github_status(a):
    return _call('GET', '/api/github/status')


def _t_search_memory(a):
    return _call('GET', '/api/memory', query={
        'q': a.get('query'),
        'namespace': a.get('namespace'),
        'limit': a.get('limit', 25),
    })


def _t_list_memory(a):
    return _call('GET', '/api/memory', query={
        'namespace': a.get('namespace'),
        'limit': a.get('limit', 100),
    })


def _t_list_apps(a):
    return _call('GET', '/api/apps')


def _t_list_triggers(a):
    _, webhooks = _api('GET', '/api/webhooks')
    _, crons = _api('GET', '/api/crons')
    return _ok(_pretty({'webhooks': webhooks, 'crons': crons}))


# ── AI CTO project tools (#465) ────────────────────────────────────────────

def _project_id_env() -> str:
    """The project this CTO thread is bound to, exported on the turn env by
    hypervisor_session (#465). '' outside a CTO turn."""
    return (os.environ.get('KC_PROJECT_ID') or '').strip()


def _t_list_projects(a):
    # Trim to the fields the model actually reasons over (name/status/north
    # star/repo + live pulse) instead of dumping every registry field — keeps
    # the portfolio view token-cheap even with many projects.
    status, payload = _api('GET', '/api/projects')
    if status != 200 or not isinstance(payload, dict):
        detail = payload.get('error') if isinstance(payload, dict) else payload
        return _err(f'dashboard API GET /api/projects returned HTTP {status}: {detail}')
    slim = []
    for p in payload.get('projects', []):
        pulse = p.get('pulse') or {}
        slim.append({
            'id': p.get('id'),
            'name': p.get('name'),
            'status': p.get('status'),
            'repo': p.get('repo') or None,
            'north_star': p.get('north_star') or None,
            'running': pulse.get('running', 0),
            'waiting': pulse.get('waiting', 0),
        })
    return _ok(_pretty({'projects': slim}))


def _t_get_project_brief(a):
    # Default to the bound project so the CTO can just ask "how are we doing"
    # without repeating the id every turn.
    pid = (a.get('project_id') or '').strip() or _project_id_env()
    if not pid:
        return _err('project_id is required (no bound project in this session)')
    status, payload = _api('GET', f'/api/projects/{urllib.parse.quote(pid)}/brief')
    if status != 200:
        detail = payload.get('error') if isinstance(payload, dict) else payload
        return _err(f'dashboard API GET brief returned HTTP {status}: {detail}')
    # The markdown digest is the token-efficient view meant for the model; hand
    # that back (falling back to the full JSON if it's somehow absent).
    if isinstance(payload, dict) and payload.get('brief_markdown'):
        return _ok(payload['brief_markdown'])
    return _ok(_pretty(payload))


def _t_update_project(a):
    pid = (a.get('project_id') or '').strip() or _project_id_env()
    if not pid:
        return _err('project_id is required')
    fields = a.get('fields')
    if not isinstance(fields, dict) or not fields:
        return _err('fields (an object of project properties to update) is required')
    status, payload = _api(
        'PUT', f'/api/projects/{urllib.parse.quote(pid)}', body=fields)
    if status != 200:
        detail = payload.get('error') if isinstance(payload, dict) else payload
        return _err(f'dashboard API PUT /api/projects/{pid} returned HTTP {status}: {detail}')
    # A short confirmation, not the whole record — the model already knows what
    # it set.
    changed = ', '.join(sorted(fields)) if isinstance(fields, dict) else ''
    return _ok(f'Updated project {pid} ({changed}).')


def _t_post_update(a):
    title = (a.get('title') or '').strip()
    if not title:
        return _err('title is required')
    kind = (a.get('kind') or 'briefing').strip()
    body = {
        'kind': kind,
        'title': title,
        'body_md': a.get('body_md') or '',
        'project_id': (a.get('project_id') or '').strip() or _project_id_env(),
        'waiting': bool(a.get('waiting')),
    }
    if a.get('links') is not None:
        body['links'] = a['links']
    # Attribute the item to this thread so the feed can show its provenance.
    tid = _hv_thread_id()
    if tid:
        body['source'] = f'agent:{tid}'
    return _call('POST', '/api/feed', body=body)


# ───────────────────────────────────────────────────────────────────────────
# Tool handlers — render (Hypervisor rich content)
#
# These don't mutate anything; they exist so the agent can render live app
# previews / images / videos inline in the Hypervisor chat. The render SIGNAL is
# the tool CALL itself (name + input), which the chat frontend keys off — the
# text returned here is just confirmation for the agent.
# ───────────────────────────────────────────────────────────────────────────

def _t_show_app_preview(a):
    port = a.get('port')
    if isinstance(port, str) and port.isdigit():
        port = int(port)
    if not isinstance(port, int) or isinstance(port, bool) or port <= 0:
        return _err('port (a positive number) is required')
    # Best-effort note about whether a listener is up — still render regardless,
    # since the app may be mid-startup.
    _, apps = _api('GET', '/api/apps')
    status = None
    if isinstance(apps, dict):
        for app in apps.get('apps', []) or []:
            if app.get('port') == port:
                status = app.get('status')
                break
    note = f'Embedding a live preview of the app on port {port} in the chat.'
    if status and status != 'running':
        note += f' (/api/apps reports this port as "{status}".)'
    elif status is None:
        note += ' (no listener detected yet — it may still be starting.)'
    return _ok(note)


def _t_show_media(a):
    media_kind = (a.get('media_kind') or a.get('kind') or 'image').strip().lower()
    if media_kind not in ('image', 'video'):
        return _err("media_kind must be 'image' or 'video'")
    path = (a.get('path') or '').strip()
    url = (a.get('url') or '').strip()
    if bool(path) == bool(url):
        return _err('provide exactly one of: path (a file under /home/dev) or url (http[s])')
    if url and not (url.startswith('http://') or url.startswith('https://')):
        return _err('url must be http(s)')
    where = f'file {path}' if path else url
    return _ok(f'Rendering {media_kind} ({where}) in the chat.')


def _t_show_file(a):
    path = (a.get('path') or '').strip()
    if not path:
        return _err('path is required (a file under /home/dev, e.g. docs/plan.md)')
    if path.startswith(('http://', 'https://')):
        return _err('path must be a workspace file under /home/dev, not a URL')
    # The client fetches /api/files/preview to classify the file and renders
    # markdown/text/code/image/video inline (PDF & HTML in a sandboxed frame);
    # existence + type are resolved there, so just echo a confirmation.
    return _ok(f'Rendering file {path} in the chat.')


# ───────────────────────────────────────────────────────────────────────────
# Tool handlers — safe write
# ───────────────────────────────────────────────────────────────────────────

# Appended to a build task's prompt when preview=True — the deterministic half
# of the first-win dispatch contract (#485). The auto-preview (#484) has nothing
# to show if the build just writes files and exits, so we tell the agent to leave
# a dev server RUNNING and DISCOVERABLE. Detached (setsid nohup) so it outlives
# this task's turn/session; loopback so the app-proxy can reach it.
_BUILD_PREVIEW_CONTRACT = (
    "\n\n[Build-preview contract: the dashboard auto-embeds a LIVE preview of "
    "this app in the chat the instant a dev server starts listening — so after "
    "you implement, actually RUN the app and LEAVE IT RUNNING. Start it detached "
    "so it survives this task ending, bound to loopback on a normal dev port "
    "(e.g. 3000 / 5173 / 8000): e.g. "
    "`setsid nohup <run-command> > /home/dev/devserver.log 2>&1 &`. For a static "
    "site, serve it (`setsid nohup python3 -m http.server 8000 >/dev/null 2>&1 "
    "&`). Then CONFIRM it is listening (curl the port) before you finish. Do not "
    "just write files and exit — a build with no running server has nothing to "
    "preview.]"
)


def _t_create_task(a):
    prompt = (a.get('prompt') or '').strip()
    if not prompt:
        return _err('prompt is required')
    # First-win auto-preview (#484/#485): unless the caller opts out, treat this
    # as a runnable build — append the dev-server contract to the prompt and arm
    # a `port` watcher on this CTO thread so a live preview auto-surfaces the
    # moment the app comes up. Harmless for non-app tasks: the watcher expires
    # silently if no new port ever appears.
    preview = a.get('preview')
    preview = True if preview is None else bool(preview)
    tid = _hv_thread_id()
    if preview:
        prompt = prompt + _BUILD_PREVIEW_CONTRACT
    # Hypervisor-spawned tasks are unattended — nobody is watching the live
    # terminal to answer the CLI's API-key dialog or per-tool permission
    # prompts — so launch in auto-approve/skip-permissions mode by default. A
    # caller can pass auto_approve=false to force the interactive behavior.
    auto_approve = a.get('auto_approve')
    body = {'prompt': prompt, 'source': 'hypervisor-tool',
            'auto_approve': True if auto_approve is None else bool(auto_approve)}
    # Bind the build to the CTO thread's project so it counts in that project's
    # brief no matter which workdir it runs in (#533).
    project_id = _project_id_env()
    if project_id:
        body['project_id'] = project_id
    if a.get('workdir'):
        body['workdir'] = a['workdir']
    if a.get('assistant'):
        body['assistant'] = a['assistant']
    status, payload = _api('POST', '/api/claude/tasks', body=body)
    if status not in (200, 201, 202):
        detail = payload.get('error') if isinstance(payload, dict) else payload
        return _err(f'dashboard API POST /api/claude/tasks returned HTTP '
                    f'{status}: {detail}')
    # Auto-arm the port watcher (best-effort — a failure here must not fail the
    # task creation, which already succeeded). Only inside a Hypervisor turn,
    # where there's a thread to surface the preview in.
    task_id = payload.get('task_id') if isinstance(payload, dict) else None
    if preview and tid and task_id:
        _api('POST',
             f'/api/hypervisor/threads/{urllib.parse.quote(tid)}/watchers',
             body={'kind': 'port', 'target': task_id, 'interval': 5,
                   'timeout': 600,
                   'note': f'live preview for build task {task_id}'})
    return _ok(_pretty(payload))


def _t_send_task_message(a):
    tid = (a.get('task_id') or '').strip()
    msg = (a.get('message') or '').strip()
    if not tid or not msg:
        return _err('task_id and message are required')
    return _call('POST', f'/api/claude/tasks/{urllib.parse.quote(tid)}/message',
                 body={'prompt': msg, 'submit': True})


def _t_add_memory(a):
    ns = (a.get('namespace') or '').strip()
    key = (a.get('key') or '').strip()
    value = a.get('value')
    if not ns or not key or value is None:
        return _err('namespace, key and value are required')
    body = {'namespace': ns, 'key': key, 'value': value}
    if a.get('kind'):
        body['kind'] = a['kind']
    if a.get('tags'):
        body['tags'] = a['tags']
    return _call('POST', '/api/memory', body=body)


def _hv_thread_id() -> str:
    """The Hypervisor thread this agent is running in. hypervisor_session's
    runner exports it on the CLI env, which the CLI's stdio MCP servers (us)
    inherit — so `watch` needs no explicit id plumbing. Empty outside a
    Hypervisor turn (e.g. the Build tab's full-config sessions)."""
    return (os.environ.get('KC_HYPERVISOR_THREAD_ID') or '').strip()


_NO_THREAD_MSG = (
    'Cross-turn watchers only work inside a Hypervisor chat turn (no '
    'KC_HYPERVISOR_THREAD_ID in the environment). In this session, wait '
    'in-process or poll the target yourself.'
)


def _t_watch(a):
    tid = _hv_thread_id()
    if not tid:
        return _err(_NO_THREAD_MSG)
    kind = (a.get('kind') or '').strip()
    target = (a.get('target') or '').strip()
    if not kind or not target:
        return _err('kind and target are required')
    body = {'kind': kind, 'target': target}
    for k in ('note', 'interval', 'timeout'):
        if a.get(k) is not None:
            body[k] = a[k]
    return _call('POST',
                 f'/api/hypervisor/threads/{urllib.parse.quote(tid)}/watchers',
                 body=body)


def _t_list_watchers(a):
    tid = _hv_thread_id()
    if not tid:
        return _err(_NO_THREAD_MSG)
    return _call('GET',
                 f'/api/hypervisor/threads/{urllib.parse.quote(tid)}/watchers')


def _t_cancel_watcher(a):
    tid = _hv_thread_id()
    if not tid:
        return _err(_NO_THREAD_MSG)
    wid = (a.get('watcher_id') or '').strip()
    if not wid:
        return _err('watcher_id is required')
    return _call('DELETE',
                 f'/api/hypervisor/threads/{urllib.parse.quote(tid)}/watchers/'
                 f'{urllib.parse.quote(wid)}')


def _t_pin_app(a):
    port = a.get('port')
    if port is None:
        return _err('port is required')
    body = {'port': port}
    if a.get('name'):
        body['name'] = a['name']
    if a.get('strip_prefix') is not None:
        body['strip_prefix'] = bool(a['strip_prefix'])
    return _call('POST', '/api/apps/pins', body=body)


# ───────────────────────────────────────────────────────────────────────────
# Tool handlers — destructive (require confirm=true)
# ───────────────────────────────────────────────────────────────────────────
_CONFIRM_HINT = (
    'CONFIRMATION_REQUIRED — this is a destructive action. Describe exactly what '
    'you are about to do to the user, wait for their explicit approval in the '
    'chat, and only then call this tool again with confirm=true.'
)


def _needs_confirm(a) -> bool:
    return not bool(a.get('confirm'))


def _t_kill_task(a):
    tid = (a.get('task_id') or '').strip()
    if not tid:
        return _err('task_id is required')
    if _needs_confirm(a):
        return _err(f'{_CONFIRM_HINT}\nAction: kill task {tid}.')
    return _call('DELETE', f'/api/claude/tasks/{urllib.parse.quote(tid)}')


def _t_delete_memory(a):
    ns = (a.get('namespace') or '').strip()
    key = (a.get('key') or '').strip()
    if not ns or not key:
        return _err('namespace and key are required')
    if _needs_confirm(a):
        return _err(f'{_CONFIRM_HINT}\nAction: delete memory {ns}/{key}.')
    return _call('DELETE',
                 f'/api/memory/{urllib.parse.quote(ns)}/{urllib.parse.quote(key)}')


# ───────────────────────────────────────────────────────────────────────────
# Tool registry
# ───────────────────────────────────────────────────────────────────────────
def _tool(name, description, handler, properties=None, required=None,
          kind='read'):
    return {
        'handler': handler,
        'kind': kind,  # read | write | destructive
        'schema': {
            'name': name,
            'description': description,
            'inputSchema': {
                'type': 'object',
                'properties': properties or {},
                'required': required or [],
            },
        },
    }


_TASK_ID = {'task_id': {'type': 'string', 'description': 'The task id.'}}

TOOLS: Dict[str, Any] = {
    # ── read ──────────────────────────────────────────────────────────────
    'get_metrics': _tool(
        'get_metrics',
        'Get live workspace resource usage (CPU, memory, disk) and alert '
        'thresholds. Call this whenever the user asks about CPU / memory / disk '
        'usage or "how is my workspace doing".',
        _t_get_metrics),
    'list_tasks': _tool(
        'list_tasks',
        'List all build/agent tasks with status (running, completed, waiting, '
        'killed). Call this when the user asks how many tasks are running or '
        'what is going on in the workspace.',
        _t_list_tasks),
    'get_task': _tool(
        'get_task',
        'Get details for one task by id (status, prompt, assistant, recent '
        'output).', _t_get_task, properties=dict(_TASK_ID), required=['task_id']),
    'get_task_output': _tool(
        'get_task_output',
        'Get the recent output/log of a task by id. Use to see what a task '
        'produced or why it failed.', _t_get_task_output,
        properties={**_TASK_ID,
                    'tail': {'type': 'number',
                             'description': 'Only the last N lines.'}},
        required=['task_id']),
    'get_service_health': _tool(
        'get_service_health',
        'Check health of workspace services (VS Code, terminal, browser).',
        _t_get_service_health),
    'get_github_status': _tool(
        'get_github_status',
        'Get git config + GitHub auth status (user name/email, SSH key, gh '
        'login). Call this when the user asks about their git/GitHub setup.',
        _t_get_github_status),
    'search_memory': _tool(
        'search_memory',
        'Search the persistent workspace memory by free text. Call this when '
        'the user asks "do you remember…", "what do I prefer…", "what is my…".',
        _t_search_memory,
        properties={'query': {'type': 'string', 'description': 'Search text.'},
                    'namespace': {'type': 'string',
                                  'description': 'Optional namespace filter.'},
                    'limit': {'type': 'number', 'description': 'Max results.'}},
        required=['query']),
    'list_memory': _tool(
        'list_memory',
        'List stored memories, optionally filtered by namespace.', _t_list_memory,
        properties={'namespace': {'type': 'string',
                                  'description': 'Optional namespace filter.'},
                    'limit': {'type': 'number', 'description': 'Max results.'}}),
    'list_apps': _tool(
        'list_apps',
        'List running/pinned applications (listening ports surfaced on the Apps '
        'page).', _t_list_apps),
    'list_triggers': _tool(
        'list_triggers',
        'List configured webhooks and cron schedules (the Triggers tab).',
        _t_list_triggers),

    # ── AI CTO projects (#465) ──────────────────────────────────────────────
    'list_projects': _tool(
        'list_projects',
        'List all registered projects with live pulse counts (running/waiting '
        'tasks, last activity). Call this to see the whole portfolio or to find '
        'a project id before get_project_brief.',
        _t_list_projects),
    'get_project_brief': _tool(
        'get_project_brief',
        'Get the live brief for a project — north star, goals, decision log, '
        'recent tasks, git branches, triggers — as a compact markdown digest. '
        'Call this FIRST on any question about a project\'s state; answer from '
        'it, not from memory of the conversation. Defaults to the project this '
        'CTO thread is bound to when project_id is omitted.',
        _t_get_project_brief,
        properties={'project_id': {
            'type': 'string',
            'description': 'Project id (default: this thread\'s bound project).'}}),
    'update_project': _tool(
        'update_project',
        'Update a project\'s metadata — e.g. set its north_star, status '
        '(active|paused|archived), name, or workdirs. Use when the user changes '
        'a project\'s direction or you record a status change. Decisions and '
        'goals are memories (use add_memory with tags decision/goal), NOT this.',
        _t_update_project,
        properties={
            'project_id': {'type': 'string',
                           'description': 'Project id (default: bound project).'},
            'fields': {'type': 'object',
                       'description': 'Object of properties to change, e.g. '
                                      '{"north_star": "...", "status": "paused"}.'},
        }, required=['fields'], kind='write'),
    'post_update': _tool(
        'post_update',
        'Post a curated item to the Feed — the user\'s single stream of what '
        'changed and what matters. Use for findings worth their attention '
        '(a briefing/digest, a dependency advisory or release note you judged '
        'relevant, a heads-up) — NOT routine chatter. System activity (tasks, '
        'decisions, triggers) is already posted automatically, so don\'t '
        'duplicate it. Defaults the project to this thread\'s bound project.',
        _t_post_update,
        properties={
            'title': {'type': 'string', 'description': 'One-line headline.'},
            'kind': {'type': 'string',
                     'description': "'briefing' (default) or 'news'."},
            'body_md': {'type': 'string',
                        'description': 'Optional markdown detail.'},
            'project_id': {'type': 'string',
                           'description': 'Project id (default: bound project).'},
            'links': {'type': 'array',
                      'description': 'Optional [{label, ref|href}] — ref is a '
                                     'typed deep-link (task:<id>, thread:<id>, '
                                     'memory:<ns>/<key>), href an external URL.'},
            'waiting': {'type': 'boolean',
                        'description': 'Flag it as needing the user.'},
        }, required=['title'], kind='write'),

    # ── render (Hypervisor rich content) ────────────────────────────────────
    'show_app_preview': _tool(
        'show_app_preview',
        'Embed a LIVE preview (iframe) of an app running on a local port inline '
        'in the Hypervisor chat. Call this when you start, build, or want to show '
        'a running web app — e.g. after launching a dev server. Use list_apps to '
        'find the port if unsure.',
        _t_show_app_preview,
        properties={
            'port': {'type': 'number',
                     'description': 'The local port the app is listening on.'},
            'title': {'type': 'string',
                      'description': 'Optional caption shown above the preview.'},
            'height': {'type': 'number',
                       'description': 'Optional preview height in px (default ~280).'},
        }, required=['port']),
    'show_media': _tool(
        'show_media',
        'Render an image or video inline in the Hypervisor chat. Source is either '
        'a workspace file path under /home/dev (e.g. a screenshot you just saved) '
        'or an http(s) URL. Call this proactively when you produce or reference a '
        'visual.',
        _t_show_media,
        properties={
            'media_kind': {'type': 'string',
                           'description': "'image' or 'video'."},
            'path': {'type': 'string',
                     'description': 'A file under /home/dev (relative), e.g. shot.png.'},
            'url': {'type': 'string',
                    'description': 'An http(s) URL (use instead of path).'},
            'title': {'type': 'string', 'description': 'Optional caption.'},
            'height': {'type': 'number', 'description': 'Optional max height in px.'},
        }, required=['media_kind']),
    'show_file': _tool(
        'show_file',
        'Render a document or file inline in the Hypervisor chat for review — '
        'markdown, text/code, PDF, HTML, or an image/video — from a workspace '
        'file path under /home/dev. Call this proactively whenever you create or '
        'reference a document you want the user to see (e.g. a plan, README, '
        'report, or generated file). Markdown renders formatted; PDF/HTML render '
        'in a sandboxed viewer.',
        _t_show_file,
        properties={
            'path': {'type': 'string',
                     'description': 'A file under /home/dev (relative), e.g. docs/plan.md.'},
            'title': {'type': 'string',
                      'description': 'Optional caption shown above the file.'},
            'height': {'type': 'number',
                       'description': 'Optional viewer height in px (used for PDF/HTML).'},
        }, required=['path']),

    # ── safe write ────────────────────────────────────────────────────────
    'create_task': _tool(
        'create_task',
        'Create a new build/agent task that runs a prompt in the workspace '
        '(the same thing the Build tab "new task" does). Use when the user asks '
        'you to run, build, test, or start something as a background task. For '
        'a task that builds or runs a web app, leave preview on (the default): '
        'a live preview auto-surfaces in this chat the moment its dev server '
        'comes up — no need to call show_app_preview yourself.',
        _t_create_task,
        properties={
            'prompt': {'type': 'string',
                       'description': 'What the task should do.'},
            'workdir': {'type': 'string',
                        'description': 'Working directory (default /home/dev).'},
            'assistant': {'type': 'string',
                          'description': 'Which agent to run it (default claude).'},
            'auto_approve': {'type': 'boolean',
                             'description': 'Skip permission prompts so the '
                             'unattended task does not stall (default true). '
                             'Set false to run with interactive approval '
                             'prompts.'},
            'preview': {'type': 'boolean',
                        'description': 'Default true: append a dev-server '
                        'contract to the prompt and auto-arm a live preview '
                        'that embeds the running app in this chat when it comes '
                        'up. Set false for tasks that do not run a web app '
                        '(pure refactors, tests, docs).'},
        }, required=['prompt'], kind='write'),
    'send_task_message': _tool(
        'send_task_message',
        'Send a follow-up message to an existing running task.',
        _t_send_task_message,
        properties={**_TASK_ID,
                    'message': {'type': 'string',
                                'description': 'The follow-up text.'}},
        required=['task_id', 'message'], kind='write'),
    'add_memory': _tool(
        'add_memory',
        'Store a durable fact in workspace memory. Use when the user says '
        '"remember that…", "note that…", or states a stable preference.',
        _t_add_memory,
        properties={
            'namespace': {'type': 'string',
                          'description': 'e.g. user.preferences or project.<repo>.'},
            'key': {'type': 'string', 'description': 'Short stable key.'},
            'value': {'type': 'string', 'description': 'The fact (one per entry).'},
            'kind': {'type': 'string',
                     'description': 'semantic|episodic|procedural|preference.'},
            'tags': {'type': 'string', 'description': 'Optional comma tags.'},
        }, required=['namespace', 'key', 'value'], kind='write'),
    'watch': _tool(
        'watch',
        'Arm a CROSS-TURN watcher owned by the workspace runner: it keeps '
        'polling after your turn ends and posts the outcome into this chat as '
        'a new message (so you get a real notification — no re-polling, and '
        'unlike Bash run_in_background it survives the turn boundary). Kinds: '
        "'task' — target is a task_id (from create_task); fires when the task "
        "completes, errors, is killed, or goes waiting-for-input. 'command' — "
        "target is a shell command; fires when it exits 0. 'file' — target is "
        'an absolute path; fires when it appears, is removed, or its mtime '
        "changes. 'port' — target is the related task_id; fires when a NEW "
        'loopback dev-server port starts listening and auto-embeds a live '
        'preview of it in this chat (you normally get this automatically from '
        'create_task; arm it by hand only for an app started some other way). '
        'On timeout you get an explicit timeout message instead (except a '
        "'port' watcher, which expires silently). After arming, just end your "
        'turn.',
        _t_watch,
        properties={
            'kind': {'type': 'string',
                     'description': "'task' | 'command' | 'file' | 'port'."},
            'target': {'type': 'string',
                       'description': 'Task id, shell predicate, or absolute '
                                      'path (per kind).'},
            'note': {'type': 'string',
                     'description': 'Optional: why you are waiting — echoed '
                                    'back in the notification.'},
            'interval': {'type': 'number',
                         'description': 'Poll interval seconds (default 20, '
                                        'min 5).'},
            'timeout': {'type': 'number',
                        'description': 'Give-up seconds (default 3600, max '
                                       '86400); a timeout is reported as its '
                                       'own message.'},
        }, required=['kind', 'target'], kind='write'),
    'list_watchers': _tool(
        'list_watchers',
        'List this chat thread\'s cross-turn watchers and their states '
        '(armed, fired, delivered, timeout, cancelled).',
        _t_list_watchers),
    'cancel_watcher': _tool(
        'cancel_watcher',
        'Cancel one of this chat thread\'s cross-turn watchers by id (from '
        'watch/list_watchers).',
        _t_cancel_watcher,
        properties={'watcher_id': {'type': 'string',
                                   'description': 'The watcher id.'}},
        required=['watcher_id'], kind='write'),
    'pin_app': _tool(
        'pin_app',
        'Pin a local port to the Applications page so it is easy to open.',
        _t_pin_app,
        properties={
            'port': {'type': 'number', 'description': 'The local port to pin.'},
            'name': {'type': 'string', 'description': 'Optional display name.'},
            'strip_prefix': {'type': 'boolean',
                             'description': 'Strip the proxy path prefix.'},
        }, required=['port'], kind='write'),

    # ── destructive (confirm=true required) ───────────────────────────────
    'kill_task': _tool(
        'kill_task',
        'Kill/stop a running task by id. DESTRUCTIVE: first call returns '
        'CONFIRMATION_REQUIRED; get the user\'s explicit approval, then call '
        'again with confirm=true.',
        _t_kill_task,
        properties={**_TASK_ID,
                    'confirm': {'type': 'boolean',
                                'description': 'Must be true to actually kill.'}},
        required=['task_id'], kind='destructive'),
    'delete_memory': _tool(
        'delete_memory',
        'Delete a stored memory by namespace+key. DESTRUCTIVE: first call '
        'returns CONFIRMATION_REQUIRED; get the user\'s explicit approval, then '
        'call again with confirm=true.',
        _t_delete_memory,
        properties={
            'namespace': {'type': 'string'},
            'key': {'type': 'string'},
            'confirm': {'type': 'boolean',
                        'description': 'Must be true to actually delete.'},
        }, required=['namespace', 'key'], kind='destructive'),
}


def _enabled_tools() -> Dict[str, Any]:
    """Filter out write + destructive tools when the workspace is read-only."""
    if _readonly():
        return {n: t for n, t in TOOLS.items() if t['kind'] == 'read'}
    return TOOLS


# ───────────────────────────────────────────────────────────────────────────
# MCP dispatch
# ───────────────────────────────────────────────────────────────────────────

def _handle_initialize(id_val, params):
    _send({
        'jsonrpc': '2.0',
        'id': id_val,
        'result': {
            'protocolVersion': '2024-11-05',
            'capabilities': {'tools': {}},
            'serverInfo': {'name': 'dashboard', 'version': '0.1.0'},
        },
    })


def _handle_list_tools(id_val, params):
    _send({
        'jsonrpc': '2.0',
        'id': id_val,
        'result': {'tools': [t['schema'] for t in _enabled_tools().values()]},
    })


def _handle_call_tool(id_val, params):
    if not isinstance(params, dict):
        _error(id_val, -32602, 'params must be an object')
        return
    name = params.get('name', '')
    args = params.get('arguments', {}) or {}
    tools = _enabled_tools()
    tool = tools.get(name)
    if tool is None:
        # Named tool exists but is gated off by READONLY_MODE.
        if name in TOOLS:
            _send({'jsonrpc': '2.0', 'id': id_val,
                   'result': _err(
                       f'Tool "{name}" is disabled: this workspace is read-only.')})
            return
        _error(id_val, -32601, f'Unknown tool: {name}')
        return
    try:
        result = tool['handler'](args)
        _send({'jsonrpc': '2.0', 'id': id_val, 'result': result})
    except Exception as e:
        _log(f'tool {name} error: {traceback.format_exc()}')
        _send({'jsonrpc': '2.0', 'id': id_val,
               'result': _err(f'{type(e).__name__}: {e}')})


_HANDLERS = {
    'initialize': _handle_initialize,
    'listTools': _handle_list_tools,
    'tools/list': _handle_list_tools,
    'tools/call': _handle_call_tool,
}


def main() -> int:
    _log(f'started (base={BASE_URL}, readonly={_readonly()})')
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
        # Notifications (no id, e.g. notifications/initialized) need no reply.
        if id_val is None and method.startswith('notifications/'):
            continue
        handler = _HANDLERS.get(method)
        if handler:
            handler(id_val, params)
        elif id_val is not None:
            _error(id_val, -32601, f'Method not found: {method}')
    _log('exiting on EOF')
    return 0


if __name__ == '__main__':
    sys.exit(main())
