"""Triggers: webhooks, crons and page-watches (#100).

Three sub-domains that share a run ledger, so they share a module. Twenty-one
routes across three verbs, and the domain #733 flagged for the one ordering
hazard that is *explicitly commented* in the source.

## The hazards, now checkable

Every ordering constraint in this domain turns out to be presentational, for
the same reason memory's was: an id is slash-free and every pattern is
`$`-anchored, so `{id}/runs` cannot be read as an `{id}`. The tests assert
that directly, by resolving against a reversed copy of the table.

One of them is worth keeping anyway, loudly. On POST,
`/api/webhooks/{id}/test` is registered ahead of `/api/webhooks/{id}` — and
those two routes sit on **opposite sides of an auth boundary**: `/test` fires
a webhook from the dashboard and is `check_claude_auth`-gated, while the bare
route is the inbound receiver, authenticated by an HMAC of the raw body and
deliberately not by a bearer token. If that order ever inverted, the
unauthenticated receiver would answer the authenticated endpoint. It cannot
today; the registration comment says why it must not start to.

(The page-watch action route carries a similar comment about preceding "the
bare /<id> route". There is no bare POST `{id}` route — only GET and DELETE
have one — so on this verb that constraint is vacuous. Order preserved as
found.)

## `sets=`

Most handlers here read their parameters off the request — `self._webhook_id`,
`self._cron_action` — rather than taking arguments, and the chain assigned
them at each dispatch site. The table's `sets=` column is that assignment (see
handlers/routing.py). Giving these handlers real parameters is a later
cleanup: doing it here would mean editing the bodies this series moves
verbatim.

## Routes that are not for the dashboard

Three endpoints are called by something other than a logged-in browser, and
each authenticates its own way: `POST /api/webhooks/{id}` (HMAC over the
body), and the two `/api/triggers/*` receivers the k8s CronJob pods curl,
which carry a per-trigger `fire_token` as a bearer credential. They are on
the same table as the CRUD routes; the gating lives in the handlers, exactly
as before.
"""

import hashlib
import json
import re
import urllib.parse

import handlers
from handlers.routing import RouteTable


