"""Workspace-level reads: mode, events, Mission Control, scans (#100).

The last domain in the series, and the one that is a domain only by
elimination: six read surfaces that belong to the workspace as a whole
rather than to any one feature.

  * `/api/mode` — the deployment-mode probe the SPA fetches at boot to decide
    which UI to hide. Deliberately unauthenticated so a read-only public demo
    can reach it without an auth proxy in front.
  * `/api/events` — the SSE firehose of task.created / task.status.
  * `/api/missioncontrol/*` — the normalized card queue and one card's detail.
  * `/api/workspace/dirs`, `/api/subagents`,
    `/api/security/instruction-scan`.

`handle_mode` is new in name only: do_GET built that JSON inline in its
dispatch branch, the same way the desktop item read did. Lifting it gives the
table something to point at and puts the flags it reports next to a comment
explaining each one, rather than in the middle of a 200-line method.
"""

import json
import os
import queue
import re
import time
import urllib.parse

import handlers
import instruction_scan
from handlers.routing import RouteTable


class WorkspaceRoutes:
    """BrowserHandler methods backing `ROUTES`."""

    def handle_mode(self):
        """GET /api/mode — the deployment-mode probe the SPA reads at boot.

        Intentionally unauthenticated so the read-only public demo can fetch
        it with no auth proxy in front. Every flag is one server.py started
        with — never derived per-request, never user-controllable. do_GET
        built this dict inline; only its location has changed.
        """
        self.send_json({
            'readOnly': handlers.server.READONLY_MODE,
            'authed': handlers.server.AUTH_MODE != 'none',
            'authMode': handlers.server.AUTH_MODE,
            'demoShowAll': handlers.server.DEMO_SHOW_ALL,
            # AI CTO gate (#467) — boot-loaded so the SPA can hide the /cto
            # nav item before the route mounts. Rides the Hypervisor.
            'ctoEnabled': handlers.server.cto_available(),
            # devcontainer.json support (#594). Independent of ctoEnabled:
            # reading the file a repo already carries is a workspace
            # capability, not part of the AI CTO.
            'devcontainerEnabled': handlers.server.DevcontainerManager.available(),
            # Board Processor (#588/#589). Independent of ctoEnabled —
            # working someone else's tracker and running an AI CTO over our
            # own projects are separate capabilities.
            'boardEnabled': handlers.server._BOARDS_AVAILABLE,
        })

    def handle_missioncontrol_queue(self):
        """Mission Control board (#425): builds + chats + sub-agents as one
        normalized card queue, grouped by what needs the human. Read-only."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        self.send_json(handlers.server.missioncontrol_queue())

    def handle_missioncontrol_card(self, card_id):
        """Drawer detail for one Mission Control card (#425 phase 3): the
        card, a normalized activity timeline, and an output tail. Read-only."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        detail = handlers.server.missioncontrol_card_detail(card_id)
        if detail is None:
            self.send_json({'error': 'Card not found'}, 404)
            return
        self.send_json(detail)

    def handle_workspace_dirs(self):
        """List candidate working directories under /home/dev for the
        new-task picker. Reuses Claude auth (OAuth header OR bearer token)."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        self.send_json({'dirs': handlers.server.WorkspaceManager.list_dirs()})

    def handle_events_stream(self):
        """Server-Sent Events firehose of dashboard events (task.created /
        task.status). Subscribes to EventBroker and forwards each event as a
        named SSE frame so the SPA can replace per-route polling (issue #93).

        Framing mirrors handle_claude_stream_output: heartbeat comments keep
        proxies from closing an idle connection, and STREAM_MAX_SECONDS caps
        the lifetime so a never-disconnecting client can't pin a handler
        thread forever (the SPA reconnects on the `end` event).
        """
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return

        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()

        def write_raw(payload_bytes):
            try:
                self.wfile.write(payload_bytes)
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        q = handlers.server.EventBroker.subscribe()
        started = time.time()
        # Greet so the client can flip to "connected" and stop its poll fallback.
        if not write_raw(b'event: ready\ndata: {}\n\n'):
            handlers.server.EventBroker.unsubscribe(q)
            return
        try:
            while True:
                if time.time() - started > handlers.server.STREAM_MAX_SECONDS:
                    write_raw(b'event: end\ndata: timeout\n\n')
                    return
                try:
                    event = q.get(timeout=15)
                except queue.Empty:
                    if not write_raw(b': keep-alive\n\n'):
                        return
                    continue
                payload = json.dumps(event.get('data', {}))
                frame = f"event: {event.get('type', 'message')}\ndata: {payload}\n\n"
                if not write_raw(frame.encode('utf-8')):
                    return
        finally:
            handlers.server.EventBroker.unsubscribe(q)

    def handle_subagents_list(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        # Parse ?parent=<task_id> filter from query string
        parent = None
        if '?' in self.path:
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            parent_val = params.get('parent', [None])[0]
            if parent_val:
                parent = parent_val
        if not parent:
            self.send_json({'subagents': [], 'count': 0,
                            'running_count': 0, 'completed_count': 0,
                            'error_count': 0, 'note': 'pass ?parent=<task_id> to list sub-agents'})
            return
        tasks = handlers.server.ClaudeTaskManager.list_tasks(parent=parent)
        subagents = []
        running = 0
        completed = 0
        errored = 0
        for t in tasks:
            status = t['status']
            sa = {
                'tool_use_id': t['task_id'],
                'tool': 'spawn_agent',
                'timestamp': t['created_at'],
                'session_id': t['task_id'],
                'project': 'kube-coder',
                'description': t.get('prompt', '')[:200],
                'subagent_type': t.get('assistant', 'claude'),
                'prompt': t.get('prompt', ''),
                'status': status,
                'ended_at': t.get('finished_at'),
                'is_error': status == 'error',
            }
            subagents.append(sa)
            if status == 'running':
                running += 1
            elif status in ('completed',):
                completed += 1
            elif status in ('error', 'killed'):
                errored += 1
        self.send_json({
            'subagents': subagents,
            'count': len(subagents),
            'running_count': running,
            'completed_count': completed,
            'error_count': errored,
        })

    def _handle_instruction_scan(self):
        """Scan agent-readable instruction files for hidden text (#559).

        WHY THIS IS A SERVER ENDPOINT AND NOT AN MCP TOOL. The threat is a
        cloned repo whose CLAUDE.md carries invisible instructions the agent
        then obeys (the TrapDoor class). If the agent is already following
        that file, asking the agent to scan is worthless — the injected text
        just says "skip the scan" or "report clean". So detection runs here,
        out of band, where a compromised agent cannot suppress it, and the
        result lands in the Feed rather than in the agent's transcript.

        Reports only. Nothing is stripped or rewritten: silently editing a
        file an agent is about to read would itself be an injection vector,
        and a false positive that mangles someone's README is unforgivable.
        """
        if not self.check_app_proxy_auth():
            self.send_response(401)
            self.end_headers()
            return
        if not handlers.server._INSTRUCTION_SCAN_AVAILABLE:
            self.send_json({'error': 'instruction_scan module unavailable'}, status=503)
            return
        qs = urllib.parse.parse_qs(self.path.split('?', 1)[1]) if '?' in self.path else {}
        confine = os.path.realpath(handlers.server.INSTRUCTION_SCAN_ROOT)
        requested = (qs.get('root') or [confine])[0]
        # Confine the walk to the persistent volume. Without this, `root` is an
        # arbitrary-path directory read for anyone who can reach the endpoint.
        # Compare realpaths and require a separator on the prefix, so neither
        # `<root>/../etc` nor a lookalike sibling like `/home/devious` passes.
        root = os.path.realpath(requested)
        if root != confine and not root.startswith(confine + os.sep):
            self.send_json(
                {'error': 'root must be under {}'.format(confine)}, status=400)
            return
        if not os.path.isdir(root):
            self.send_json({'error': 'root is not a directory'}, status=404)
            return
        try:
            report = instruction_scan.scan_tree(root)
        except Exception as e:                      # never 500 on a scan
            self.send_json({'error': 'scan failed: {}'.format(e)}, status=500)
            return
        # Surface high-severity hits in the Feed. Deduped per root so a repeated
        # scan updates one item instead of spamming; the user sees this whether
        # or not they were looking at the dashboard when it ran.
        # `hasattr(handlers.server, …)` where server.py wrote
        # `'FeedManager' in globals()`. The intent — "only emit if the Feed
        # exists" — is the same, but `globals()` resolves to the *defining*
        # module, so left verbatim this guard would ask whether FeedManager
        # is defined in handlers/workspace.py, always be False, and silently
        # stop every high-severity scan from reaching the Feed.
        if report['high'] and hasattr(handlers.server, 'FeedManager'):
            files = ', '.join(os.path.relpath(r['path'], root)
                              for r in report['results'] if r['counts']['high'])[:200]
            decoded = ' / '.join(r['decoded_hidden_text']
                                 for r in report['results']
                                 if r['decoded_hidden_text'])[:400]
            body = [
                'Hidden, non-rendering characters were found in files that '
                'coding agents read as instructions.',
                '',
                '**Files:** {}'.format(files),
                '**High-severity findings:** {}'.format(report['high']),
            ]
            if decoded:
                body += ['', '**Decoded hidden text:**', '', '```', decoded, '```']
            body += [
                '',
                'This is the technique used by the TrapDoor supply-chain campaign: '
                'text invisible to a human reviewer but read verbatim by the agent. '
                'Nothing has been modified — review these files before letting an '
                'agent work in this tree.',
            ]
            handlers.server.FeedManager.emit(
                'news',
                'Hidden text found in agent instruction files',
                body_md='\n'.join(body),
                source='instruction-scan',
                waiting=True,
                dedupe_key='instruction-scan:{}'.format(root),
            )
        self.send_json(report)


ROUTES = RouteTable()

# Unauthenticated on purpose — see handle_mode.
ROUTES.add('GET', '/api/mode', 'handle_mode')
ROUTES.add('GET', '/api/events', 'handle_events_stream')
ROUTES.add('GET', '/api/workspace/dirs', 'handle_workspace_dirs')
ROUTES.add('GET', '/api/subagents', 'handle_subagents_list')
# Hidden-text scan of agent-readable instruction files (#559). Server-side on
# purpose: a compromised agent must not be able to suppress its own scan.
ROUTES.add('GET', '/api/security/instruction-scan', '_handle_instruction_scan')

# Mission Control (#425): the normalized card queue, and one card's detail.
# A card id is `<kind>:<id>`, and the kind is an allowlist.
ROUTES.add('GET', '/api/missioncontrol/queue', 'handle_missioncontrol_queue')
ROUTES.add('GET', re.compile(
    r'^/api/missioncontrol/cards/((?:build|chat|subagent):[A-Za-z0-9_-]+)$'),
    'handle_missioncontrol_card')
