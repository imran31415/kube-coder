"""Conversation Gateway: WhatsApp in, Hypervisor out (#100, #306/#328/#329).

Thirteen routes across four verbs. Contiguous on every verb, like boards, so
the table below is a straight lift of the chains' order.

## Three different auth postures on one table

This is the domain where "who may call this" varies most, and none of it is
the table's business — every gate lives in the handler, exactly as before.
Worth stating because the routes sit side by side:

* `POST /api/gateway/whatsapp/webhook` and `GET` (the Meta verify handshake)
  are deliberately NOT behind `check_claude_auth`. An external provider
  (Twilio/Meta) cannot carry an OAuth session, so it authenticates with the
  provider signature the adapter verifies — the same posture as the inbound
  webhook receiver in handlers/triggers.py.
* The link CRUD and credentials routes DO require bearer/OAuth: they manage
  identity bindings and provider secrets.
* The `internal/*` loopback routes are the in-app Walkie-Talkie preview. They
  are bearer-authed (the caller is the app user) and drive the SAME gateway
  core through a loopback adapter, so the preview shows exactly what WhatsApp
  would see.

## One route matches on a hash, not a name

`DELETE /api/gateway/link/{id}` takes a 64-hex sha256 identity hash, not a
slug. The charset is the constraint, and it is what keeps that route from
colliding with anything else under `/api/gateway/`.

Note the singular/plural split, preserved as found: the collection read is
`/api/gateway/links`, the create is `/api/gateway/link`, and the delete is
`/api/gateway/link/{hash}`.
"""

import json
import re
import urllib.parse

import handlers
from handlers.routing import RouteTable


