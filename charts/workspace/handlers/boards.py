"""Board Processor: connectors, runs, review and strategies (#100, #588/#589).

The largest domain in the series — 28 routes across four verbs and 32 handler
methods — and the first to put anything in `do_PUT`'s table.

Unlike tasks and hypervisor, every one of its branches was already contiguous
on every verb, so this is a straight lift: the order below is the order the
chains had, section comments and all.

## The one ordering hazard that is load-bearing

`credentials` and `templates` are both legal spellings of `([a-zA-Z0-9_-]+)`,
so on GET the two exact routes must stay ahead of `/api/boards/{id}` or they
would be read as boards with those ids. They are reserved for exactly that
reason — `schema.RESERVED_BOARD_IDS` — and the test asserts both the
resolution and the reservation.

Everything else the chains flagged turns out to be presentational, and the
tests say so rather than leaving the comments to imply otherwise:

* `PUT`/`DELETE` `credentials/{NAME}` ahead of `{id}` — those patterns differ
  in segment count and both are `$`-anchored, so they cannot collide.
* POST `strategies/preview` ahead of `strategies` — the save route is
  `…/strategies$`, which an extra `/preview` segment cannot match. The source
  comment said preview "would otherwise be read as a strategy named
  preview"; that is true of the DELETE route (`strategies/(.+)$`) but not of
  this one. Order preserved as found, claim corrected.

One genuine ambiguity IS preserved: `GET /api/boards/templates/strategies`
resolves to `handle_board_template_get('strategies')`, because the template
route is registered before `{id}/strategies`. That is pre-existing behaviour
and `templates` is reserved, so no real board can be shadowed.

## Three routes take an adapter

The chain did work at the dispatch site that the table cannot express as a
column: percent-decoding an item id out of the URL, and turning the
`send-back` spelling in the path into the `send_back` the handler expects.
Those three get a thin `route_*` wrapper here, the same way memory's
`neighbors` and `unlink` routes did — the handler bodies stay verbatim.
"""

import json
import re
import urllib.parse

import handlers
import safe_http
from handlers.routing import RouteTable


