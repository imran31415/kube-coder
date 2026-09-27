"""Builds: the Claude Task API and the isolated worktrees behind it (#100).

Twenty-one routes across three verbs — the biggest surface in the series so
far, and the first one that was **not contiguous** in the dispatch chains.

## Not contiguous, and why hoisting is still behaviour-preserving

Every domain lifted before this one occupied an unbroken run of elif branches,
so reproducing the old order was a copy. The task routes are interleaved with
the hypervisor and gateway ones instead: on GET, `/api/claude/tasks` sits above
five `/api/hypervisor/*` and five `/api/gateway/*` branches while
`/api/claude/tasks/{id}` sits below them; on POST the four exact routes are in
the elif chain and the six `{id}/…` ones are in the regex block at the bottom,
with eight hypervisor routes in between.

Collapsing that into one table hoists the later task routes above everything
they used to sit under. That is safe here for a reason worth stating rather
than assuming: **no pattern in this table can match a path owned by any route
it jumped over, and none of those routes can match a path in this table.**
Every task path starts with the literal `/api/claude/tasks`, `/api/claude/auth`,
`/api/claude/assistants` or `/api/worktrees`; every route hoisted past starts
with `/api/hypervisor`, `/api/gateway`, `/api/missioncontrol`,
`/api/claude/apps`, `/api/workspace`, `/api/provider-keys`, `/api/subscriptions`,
`/api/mcp-servers`, `/api/projects`, `/api/boards`, `/api/devcontainer`,
`/api/feed`, `/api/push` or `/api/desktop`. The prefixes are disjoint literals.

`tests/tasks_routes_test.py` asserts exactly that, by name, against the real
table — the list of jumped-over paths is in the test, so a future route added
to this table that *would* shadow one of them fails there.

## The ordering hazards inside the table

`{id}/stream`, `{id}/output`, `{id}/worktree` and `{id}/worktree/diff` all
precede the bare `{id}`. As with memory and triggers, that turns out to be
presentational: a task id is `[A-Za-z0-9_-]+`, which excludes `/`, and every
pattern is `$`-anchored, so `{id}/output` cannot be read as an `{id}`. The
tests assert it by resolving against a reversed copy of the table.

One route was a branch on a capture group rather than two routes:

    m = re.match(r'^/api/claude/tasks/([^/]+)/worktree(/diff)?$', path)
    if m.group(2): self.handle_task_worktree_diff()
    else:          self.handle_task_worktree_status()

It is two anchored routes here, `/worktree/diff` before `/worktree`, which is
the same resolution with the optional group gone.

## `worktrees` is reached through `handlers.server`

`handle_worktrees_sweep` catches `worktrees.WorktreeError`, and server.py
imports that module **guarded** — `worktrees` is None when the import fails and
`WorktreeManager.available()` reports it. An `import worktrees` here would turn
that graceful degradation into a hard failure at module load, so the reference
goes through the bound server module like every other one.
"""

import json
import os
import re
import subprocess
import time
import urllib.parse

import handlers
from handlers.routing import RouteTable


