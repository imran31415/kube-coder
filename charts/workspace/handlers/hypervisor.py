"""Hypervisor: structured agent-session chat (#100).

Each thread is a HypervisorSession (see hypervisor_session.py): the selected
CLI is run in its machine-readable streaming mode over pipes (no tmux, no TTY)
and normalized into a canonical event stream persisted as events.jsonl. The
frontend renders those events — it never sees a terminal, so there are no
interactive dialogs to answer and no rendered pane to un-scrape. Adding an
assistant means adding one adapter; this facade and the frontend don't change.

Eighteen routes across three verbs. Like tasks (#738), and unlike the domains
lifted before it, the branches were **not contiguous**: on GET the three
collection routes sat above five `/api/gateway/*` branches while
`threads/{id}` and its two sub-resources sat below them; on POST the two exact
routes were in the elif chain and the eight `{id}/…` ones were in the regex
block at the bottom. DELETE's two were already adjacent.

Collapsing that hoists the later routes above everything they used to sit
under. Safe for the same structural reason: every path this table owns starts
with the literal `/api/hypervisor`, and nothing it was hoisted past does.
`tests/hypervisor_routes_table_test.py` lists those paths and asserts it.

## Ordering inside the table

`{id}/activity` and `{id}/watchers` precede the bare `{id}` on GET, and
`{id}/watchers/{wid}` precedes it on DELETE. As everywhere else in this
series, that turns out to be presentational — a thread id is
`[A-Za-z0-9_-]+`, which excludes `/`, and every pattern is `$`-anchored — and
the test proves it by resolving against a reversed copy of the table.

## Everything here reaches server.py through `handlers.server`

Twenty-four names, more than any domain so far, and they are not all managers:
`_HYPERVISOR_AVAILABLE`, `_BOARDS_AVAILABLE` and `_SKILLS_AVAILABLE` are the
guarded-import flags, and `HYPERVISOR_ENABLED` / `HYPERVISOR_WORKDIR` /
`HYPERVISOR_DEFAULT_ASSISTANT` / `READONLY_MODE` are boot configuration the
tests monkeypatch on the server module. Reading them through the bound module
is what keeps a patched value visible, exactly as it was when these bodies
read the globals directly.
"""

import json
import re
import sys
import urllib.parse

import handlers
import runtimes
from handlers.routing import RouteTable