class TriggerRoutes:
    """BrowserHandler methods backing `ROUTES`."""

    def handle_webhook_list(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        # Public view — secrets are stripped out by WebhookManager._public_view
        self.send_json({'webhooks': handlers.server.WebhookManager.list_webhooks()})

    def handle_webhook_get(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        cfg = handlers.server.WebhookManager.get_webhook(self._webhook_id)
        if cfg is None:
            self.send_json({'error': 'Webhook not found'}, 404)
            return
        # Include the receive URL so the dashboard can render a copy button.
        cfg['receive_url'] = self._build_receive_url(self._webhook_id)
        self.send_json(cfg)

    def _trigger_runs_page(self):
        """`?limit=&offset=` for a runs listing. Clamping lives in
        TriggerRunsManager.list_runs so the MCP/CLI callers get it too."""
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return {
            'limit': (qs.get('limit') or [None])[0],
            'offset': (qs.get('offset') or ['0'])[0],
        }

    def handle_webhook_runs(self):
        """GET /api/webhooks/<id>/runs — this webhook's fire history (#91).

        A read, so `_readonly_block` does not apply; the read-only public demo
        is allowed to show a trigger's history for the same reason it is allowed
        to list the triggers themselves."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if handlers.server.WebhookManager.get_webhook(self._webhook_id) is None:
            self.send_json({'error': 'Webhook not found'}, 404)
            return
        self.send_json(handlers.server.TriggerRunsManager.list_runs(
            'webhook', self._webhook_id, **self._trigger_runs_page()))

    def handle_webhook_create(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        cfg, err = handlers.server.WebhookManager.create_or_update(data)
        if err:
            self.send_json({'error': err}, 400)
            return
        # On create we surface the hmac_secret ONCE so the user can copy it
        # into the upstream service (GitHub/Stripe/etc.). After this, it's
        # only ever returned as hmac_secret_set: true.
        response = handlers.server.WebhookManager._public_view(cfg)
        if cfg.get('hmac_secret'):
            response['hmac_secret_once'] = cfg['hmac_secret']
        response['receive_url'] = self._build_receive_url(cfg['id'])
        self.send_json(response, 201)

    def handle_webhook_delete(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        ok = handlers.server.WebhookManager.delete(self._webhook_id)
        if not ok:
            self.send_json({'error': 'Webhook not found'}, 404)
            return
        self.send_json({'ok': True})

    def handle_webhook_receive(self):
        """Inbound receiver. Auth via HMAC of the raw body — NO bearer token.
        Triggers a Claude task and returns the task_id."""
        cfg = handlers.server.WebhookManager.get_webhook(self._webhook_id, include_secrets=True)
        if cfg is None:
            # Don't leak existence: same response as a real auth failure.
            self.send_json({'error': 'Not found or unauthorized'}, 404)
            return

        # Every early return below also writes a ledger entry (#91). The
        # rejection branches are the whole point of the feature: a bad HMAC and
        # a replayed body are what people are actually trying to debug, and
        # until now they returned a deliberately vague 404 and left no trace.
        # The id is known to exist at this point, so nothing an anonymous
        # caller sends can create a ledger file for a trigger we do not have.
        def _ledger(outcome, reason, **kw):
            handlers.server.TriggerRunsManager.record(
                'webhook', cfg['id'], outcome, reason=reason,
                source_ip=self._peer_ip(), forwarded_for=self._forwarded_for(),
                provider=cfg.get('provider') or 'generic', **kw)

        # Read the raw body for HMAC verification BEFORE JSON parsing.
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length < 0 or content_length > 1 * 1024 * 1024:  # 1 MiB cap
            # No signature_verified: verification has not run yet, and claiming
            # False here would read as "the signature was wrong".
            _ledger('rejected', 'payload_too_large')
            self.send_json({'error': 'payload too large'}, 413)
            return
        raw_body = self.rfile.read(content_length) if content_length else b''

        # Pass full headers — Slack/Stripe verifiers read multiple of them
        # (e.g. X-Slack-Request-Timestamp alongside X-Slack-Signature).
        if not handlers.server.WebhookManager.verify_signature(cfg, raw_body, self.headers):
            _ledger('rejected', 'bad_signature', signature_verified=False)
            # Same shape as the not-found response to avoid leaking which is which.
            self.send_json({'error': 'Not found or unauthorized'}, 404)
            return

        # Replay protection: reject identical signed bodies seen within the
        # 5-minute window. Provider-level timestamp checks (Slack/Stripe) and
        # this cache are belt-and-suspenders — Slack/Stripe alone allow up to
        # 5 minutes of replay; this cache closes that window to "exactly once".
        replay_key = (cfg['id'], hashlib.sha256(raw_body).hexdigest())
        if not handlers.server.WebhookManager.REPLAY_CACHE.check_and_record(replay_key):
            # signature_verified is True here on purpose: the signature DID
            # check out, and the body was refused for being a duplicate. The
            # two failures have different fixes, so the ledger separates them.
            _ledger('rejected', 'replay', signature_verified=self._signature_checked(cfg),
                    error='identical signed body already seen in the 5-minute window')
            self.send_json({'error': 'duplicate request (replay)'}, 409)
            return

        try:
            payload = json.loads(raw_body.decode('utf-8')) if raw_body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            _ledger('rejected', 'invalid_payload',
                    signature_verified=self._signature_checked(cfg))
            self.send_json({'error': 'invalid JSON payload'}, 400)
            return

        self._fire_webhook(cfg, payload, status=202,
                           signature_verified=self._signature_checked(cfg))

    def handle_webhook_test(self):
        """Dashboard 'Test' button: fire as if a real call came in, but with
        bearer auth instead of HMAC. Payload comes from the JSON body."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        cfg = handlers.server.WebhookManager.get_webhook(self._webhook_id, include_secrets=True)
        if cfg is None:
            self.send_json({'error': 'Webhook not found'}, 404)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        payload = data.get('payload', {}) if isinstance(data, dict) else {}
        # manual=True, and no signature_verified at all: this path authenticates
        # with the dashboard's own bearer/OAuth session and never looks at an
        # HMAC, so a tick or a cross in that column would both be wrong.
        self._fire_webhook(cfg, payload, status=202, manual=True)

    def _fire_webhook(self, cfg, payload, status=202, *,
                      signature_verified=None, manual=False):
        prompt = handlers.server.WebhookManager.render_prompt(cfg, payload)
        task = handlers.server.ClaudeTaskManager.create_task(
            prompt,
            workdir=cfg.get('workdir') or '/home/dev',
            response_url=cfg.get('response_url'),
            response_secret=cfg.get('response_secret'),
            source=f"webhook:{cfg['id']}",
        )

        def _ledger(outcome, reason=None, **kw):
            handlers.server.TriggerRunsManager.record(
                'webhook', cfg['id'], outcome, reason=reason,
                source_ip=self._peer_ip(), forwarded_for=self._forwarded_for(),
                signature_verified=signature_verified,
                provider=cfg.get('provider') or 'generic',
                manual=manual, **kw)

        if task.get('status') == 'rejected':
            _ledger('rejected', 'at_capacity', error=task.get('error'))
            self.send_json({
                'error': task.get('error'),
                'webhook_id': cfg['id'],
            }, 429)
            return
        # Propagate task-creation failures (tmux unreachable, fs error) so
        # the upstream sees a 5xx and can retry, rather than a 202 with a
        # task_id that never runs.
        if task.get('status') == 'error':
            _ledger('error', 'spawn_failed', task_id=task.get('task_id'),
                    error=task.get('error') or 'failed to spawn task')
            self.send_json({
                'error': task.get('error') or 'failed to spawn task',
                'webhook_id': cfg['id'],
                'task_id': task.get('task_id'),
            }, 502)
            return
        _ledger('spawned', task_id=task['task_id'])
        handlers.server.EventBroker.publish('trigger.fired', {
            'trigger_type': 'webhook',
            'trigger_id': cfg['id'],
            'task_id': task['task_id'],
        })
        handlers.server.FeedManager.emit_trigger('webhook', cfg['id'], cfg.get('workdir') or '')
        self.send_json({
            'task_id': task['task_id'],
            'webhook_id': cfg['id'],
            'status': task['status'],
        }, status)

    # --- Run-ledger helpers (#91) ----------------------------------------

    def _peer_ip(self):
        """The socket peer, or '' if it cannot be read.

        Deliberately NOT X-Forwarded-For. Behind the workspace ingress the peer
        is the ingress controller, so this is often the same value for every
        entry — but it is the one address the caller cannot choose, and an audit
        column that an attacker writes is worse than a boring one.
        `_forwarded_for` carries the claimed hop separately, so a reader can
        tell proof from assertion."""
        try:
            addr = self.client_address
        except AttributeError:      # pragma: no cover - always set by the base class
            return ''
        if isinstance(addr, (tuple, list)) and addr:
            return str(addr[0])[:64]
        return str(addr or '')[:64]

    def _forwarded_for(self):
        """Leftmost X-Forwarded-For hop, or '' when the header is absent.

        Caller-asserted by definition — anyone can send the header — which is
        exactly why it is a second field rather than overwriting `source_ip`."""
        raw = self.headers.get('X-Forwarded-For', '') or ''
        return raw.split(',')[0].strip()[:64]

    @staticmethod
    def _signature_checked(cfg):
        """Whether an HMAC was actually verified, not merely whether we let the
        request in. A secret-less webhook running with
        KC_ALLOW_UNSIGNED_WEBHOOKS=1 is accepted with nothing checked, and the
        ledger says so — that is a finding, not a tick."""
        return bool(cfg.get('hmac_secret'))

    def _build_receive_url(self, webhook_id):
        """Construct the public URL the upstream service should POST to.
        Uses the Host header; ingress strips /oauth so we don't prepend it."""
        host = self.headers.get('Host', '')
        proto = self.headers.get('X-Forwarded-Proto', 'https')
        if not host:
            return f'/api/webhooks/{webhook_id}'
        return f'{proto}://{host}/api/webhooks/{webhook_id}'

    # --- Cron handlers ---
    # CRUD uses check_claude_auth (dashboard / scripts). The cron-fire receiver
    # uses the per-cron fire_token instead — k8s CronJob pods are not part of
    # the OAuth session.

    def handle_cron_list(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        crons = handlers.server.CronManager.list_crons()
        # Decorate with k8s status — best-effort, won't fail the request.
        for c in crons:
            try:
                c.update(handlers.server.CronManager.kubectl_status(c['id']))
            except Exception:
                pass
        self.send_json({'crons': crons})

    def handle_cron_get(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        cfg = handlers.server.CronManager.get_cron(self._cron_id)
        if cfg is None:
            self.send_json({'error': 'Cron not found'}, 404)
            return
        cfg.update(handlers.server.CronManager.kubectl_status(self._cron_id))
        self.send_json(cfg)

    def handle_cron_create(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        cfg, err = handlers.server.CronManager.create_or_update(data)
        # err may be a "soft" error (k8s apply failed but config saved); still 4xx.
        if cfg is None:
            self.send_json({'error': err}, 400)
            return
        response = handlers.server.CronManager._public_view(cfg)
        if err:
            response['warning'] = err
            self.send_json(response, 202)
            return
        self.send_json(response, 201)

    def handle_cron_delete(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        ok = handlers.server.CronManager.delete(self._cron_id)
        if not ok:
            self.send_json({'error': 'Cron not found'}, 404)
            return
        self.send_json({'ok': True})

    def handle_cron_runs(self):
        """GET /api/crons/<id>/runs — this cron's fire history (#91)."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if handlers.server.CronManager.get_cron(self._cron_id) is None:
            self.send_json({'error': 'Cron not found'}, 404)
            return
        self.send_json(handlers.server.TriggerRunsManager.list_runs(
            'cron', self._cron_id, **self._trigger_runs_page()))

    def handle_cron_action(self):
        """suspend / resume / run — dashboard buttons."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        action = self._cron_action
        if action == 'suspend':
            cfg = handlers.server.CronManager.set_suspended(self._cron_id, True)
            if cfg is None:
                self.send_json({'error': 'Cron not found'}, 404)
                return
            self.send_json(handlers.server.CronManager._public_view(cfg))
        elif action == 'resume':
            cfg = handlers.server.CronManager.set_suspended(self._cron_id, False)
            if cfg is None:
                self.send_json({'error': 'Cron not found'}, 404)
                return
            self.send_json(handlers.server.CronManager._public_view(cfg))
        elif action == 'run':
            ok, info = handlers.server.CronManager.run_now(self._cron_id)
            if not ok:
                self.send_json({'error': info}, 500)
                return
            self.send_json({'ok': True, 'job': info})
        elif action == 'rotate-token':
            cfg, new_token = handlers.server.CronManager.rotate_token(self._cron_id)
            if cfg is None:
                self.send_json({'error': 'rotate failed (see pod logs)'}, 500)
                return
            response = handlers.server.CronManager._public_view(cfg)
            # One-time reveal of the new token, matching webhook secret-reveal UX
            response['fire_token_once'] = new_token
            self.send_json(response)
        else:
            self.send_json({'error': 'unknown action'}, 400)

    def handle_cron_fire(self):
        """Receiver called by the k8s CronJob pod with the per-cron fire_token.
        Renders the cron's prompt template against its static payload and
        spawns a Claude task. Never touches OAuth headers — this is an
        internal-cluster call."""
        auth = self.headers.get('Authorization', '')
        token = auth[7:].strip() if auth.startswith('Bearer ') else ''
        ok, cfg = handlers.server.CronManager.verify_fire_token(self._cron_id, token)

        def _ledger(outcome, reason=None, **kw):
            """Record against the cron's OWN id. `cfg` is never read for it:
            verify_fire_token hands back the config with its fire_token still
            in it on a failed auth, and this ledger must not grow a habit of
            touching that object."""
            handlers.server.TriggerRunsManager.record(
                'cron', self._cron_id, outcome, reason=reason,
                source_ip=self._peer_ip(), forwarded_for=self._forwarded_for(),
                **kw)

        if not ok or cfg is None:
            # cfg is None for an id that does not exist, and non-None when the
            # id is real but the bearer was wrong. Only the second is recorded:
            # a ledger file per guessed id would be a disk-fill primitive, and
            # "someone fired this cron with a stale token" is the entry worth
            # having — it is what a rotated fire_token looks like from here.
            if cfg is not None:
                _ledger('rejected', 'bad_token', signature_verified=False)
            # Don't leak existence; same response for unknown id vs bad token.
            self.send_json({'error': 'Not found or unauthorized'}, 404)
            return
        # Refuse to spawn tasks for suspended crons. Belt-and-suspenders: the
        # CronJob shouldn't fire when suspended, but if someone hits this
        # endpoint manually we want the suspend flag to be authoritative.
        if cfg.get('suspended'):
            _ledger('rejected', 'suspended', signature_verified=True)
            self.send_json({'error': 'cron is suspended'}, 409)
            return
        prompt = handlers.server.CronManager.render_prompt(cfg)
        task = handlers.server.ClaudeTaskManager.create_task(
            prompt,
            workdir=cfg.get('workdir') or '/home/dev',
            response_url=cfg.get('response_url'),
            response_secret=cfg.get('response_secret'),
            source=f"cron:{cfg['id']}",
        )
        if task.get('status') == 'rejected':
            _ledger('rejected', 'at_capacity', signature_verified=True,
                    error=task.get('error'))
            self.send_json({'error': task.get('error'), 'cron_id': cfg['id']}, 429)
            return
        # The ledger reports what the spawn actually did. This handler answers
        # 202 either way (changing that is a separate call), but writing
        # 'spawned' for a task whose status came back 'error' would make the
        # audit log agree with the HTTP code instead of with reality.
        if task.get('status') == 'error':
            _ledger('error', 'spawn_failed', signature_verified=True,
                    task_id=task.get('task_id'),
                    error=task.get('error') or 'failed to spawn task')
        else:
            _ledger('spawned', signature_verified=True, task_id=task.get('task_id'))
        handlers.server.EventBroker.publish('trigger.fired', {
            'trigger_type': 'cron',
            'trigger_id': cfg['id'],
            'task_id': task['task_id'],
        })
        handlers.server.FeedManager.emit_trigger('cron', cfg['id'], cfg.get('workdir') or '')
        self.send_json({
            'task_id': task['task_id'],
            'cron_id': cfg['id'],
            'status': task['status'],
        }, 202)

    # --- Page-watch handlers (#681) --------------------------------------

    def handle_page_watch_list(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        self.send_json({'page_watches': handlers.server.PageWatchManager.list_page_watches()})

    def handle_page_watch_get(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        cfg = handlers.server.PageWatchManager.get_page_watch(self._page_watch_id)
        if cfg is None:
            self.send_json({'error': 'Not found'}, 404)
            return
        self.send_json(cfg)

    def handle_page_watch_runs(self):
        """GET /api/page-watches/<id>/runs — this watch's check history (#91)."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if handlers.server.PageWatchManager.get_page_watch(self._page_watch_id) is None:
            self.send_json({'error': 'Not found'}, 404)
            return
        self.send_json(handlers.server.TriggerRunsManager.list_runs(
            'page-watch', self._page_watch_id, **self._trigger_runs_page()))

    def handle_page_watch_create(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        existing = data.get('id') if handlers.server.PageWatchManager.get_page_watch(
            data.get('id') or '') else None
        cfg, err = handlers.server.PageWatchManager.create_or_update(data, existing_id=existing)
        if cfg is None:
            self.send_json({'error': err}, 400)
            return
        response = handlers.server.PageWatchManager._public_view(cfg)
        if err:
            # Saved locally but the CronJob did not apply — surfaced rather
            # than swallowed, because a watch with no timer never fires.
            response['warning'] = err
            self.send_json(response, 202)
            return
        self.send_json(response, 201)

    def handle_page_watch_delete(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        ok = handlers.server.PageWatchManager.delete(self._page_watch_id)
        if not ok:
            self.send_json({'error': 'Not found'}, 404)
            return
        self.send_json({'ok': True})

    def handle_page_watch_action(self):
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        action = self._page_watch_action
        if action in ('suspend', 'resume'):
            cfg = handlers.server.PageWatchManager.set_suspended(
                self._page_watch_id, action == 'suspend')
            if cfg is None:
                self.send_json({'error': 'Not found'}, 404)
                return
            self.send_json(handlers.server.PageWatchManager._public_view(cfg))
            return
        if action == 'check':
            # "Check now" from the dashboard. Same code path as the scheduled
            # check, so what the button does and what the CronJob does can
            # never drift apart — including the suspend guard. Without this,
            # Pause would mean "pause the timer" while the button still fired
            # tasks, which is not what the row says it does.
            cfg = handlers.server.PageWatchManager.get_page_watch(self._page_watch_id)
            if cfg is None:
                self.send_json({'error': 'Not found'}, 404)
                return
            if cfg.get('suspended'):
                self.send_json({'error': 'page-watch is suspended'}, 409)
                return
            self._run_page_watch_check(manual=True)
            return
        self.send_json({'error': 'unknown action'}, 400)

    def handle_page_watch_check(self):
        """Receiver called by the k8s CronJob pod with the per-watch fire_token.

        Never touches OAuth headers — this is an internal-cluster call, exactly
        like handle_cron_fire.
        """
        auth = self.headers.get('Authorization', '')
        token = auth[7:].strip() if auth.startswith('Bearer ') else ''
        ok, cfg = handlers.server.PageWatchManager.verify_fire_token(self._page_watch_id, token)
        if not ok or cfg is None:
            # Same response for unknown id and bad token, so the endpoint does
            # not confirm which watches exist. No ledger entry either, and
            # unlike the cron path that is not a choice made here:
            # PageWatchManager.verify_fire_token returns (False, None) on ANY
            # failure by design, so this branch genuinely cannot tell a bad
            # token from an id that was never real.
            self.send_json({'error': 'Not found or unauthorized'}, 404)
            return
        if cfg.get('suspended'):
            handlers.server.TriggerRunsManager.record(
                'page-watch', self._page_watch_id, 'rejected', reason='suspended',
                signature_verified=True, source_ip=self._peer_ip(),
                forwarded_for=self._forwarded_for())
            self.send_json({'error': 'page-watch is suspended'}, 409)
            return
        self._run_page_watch_check(manual=False)

    def _run_page_watch_check(self, *, manual):
        """Shared body of the scheduled check and the dashboard's Check now."""
        watch_id = self._page_watch_id
        outcome, cfg, detail = handlers.server.PageWatchManager.check_once(watch_id)

        def _ledger(led_outcome, reason=None, **kw):
            """A page-watch records EVERY check, including the ones that found
            nothing. For the other two kinds "it fired" and "it arrived" are the
            same event; for a watch they are not, and "it checked on time and
            the page had not moved" is the single most common answer to "why
            didn't my watch fire?". A 5-minute watch writes ~288 entries a day,
            which the 256 KiB cap holds for about a fortnight.

            signature_verified is only claimed for the scheduled call, which
            arrived carrying the watch's fire_token; Check-now rides the
            dashboard session instead."""
            handlers.server.TriggerRunsManager.record(
                'page-watch', watch_id, led_outcome, reason=reason,
                source_ip=self._peer_ip(), forwarded_for=self._forwarded_for(),
                signature_verified=None if manual else True,
                manual=manual, **kw)

        if outcome == 'missing':
            # Nothing to attribute an entry to — the watch is gone.
            self.send_json({'error': 'Not found'}, 404)
            return
        if outcome == 'busy':
            # Another check for this watch is already in flight — the schedule
            # and the button landed together. That check stores its own
            # result, so there is nothing for this one to add, and running it
            # anyway is precisely how one change becomes two agent runs.
            # 200 for the same reason 'error' is 200: nothing went wrong, so
            # failing the CronJob's `curl -f` over it would be noise.
            _ledger('skipped', 'busy')
            self.send_json({
                'page_watch_id': watch_id,
                'outcome': 'busy',
                'error': 'a check for this page-watch is already running',
            })
            return
        if outcome == 'error':
            # 200, not 5xx: the check ran correctly and its answer is "the page
            # could not be read". A non-2xx would make the CronJob's `curl -f`
            # fail the Job and bury a routine, expected outcome in k8s noise.
            _ledger('error', 'fetch_failed', error=detail.get('error'))
            self.send_json({
                'page_watch_id': watch_id,
                'outcome': 'error',
                'error': detail.get('error'),
                'consecutive_failures': cfg.get('consecutive_failures'),
            })
            return
        if outcome in ('baseline', 'unchanged'):
            _ledger('skipped', outcome)
            self.send_json({
                'page_watch_id': watch_id,
                'outcome': outcome,
                'last_checked_at': cfg.get('last_checked_at'),
            })
            return

        # outcome == 'changed' — the new hash is already persisted, so a
        # failure from here on costs a notification, never a repeat.
        if cfg.get('suspended'):
            # Paused while this check was mid-fetch. cfg here is the record as
            # it now stands on disk (check_once merges rather than writes back
            # its own stale copy), so this sees the pause. pending_fire is
            # already set, which means resuming re-offers this change rather
            # than losing it.
            _ledger('rejected', 'suspended',
                    error='paused mid-check; the change is still owed')
            self.send_json({
                'page_watch_id': cfg['id'],
                'outcome': 'changed',
                'pending': True,
                'error': 'page-watch is suspended',
            }, 409)
            return
        payload = handlers.server.PageWatchManager.build_payload(
            cfg, detail.get('text'), now=cfg.get('last_changed_at'))
        prompt = handlers.server.PageWatchManager.render_prompt(cfg, payload)
        task = handlers.server.ClaudeTaskManager.create_task(
            prompt,
            workdir=cfg.get('workdir') or '/home/dev',
            response_url=cfg.get('response_url'),
            response_secret=cfg.get('response_secret'),
            source=f"page-watch:{cfg['id']}",
            # Explicit, not inherited. 'page-watch:' is deliberately absent
            # from _UNATTENDED_SOURCE_PREFIXES so this resolves False anyway;
            # passing it here makes the intent unmissable to the next reader.
            # A third-party web page must never start a permission-skipping
            # agent on a timer.
            auto_approve=False,
        )
        if task.get('status') == 'rejected':
            # At the task cap. pending_fire stays set, so the next successful
            # check re-fires instead of silently swallowing the change.
            _ledger('rejected', 'at_capacity', error=task.get('error'))
            self.send_json({
                'error': task.get('error'),
                'page_watch_id': cfg['id'],
                'outcome': 'changed',
                'pending': True,
            }, 429)
            return
        if task.get('status') == 'error':
            _ledger('error', 'spawn_failed', task_id=task.get('task_id'),
                    error=task.get('error') or 'task failed to start')
            self.send_json({
                'error': task.get('error') or 'task failed to start',
                'page_watch_id': cfg['id'],
                'outcome': 'changed',
                'pending': True,
            }, 502)
            return

        _ledger('spawned', 'changed', task_id=task['task_id'])
        handlers.server.PageWatchManager.clear_pending_fire(cfg['id'])
        handlers.server.EventBroker.publish('trigger.fired', {
            'trigger_type': 'page-watch',
            'trigger_id': cfg['id'],
            'task_id': task['task_id'],
        })
        handlers.server.FeedManager.emit_trigger('page-watch', cfg['id'], cfg.get('workdir') or '')
        self.send_json({
            'task_id': task['task_id'],
            'page_watch_id': cfg['id'],
            'outcome': 'changed',
            'status': task['status'],
        }, 202)


#: Consulted by do_GET, do_POST and do_DELETE where each verb's branches used
#: to sit. Registration order is the chain's order.
ROUTES = RouteTable()

#: Webhook ids allow upper case and underscores; cron and page-watch ids are
#: generated slugs. Both exclude '/', which is what makes the `{id}/runs` and
#: `{id}/test` routes unable to collide with the bare `{id}` ones.
_WEBHOOK_ID = r'([a-zA-Z0-9_-]+)'
_SLUG = r'([a-z0-9-]+)'

# --- reads ---------------------------------------------------------------
ROUTES.add('GET', '/api/webhooks', 'handle_webhook_list')
ROUTES.add('GET', re.compile(rf'^/api/webhooks/{_WEBHOOK_ID}/runs$'),
           'handle_webhook_runs', sets='_webhook_id')
ROUTES.add('GET', re.compile(rf'^/api/webhooks/{_WEBHOOK_ID}$'),
           'handle_webhook_get', sets='_webhook_id')

ROUTES.add('GET', '/api/crons', 'handle_cron_list')
ROUTES.add('GET', re.compile(rf'^/api/crons/{_SLUG}/runs$'),
           'handle_cron_runs', sets='_cron_id')
ROUTES.add('GET', re.compile(rf'^/api/crons/{_SLUG}$'),
           'handle_cron_get', sets='_cron_id')

ROUTES.add('GET', '/api/page-watches', 'handle_page_watch_list')
ROUTES.add('GET', re.compile(rf'^/api/page-watches/{_SLUG}/runs$'),
           'handle_page_watch_runs', sets='_page_watch_id')
ROUTES.add('GET', re.compile(rf'^/api/page-watches/{_SLUG}$'),
           'handle_page_watch_get', sets='_page_watch_id')

# --- creates -------------------------------------------------------------
ROUTES.add('POST', '/api/webhooks', 'handle_webhook_create')
ROUTES.add('POST', '/api/crons', 'handle_cron_create')
ROUTES.add('POST', '/api/page-watches', 'handle_page_watch_create')

# --- fires and state changes ---------------------------------------------
# ORDERING HAZARD, and the one that guards an auth boundary: /test is
# dashboard-authed, the bare route below it is the HMAC-authed inbound
# receiver. Keep /test first. See the module docstring.
ROUTES.add('POST', re.compile(rf'^/api/webhooks/{_WEBHOOK_ID}/test$'),
           'handle_webhook_test', sets='_webhook_id')
ROUTES.add('POST', re.compile(rf'^/api/webhooks/{_WEBHOOK_ID}$'),
           'handle_webhook_receive', sets='_webhook_id')

ROUTES.add('POST',
           re.compile(rf'^/api/crons/{_SLUG}/(suspend|resume|run|rotate-token)$'),
           'handle_cron_action', sets=('_cron_id', '_cron_action'))
# Receiver curl'd by the CronJob pod — fire_token, not OAuth.
ROUTES.add('POST', re.compile(rf'^/api/triggers/cron-fire/{_SLUG}$'),
           'handle_cron_fire', sets='_cron_id')

ROUTES.add('POST',
           re.compile(rf'^/api/page-watches/{_SLUG}/(suspend|resume|check)$'),
           'handle_page_watch_action',
           sets=('_page_watch_id', '_page_watch_action'))
ROUTES.add('POST', re.compile(rf'^/api/triggers/page-watch-check/{_SLUG}$'),
           'handle_page_watch_check', sets='_page_watch_id')

# --- deletes -------------------------------------------------------------
ROUTES.add('DELETE', re.compile(rf'^/api/webhooks/{_WEBHOOK_ID}$'),
           'handle_webhook_delete', sets='_webhook_id')
ROUTES.add('DELETE', re.compile(rf'^/api/crons/{_SLUG}$'),
           'handle_cron_delete', sets='_cron_id')
ROUTES.add('DELETE', re.compile(rf'^/api/page-watches/{_SLUG}$'),
           'handle_page_watch_delete', sets='_page_watch_id')