class TaskRoutes:
    """BrowserHandler methods backing `ROUTES`."""

    def handle_claude_list_tasks(self):
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
        tasks = handlers.server.ClaudeTaskManager.list_tasks(parent=parent)
        self.send_json({'tasks': tasks})

    def handle_claude_get_task(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        task = handlers.server.ClaudeTaskManager.get_task(self._claude_task_id)
        if task is None:
            self.send_json({'error': 'Task not found'}, 404)
            return
        self.send_json(task)

    def handle_claude_get_output(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        # Parse ?tail=N and ?ansi=1 from query string
        tail = None
        ansi = False
        if '?' in self.path:
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            tail_val = params.get('tail', [None])[0]
            if tail_val and tail_val.isdigit():
                tail = int(tail_val)
            ansi = params.get('ansi', ['0'])[0] in ('1', 'true')
        output = handlers.server.ClaudeTaskManager.get_task_output(self._claude_task_id, tail=tail, ansi=ansi)
        if output is None:
            self.send_json({'error': 'Task or output not found'}, 404)
            return
        # JSON-wrap the body so the SPA's typed fetch client (which expects
        # `{output: string}`) deserializes correctly. The legacy dashboard's
        # raw-text consumer was retired with the dashboard.html removal.
        self.send_json({'output': output})

    def handle_claude_stream_output(self):
        """Server-Sent Events stream of a task's rendered tmux output.

        Polls `tmux capture-pane` every ~1.5s and emits the diff vs the previous
        capture. We use capture-pane (not raw pipe-pane bytes) because claude-code
        is an interactive TUI: it emits cursor-moves, \\r-redraws, and spinner
        animations that look like garbage when streamed raw. tmux maintains the
        rendered screen state, so capture-pane gives us clean text.

        For completed tasks (tmux session gone) falls back to output.log.

        Query params:
          from=start (default) — send the current full capture once, then diffs.
          from=live            — skip initial capture, only send what changes after.
        """
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return

        task_id = self._claude_task_id
        task_dir = os.path.join(handlers.server.ClaudeTaskManager.TASKS_DIR, task_id)
        meta_path = os.path.join(task_dir, 'task.json')
        if not os.path.isfile(meta_path):
            self.send_json({'error': 'Task not found'}, 404)
            return

        from_param = 'start'
        if '?' in self.path:
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            from_param = params.get('from', ['start'])[0]

        try:
            with open(meta_path, 'r') as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError):
            self.send_json({'error': 'Task metadata unreadable'}, 500)
            return
        session_name = meta.get('tmux_session', f'kube-coder-{task_id}')
        output_log = os.path.join(task_dir, 'output.log')

        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        # Tell ingress-nginx not to buffer — otherwise SSE chunks won't flush in real time.
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

        def write_sse(data):
            # SSE framing: prefix every line with "data: ", terminate event with blank line.
            lines = data.split('\n')
            block = ''.join(f'data: {line}\n' for line in lines) + '\n'
            return write_raw(block.encode('utf-8'))

        def capture():
            """Return the current pane content (with history), or fall back to
            output.log if the tmux session is gone (task completed/killed)."""
            r = subprocess.run(
                ['tmux', 'capture-pane', '-J', '-t', session_name, '-p', '-S', '-2000'],
                capture_output=True, text=True,
            )
            if r.returncode == 0 and r.stdout:
                return handlers.server.strip_ansi(r.stdout).rstrip('\n') + '\n'
            if os.path.exists(output_log):
                try:
                    with open(output_log, 'r', errors='replace') as f:
                        return handlers.server.strip_ansi(f.read()).rstrip('\n') + '\n'
                except OSError:
                    pass
            return ''

        last_capture = ''
        if from_param == 'start':
            initial = capture()
            if initial:
                if not write_sse(initial):
                    return
                last_capture = initial
        else:
            # Live mode: prime last_capture so we don't replay history.
            last_capture = capture()

        last_heartbeat = time.time()
        last_change = time.time()
        started = time.time()
        poll_interval = 1.5
        heartbeat_interval = 15
        idle_grace = 5  # seconds after task stops with no diff before we close

        while True:
            # Hard cap so a client that never disconnects can't pin a
            # handler thread forever (combined with ThreadingHTTPServer's
            # unbounded thread spawn this is the easiest path to DoS).
            # Emit a graceful end event so the SPA reconnects cleanly.
            if time.time() - started > handlers.server.STREAM_MAX_SECONDS:
                write_raw(b'event: end\ndata: timeout\n\n')
                return
            new_capture = capture()
            if new_capture != last_capture:
                if new_capture.startswith(last_capture):
                    # Pure append — emit just the new tail.
                    diff = new_capture[len(last_capture):]
                elif last_capture.startswith(new_capture):
                    # Capture shrank (rare; pane cleared). Wait for next snapshot.
                    diff = ''
                else:
                    # History scrolled off / pane cleared. Send a marker plus the
                    # new content so the user sees something without a giant replay.
                    diff = '\n[output buffer rewound]\n' + new_capture
                last_capture = new_capture
                if diff:
                    if not write_sse(diff):
                        return
                    last_change = time.time()
                    last_heartbeat = last_change

            if time.time() - last_heartbeat > heartbeat_interval:
                if not write_raw(b': keep-alive\n\n'):
                    return
                last_heartbeat = time.time()

            try:
                with open(meta_path, 'r') as f:
                    meta = json.load(f)
                handlers.server.ClaudeTaskManager._reconcile_status(meta, task_dir)
                status = meta.get('status', 'unknown')
            except (OSError, json.JSONDecodeError):
                status = 'unknown'

            if status not in ('running', 'waiting-for-input') and time.time() - last_change > idle_grace:
                write_raw(f'event: end\ndata: {status}\n\n'.encode('utf-8'))
                return

            time.sleep(poll_interval)

    def handle_claude_list_assistants(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        self.send_json({'assistants': handlers.server.ClaudeTaskManager.available_assistants()})

    def handle_claude_get_token(self):
        if not self.check_oauth_only():
            self.send_json({'error': 'This endpoint requires OAuth2 authentication (browser session)'}, 401)
            return
        token = handlers.server.ClaudeTaskManager.get_or_create_token()
        self.send_json({'token': token})

    def handle_claude_regenerate_token(self):
        if not self.check_oauth_only():
            self.send_json({'error': 'This endpoint requires OAuth2 authentication (browser session)'}, 401)
            return
        token = handlers.server.ClaudeTaskManager.regenerate_token()
        self.send_json({'token': token})

    def _query_params(self):
        if '?' not in self.path:
            return {}
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def _query_flag(self, name):
        return (self._query_params().get(name, ['0'])[0] or '').lower() in (
            '1', 'true', 'yes')

    def handle_task_worktree_status(self):
        """GET /api/claude/tasks/{id}/worktree[?fresh=1] — branch, what
        changed since the base (committed and not), push command, and whether
        it can be removed."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        body, status = handlers.server.WorktreeManager.status_for_task(
            self._claude_task_id, fresh=self._query_flag('fresh'))
        self.send_json(body, status)

    def handle_task_worktree_diff(self):
        """GET /api/claude/tasks/{id}/worktree/diff?file=<path> — one changed
        file's diff against the base. Only files the status lists.

        Always needs a real identity: this returns file CONTENTS, and a public
        read-only demo (AUTH_MODE=none) confines its file reads to
        PUBLIC_FILE_ROOT and refuses dot-paths — `~/.worktrees` is both."""
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        file = (self._query_params().get('file') or [''])[0]
        body, status = handlers.server.WorktreeManager.diff_for_task(self._claude_task_id, file)
        self.send_json(body, status)

    def handle_task_worktree_remove(self):
        """DELETE /api/claude/tasks/{id}/worktree[?force=1] — remove the
        directory. Refused while its Build runs; refused when dirty unless
        forced; the branch is always kept."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        body, status = handlers.server.WorktreeManager.remove_for_task(
            self._claude_task_id, force=self._query_flag('force'))
        self.send_json(body, status)

    def handle_worktrees_list(self):
        """GET /api/worktrees — every worktree on the PVC (Settings)."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        self.send_json(handlers.server.WorktreeManager.list_view())

    def handle_worktrees_remove(self, key, slug):
        """DELETE /api/worktrees/{repo}/{slug}[?force=1] — for worktrees whose
        Build is gone (or that were made by hand)."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        body, status = handlers.server.WorktreeManager.remove_by_key(
            urllib.parse.unquote(key), urllib.parse.unquote(slug),
            force=self._query_flag('force'))
        self.send_json(body, status)

    def handle_worktrees_sweep(self):
        """POST /api/worktrees/sweep {dry_run?} — clean up now."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body() if int(
                self.headers.get('Content-Length') or 0) else {}
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        dry_run = isinstance(data, dict) and data.get('dry_run') is True
        try:
            report = handlers.server.WorktreeManager.sweep(dry_run=dry_run)
        except handlers.server.worktrees.WorktreeError as e:
            self.send_json(e.as_dict(), 503 if e.code == 'lock_timeout' else 500)
            return
        self.send_json(report)

    def handle_claude_create_task(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        # Prompt is optional — the SPA's "Create build" flow drops the textarea
        # and spawns an interactive Claude/OpenCode session the user can type
        # into directly. Empty prompt → no-op Enter after the 3s assistant init.
        prompt = data.get('prompt', '').strip()
        workdir = data.get('workdir')
        response_url = data.get('response_url') or None
        response_secret = data.get('response_secret') or None
        source = data.get('source') or None
        disable_memory_injection = bool(data.get('disable_memory_injection'))
        assistant = data.get('assistant') or None
        # Optional per-build model / reasoning effort (#483, #362). Omitted →
        # create_task falls back to the bound project's defaults, then the
        # workspace ones; both are validated there, so a bad value degrades
        # rather than 400-ing a build.
        model = data.get('model') or None
        effort = data.get('effort') or None
        parent_task_id = data.get('parent_task_id') or None
        # Explicit project binding (#533) — the CTO's dispatch tool passes the
        # thread's project so the build attributes to it regardless of workdir.
        # Unknown ids fall through to workdir inference rather than 400-ing: a
        # bad hint must not block a build.
        project_id = (data.get('project_id') or '').strip() or None
        if project_id and (not handlers.server.ProjectsManager.valid_id(project_id)
                           or handlers.server.ProjectsManager.get_project(project_id) is None):
            project_id = None
        # Skip-permissions default: honor an explicit body flag; otherwise let
        # the source decide — unattended sources auto-approve, the interactive
        # Build tab does not (issue #296).
        auto_approve = handlers.server.ClaudeTaskManager.resolve_auto_approve(
            source, data.get('auto_approve'))
        if response_url and not handlers.server.ClaudeTaskManager._is_safe_response_url(response_url):
            self.send_json({'error': 'response_url must be http(s)'}, 400)
            return
        # An EXPLICITLY requested assistant that is listed but unauthenticated
        # (#702) is rejected here with the missing key named, instead of being
        # quietly downgraded to the workspace default by resolve_assistant. The
        # picker already blocks Start build, so reaching this is either a
        # free-form client or a key cleared between load and submit — both want
        # to be told which key is missing, not handed a different agent's build.
        not_ready = handlers.server.ClaudeTaskManager.assistant_not_ready_error(assistant)
        if not_ready:
            self.send_json({'error': not_ready}, 400)
            return
        # Isolation (#701). Only a JSON `true` counts — a string "false" is
        # truthy, and turning isolation on by accident is a surprise worktree.
        isolate = data.get('isolate') is True
        base_ref = data.get('base_ref') or None
        worktree_slug = data.get('worktree_slug') or None
        for name, value in (('base_ref', base_ref),
                            ('worktree_slug', worktree_slug)):
            if value is not None and (not isinstance(value, str)
                                      or len(value) > 200):
                self.send_json({'error': f'{name} must be a short string',
                                'code': 'bad_ref' if name == 'base_ref'
                                else 'bad_slug'}, 400)
                return
        if (base_ref or worktree_slug) and not isolate:
            self.send_json({'error': 'base_ref / worktree_slug need '
                                     '"isolate": true', 'code': 'invalid'}, 400)
            return
        task = handlers.server.ClaudeTaskManager.create_task(
            prompt,
            workdir=workdir,
            response_url=response_url,
            response_secret=response_secret,
            source=source,
            disable_memory_injection=disable_memory_injection,
            assistant=assistant,
            model=model,
            effort=effort,
            parent_task_id=parent_task_id,
            auto_approve=auto_approve,
            project_id=project_id,
            isolate=isolate,
            base_ref=base_ref,
            worktree_slug=worktree_slug,
        )
        refused = handlers.server.WorktreeManager.HTTP_STATUS.get(task.get('status'))
        if refused and task.get('task_id') is None:
            body = {'error': task.get('error')}
            if task.get('code'):
                body.update({k: v for k, v in task.items()
                             if k not in ('status', 'task_id')})
            self.send_json(body, refused)
            return
        self.send_json(task, 201)

    def handle_claude_create_terminal_task(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        # Body is optional — accept {} or no body at all.
        workdir = None
        try:
            data = self.read_json_body()
            if isinstance(data, dict):
                workdir = data.get('workdir') or None
        except (json.JSONDecodeError, ValueError):
            pass
        task = handlers.server.ClaudeTaskManager.create_terminal_task(workdir=workdir)
        if task.get('status') == 'rejected':
            self.send_json({'error': task.get('error')}, 429)
            return
        # Pre-arm the ttyd entry script so the next /oauth/terminal/ load
        # attaches to this session instead of dropping to a fresh bash.
        if task.get('status') != 'error':
            session_name = task.get('tmux_session', '')
            try:
                with open('/tmp/.claude-terminal-pending', 'w') as f:
                    f.write(session_name)
            except OSError:
                pass
        self.send_json(task, 201)

    def handle_claude_followup(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        prompt = data.get('prompt', '').strip()
        if not prompt:
            self.send_json({'error': 'prompt is required'}, 400)
            return
        # submit defaults to True (normal send). False = paste-only (no Enter).
        submit = data.get('submit', True) is not False
        task, err = handlers.server.ClaudeTaskManager.send_followup(self._claude_task_id, prompt, submit=submit)
        if task is None:
            self.send_json({'error': err or 'Task not found'}, 404)
            return
        self.send_json(task)

    def handle_claude_rename_task(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        if not isinstance(data, dict):
            self.send_json({'error': 'Body must be a JSON object'}, 400)
            return
        meta, err = handlers.server.ClaudeTaskManager.rename_task(self._claude_task_id, data)
        if meta is None:
            status = 404 if err == 'not_found' else 400
            self.send_json({'error': err or 'Task not found'}, status)
            return
        self.send_json(meta)

    def handle_claude_delete_task(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        task = handlers.server.ClaudeTaskManager.delete_task(self._claude_task_id)
        if task is None:
            self.send_json({'error': 'Task not found'}, 404)
            return
        self.send_json(task)

    def handle_claude_redeliver_hook(self):
        """POST /api/claude/tasks/{id}/redeliver-hook — re-attempt a failed
        completion hook (issue #97). Auth + readonly gated."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if self._readonly_block():
            return
        ok, msg = handlers.server.ClaudeTaskManager.redeliver_hook(self._claude_task_id)
        if not ok:
            code = 404 if msg == 'task not found' else 400
            self.send_json({'error': msg}, code)
            return
        self.send_json({'task_id': self._claude_task_id, 'status': msg}, 202)

    def handle_claude_prepare_terminal(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        task_id = self._claude_task_id
        task = handlers.server.ClaudeTaskManager.get_task(task_id)
        if task is None:
            self.send_json({'error': 'Task not found'}, 404)
            return
        session_name = task.get('tmux_session', f'kube-coder-{task_id}')
        # Wait up to ~3s for the tmux session to be visible to has-session
        # before declaring ourselves ready. Without this, the SPA can load
        # the iframe and run terminal-entry.sh while the session that
        # create_task spawned is still mid-registration; the entry script
        # falls through to bash and the user sees a fresh shell instead
        # of their task. Cheap on the happy path — has-session is ~1ms.
        deadline = time.time() + 3.0
        session_ready = False
        while time.time() < deadline:
            check = subprocess.run(
                ['tmux', 'has-session', '-t', session_name],
                capture_output=True,
            )
            if check.returncode == 0:
                session_ready = True
                break
            time.sleep(0.1)
        try:
            with open('/tmp/.claude-terminal-pending', 'w') as f:
                f.write(session_name)
            self.send_json({
                'ok': True,
                'session': session_name,
                'session_ready': session_ready,
            })
        except OSError as e:
            self.send_json({'error': str(e)}, 500)

    def handle_claude_scroll_mode(self):
        """Toggle tmux copy-mode for a task's pane.
        Replaces the user holding Ctrl+B [ to scroll and `q` to exit —
        instead the SPA shows a single Scroll-mode button that POSTs here.
        Once in copy-mode, arrow keys / Page Up / mouse wheel all navigate
        the scrollback (xterm.js's alt-screen wheel→arrow conversion lands
        on copy-mode's own arrow bindings, which is what we want here)."""
        if self._readonly_block():
            return
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        action = (data.get('action') or '').strip().lower()
        # enter/exit toggle copy-mode. The scroll directions drive copy-mode
        # navigation server-side so touch clients (mobile sends no wheel events,
        # so xterm's wheel->arrow conversion inside the ttyd iframe never fires)
        # can scroll the scrollback by POSTing here instead.
        SCROLL_CMDS = {
            'up': 'scroll-up', 'down': 'scroll-down',
            'page-up': 'page-up', 'page-down': 'page-down',
        }
        if action not in ('enter', 'exit') and action not in SCROLL_CMDS:
            self.send_json(
                {'error': "action must be 'enter', 'exit', or a scroll direction"},
                400,
            )
            return
        task_id = self._claude_task_id
        task = handlers.server.ClaudeTaskManager.get_task(task_id)
        if task is None:
            self.send_json({'error': 'Task not found'}, 404)
            return
        session_name = task.get('tmux_session', f'kube-coder-{task_id}')
        if action == 'enter':
            cmd = ['tmux', 'copy-mode', '-t', session_name]
        elif action == 'exit':
            cmd = ['tmux', 'send-keys', '-t', session_name, '-X', 'cancel']
        else:
            # Repeat the copy-mode motion `lines` times so one touch gesture can
            # scroll several lines. Clamp so a fling can't send a runaway count.
            try:
                lines = int(data.get('lines') or 1)
            except (TypeError, ValueError):
                lines = 1
            lines = max(1, min(lines, 40))
            cmd = ['tmux', 'send-keys', '-t', session_name,
                   '-X', '-N', str(lines), SCROLL_CMDS[action]]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            self.send_json({
                'error': result.stderr.strip() or 'tmux command failed',
            }, 500)
            return
        self.send_json({'ok': True, 'mode': action})

    def handle_claude_send_key(self):
        """Send a single control key (Shift-Tab, Esc, arrows, Ctrl-C, …) to the
        live tmux session, for mobile clients with no physical keyboard."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body()
        except ValueError:
            self.send_json({'error': 'invalid body'}, 400)
            return
        key = str(data.get('key', '')).strip().lower()
        tmux_key = self._KEYMAP.get(key)
        if not tmux_key:
            self.send_json({'error': 'unsupported key; allowed: ' + ', '.join(sorted(self._KEYMAP))}, 400)
            return
        task = handlers.server.ClaudeTaskManager.get_task(self._claude_task_id)
        if task is None:
            self.send_json({'error': 'Task not found'}, 404)
            return
        session_name = task.get('tmux_session', f'kube-coder-{self._claude_task_id}')

        # A session that has already gone is NOT an error. The web composer's
        # Stop button sends `escape` here, and Stop is pressed while a turn is
        # ending — so the click landing microseconds after the CLI settles is
        # the common race, not an edge case. Reporting it as a failure raises
        # an error toast for a turn that did exactly what the user asked.
        # `delivered` lets a caller tell "key sent" from "nothing left to send
        # it to"; both are successes. handle_hypervisor_stop reasons the same
        # way about idle threads.
        #
        # Every tmux call is bounded: unbounded, a wedged tmux server parks
        # this request-handler thread permanently.
        try:
            live = subprocess.run(
                ['tmux', 'has-session', '-t', session_name],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            self.send_json({'ok': True, 'key': key, 'delivered': False})
            return
        if live.returncode != 0:
            self.send_json({'ok': True, 'key': key, 'delivered': False})
            return

        try:
            result = subprocess.run(
                ['tmux', 'send-keys', '-t', session_name, tmux_key],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as e:
            self.send_json({'error': f'tmux send-keys failed: {e}'}, 502)
            return
        if result.returncode != 0:
            # The session exists but refused the key — a real fault, and an
            # upstream one, so 502 rather than 500.
            self.send_json(
                {'error': result.stderr.strip() or 'tmux send-keys failed'}, 502)
            return
        self.send_json({'ok': True, 'key': key, 'delivered': True})


#: Consulted by do_GET where the first task branch sat (just after the
#: missioncontrol card route), and by do_POST / do_DELETE at the same place
#: their first task branch did. Registration order is match order.
ROUTES = RouteTable()

#: Task ids are `<epoch>-<hex>`; the charset excludes '/', which is what makes
#: every `{id}/<sub>` route unable to collide with the bare `{id}` one.
_TASK_ID = r'([A-Za-z0-9_-]+)'

# --- reads ---------------------------------------------------------------
ROUTES.add('GET', '/api/claude/tasks', 'handle_claude_list_tasks')
ROUTES.add('GET', '/api/claude/auth/token', 'handle_claude_get_token')
ROUTES.add('GET', '/api/claude/assistants', 'handle_claude_list_assistants')
# Sub-resources before the bare {id}. Anchored, so the order is presentational
# — but keep the specific ones first so widening one is caught by reading.
ROUTES.add('GET', re.compile(rf'^/api/claude/tasks/{_TASK_ID}/stream$'),
           'handle_claude_stream_output', sets='_claude_task_id')
ROUTES.add('GET', re.compile(rf'^/api/claude/tasks/{_TASK_ID}/output$'),
           'handle_claude_get_output', sets='_claude_task_id')
# Was one pattern with an optional `(/diff)?` group the handler branched on.
ROUTES.add('GET', re.compile(rf'^/api/claude/tasks/{_TASK_ID}/worktree/diff$'),
           'handle_task_worktree_diff', sets='_claude_task_id')
ROUTES.add('GET', re.compile(rf'^/api/claude/tasks/{_TASK_ID}/worktree$'),
           'handle_task_worktree_status', sets='_claude_task_id')
ROUTES.add('GET', '/api/worktrees', 'handle_worktrees_list')
ROUTES.add('GET', re.compile(rf'^/api/claude/tasks/{_TASK_ID}$'),
           'handle_claude_get_task', sets='_claude_task_id')

# --- creates -------------------------------------------------------------
# `/terminal` is an exact route and there is no bare POST {id}, so it cannot be
# read as a task named "terminal".
ROUTES.add('POST', '/api/claude/tasks', 'handle_claude_create_task')
ROUTES.add('POST', '/api/claude/tasks/terminal',
           'handle_claude_create_terminal_task')
ROUTES.add('POST', '/api/worktrees/sweep', 'handle_worktrees_sweep')
ROUTES.add('POST', '/api/claude/auth/token/regenerate',
           'handle_claude_regenerate_token')

# --- per-task actions ----------------------------------------------------
ROUTES.add('POST', re.compile(rf'^/api/claude/tasks/{_TASK_ID}/message$'),
           'handle_claude_followup', sets='_claude_task_id')
ROUTES.add('POST', re.compile(rf'^/api/claude/tasks/{_TASK_ID}/rename$'),
           'handle_claude_rename_task', sets='_claude_task_id')
ROUTES.add('POST',
           re.compile(rf'^/api/claude/tasks/{_TASK_ID}/redeliver-hook$'),
           'handle_claude_redeliver_hook', sets='_claude_task_id')
ROUTES.add('POST',
           re.compile(rf'^/api/claude/tasks/{_TASK_ID}/prepare-terminal$'),
           'handle_claude_prepare_terminal', sets='_claude_task_id')
ROUTES.add('POST', re.compile(rf'^/api/claude/tasks/{_TASK_ID}/scroll-mode$'),
           'handle_claude_scroll_mode', sets='_claude_task_id')
ROUTES.add('POST', re.compile(rf'^/api/claude/tasks/{_TASK_ID}/key$'),
           'handle_claude_send_key', sets='_claude_task_id')

# --- deletes -------------------------------------------------------------
# `?force=1` rides the query string, and DELETE routes on the un-stripped path,
# so both of these carry strip_query — as /api/files does.
ROUTES.add('DELETE', re.compile(rf'^/api/claude/tasks/{_TASK_ID}/worktree$'),
           'handle_task_worktree_remove', sets='_claude_task_id',
           strip_query=True)
ROUTES.add('DELETE', re.compile(r'^/api/worktrees/([^/?]+)/([^/?]+)$'),
           'handle_worktrees_remove', strip_query=True)
ROUTES.add('DELETE', re.compile(rf'^/api/claude/tasks/{_TASK_ID}$'),
           'handle_claude_delete_task', sets='_claude_task_id')