class HypervisorRoutes:
    """BrowserHandler methods backing `ROUTES`."""

    def _hv_session_or_404(self, thread_id):
        s = handlers.server.HypervisorSession.get(thread_id) if handlers.server._HYPERVISOR_AVAILABLE else None
        if s is None:
            self.send_json({'error': 'Thread not found'}, 404)
            return None
        return s

    def handle_hypervisor_config(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        self.send_json({
            'enabled': handlers.server.HYPERVISOR_ENABLED and handlers.server._HYPERVISOR_AVAILABLE,
            # AI CTO gate (#467) — the SPA hides the /cto nav item and the CTO
            # route when this is false. Rides the Hypervisor, so it's never true
            # when the Hypervisor is unavailable.
            'ctoEnabled': handlers.server.cto_available(),
            'defaultAssistant': handlers.server.HYPERVISOR_DEFAULT_ASSISTANT,
            'workdir': handlers.server.HYPERVISOR_WORKDIR,
            'readOnly': handlers.server.READONLY_MODE,
            'assistants': handlers.server.ClaudeTaskManager.available_assistants(),
            'authHelp': {rid: {'label': entry['label'], 'instructions': entry['auth_help']}
                         for rid, entry in runtimes.RUNTIMES.items()},
            # Invocable skills + custom slash commands the composer's `/` picker
            # offers (issue #302). Claude-scoped: the Hypervisor runs Claude at
            # /home/dev and that adapter is the one confirmed to expand `/name`
            # inline in headless print mode — so the frontend shows the picker
            # only for the `claude` assistant. Provider expansion (ante/opencode)
            # is a follow-up: the source can grow a `systems` filter without a
            # client redesign.
            'commands': self._hypervisor_commands(),
            # Whether POST /api/hypervisor/transcribe has a provider key to
            # work with (issue #396) — clients without a browser SpeechRecognition
            # (the mobile app) show the mic only when this is true.
            'stt': handlers.server.SpeechTranscriber.available(),
        })

    @staticmethod
    def _hypervisor_commands():
        """Composer picker source: custom `/commands` + invocable skills.

        Each entry: {name, kind: 'command'|'skill', description,
        argument_hint, scope}. Deduped by name (a custom command shadows a
        same-named skill). Never raises — a discovery hiccup degrades to
        fewer picker entries, never a broken config response."""
        out = []
        seen = set()
        # Custom slash commands (.claude/commands/*.md) — Claude-native, not in
        # the skills registry.
        if handlers.server._SKILLS_AVAILABLE and handlers.server.discover_commands is not None:
            try:
                for c in handlers.server.discover_commands():
                    if c['name'] in seen:
                        continue
                    seen.add(c['name'])
                    out.append({**c, 'kind': 'command'})
            except Exception as e:
                print(f'[hypervisor] command discovery failed: {e}',
                      file=sys.stderr)
        # Invocable skills the registry already tracks, filtered to those Claude
        # can actually run (systems includes 'claude').
        if handlers.server._SKILLS_AVAILABLE and handlers.server.SkillsSyncer is not None:
            try:
                for r in handlers.server.SkillsSyncer.snapshot():
                    if not r.user_invocable or 'claude' not in r.systems:
                        continue
                    if r.name in seen:
                        continue
                    seen.add(r.name)
                    out.append({
                        'name': r.name,
                        'kind': 'skill',
                        'description': r.description,
                        'argument_hint': r.argument_hint,
                        'scope': r.scope,
                    })
            except Exception as e:
                print(f'[hypervisor] skill picker snapshot failed: {e}',
                      file=sys.stderr)
        out.sort(key=lambda c: c['name'])
        return out

    def handle_hypervisor_health(self):
        """Global hypervisor runner health: live turn/subprocess counts and a
        per-thread status + recent-error snapshot. Read-only, auth-gated."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if not handlers.server._HYPERVISOR_AVAILABLE or handlers.server.hv_health is None:
            self.send_json({'error': 'Hypervisor unavailable'}, 503)
            return
        self.send_json(handlers.server.hv_health())

    def handle_hypervisor_list_threads(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        # ?deleted=1 → the "Recently deleted" trash view (soft-deleted only).
        # The query string is stripped from the route match, so re-parse it.
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        only_deleted = (qs.get('deleted') or [''])[0] in ('1', 'true')
        if not handlers.server._HYPERVISOR_AVAILABLE:
            self.send_json({'threads': []})
            return
        threads = handlers.server.HypervisorSession.list(only_deleted=only_deleted)
        # Persona/project filter (#465). No `persona` param → every thread,
        # which is what the dashboard asks for since #683 merged the two lists;
        # `persona=cto` → only CTO threads; `persona=default`/`none` → only
        # plain Hypervisor threads; `project=<id>` → additionally that project.
        # The narrow forms are kept: they are how an API client scopes a query,
        # and dropping them would be a breaking change for no gain.
        persona = (qs.get('persona') or [''])[0].strip().lower()
        project = (qs.get('project') or [''])[0].strip()
        if persona == 'cto':
            threads = [t for t in threads if (t.get('persona') or '') == 'cto']
        elif persona in ('default', 'none', 'hypervisor'):
            threads = [t for t in threads if (t.get('persona') or '') != 'cto']
        if project:
            threads = [t for t in threads if (t.get('project_id') or '') == project]
        self.send_json({'threads': threads})

    def handle_hypervisor_get_thread(self, thread_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        session = self._hv_session_or_404(thread_id)
        if session is None:
            return
        qs = self.path.split('?', 1)[1] if '?' in self.path else ''
        try:
            since = int((urllib.parse.parse_qs(qs).get('since') or ['0'])[0])
        except (TypeError, ValueError):
            since = 0
        # Prefer Claude Code's own JSONL session log (structured, complete,
        # restart-proof) for the transcript; fall back to the live events.jsonl
        # capture when it's unavailable. `source` tells the client which won.
        tx = session.transcript(since_seq=since)
        self.send_json({
            'thread': session.summary(),
            'events': tx['events'],
            'source': tx['source'],
        })

    def handle_hypervisor_get_activity(self, thread_id):
        """Per-thread observability view: a normalized activity timeline (tool
        calls + results + durations, errors, status transitions) derived from
        events.jsonl, plus a bounded tail of the runner.log (subprocess stderr +
        runner diagnostics). Read-only; behind the same auth gate as the thread
        endpoint."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        session = self._hv_session_or_404(thread_id)
        if session is None:
            return
        if handlers.server.hv_build_activity is None:
            self.send_json({'error': 'Hypervisor unavailable'}, 503)
            return
        activity = handlers.server.hv_build_activity(session.read_events())
        activity['thread'] = session.summary()
        activity['runner_log'] = session.read_runner_log()
        self.send_json(activity)

    def handle_hypervisor_create_thread(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if not (handlers.server.HYPERVISOR_ENABLED and handlers.server._HYPERVISOR_AVAILABLE):
            self.send_json({'error': 'Hypervisor is disabled'}, 404)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        message = (data.get('message') or '').strip()
        # AI CTO persona (#465): a 'cto' thread swaps in CTO_PREAMBLE + the
        # project's markdown brief (injected on turn 1 via the adapter's
        # preamble path) and binds a project_id. Any other/absent persona is a
        # normal Hypervisor thread — zero change from before.
        persona = (data.get('persona') or '').strip().lower()
        # Board Processor personas (#588/#589): 'board' works ONE item on an
        # external board, 'board-gen' authors a connector. Both are honored only
        # while the boards package is importable; otherwise the thread degrades
        # to a plain chat rather than starting with a preamble describing tools
        # that aren't there.
        if persona in ('board', 'board-gen'):
            if not handlers.server._BOARDS_AVAILABLE:
                persona = ''
        # A CTO persona is honored only when the feature is enabled (#467);
        # otherwise the thread degrades to a plain Hypervisor chat.
        elif persona != 'cto' or not handlers.server.cto_available():
            persona = ''
        # A board thread binds to a board (and usually one item), the way a CTO
        # thread binds to a project. An unknown board drops the binding so we
        # never export a KC_BOARD_ID whose tools would 404 every call.
        board_id = (data.get('board_id') or '').strip()
        board_item_id = str(data.get('board_item_id') or '').strip()
        if board_id and (not handlers.server._BOARDS_AVAILABLE
                         or not handlers.server.BoardsManager.valid_id(board_id)
                         or handlers.server.BoardsManager.get(board_id) is None):
            board_id, board_item_id = '', ''
        if not board_id:
            board_item_id = ''
        # A project binding is no longer CTO-only (#358): an ordinary chat can be
        # filed into a project too, which is what makes the chat list groupable
        # and gives the turn a project memory namespace to work in.
        project_id = (data.get('project_id') or '').strip()
        # Drop an unknown/invalid binding so we never export a KC_PROJECT_ID that
        # 404s every project tool — the thread just becomes a Workspace-scope
        # chat (#465, review L5).
        if project_id and (not handlers.server.ProjectsManager.valid_id(project_id)
                           or handlers.server.ProjectsManager.get_project(project_id) is None):
            project_id = ''
        # Per-project assistant configuration (#483). A CTO thread whose body
        # omits assistant/model/effort inherits the bound project's defaults,
        # then the workspace default — so every client is correct, including the
        # MCP and cron paths that never send the fields. An explicit body value
        # always wins, and everything still runs through resolve_* so a stale or
        # disabled project default degrades gracefully instead of launching a
        # dead provider. Resolved here (after the project binding) rather than at
        # the top of the handler because the defaults hang off that project.
        # Deliberately still CTO-only: a project's assistant defaults are the
        # ones its CTO threads and dispatched builds run on, and the Chat tab
        # always sends its own explicit picker values — so a plain chat filed
        # into a project (#358) keeps whatever agent the user chose for it.
        p_assistant, p_model, p_effort = handlers.server.ProjectsManager.defaults_for(
            project_id if persona == 'cto' else '')
        # Same #702 rule as the build path: a chat the caller explicitly asked
        # to run on a listed-but-unauthenticated agent is refused with the key
        # named, rather than opening a thread whose every turn fails with
        # "Authentication Fails". Only the caller's own choice is rejected — a
        # stale PROJECT default still degrades through resolve_assistant, since
        # nobody chose it for this turn.
        not_ready = handlers.server.ClaudeTaskManager.assistant_not_ready_error(
            data.get('assistant'))
        if not_ready:
            self.send_json({'error': not_ready}, 400)
            return
        assistant = handlers.server.ClaudeTaskManager.resolve_assistant(
            data.get('assistant') or p_assistant or handlers.server.HYPERVISOR_DEFAULT_ASSISTANT)
        # Per-thread model choice (#308) — validated against the assistant's
        # allow-list; '' when the assistant offers no choice (adapter default).
        model = handlers.server.ClaudeTaskManager.resolve_model(
            assistant, data.get('model') or p_model)
        # Per-thread reasoning effort (#362) — validated against the canonical
        # 5-stop axis; '' when the assistant has no effort knob (selector hidden).
        effort = handlers.server.ClaudeTaskManager.resolve_effort(
            assistant, data.get('effort') or p_effort)
        preamble = handlers.server.HYPERVISOR_PREAMBLE
        if persona == 'cto':
            preamble = handlers.server.CTO_PREAMBLE
            brief = handlers.server.ProjectsManager.brief(project_id) if project_id else None
            if brief and brief.get('brief_markdown'):
                preamble = (
                    handlers.server.CTO_PREAMBLE
                    + f"[System: Project brief for `{project_id}` — a "
                    "point-in-time snapshot; call get_project_brief for live "
                    "state.]\n\n" + brief['brief_markdown'] + "\n\n")
            # First-win fast-path (#486): the user's first-ever CTO thread that
            # opens with a sentence should build immediately, without the
            # ```choice gate. Append the addendum LAST so it overrides the
            # DISPATCH rule for the opening turn. An empty opener (thread opened
            # with no message) is not a build request, so it stays gated.
            if message and handlers.server._is_first_cto_thread():
                preamble = preamble + handlers.server.CTO_FIRST_WIN_ADDENDUM
        elif persona == 'board':
            preamble = handlers.server.BOARD_PREAMBLE
        elif persona == 'board-gen':
            preamble = handlers.server.BOARD_GEN_PREAMBLE
        # A CTO thread defaults its workdir to the project's first workdir (so
        # relative paths + dispatched tasks land in the project) when the client
        # didn't pin one; otherwise the usual hypervisor workdir.
        workdir = data.get('workdir') or handlers.server.HYPERVISOR_WORKDIR
        if persona == 'cto' and not data.get('workdir') and project_id:
            proj = handlers.server.ProjectsManager.get_project(project_id)
            if proj and proj.get('workdirs'):
                workdir = proj['workdirs'][0]
        # cli_cmd is only consumed by the non-structured fallback adapter; the
        # Claude adapter builds its own argv. auto_approve keeps any fallback
        # CLI from blocking on an approval it can't answer.
        cli_cmd = handlers.server.ClaudeTaskManager.assistant_command(assistant, auto_approve=True)
        try:
            session = handlers.server.HypervisorSession.create(
                assistant=assistant, workdir=workdir, cli_cmd=cli_cmd,
                preamble=preamble, title=message, model=model, effort=effort,
                persona=persona, project_id=project_id)
        except Exception as e:
            self.send_json({'error': f'failed to start chat: {e}'}, 500)
            return
        # Bind AFTER create so the binding rides thread meta (and therefore the
        # turn env) exactly like set_project's, rather than becoming another
        # constructor argument every caller has to know about.
        if board_id:
            session.set_board(board_id, board_item_id)
        if message:
            session.send(message)
        self.send_json({'thread': session.summary()}, 201)

    def handle_hypervisor_send_message(self, thread_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        session = self._hv_session_or_404(thread_id)
        if session is None:
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        message = (data.get('message') or '').strip()
        if not message:
            self.send_json({'error': 'message is required'}, 400)
            return
        if session.status() == 'running':
            self.send_json({'error': 'assistant is still responding'}, 409)
            return
        session.send(message)
        self.send_json({'ok': True})

    def handle_hypervisor_transcribe(self):
        """POST /api/hypervisor/transcribe (issue #396, tier 1) — raw audio in
        the body, transcript out. Serves clients without a browser SpeechRecognition
        (the Expo mobile app); the transcript feeds the ordinary send path
        client-side, so this endpoint never touches a thread itself."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            content_length = int(self.headers.get('Content-Length', 0) or 0)
        except ValueError:
            self.send_json({'error': 'invalid Content-Length'}, 400)
            return
        if content_length <= 0:
            self.send_json({'error': 'empty audio body'}, 400)
            return
        if content_length > handlers.server.SpeechTranscriber.MAX_AUDIO_BYTES:
            self.send_json({'error': 'audio too large '
                            f'(max {handlers.server.SpeechTranscriber.MAX_AUDIO_BYTES} bytes)'}, 413)
            return
        audio = self.rfile.read(content_length)
        ctype = (self.headers.get('Content-Type')
                 or 'application/octet-stream').split(';')[0].strip()
        # URL-encoded like the file-upload headers (header values are ISO-8859-1).
        filename = urllib.parse.unquote(
            (self.headers.get('X-Filename') or '').strip()) or None
        text, err = handlers.server.SpeechTranscriber.transcribe(audio, ctype, filename)
        if err is not None:
            self.send_json({'error': err[1]}, err[0])
            return
        self.send_json({'text': text})

    def handle_hypervisor_stop(self, thread_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        session = self._hv_session_or_404(thread_id)
        if session is None:
            return
        stopped = session.stop()
        # Idle threads are a safe no-op — report 'idle' rather than erroring so
        # the client can fire-and-forget without racing the turn's completion.
        self.send_json({'ok': True, 'stopped': stopped})

    def handle_hypervisor_rename_thread(self, thread_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        session = self._hv_session_or_404(thread_id)
        if session is None:
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        title = (data.get('title') or '').strip()
        if not title:
            self.send_json({'error': 'title is required'}, 400)
            return
        summary = session.set_title(title)
        if summary is None:
            self.send_json({'error': 'not found'}, 404)
            return
        self.send_json({'thread': summary})

    def handle_hypervisor_set_model(self, thread_id):
        """Switch a live thread's model (#308). Takes effect on the next turn —
        the Claude adapter reads ctx['model'] each build, and `--resume` carries
        the same session across the change. Validated against the thread's own
        assistant so a client can't smuggle in an off-list model."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        session = self._hv_session_or_404(thread_id)
        if session is None:
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        meta = session.read_meta() or {}
        model = handlers.server.ClaudeTaskManager.resolve_model(
            meta.get('assistant') or '', data.get('model'))
        summary = session.set_model(model)
        if summary is None:
            self.send_json({'error': 'not found'}, 404)
            return
        self.send_json({'thread': summary})

    def handle_hypervisor_set_effort(self, thread_id):
        """Switch a live thread's reasoning effort (#362). Takes effect on the
        next turn — each adapter reads ctx['effort'] fresh at build and maps it
        to its CLI's native knob. Validated against the thread's own assistant
        (canonical 5-stop axis); an assistant with no effort knob resolves to ''
        and the field is simply cleared."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        session = self._hv_session_or_404(thread_id)
        if session is None:
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        meta = session.read_meta() or {}
        effort = handlers.server.ClaudeTaskManager.resolve_effort(
            meta.get('assistant') or '', data.get('effort'))
        summary = session.set_effort(effort)
        if summary is None:
            self.send_json({'error': 'not found'}, 404)
            return
        self.send_json({'thread': summary})

    def handle_hypervisor_set_project(self, thread_id):
        """File a chat into a project, or clear the binding with '' (#358).

        Takes effect on the next turn: _run_turn re-reads the thread meta each
        turn, so the new project rides KC_PROJECT_ID (and with it the project's
        memory namespace scope, #359) from then on. An unknown project id is a
        400 rather than a silent drop — this is an explicit user action, and
        pretending it worked would leave the chat filed nowhere. A CTO thread is
        refused: its project brief is baked into the preamble at creation, so
        re-binding one would leave the two disagreeing (#465)."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        session = self._hv_session_or_404(thread_id)
        if session is None:
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        meta = session.read_meta() or {}
        if (meta.get('persona') or '') == 'cto':
            self.send_json(
                {'error': "a CTO chat's project is fixed at creation"}, 400)
            return
        project_id = (data.get('project_id') or '').strip()
        if project_id and (not handlers.server.ProjectsManager.valid_id(project_id)
                           or handlers.server.ProjectsManager.get_project(project_id) is None):
            self.send_json({'error': f'unknown project: {project_id}'}, 400)
            return
        summary = session.set_project(project_id)
        if summary is None:
            self.send_json({'error': 'not found'}, 404)
            return
        self.send_json({'thread': summary})

    # ── Cross-turn watchers (issue #402) ──────────────────────────────────
    # The runner-owned watch primitive: an in-turn agent arms a watcher here
    # (via the dashboard MCP `watch` tool); hypervisor_session.WATCHERS polls
    # the condition after the turn ends and injects the outcome back into the
    # thread as a follow-up turn. These endpoints are thin wrappers over it.
    def handle_hypervisor_list_watchers(self, thread_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        session = self._hv_session_or_404(thread_id)
        if session is None:
            return
        if handlers.server.hv_watchers is None:
            self.send_json({'error': 'Hypervisor unavailable'}, 503)
            return
        self.send_json({'watchers': handlers.server.hv_watchers.list(thread_id)})

    def handle_hypervisor_create_watcher(self, thread_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        session = self._hv_session_or_404(thread_id)
        if session is None:
            return
        if handlers.server.hv_watchers is None:
            self.send_json({'error': 'Hypervisor unavailable'}, 503)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        try:
            watcher = handlers.server.hv_watchers.arm(
                thread_id,
                kind=data.get('kind') or '',
                target=data.get('target') or '',
                note=data.get('note') or '',
                interval=data.get('interval'),
                timeout=data.get('timeout'))
        except ValueError as e:
            self.send_json({'error': str(e)}, 400)
            return
        self.send_json({'watcher': watcher}, 201)

    def handle_hypervisor_cancel_watcher(self, thread_id, watcher_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        session = self._hv_session_or_404(thread_id)
        if session is None:
            return
        if handlers.server.hv_watchers is None:
            self.send_json({'error': 'Hypervisor unavailable'}, 503)
            return
        cancelled = handlers.server.hv_watchers.cancel(thread_id, watcher_id)
        # Cancelling an already-finished/unknown watcher is a reported no-op,
        # mirroring stop()'s fire-and-forget shape.
        self.send_json({'ok': True, 'cancelled': cancelled})

    def handle_hypervisor_delete_thread(self, thread_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        session = self._hv_session_or_404(thread_id)
        if session is None:
            return
        # Soft-delete: the thread drops out of the default listing but its files
        # survive so it can be restored from "Recently deleted".
        session.delete()
        self.send_json({'ok': True})

    def handle_hypervisor_restore_thread(self, thread_id):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        session = self._hv_session_or_404(thread_id)
        if session is None:
            return
        revived = session.revive()
        self.send_json({'ok': True, 'restored': revived})


#: Consulted by do_GET / do_POST / do_DELETE where each verb's first
#: hypervisor branch sat. Registration order is match order.
ROUTES = RouteTable()

#: Thread ids exclude '/', which is what makes every `{id}/<sub>` route unable
#: to collide with the bare `{id}` one.
_THREAD = r'([A-Za-z0-9_-]+)'
_THREADS = r'^/api/hypervisor/threads/' + _THREAD

# --- reads ---------------------------------------------------------------
ROUTES.add('GET', '/api/hypervisor/config', 'handle_hypervisor_config')
ROUTES.add('GET', '/api/hypervisor/threads', 'handle_hypervisor_list_threads')
ROUTES.add('GET', '/api/hypervisor/health', 'handle_hypervisor_health')
# Sub-resources before the bare {id}. Anchored, so presentational — but keep
# the specific ones first so widening one is caught by reading.
ROUTES.add('GET', re.compile(_THREADS + r'/activity$'),
           'handle_hypervisor_get_activity')
ROUTES.add('GET', re.compile(_THREADS + r'/watchers$'),
           'handle_hypervisor_list_watchers')
ROUTES.add('GET', re.compile(_THREADS + r'$'), 'handle_hypervisor_get_thread')

# --- creates -------------------------------------------------------------
ROUTES.add('POST', '/api/hypervisor/threads', 'handle_hypervisor_create_thread')
# Voice interface (#396): server-side speech-to-text.
ROUTES.add('POST', '/api/hypervisor/transcribe', 'handle_hypervisor_transcribe')

# --- per-thread actions --------------------------------------------------
ROUTES.add('POST', re.compile(_THREADS + r'/messages$'),
           'handle_hypervisor_send_message')
ROUTES.add('POST', re.compile(_THREADS + r'/stop$'), 'handle_hypervisor_stop')
# Undo a soft-delete.
ROUTES.add('POST', re.compile(_THREADS + r'/restore$'),
           'handle_hypervisor_restore_thread')
ROUTES.add('POST', re.compile(_THREADS + r'/watchers$'),
           'handle_hypervisor_create_watcher')
ROUTES.add('POST', re.compile(_THREADS + r'/rename$'),
           'handle_hypervisor_rename_thread')
ROUTES.add('POST', re.compile(_THREADS + r'/model$'),
           'handle_hypervisor_set_model')
ROUTES.add('POST', re.compile(_THREADS + r'/effort$'),
           'handle_hypervisor_set_effort')
# File the chat into a project, or clear the binding (#358).
ROUTES.add('POST', re.compile(_THREADS + r'/project$'),
           'handle_hypervisor_set_project')

# --- deletes -------------------------------------------------------------
# Cancel one watcher before the soft-delete of the whole thread.
ROUTES.add('DELETE', re.compile(_THREADS + r'/watchers/([A-Za-z0-9_-]+)$'),
           'handle_hypervisor_cancel_watcher')
ROUTES.add('DELETE', re.compile(_THREADS + r'$'),
           'handle_hypervisor_delete_thread')
