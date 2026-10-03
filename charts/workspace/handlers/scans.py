"""Security scans: the HTTP surface (#726).

Thin on purpose. Every decision lives in `scans.py` (what a scan is) and
`strix_connection.py` (how the scanner reaches a model); this module turns
requests into calls on those and shapes the replies. A handler here that grew
a rule would be a rule the unit suites cannot reach.

## No ordering hazard, and that is by construction

Most domains in this package carry a comment about which route must precede
which, because an id pattern like `([a-zA-Z0-9_-]+)` also matches a literal
sub-resource name. A scan id is `scn_` plus twelve lower-case hex-ish
characters, so `targets` and `connection` cannot be read as ids no matter what
order the table is in. The tight pattern is the reason, and it is asserted in
`tests/scans_routes_table_test.py` so it stays true.

## Why the connection routes refuse the unauthenticated demo mode

`check_claude_auth(allow_none_mode=False)` on everything under
`/api/scans/connection`: those routes read and write the workspace owner's
model settings, and the read-only public demo runs with no identity at all.
Every other route uses the ordinary gate.
"""

import json
import re

import handlers
import scans
import strix_connection
from handlers.routing import RouteTable


class ScanRoutes:
    """BrowserHandler methods backing `ROUTES`."""

    # ── gates ──────────────────────────────────────────────────────────────

    def _scan_guard(self, *, allow_none_mode=True):
        """Shared entry check. False means the reply has already been sent."""
        if not self.check_claude_auth(allow_none_mode=allow_none_mode):
            self.send_json({'error': 'Unauthorized'}, 401)
            return False
        if not handlers.server.SCANS_ENABLED:
            self.send_json({
                'error': 'Security scanning is switched off in this '
                         'workspace. It needs to be enabled by whoever '
                         'deployed it.',
                'code': 'disabled',
            }, 503)
            return False
        return True

    def _scan_or_404(self, scan_id):
        record = scans.ScansManager.get(scan_id)
        if record is None:
            self.send_json({'error': 'No such scan.'}, 404)
            return None
        return record

    def _body_or_400(self):
        """Parsed JSON body, or None once a 400 has been sent."""
        try:
            return self.read_json_body()
        except (json.JSONDecodeError, ValueError):
            self.send_json({'error': 'Invalid JSON body'}, 400)
            return None

    # ── reads ──────────────────────────────────────────────────────────────

    def handle_scans_list(self):
        if not self._scan_guard():
            return
        self.send_json({'scans': scans.ScansManager.list_scans()})

    def handle_scan_targets(self):
        """The apps a scan may point at, plus why one cannot be reached.

        The warning matters more than the list: an app listening only on
        loopback is invisible to the scanner, which then reports no problems
        having tested nothing.
        """
        if not self._scan_guard():
            return
        self.send_json({'targets': scans.ScansManager.targets()})

    def handle_scan_get(self, scan_id):
        if not self._scan_guard():
            return
        record = self._scan_or_404(scan_id)
        if record is None:
            return
        view = scans.public_view(record)
        view['summary'] = scans.result_summary(record)
        self.send_json(view)

    # ── running a scan ─────────────────────────────────────────────────────

    def handle_scan_create(self):
        """Start a scan and answer immediately with its id.

        Nothing here waits for the scan: it runs for minutes to hours, and the
        request that started it is answered in well under a second.
        """
        if not self._scan_guard():
            return
        body = self._body_or_400()
        if body is None:
            return

        connection = strix_connection.connection_view()
        if not connection['configured']:
            self.send_json({
                'error': 'No model is connected yet, so a scan cannot run.',
                'code': 'not_connected',
            }, 409)
            return

        spec, error = scans.validate_create(
            body, scans.ScansManager.targets(),
            connected_model=connection['model'])
        if error:
            self.send_json({'error': error}, 400)
            return

        record, error = scans.ScansManager.create(spec)
        if error:
            self.send_json({'error': error}, 502)
            return
        self.send_json({'scan_id': record['id'], 'status': record['status'],
                        'target': record['target']}, 202)

    def handle_scan_stop(self, scan_id):
        if not self._scan_guard():
            return
        if self._scan_or_404(scan_id) is None:
            return
        record = scans.ScansManager.stop(scan_id)
        if record is None:
            # A concurrent DELETE can land between the check above and here,
            # in which case stop() returns None and the attribute access
            # below would 500 on a request that simply lost a race.
            self.send_json({'error': 'No such scan.'}, 404)
            return
        self.send_json({'ok': True, 'status': record.get('status')})

    def handle_scan_delete(self, scan_id):
        if not self._scan_guard():
            return
        if self._scan_or_404(scan_id) is None:
            return
        self.send_json({'ok': True,
                        'removed': scans.ScansManager.delete(scan_id)})

    def handle_scan_disposition(self, scan_id, finding_id):
        """Mark one finding dismissed, or put it back.

        Recorded beside the scanner's findings, never inside them — what the
        tool asserted and what the user decided about it are different facts.
        """
        if not self._scan_guard():
            return
        if self._scan_or_404(scan_id) is None:
            return
        body = self._body_or_400()
        if body is None:
            return
        record, error = scans.ScansManager.set_disposition(
            scan_id, finding_id, (body.get('disposition') or '').strip())
        if error:
            self.send_json({'error': error}, 400)
            return
        self.send_json({'ok': True,
                        'dispositions': record.get('dispositions') or {}})

    # ── connecting a model ─────────────────────────────────────────────────

    def handle_scan_connection_get(self):
        """Everything the Connect screen needs, and never the key itself."""
        if not self._scan_guard(allow_none_mode=False):
            return
        view = dict(strix_connection.connection_view())
        view['install'] = strix_connection.install_state()
        view['backend'] = scans.ScansManager.backend().preflight()
        self.send_json(view)

    def handle_scan_connection_set(self):
        """Save the model, key and optional server address.

        A field left out of the body is left as it was, so changing the model
        does not require the browser to hold the key and send it back.
        Installing the scanner starts here, in the background, because this is
        the first moment we know the workspace actually wants it.
        """
        if not self._scan_guard(allow_none_mode=False):
            return
        body = self._body_or_400()
        if body is None:
            return
        view, error = strix_connection.save_connection(
            model=body.get('model'),
            api_key=body.get('api_key'),
            api_base=body.get('api_base'))
        if error:
            self.send_json({'error': error}, 400)
            return
        view = dict(view)
        view['install'] = strix_connection.ensure_installed()
        self.send_json(view)

    def handle_scan_connection_clear(self):
        if not self._scan_guard(allow_none_mode=False):
            return
        self.send_json(strix_connection.clear_connection())

    # ── signing in to a subscription ───────────────────────────────────────
    # Four steps rather than one call, because the user completes the sign-in
    # in their OWN browser: this pod cannot receive the callback, so the
    # dashboard opens the link, the user pastes back where they landed, and we
    # poll until the scanner says it worked. Same shape as the Claude sign-in
    # in handlers/settings.py, for the same reason.

    def handle_scan_signin_start(self):
        if not self._scan_guard(allow_none_mode=False):
            return
        try:
            self.send_json(strix_connection.SubscriptionSignIn.start(), 200)
        except RuntimeError as e:
            self.send_json({'error': str(e)}, 502)

    def handle_scan_signin_submit(self):
        """Forward what the user pasted to the waiting sign-in.

        Single-use and never logged — this handler moves it and keeps nothing.
        """
        if not self._scan_guard(allow_none_mode=False):
            return
        body = self._body_or_400()
        if body is None:
            return
        ok, error = strix_connection.SubscriptionSignIn.submit(
            body.get('redirect'))
        if not ok:
            self.send_json({'error': error}, 400)
            return
        self.send_json({'ok': True})

    def handle_scan_signin_poll(self):
        if not self._scan_guard(allow_none_mode=False):
            return
        self.send_json(strix_connection.SubscriptionSignIn.poll(), 200)

    def handle_scan_signin_cancel(self):
        if not self._scan_guard(allow_none_mode=False):
            return
        strix_connection.SubscriptionSignIn.cancel()
        self.send_json({'ok': True})

    def handle_scan_signout(self):
        """Ask the scanner to forget its subscription credentials."""
        if not self._scan_guard(allow_none_mode=False):
            return
        self.send_json({'ok': strix_connection.sign_out_subscription(),
                        'connection': strix_connection.connection_view()})

    def handle_scan_connection_test(self):
        """Ask the provider whether these settings actually work.

        A real request rather than a shape check: the failures that matter —
        a key over its spending limit, a model name that does not exist on
        this provider — all look perfectly well-formed.
        """
        if not self._scan_guard(allow_none_mode=False):
            return
        ok, detail = strix_connection.test_connection()
        self.send_json({'ok': ok, 'detail': detail})