class GatewayRoutes:
    """BrowserHandler methods backing `ROUTES`."""

    def _gateway_raw_request(self, method):
        """Build a gateway.RawRequest from this HTTP request: raw body (capped),
        parsed form (Twilio), full external URL (Twilio signs over it), headers,
        and query. Returns None if the body exceeds the 1 MiB cap."""
        try:
            content_length = int(self.headers.get('Content-Length', 0) or 0)
        except (TypeError, ValueError):
            content_length = 0
        if content_length < 0 or content_length > 1 * 1024 * 1024:
            return None
        raw_body = self.rfile.read(content_length) if content_length else b''
        ctype = self.headers.get('Content-Type', '') or ''
        form = {}
        if 'application/x-www-form-urlencoded' in ctype and raw_body:
            parsed = urllib.parse.parse_qs(
                raw_body.decode('utf-8', 'replace'), keep_blank_values=True)
            form = {k: v[0] for k, v in parsed.items()}
        host = self.headers.get('Host', '')
        proto = self.headers.get('X-Forwarded-Proto', 'https')
        path = self.path.split('?', 1)[0]
        url = f'{proto}://{host}{path}' if host else path
        query = {k: v[0] for k, v in urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query).items()}
        headers = {k: v for k, v in self.headers.items()}
        return handlers.server.RawRequest(method=method, url=url, headers=headers,
                          raw_body=raw_body, form=form, query=query)

    def handle_gateway_whatsapp_webhook(self):
        """Inbound WhatsApp webhook. Provider-signature authed (in the adapter),
        idempotent on the provider message id, fast 200 so retries stop."""
        if handlers.server._gateway_disabled(self):
            return
        gw = handlers.server.get_gateway()
        adapter = handlers.server.get_gateway_adapter()
        if gw is None or adapter is None:
            self.send_json({'error': 'gateway unavailable'}, 503)
            return
        raw = self._gateway_raw_request('POST')
        if raw is None:
            self.send_json({'error': 'payload too large'}, 413)
            return
        result = gw.handle_inbound(adapter, raw)
        self.send_json({'status': result.action}, result.status)

    def handle_gateway_whatsapp_verify(self):
        """Meta Cloud API GET verification handshake — echo hub.challenge on a
        verify-token match, else 403. No-op (403) for providers without a
        handshake (Twilio)."""
        adapter = handlers.server.get_gateway_adapter() if handlers.server.GATEWAY_ENABLED else None
        if adapter is None:
            self.send_response(503)
            self.end_headers()
            return
        raw = self._gateway_raw_request('GET')
        challenge = adapter.handshake(raw) if raw is not None else None
        if challenge is not None:
            self.send_response(200)
            self.send_header('Content-type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(str(challenge).encode('utf-8'))
        else:
            self.send_response(403)
            self.end_headers()

    def _current_bearer_token(self):
        """The workspace Bearer token the app already holds — stored with the
        pairing so revoking/rotating the token also orphans the WhatsApp link."""
        try:
            with open(handlers.server.ClaudeTaskManager.TOKEN_FILE) as f:
                return f.read().strip()
        except OSError:
            return ''

    def handle_gateway_link_create(self):
        """Mint a single-use pairing code (dashboard 'Link WhatsApp'). The user
        sends this code once over WhatsApp to bind their number."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if handlers.server._gateway_disabled(self):
            return
        # Each code is a live, bindable credential for 600s — cap how many can
        # be minted per hour so a compromised session can't spray them.
        if handlers.server._GW_LINK_LIMITER is not None and not handlers.server._GW_LINK_LIMITER.allow('link'):
            self.send_json({'error': 'too many pairing codes — try again later'}, 429)
            return
        gw = handlers.server.get_gateway()
        if gw is None:
            self.send_json({'error': 'gateway unavailable'}, 503)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        workspace = (data.get('workspace') or 'workspace').strip()[:64] or 'workspace'
        host = (data.get('workspace_host') or self.headers.get('Host', '')).strip()
        token = self._current_bearer_token()
        if not token:
            self.send_json({'error': 'workspace has no API token yet'}, 409)
            return
        try:
            code = gw.registry.mint_pairing_code(
                workspace=workspace, workspace_host=host, token=token,
                ttl_seconds=600)
        except Exception as e:
            self.send_json({'error': f'could not mint pairing code: {e}'}, 500)
            return
        self.send_json({
            'code': code,
            'expires_in': 600,
            'whatsapp_number': handlers.server.GATEWAY_WHATSAPP_NUMBER,
            'workspace': workspace,
        }, 201)

    def handle_gateway_link_list(self):
        """List the identity bindings — redacted (no raw number, no token)."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        gw = handlers.server.get_gateway() if handlers.server.GATEWAY_ENABLED else None
        if gw is None:
            # Soft shape (not a 503): the Settings UI renders its "messaging
            # unavailable" state from this rather than surfacing an error toast.
            self.send_json({'links': [], 'available': False})
            return
        self.send_json({
            'links': gw.registry.list_links(),
            'available': True,
            'whatsapp_number': handlers.server.GATEWAY_WHATSAPP_NUMBER,
            'proactive': handlers.server.get_gateway_adapter().capabilities.proactive
                if handlers.server.get_gateway_adapter() else False,
        })

    def handle_gateway_link_delete(self, link_id):
        """Revoke a link by id (== identity hash). The `unlink` keyword does the
        same over WhatsApp."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if handlers.server._gateway_disabled(self):
            return
        gw = handlers.server.get_gateway()
        if gw is None:
            self.send_json({'error': 'gateway unavailable'}, 503)
            return
        ok = gw.registry.revoke(link_id)
        self.send_json({'ok': ok}, 200 if ok else 404)

    # --- Messaging provider config (issue #329) ---
    # The data-driven catalog + per-workspace credential store the Settings
    # "Messaging / WhatsApp" section (stage 3) drives. Credentials are stored on
    # the PVC (0600, redacted in every read), and saving hot-swaps the live
    # adapter so the inbound webhook uses the new provider with no pod restart.

    def handle_gateway_providers(self):
        """The provider catalog + each provider's field spec (issue #328), so the
        Settings form is entirely data-driven. No secrets — just the schema."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if handlers.server.gw_list_providers is None or not handlers.server.GATEWAY_ENABLED:
            # Soft shape so the Settings section renders "not available"
            # instead of erroring when messaging is switched off.
            self.send_json({'providers': [], 'available': False})
            return
        self.send_json({
            'providers': [s.to_dict() for s in handlers.server.gw_list_providers()],
            'available': True,
        })

    def handle_gateway_credentials_get(self):
        """Current selection, redacted (never a secret value)."""
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if handlers.server._gateway_disabled(self):
            return
        self.send_json({'credentials': handlers.server.GatewayCredentialsManager.public_view()})

    def handle_gateway_credentials_put(self):
        """Set provider + creds + sender number, then hot-swap the live adapter.
        Responds with the redacted view (so the client never round-trips a
        secret back)."""
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if handlers.server._gateway_disabled(self):
            return
        # Credential writes share the test bucket — both touch provider config.
        if handlers.server._GW_TEST_LIMITER is not None and not handlers.server._GW_TEST_LIMITER.allow('test'):
            self.send_json({'error': 'too many requests — try again later'}, 429)
            return
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        provider_id = (data.get('provider_id') or '').strip()
        creds = data.get('creds')
        if creds is not None and not isinstance(creds, dict):
            self.send_json({'error': 'creds must be an object'}, 400)
            return
        ok, err = handlers.server.GatewayCredentialsManager.set(
            provider_id, creds or {}, sender_number=data.get('sender_number'))
        if not ok:
            self.send_json({'error': err}, 400)
            return
        handlers.server.rebuild_gateway_adapter()
        self.send_json({'ok': True,
                        'credentials': handlers.server.GatewayCredentialsManager.public_view()})

    def handle_gateway_credentials_delete(self):
        """Clear the store (disables the channel) and hot-swap back to the env
        fallback."""
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if handlers.server._gateway_disabled(self):
            return
        handlers.server.GatewayCredentialsManager.clear()
        handlers.server.rebuild_gateway_adapter()
        self.send_json({'ok': True})

    def handle_gateway_test(self):
        """Validate the stored creds against the provider — no message to a real
        user. 400 when nothing is configured yet."""
        if not self.check_claude_auth(allow_none_mode=False):
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        if handlers.server._gateway_disabled(self):
            return
        # This is the one endpoint that makes an OUTBOUND network call per
        # request, so it's the real abuse vector — throttle it.
        if handlers.server._GW_TEST_LIMITER is not None and not handlers.server._GW_TEST_LIMITER.allow('test'):
            self.send_json({'error': 'too many test requests — try again later'}, 429)
            return
        ok, detail = handlers.server.GatewayCredentialsManager.validate_stored()
        if not ok and detail == 'no credentials configured':
            self.send_json({'ok': False, 'detail': detail}, 400)
            return
        self.send_json({'ok': ok, 'detail': detail})

    # --- Walkie-Talkie preview (in-app loopback) ---
    # Bearer-authed (it's the app user). Drives the SAME gateway core through the
    # loopback adapter so the preview shows exactly what WhatsApp would see —
    # projection, choice→buttons, chunking, ack/final, out-of-window template —
    # while running a real Hypervisor turn locally.

    def _gw_preview_bundle(self):
        """(gateway, preview, loopback) or None if the gateway is unavailable."""
        gw = handlers.server.get_gateway()
        preview = handlers.server.get_gateway_preview()
        loop = handlers.server.get_gateway_loopback()
        if gw is None or preview is None or loop is None:
            return None
        return gw, preview, loop

    def _gw_internal_status(self, gw):
        """(thread_id, busy) for the internal identity's active thread."""
        rec = gw.registry.lookup(handlers.server.INTERNAL_IDENTITY)
        if not rec:
            return None, False
        binding = gw.registry.select_binding(rec)
        thread_id = binding.get('default_thread_id') if binding else None
        busy = False
        if thread_id and handlers.server._HYPERVISOR_AVAILABLE:
            # Live in-process signal only (#474) — never trust thread.json's
            # persisted status, which can be stuck at 'running' forever after a
            # mid-turn crash (#462) and would otherwise pin the Walkie-Talkie
            # orb on THINKING… indefinitely.
            busy = handlers.server.HypervisorSession.is_turn_live(thread_id)
        return thread_id, busy

    def handle_gateway_internal_inbound(self):
        """Send a message into the loopback as if it arrived over WhatsApp."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        bundle = self._gw_preview_bundle()
        if bundle is None:
            self.send_json({'error': 'gateway unavailable'}, 503)
            return
        gw, preview, loop = bundle
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        text = (data.get('text') or '').strip()
        button = (data.get('button') or '').strip()
        display = button or text
        if not display:
            self.send_json({'error': 'text is required'}, 400)
            return
        # Record the user's own bubble + the inbound "wire" (the provider webhook
        # shape WhatsApp would POST) so the UI can show both sides of the wire.
        # Added BEFORE dispatch so it appears instantly, but the thread it
        # belongs to isn't known yet (a `new chat` mints a brand-new one inside
        # handle_inbound) — back-fill it from the result below (#474).
        item = preview.transcript.add('in', display, kind='message', wire={
            'inbound': {'from': handlers.server.INTERNAL_IDENTITY, 'text': text, 'button': button}})
        raw = handlers.server.RawRequest(method='POST', form={
            'from': handlers.server.INTERNAL_IDENTITY, 'text': text, 'button': button})
        result = gw.handle_inbound(loop, raw)
        if result.thread_id:
            preview.transcript.set_meta(item['seq'], {'thread_id': result.thread_id})
        self.send_json({'ok': True, 'action': result.action,
                        'cursor': preview.transcript.cursor()})

    def handle_gateway_internal_transcript(self):
        """Poll the preview transcript (both directions, each with its wire
        payload) since a cursor, plus link/simulate/thread status."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        bundle = self._gw_preview_bundle()
        if bundle is None:
            self.send_json({'available': False, 'messages': []})
            return
        gw, preview, loop = bundle
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            since = int((qs.get('since') or ['0'])[0])
        except (TypeError, ValueError):
            since = 0
        thread_id, busy = self._gw_internal_status(gw)
        self.send_json({
            'available': True,
            'messages': preview.transcript.since(since),
            'cursor': preview.transcript.cursor(),
            'linked': gw.registry.is_linked(handlers.server.INTERNAL_IDENTITY),
            'simulate_out_of_window': preview.simulate_out_of_window,
            'provider': loop.wire_provider.name,
            'identity': handlers.server.INTERNAL_IDENTITY,
            'busy': busy,
            'thread_id': thread_id,
        })

    def handle_gateway_internal_control(self):
        """Link (mint+inject a pairing code so the real enrollment path shows in
        the transcript), toggle the out-of-window simulation, or reset."""
        if not self.check_claude_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return
        bundle = self._gw_preview_bundle()
        if bundle is None:
            self.send_json({'error': 'gateway unavailable'}, 503)
            return
        gw, preview, loop = bundle
        try:
            data = self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return
        action = (data.get('action') or '').strip()
        if action == 'link':
            if gw.registry.is_linked(handlers.server.INTERNAL_IDENTITY):
                self.send_json({'ok': True, 'linked': True})
                return
            token = self._current_bearer_token()
            if not token:
                self.send_json({'error': 'workspace has no API token yet'}, 409)
                return
            code = gw.registry.mint_pairing_code(
                workspace=preview.workspace,
                workspace_host=self.headers.get('Host', ''), token=token)
            # Inject the code as a loopback inbound so the REAL pairing path runs
            # and the code→"✅ Linked" exchange is visible in the transcript.
            preview.transcript.add('in', code, kind='notice', wire={
                'inbound': {'from': handlers.server.INTERNAL_IDENTITY, 'text': code}})
            gw.handle_inbound(loop, handlers.server.RawRequest(form={
                'from': handlers.server.INTERNAL_IDENTITY, 'text': code}))
            self.send_json({'ok': True,
                            'linked': gw.registry.is_linked(handlers.server.INTERNAL_IDENTITY)})
        elif action == 'simulate':
            preview.simulate_out_of_window = bool(data.get('on'))
            self.send_json({'ok': True,
                            'simulate_out_of_window': preview.simulate_out_of_window})
        elif action == 'reset':
            gw.registry.revoke_identity(handlers.server.INTERNAL_IDENTITY)
            preview.transcript.clear()
            preview.simulate_out_of_window = False
            self.send_json({'ok': True})
        else:
            self.send_json({'error': "action must be 'link', 'simulate', or 'reset'"}, 400)


#: Consulted by all four verbs where this domain's (contiguous) branches sat.
ROUTES = RouteTable()

#: A link id is the sha256 of the identity it binds — 64 lower-case hex.
_LINK = r'([a-f0-9]{64})'

# --- reads ---------------------------------------------------------------
# The Meta GET verify handshake. NO auth: the provider cannot carry a session.
ROUTES.add('GET', '/api/gateway/whatsapp/webhook',
           'handle_gateway_whatsapp_verify')
ROUTES.add('GET', '/api/gateway/links', 'handle_gateway_link_list')
ROUTES.add('GET', '/api/gateway/providers', 'handle_gateway_providers')
ROUTES.add('GET', '/api/gateway/credentials', 'handle_gateway_credentials_get')
ROUTES.add('GET', '/api/gateway/internal/transcript',
           'handle_gateway_internal_transcript')

# --- writes --------------------------------------------------------------
# Inbound webhook: provider-signature authed, NOT bearer.
ROUTES.add('POST', '/api/gateway/whatsapp/webhook',
           'handle_gateway_whatsapp_webhook')
ROUTES.add('POST', '/api/gateway/link', 'handle_gateway_link_create')
ROUTES.add('POST', '/api/gateway/test', 'handle_gateway_test')
# Walkie-Talkie in-app loopback preview (bearer-authed).
ROUTES.add('POST', '/api/gateway/internal/inbound',
           'handle_gateway_internal_inbound')
ROUTES.add('POST', '/api/gateway/internal/control',
           'handle_gateway_internal_control')

# --- credentials replace + deletes ---------------------------------------
ROUTES.add('PUT', '/api/gateway/credentials',
           'handle_gateway_credentials_put')
ROUTES.add('DELETE', re.compile(rf'^/api/gateway/link/{_LINK}$'),
           'handle_gateway_link_delete')
ROUTES.add('DELETE', '/api/gateway/credentials',
           'handle_gateway_credentials_delete')