class BoardRoutes:
    """BrowserHandler methods backing `ROUTES`."""

    def _board_guard(self):
        """Shared entry gate for every /api/boards route. Returns False (and
        has already answered) when the request must not proceed."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return False
        if not handlers.server._BOARDS_AVAILABLE:
            self.send_json({'error': 'Board Processor is unavailable on this '
                                     'workspace'}, 503)
            return False
        return True

    def _board_or_404(self, board_id):
        cfg = handlers.server.BoardsManager.get(board_id)
        if cfg is None:
            self.send_json({'error': 'Board not found'}, 404)
            return None
        return cfg

    def handle_boards_list(self):
        if not self._board_guard():
            return
        self.send_json({'boards': handlers.server.BoardsManager.list_boards()})

    def handle_board_credentials_list(self):
        """Every stored board credential, WITHOUT values. There is deliberately
        no route that reads one back: `get_raw` is reachable only from
        `BoardsManager._credential_for`."""
        if not self._board_guard():
            return
        self.send_json({'credentials': handlers.server.BoardCredentialsManager.public_view()})

    def handle_board_credential_put(self):
        if not self._board_guard():
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        ok, err = handlers.server.BoardCredentialsManager.set(
            self._board_cred_name,
            data.get('secret'),
            fmt=data.get('format') or 'token',
            username=data.get('username') or '')
        if not ok:
            self.send_json({'error': err}, 400)
            return
        # The event carries the NAME only. Boards whose credential_ref points at
        # it may have just gone from "needs key" to usable, so the UI refreshes.
        handlers.server.EventBroker.publish('boards.changed', {
            'op': 'credential', 'name': self._board_cred_name})
        self.send_json({'credentials': handlers.server.BoardCredentialsManager.public_view()})

    def handle_board_credential_delete(self):
        if not self._board_guard():
            return
        if not handlers.server.BoardCredentialsManager.delete(self._board_cred_name):
            self.send_json({'error': 'Credential not found'}, 404)
            return
        handlers.server.EventBroker.publish('boards.changed', {
            'op': 'credential', 'name': self._board_cred_name})
        self.send_json({'ok': True})

    def handle_board_get(self):
        if not self._board_guard():
            return
        cfg = self._board_or_404(self._board_id)
        if cfg is None:
            return
        view = handlers.server.boards.schema.public_view(cfg)
        view['credential'] = handlers.server.BoardsManager.credential_status(cfg)
        view['actions_allowed'] = handlers.server.boards.schema.action_names(cfg)
        self.send_json(view)

    def handle_board_create(self):
        if not self._board_guard():
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        board_id = (data.get('id') or '').strip()
        if handlers.server.BoardsManager.valid_id(board_id) and handlers.server.BoardsManager.get(board_id):
            self.send_json({'error': f'board {board_id!r} already exists'}, 409)
            return
        cfg, err = handlers.server.BoardsManager.create_or_update(data)
        if err:
            self.send_json({'error': err}, 400)
            return
        handlers.server.EventBroker.publish('boards.changed', {'op': 'create', 'id': cfg['id']})
        self.send_json(handlers.server.boards.schema.public_view(cfg), 201)

    def handle_board_update(self):
        if not self._board_guard():
            return
        if self._board_or_404(self._board_id) is None:
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        cfg, err = handlers.server.BoardsManager.create_or_update(data, existing_id=self._board_id)
        if err:
            self.send_json({'error': err}, 400)
            return
        handlers.server.EventBroker.publish('boards.changed', {'op': 'update', 'id': cfg['id']})
        self.send_json(handlers.server.boards.schema.public_view(cfg))

    def handle_board_delete(self):
        if not self._board_guard():
            return
        if not handlers.server.BoardsManager.delete(self._board_id):
            self.send_json({'error': 'Board not found'}, 404)
            return
        handlers.server.EventBroker.publish('boards.changed', {'op': 'delete', 'id': self._board_id})
        self.send_json({'ok': True})

    def handle_board_test_fetch(self):
        """The verification oracle.

        Runs the connector through the SAME deterministic engine production
        uses, so a connector that passes here has demonstrably worked rather
        than been asserted to work. Returns normalized items BESIDE their raw
        vendor objects, because the agent authoring the connector has to be
        able to see what it got wrong.
        """
        if not self._board_guard():
            return
        cfg = self._board_or_404(self._board_id)
        if cfg is None:
            return
        try:
            body = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            body = {}
        max_pages = body.get('max_pages')
        if not isinstance(max_pages, int) or not (
                1 <= max_pages <= handlers.server.BoardsManager.TEST_FETCH_MAX_PAGES):
            max_pages = handlers.server.BoardsManager.TEST_FETCH_MAX_PAGES

        result, err = handlers.server.BoardsManager.fetch(cfg, max_pages=max_pages)
        if err:
            self.send_json({'error': err}, 502)
            return
        limit = 25
        self.send_json({
            'complete': result['complete'],
            'truncation_reason': result['truncation_reason'],
            'pages_fetched': result['pages_fetched'],
            'raw_count': result['raw_count'],
            'map_errors': result.get('map_errors', []),
            'items': result['items'][:limit],
            'truncated_for_display': result['raw_count'] > limit,
        })

    def handle_board_items(self):
        if not self._board_guard():
            return
        cfg = self._board_or_404(self._board_id)
        if cfg is None:
            return
        result, err = handlers.server.BoardsManager.fetch(cfg)
        if err:
            self.send_json({'error': err}, 502)
            return
        self.send_json({
            'items': result['items'],
            'complete': result['complete'],
            'truncation_reason': result['truncation_reason'],
            'pages_fetched': result['pages_fetched'],
        })

    def handle_board_draft(self):
        """Validate (and optionally dry-run) a connector WITHOUT persisting it.

        This is what the generation agent iterates against: it drafts adapter
        JSON, posts it here, and gets either the full list of schema errors or
        a real fetch through the production engine.
        """
        if not self._board_guard():
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return

        connector = data.get('connector')
        cleaned, errors = handlers.server.boards.schema.validate_connector(
            connector if isinstance(connector, dict) else {})
        if errors:
            self.send_json({'valid': False, 'errors': errors})
            return

        out = {'valid': True, 'errors': [],
               'actions_allowed': handlers.server.boards.schema.action_names(cleaned)}
        # Probing is the DEFAULT: this endpoint exists so a draft can be proven
        # against the live board through the production engine, and a caller
        # that only wants a schema check opts out with probe=false. Reading is
        # the only thing a probe ever does — no action is ever executed here.
        if data.get('probe') is False:
            self.send_json(out)
            return

        # A draft has no id yet; give it one so the engine's marker/limit keys
        # are well-formed. Nothing is written to disk.
        cleaned['id'] = (data.get('id') or 'draft').strip() or 'draft'
        allow_internal = cleaned.get('allow_internal') or handlers.server.ALLOW_INTERNAL_HOOKS
        if not safe_http.is_safe_url(cleaned['base_url'],
                                     allow_internal=allow_internal):
            out.update({'probed': False,
                        'error': f'base_url {cleaned["base_url"]!r} is '
                                 f'unreachable or resolves to a non-public address'})
            self.send_json(out)
            return

        result, err = handlers.server.BoardsManager.fetch(
            cleaned, max_pages=handlers.server.BoardsManager.TEST_FETCH_MAX_PAGES)
        if err:
            out.update({'probed': False, 'error': err})
            self.send_json(out)
            return
        out.update({
            'probed': True,
            'complete': result['complete'],
            'truncation_reason': result['truncation_reason'],
            'pages_fetched': result['pages_fetched'],
            'raw_count': result['raw_count'],
            'map_errors': result.get('map_errors', []),
            'items': result['items'][:10],
        })
        self.send_json(out)

    def handle_board_action(self):
        """Execute one allow-listed action against one item.

        READONLY_MODE already blocked this at do_POST. What this handler adds
        is the allowlist check and the connector's declared rate limits — an
        agent names an action, it never constructs a request.
        """
        if not self._board_guard():
            return
        cfg = self._board_or_404(self._board_id)
        if cfg is None:
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return

        action_name = (data.get('action') or '').strip()
        params = data.get('params') or {}
        if not isinstance(params, dict):
            self.send_json({'error': 'params must be an object'}, 400)
            return

        # The item is ALWAYS re-fetched from the board, and any item the caller
        # sent is ignored outright. The action URLs interpolate `item.ref`, so
        # trusting a caller-supplied item would let an agent name item 46 and
        # hand us a ref pointing at someone else's ticket — naming one target
        # and writing to another. The id in the URL is the only thing the caller
        # gets to choose.
        result, err = handlers.server.BoardsManager.fetch(cfg)
        if err:
            self.send_json({'error': err}, 502)
            return
        wanted = str(self._board_item_id)
        item = next((i for i in result['items'] if str(i['id']) == wanted), None)
        if item is None:
            detail = f'item {wanted!r} not found on this board'
            if not result['complete']:
                # Saying "not found" about a truncated listing would be a lie.
                detail += (f' (the listing was INCOMPLETE — '
                           f'{result["truncation_reason"]} — so the item may '
                           f'exist beyond what we could read)')
            self.send_json({'error': detail}, 404)
            return

        # PROPOSE MODE (#588 Phase 5). If a live propose-mode run holds this
        # item, the write is STAGED rather than sent. The decision reads the
        # LEASE — server state no agent can reach — so an agent cannot opt out
        # of review by omitting or forging a field in the request body.
        staging_run = handlers.server.BoardReviewManager.staging_run_for(cfg['id'], item['id'])
        if staging_run is not None:
            record, err = handlers.server.BoardReviewManager.stage(
                cfg, item, action_name, params,
                run_id=staging_run['id'],
                task_id=(staging_run.get('items', {})
                         .get(str(item['id']), {}).get('task_id', '')),
                preview=(data.get('preview') or ''))
            if err:
                self.send_json({'error': err}, 400)
                return
            self.send_json({
                'ok': True, 'staged': True, 'action': action_name,
                'detail': 'held for human approval — nothing was written to '
                          'the board yet',
                'record': handlers.server.boards.review.public_view(record),
            }, 202)
            return

        limiter = handlers.server.BoardsManager.limiter_for(cfg)
        result, err = handlers.server.BoardsManager.run_action(
            cfg, item, action_name, params, limiter=limiter)
        if err:
            status = 429 if err.startswith('rate limited') else 400
            self.send_json({'error': err}, status)
            return
        handlers.server.EventBroker.publish('boards.changed', {
            'op': 'action', 'id': cfg['id'], 'item': item.get('id'),
            'action': action_name, 'ok': result.get('ok')})
        self.send_json(result, 200 if result.get('ok') else 502)

    # ── review (#588 Phase 5) ──────────────────────────────────────────────

    def _review_item(self, cfg):
        """Re-fetch the item named in the URL. Returns the item or None (and
        has already answered). Same discipline as the action route: the caller
        chooses an id, never an item."""
        result, err = handlers.server.BoardsManager.fetch(cfg)
        if err:
            self.send_json({'error': err}, 502)
            return None
        wanted = str(self._board_item_id)
        item = next((i for i in result['items'] if str(i['id']) == wanted), None)
        if item is None:
            detail = f'item {wanted!r} not found on this board'
            if not result['complete']:
                detail += (f' (the listing was INCOMPLETE — '
                           f'{result["truncation_reason"]} — so the item may '
                           f'exist beyond what we could read)')
            self.send_json({'error': detail}, 404)
            return None
        return item

    def _review_error(self, err):
        """Map a ReviewError to a status. `stale` and `hash_mismatch` are 409
        because they are conflicts a reload resolves; the rest are 400."""
        status = {'stale': 409, 'hash_mismatch': 409, 'already_decided': 409,
                  'conflict': 409, 'not_found': 404, 'item_gone': 404,
                  'fetch_failed': 502}.get(err.code, 400)
        # `note_required` is a 400: the body is wrong, and a client that omits
        # the note should fix the request rather than reload and retry.
        self.send_json({'error': err.detail, 'code': err.code}, status)

    def handle_board_review_list(self):
        if not self._board_guard():
            return
        if self._board_or_404(self._board_id) is None:
            return
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        open_only = (qs.get('open') or [''])[0] in ('1', 'true')
        records = handlers.server.BoardReviewManager.list_records(self._board_id,
                                                  open_only=open_only)
        # What each item's Build changed (#701), so a reviewer sees the code
        # next to the proposed reply. From task.json only — this list is
        # polled, and it never runs git.
        briefs = {}
        for rec in records:
            tid = rec.get('task_id') or ''
            if not tid:
                continue
            if tid not in briefs:
                meta = handlers.server.ClaudeTaskManager.read_meta(tid)
                briefs[tid] = (dict(handlers.server.WorktreeManager.brief(meta), task_id=tid)
                               if meta and meta.get('worktree') else None)
            if briefs[tid]:
                rec['worktree'] = briefs[tid]
        self.send_json({
            'groups': handlers.server.boards.review.group_by_disposition(records),
            'total': len(records),
            'open': sum(1 for r in records if r.get('open')),
        })

    def handle_board_disposition(self):
        """An agent reporting what happened. Not a human decision."""
        if not self._board_guard():
            return
        cfg = self._board_or_404(self._board_id)
        if cfg is None:
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        item = self._review_item(cfg)
        if item is None:
            return
        # Mode-agnostic on purpose: an autonomous run's items must record their
        # disposition too, and staging_run_for answers only for propose runs.
        run = handlers.server.BoardRunsManager.run_holding(cfg['id'], item['id'])
        record, err = handlers.server.BoardReviewManager.report(
            cfg, item, (data.get('disposition') or '').strip(),
            reason=data.get('reason') or '',
            evidence=data.get('evidence') if isinstance(
                data.get('evidence'), dict) else {},
            run_id=(run or {}).get('id', ''),
            # Which Build reported (#701): a report-only record carried no
            # task id, so its review card could not link to the Build's
            # changes and a send-back could not find where it ran.
            task_id=(((run or {}).get('items') or {}).get(str(item['id']))
                     or {}).get('task_id', ''))
        if err:
            self.send_json({'error': err}, 400)
            return
        self.send_json(handlers.server.boards.review.public_view(record))

    def handle_board_review_decide(self, decision):
        if not self._board_guard():
            return
        cfg = self._board_or_404(self._board_id)
        if cfg is None:
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return

        approval_id = str(data.get('approval_id') or '')
        # Who approved a customer-visible reply is worth recording. Same
        # derivation the memory store uses, and it never trusts an upstream
        # header the Bearer ingress could have supplied (see _memory_actor).
        actor = self._memory_actor()

        if decision == 'approve':
            result, err = handlers.server.BoardReviewManager.approve(
                cfg, self._board_item_id,
                content_hash=str(data.get('content_hash') or ''),
                approval_id=approval_id, actor=actor)
        elif decision == 'edit':
            result, err = handlers.server.BoardReviewManager.edit(
                cfg, self._board_item_id, str(data.get('action_id') or ''),
                data.get('params') if isinstance(data.get('params'), dict) else {})
        else:
            # `note` is what the send-back dialog calls it and what it IS — an
            # instruction to the agent, not a reason for the record. `reason`
            # stays accepted so an existing client (and reject, where the field
            # really is a reason) keeps working.
            result, err = handlers.server.BoardReviewManager.decide(
                cfg, self._board_item_id,
                state='rejected' if decision == 'reject' else 'sent_back',
                approval_id=approval_id,
                reason=data.get('note') or data.get('reason') or '',
                actor=actor)
        if err is not None:
            self._review_error(err)
            return
        self.send_json(result)

    # ── templates, strategies, metrics (#588 Phase 7) ──────────────────────

    def handle_board_templates(self):
        if not self._board_guard():
            return
        self.send_json({'templates': handlers.server.boards.templates.listing()})

    def handle_board_template_get(self, template_id):
        if not self._board_guard():
            return
        cfg = handlers.server.boards.templates.get(template_id)
        if cfg is None:
            self.send_json({'error': f'no template {template_id!r}'}, 404)
            return
        # `verified: False` travels WITH the config, not just in the listing.
        # A client that fetched this straight into an editor must still be able
        # to tell the user it has demonstrated nothing yet.
        self.send_json({
            'id': template_id, 'connector': cfg,
            'needs': handlers.server.boards.templates.NEEDS.get(template_id, []),
            'verified': False,
            'note': 'a starting point, not a verified connector — fill in the '
                    'placeholders and run test-fetch before trusting it',
        })

    def handle_board_template_fill(self, template_id):
        """Template + the operator's answers → a connector, unsaved.

        Substitution happens HERE rather than in the browser for one reason
        worth stating: the placeholders sit inside URL paths and, for Jira,
        inside a JQL string, so a value carrying a space or a slash silently
        becomes a different query against a different board. `fill` restricts
        what a value may contain, and a client cannot opt out of a check it
        does not perform.

        Returns the connector for the caller to POST to /api/boards. Kept
        separate from create so what gets saved is still the same validated
        payload any other author would send.
        """
        if not self._board_guard():
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        cfg, err = handlers.server.boards.templates.fill(
            template_id,
            data.get('values') or {},
            board_id=(data.get('id') or ''),
            display_name=(data.get('display_name') or ''),
        )
        if err:
            self.send_json({'error': err},
                           404 if err.startswith('no template') else 400)
            return
        # Still not verified: every placeholder is filled and nothing has been
        # fetched. The client is expected to run test-fetch next, and the flag
        # travels with the payload so it cannot forget.
        self.send_json({'connector': cfg, 'verified': False})

    def handle_board_strategies_list(self):
        if not self._board_guard():
            return
        if self._board_or_404(self._board_id) is None:
            return
        self.send_json({
            'strategies': handlers.server.BoardStrategiesManager.list_for(self._board_id),
            'orders': list(handlers.server.boards.runs.ORDERS),
        })

    def handle_board_strategy_save(self):
        if not self._board_guard():
            return
        if self._board_or_404(self._board_id) is None:
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        strategies, err = handlers.server.BoardStrategiesManager.save(
            self._board_id, (data or {}).get('name'), (data or {}).get('select'))
        if err:
            self.send_json({'error': err}, 400)
            return
        self.send_json({'strategies': strategies}, 200)

    def handle_board_strategy_delete(self, name):
        if not self._board_guard():
            return
        if self._board_or_404(self._board_id) is None:
            return
        strategies, removed = handlers.server.BoardStrategiesManager.delete(self._board_id, name)
        if not removed:
            self.send_json({'error': f'no strategy named {name!r}'}, 404)
            return
        self.send_json({'strategies': strategies})

    def handle_board_strategy_preview(self):
        """What a run with this selection WOULD work, without spending agents."""
        if not self._board_guard():
            return
        cfg = self._board_or_404(self._board_id)
        if cfg is None:
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        preview, err = handlers.server.BoardStrategiesManager.preview(
            cfg, (data or {}).get('select'))
        if err:
            # A vendor/credential failure is not the caller's bad request.
            status = 502 if err.startswith(('refused for safety', 'no stored',
                                            'the board could not be listed',
                                            'the workspace GitHub App')) else 400
            self.send_json({'error': err}, status)
            return
        self.send_json(preview)

    def handle_board_metrics(self):
        if not self._board_guard():
            return
        if self._board_or_404(self._board_id) is None:
            return
        self.send_json(handlers.server.BoardMetricsManager.for_board(self._board_id))

    def handle_board_standing(self):
        """The board's overall state in one object (#712).

        Local reads only, so this is the one board endpoint a phone can poll on
        a 15-second timer without spending anybody's rate limit.
        """
        if not self._board_guard():
            return
        if self._board_or_404(self._board_id) is None:
            return
        self.send_json(handlers.server.BoardsManager.standing(self._board_id) or {})

    # ── runs (#588 Phase 4) ────────────────────────────────────────────────

    def handle_board_runs_list(self):
        if not self._board_guard():
            return
        if self._board_or_404(self._board_id) is None:
            return
        self.send_json({'runs': handlers.server.BoardRunsManager.list_runs(self._board_id)})

    def handle_board_run_get(self):
        if not self._board_guard():
            return
        run = handlers.server.BoardRunsManager.get(self._board_run_id)
        if run is None or run.get('board_id') != self._board_id:
            self.send_json({'error': 'Run not found'}, 404)
            return
        # The full record, items included: a run detail view IS the per-item
        # table, and paginating it would make "which item stalled?" a two-hop
        # question at exactly the moment someone is trying to answer it fast.
        self.send_json(run)

    def handle_board_run_create(self):
        if not self._board_guard():
            return
        cfg = self._board_or_404(self._board_id)
        if cfg is None:
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        run, err = handlers.server.BoardRunsManager.create(cfg, data if isinstance(data, dict) else {})
        if err:
            # A vendor/credential failure is not the caller's bad request, and
            # 502 vs 400 is the difference between "retry" and "fix your body".
            # A run already in flight is neither: nothing is wrong with the
            # request, it just conflicts with the board's current state (#712).
            if err.startswith('a run is already in flight'):
                status = 409
            else:
                status = 502 if err.startswith(('refused for safety', 'no stored',
                                                'the workspace GitHub App')) else 400
            self.send_json({'error': err}, status)
            return
        self.send_json(run, 201)

    def handle_board_run_stop(self):
        if not self._board_guard():
            return
        run = handlers.server.BoardRunsManager.get(self._board_run_id)
        if run is None or run.get('board_id') != self._board_id:
            self.send_json({'error': 'Run not found'}, 404)
            return
        if not handlers.server.BoardRunsManager.request_stop(self._board_run_id):
            self.send_json({'error': 'Run is not running'}, 409)
            return
        # Items already dispatched finish; nothing new is claimed. Killing a
        # Build mid-write leaves a half-applied multi-step action, which is a
        # worse outcome than one extra comment.
        self.send_json({'ok': True, 'detail': 'no further items will be claimed; '
                                              'items already dispatched will finish'})

    # ── dispatch adapters ─────────────────────────────────────────────────
    # The chain did these three transformations at the dispatch site. They are
    # not columns the table can express, so they are one wrapper each rather
    # than an edit to a handler body this series moves verbatim.

    def route_board_review_decide(self, board_id, item_id, decision):
        """`/staged/{item}/{approve|reject|send-back|edit}`.

        The item id is percent-encoded in the URL, and the path spells the
        third decision `send-back` while the handler takes `send_back`.
        """
        self._board_id = board_id
        self._board_item_id = urllib.parse.unquote(item_id)
        self.handle_board_review_decide(decision.replace('send-back', 'send_back'))

    def route_board_disposition(self, board_id, item_id):
        """`/items/{item}/disposition` — percent-decoded item id."""
        self._board_id = board_id
        self._board_item_id = urllib.parse.unquote(item_id)
        self.handle_board_disposition()

    def route_board_action(self, board_id, item_id):
        """`/items/{item}/actions` — percent-decoded item id."""
        self._board_id = board_id
        self._board_item_id = urllib.parse.unquote(item_id)
        self.handle_board_action()

    def route_board_strategy_delete(self, board_id, name):
        """`/strategies/{name}` — the name is percent-encoded and may contain
        anything, so the pattern is `(.+)` rather than a charset."""
        self._board_id = board_id
        self.handle_board_strategy_delete(urllib.parse.unquote(name))


#: Consulted by all four verbs where this domain's (contiguous) branches sat.
#: Registration order is match order.
ROUTES = RouteTable()

#: Board ids allow upper case and underscores. `credentials` and `templates`
#: are legal spellings of it, which is why they are reserved
#: (schema.RESERVED_BOARD_IDS) and why their routes come first.
_BOARD = r'([a-zA-Z0-9_-]+)'
_BOARDS = r'^/api/boards/' + _BOARD
#: A credential name is SCREAMING_SNAKE; a run id carries its `run-` prefix;
#: an item id is percent-encoded and may hold anything but a slash.
_CRED = r'([A-Z][A-Z0-9_]{2,63})'
_RUN = r'(run-[a-z0-9-]+)'
_ITEM = r'([^/]+)'
_TEMPLATE = r'([a-z0-9-]+)'

# --- reads ---------------------------------------------------------------
ROUTES.add('GET', '/api/boards', 'handle_boards_list')
# ORDERING HAZARD: both of these are legal board ids, so they must precede the
# `/api/boards/{id}` catch-all at the end of this GET block.
ROUTES.add('GET', '/api/boards/credentials', 'handle_board_credentials_list')
ROUTES.add('GET', '/api/boards/templates', 'handle_board_templates')
ROUTES.add('GET', re.compile(rf'^/api/boards/templates/{_TEMPLATE}$'),
           'handle_board_template_get')
ROUTES.add('GET', re.compile(_BOARDS + r'/strategies$'),
           'handle_board_strategies_list', sets='_board_id')
ROUTES.add('GET', re.compile(_BOARDS + r'/metrics$'),
           'handle_board_metrics', sets='_board_id')
ROUTES.add('GET', re.compile(_BOARDS + r'/items$'),
           'handle_board_items', sets='_board_id')
ROUTES.add('GET', re.compile(_BOARDS + r'/review$'),
           'handle_board_review_list', sets='_board_id')
ROUTES.add('GET', re.compile(_BOARDS + r'/standing$'),
           'handle_board_standing', sets='_board_id')
ROUTES.add('GET', re.compile(_BOARDS + r'/runs$'),
           'handle_board_runs_list', sets='_board_id')
ROUTES.add('GET', re.compile(_BOARDS + rf'/runs/{_RUN}$'),
           'handle_board_run_get', sets=('_board_id', '_board_run_id'))
ROUTES.add('GET', re.compile(_BOARDS + r'$'), 'handle_board_get',
           sets='_board_id')

# --- creates and writes --------------------------------------------------
# test-fetch is the verification oracle; /draft validates (and optionally
# probes) without persisting; the action route is the only write path into a
# tracker and is gated by the connector's own allowlist.
ROUTES.add('POST', '/api/boards', 'handle_board_create')
ROUTES.add('POST', '/api/boards/draft', 'handle_board_draft')
# `templates` is reserved, so nothing can collide with this.
ROUTES.add('POST', re.compile(rf'^/api/boards/templates/{_TEMPLATE}/fill$'),
           'handle_board_template_fill')
ROUTES.add('POST', re.compile(_BOARDS + r'/test-fetch$'),
           'handle_board_test_fetch', sets='_board_id')
# `preview` ahead of the save route, as the chain had it. Presentational: the
# save pattern is `/strategies$`, which an extra segment cannot match.
ROUTES.add('POST', re.compile(_BOARDS + r'/strategies/preview$'),
           'handle_board_strategy_preview', sets='_board_id')
ROUTES.add('POST', re.compile(_BOARDS + r'/strategies$'),
           'handle_board_strategy_save', sets='_board_id')
# Review decisions (#588 Phase 5): approve/reject/send-back carry a
# client-generated approval_id and are consume-once; approve also carries the
# content_hash the reviewer was shown.
ROUTES.add('POST',
           re.compile(_BOARDS + rf'/staged/{_ITEM}/(approve|reject|send-back|edit)$'),
           'route_board_review_decide')
ROUTES.add('POST', re.compile(_BOARDS + rf'/items/{_ITEM}/disposition$'),
           'route_board_disposition')
ROUTES.add('POST', re.compile(_BOARDS + r'/runs$'),
           'handle_board_run_create', sets='_board_id')
ROUTES.add('POST', re.compile(_BOARDS + rf'/runs/{_RUN}/stop$'),
           'handle_board_run_stop', sets=('_board_id', '_board_run_id'))
ROUTES.add('POST', re.compile(_BOARDS + rf'/items/{_ITEM}/actions$'),
           'route_board_action')

# --- replaces ------------------------------------------------------------
# A connector is validated as a whole, so PUT is a full replace: a partial
# merge could leave it incoherent.
ROUTES.add('PUT', re.compile(rf'^/api/boards/credentials/{_CRED}$'),
           'handle_board_credential_put', sets='_board_cred_name')
ROUTES.add('PUT', re.compile(_BOARDS + r'$'), 'handle_board_update',
           sets='_board_id')

# --- deletes -------------------------------------------------------------
ROUTES.add('DELETE', re.compile(rf'^/api/boards/credentials/{_CRED}$'),
           'handle_board_credential_delete', sets='_board_cred_name')
ROUTES.add('DELETE', re.compile(_BOARDS + r'/strategies/(.+)$'),
           'route_board_strategy_delete')
ROUTES.add('DELETE', re.compile(_BOARDS + r'$'), 'handle_board_delete',
           sets='_board_id')