ROUTES = RouteTable()

#: A scan id is `scn_` + 12 lower-case alphanumerics, and this pattern is the
#: same shape `scans.valid_scan_id` enforces. Because it cannot match a word
#: like `targets` or `connection`, this table has no ordering hazard — the one
#: thing most other domains in this package need a comment about.
_SCAN = r'/api/scans/(scn_[a-z0-9]{12})'
_FINDING = r'([A-Za-z0-9_.:-]{1,128})'

# --- reads ----------------------------------------------------------------
ROUTES.add('GET', '/api/scans', 'handle_scans_list')
ROUTES.add('GET', '/api/scans/targets', 'handle_scan_targets')
ROUTES.add('GET', '/api/scans/connection', 'handle_scan_connection_get')
ROUTES.add('GET', re.compile(rf'^{_SCAN}$'), 'handle_scan_get')

# --- writes ---------------------------------------------------------------
ROUTES.add('POST', '/api/scans', 'handle_scan_create')
ROUTES.add('POST', '/api/scans/connection', 'handle_scan_connection_set')
ROUTES.add('POST', '/api/scans/connection/test', 'handle_scan_connection_test')
# Subscription sign-in. `/signin/...` cannot collide with `/test` or with a
# scan id, so these need no particular position in the table.
ROUTES.add('POST', '/api/scans/connection/signin/start',
           'handle_scan_signin_start')
ROUTES.add('POST', '/api/scans/connection/signin/submit',
           'handle_scan_signin_submit')
ROUTES.add('POST', '/api/scans/connection/signin/poll',
           'handle_scan_signin_poll')
ROUTES.add('POST', '/api/scans/connection/signin/cancel',
           'handle_scan_signin_cancel')
ROUTES.add('POST', '/api/scans/connection/signout', 'handle_scan_signout')
ROUTES.add('POST', re.compile(rf'^{_SCAN}/stop$'), 'handle_scan_stop')
ROUTES.add('POST',
           re.compile(rf'^{_SCAN}/findings/{_FINDING}/disposition$'),
           'handle_scan_disposition')

# --- deletes --------------------------------------------------------------
# Stopping and removing are separate verbs, as they are for board runs: a
# stopped scan keeps its findings, and removing one throws them away.
ROUTES.add('DELETE', '/api/scans/connection', 'handle_scan_connection_clear')
ROUTES.add('DELETE', re.compile(rf'^{_SCAN}$'), 'handle_scan_delete')
