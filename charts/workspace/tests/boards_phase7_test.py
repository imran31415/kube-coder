"""Strategies, templates and metrics (#588 Phase 7).

Three things the epic asks for under "advanced use", and the reason each is
here rather than obvious:

- **Strategies** were 80% built and 0% reachable. `validate_select` already
  accepted status / priority / tags / unassigned / updated_since / query and
  three orderings; the run form sent `{limit, order}`. So this is mostly about
  reaching it — plus `preview`, because the alternative to knowing what a
  selection will do is spending twenty agents to find out.

- **Templates** must never claim to be verified. Only `test-fetch` earns that.

- **Metrics** read the decision LEDGER, not the review queue, because the queue
  overwrites decided records. `test_approval_rate_survives_the_queue_being_...`
  is the load-bearing one.

Run:  python3 -m unittest tests.boards_phase7_test  (from charts/workspace/)
"""

import copy
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

try:
    import fcntl  # noqa: F401
except ImportError:  # pragma: no cover - platform shim
    import types
    _shim = types.ModuleType('fcntl')
    _shim.flock = lambda *a, **k: None
    _shim.lockf = lambda *a, **k: None
    _shim.LOCK_EX = _shim.LOCK_UN = _shim.LOCK_SH = _shim.LOCK_NB = 0
    _shim._kube_coder_shim = True
    sys.modules['fcntl'] = _shim

import safe_http  # noqa: E402
import server  # noqa: E402
from boards import schema as bschema, templates as btemplates  # noqa: E402
from tests import board_fixtures as fx  # noqa: E402

BM = server.BoardsManager
BCM = server.BoardCredentialsManager
RM = server.BoardRunsManager
VM = server.BoardReviewManager
SM = server.BoardStrategiesManager
MM = server.BoardMetricsManager

APPROVAL = 'a1b2c3d4-e5f6-4711-8899-aabbccddeeff'


def J(obj, status=200, headers=None):
    return (status, headers or {}, json.dumps(obj).encode('utf-8'))


def issue(item_id='46', summary='Refund not received', status='To Do',
          priority='High', labels=None, assignee=True):
    fields = {'summary': summary, 'description': 'Dana says so.',
              'status': {'name': status}, 'priority': {'name': priority},
              'labels': labels if labels is not None else [],
              'updated': '2026-02-01', 'created': '2026-01-01'}
    if assignee:
        fields['assignee'] = {'accountId': 'u1', 'displayName': 'Sam'}
    return {'id': item_id, 'key': f'SUP-{item_id}', 'fields': fields}


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = os.path.realpath(tempfile.mkdtemp(prefix='kc-p7-'))
        # A board run refuses to start without a usable task-API
        # token (#633); model a configured workspace. See
        # board_fixtures.workspace_token_patch.
        _tok = fx.workspace_token_patch()
        _tok.start()
        cls.addClassCleanup(_tok.stop)

        cls._saved_home, BM.HOME_ROOT = BM.HOME_ROOT, cls.tmpdir
        cls._saved_cred, BCM.HOME_ROOT = BCM.HOME_ROOT, cls.tmpdir
        cls._auth_save = server.BrowserHandler.check_claude_auth
        server.BrowserHandler.check_claude_auth = lambda self: True
        cls._safe_save = safe_http.is_safe_url
        safe_http.is_safe_url = lambda url, **kw: True
        cls.httpd = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0), server.BrowserHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        BM.HOME_ROOT = cls._saved_home
        BCM.HOME_ROOT = cls._saved_cred
        server.BrowserHandler.check_claude_auth = cls._auth_save
        safe_http.is_safe_url = cls._safe_save
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        shutil.rmtree(BM.boards_dir(), ignore_errors=True)
        try:
            os.remove(BCM.creds_file())
        except OSError:
            pass
        BCM.set('JIRA_API_TOKEN', 'secret-token')
        self.responses = []
        self.calls = []

        def fake(url, *, method='GET', headers=None, body=None, timeout=30,
                 allow_internal=False):
            self.calls.append({'url': url, 'method': method, 'body': body})
            if not self.responses:
                raise AssertionError(f'no stubbed response for {method} {url}')
            return self.responses.pop(0)

        p = mock.patch.object(safe_http, 'fetch', fake)
        p.start()
        self.addCleanup(p.stop)
        for name, fn in (('create_task',
                          lambda prompt, **kw: {'status': 'running',
                                                'task_id': 'task-1'}),
                         ('task_status', lambda t: 'running'),
                         ('count_live_tasks', lambda: 0)):
            p = mock.patch.object(server.ClaudeTaskManager, name, fn)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(RM, '_spawn_driver',
                              classmethod(lambda cls, r: None))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(server.FeedManager, 'emit', lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {'Content-Type': 'application/json'} if data else {}
        r = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}',
                                   data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw

    def _board(self, board_id='acme-jira'):
        cfg = copy.deepcopy(fx.JIRA)
        cfg['id'] = board_id
        saved, err = BM.create_or_update(cfg)
        self.assertIsNone(err, err)
        return saved


# ── templates ──────────────────────────────────────────────────────────────

class TemplateTests(_Base):
    def test_every_template_validates(self):
        """A template that could not be saved is a worse starting point than a
        blank page."""
        for tid, cfg in btemplates.TEMPLATES.items():
            _cleaned, errors = bschema.validate_connector(dict(cfg, id=tid))
            self.assertEqual(errors, [], f'{tid}: {errors}')

    def test_the_three_the_issue_names_are_present(self):
        self.assertEqual(sorted(btemplates.TEMPLATES),
                         ['github-issues', 'jira-cloud', 'zendesk'])

    def test_a_template_never_claims_to_be_verified(self):
        """Only test-fetch earns that word — the same discipline the connector
        authoring preamble imposes on an agent writing one from scratch."""
        for entry in btemplates.listing():
            self.assertIs(entry['verified'], False)
            self.assertIn('not a verified connector', entry['note'])

    def test_every_template_says_what_it_needs_filled_in(self):
        for entry in btemplates.listing():
            self.assertTrue(entry['needs'],
                            f'{entry["id"]} must say what to replace')

    def test_the_listing_route_works(self):
        status, body = self._req('GET', '/api/boards/templates')
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body['templates']), 3)

    def test_one_template_can_be_fetched_whole(self):
        status, body = self._req('GET', '/api/boards/templates/zendesk')
        self.assertEqual(status, 200, body)
        self.assertEqual(body['connector']['vendor'], 'zendesk')
        self.assertIs(body['verified'], False)

    def test_the_fill_route_returns_a_connector_create_then_accepts(self):
        """The whole connect flow in two calls: fill, then create. Anything
        `fill` returns that `create` would reject is a dead end a user reaches
        only after typing everything in."""
        status, body = self._req(
            'POST', '/api/boards/templates/jira-cloud/fill',
            {'values': {'YOURSITE': 'acme', 'PROJ': 'SUP'}})
        self.assertEqual(status, 200, body)
        self.assertIs(body['verified'], False)
        status, created = self._req('POST', '/api/boards', body['connector'])
        self.assertEqual(status, 201, created)
        self.assertEqual(created['vendor'], 'jira')
        self.assertEqual(created['id'], 'jira-sup')

    def test_the_github_template_needs_the_workspace_app_token_to_EXIST(self):
        """GitHub asks for no credential because the workspace brokers one —
        which is a better story right up until the workspace has not got one,
        and then a form with nothing to fill in produces a board that cannot be
        created at all.

        Pinned as a real precondition rather than a surprise: the create route
        refuses, in the one sentence that explains why, and the connect form
        shows that sentence instead of a bare 400.
        """
        status, body = self._req(
            'POST', '/api/boards/templates/github-issues/fill',
            {'values': {'OWNER': 'acme', 'REPO': 'billing-api'}})
        self.assertEqual(status, 200, body)
        status, created = self._req('POST', '/api/boards', body['connector'])
        self.assertEqual(status, 400)
        self.assertIn('GitHub App token', created['error'])

    def test_filling_is_matched_before_the_board_id_routes(self):
        """`templates` is reserved precisely so this cannot become a board
        called "templates" swallowing its own sub-route."""
        self.assertIn('templates', bschema.RESERVED_BOARD_IDS)

    def test_an_unanswered_placeholder_is_a_400_not_a_broken_board(self):
        status, body = self._req(
            'POST', '/api/boards/templates/github-issues/fill',
            {'values': {'OWNER': 'acme'}})
        self.assertEqual(status, 400, body)
        self.assertIn('Repository', body['error'])

    def test_filling_an_unknown_template_is_404(self):
        status, _body = self._req('POST', '/api/boards/templates/trello/fill',
                                  {'values': {}})
        self.assertEqual(status, 404)

    def test_an_unknown_template_is_404(self):
        status, _b = self._req('GET', '/api/boards/templates/servicenow')
        self.assertEqual(status, 404)

    def test_templates_is_a_RESERVED_board_id(self):
        """Otherwise a board called `templates` would shadow the route and be
        permanently unreachable."""
        for reserved in ('templates', 'strategies', 'metrics'):
            self.assertIn(reserved, bschema.RESERVED_BOARD_IDS)
            cfg = copy.deepcopy(fx.JIRA)
            cfg['id'] = reserved
            _saved, err = BM.create_or_update(cfg)
            self.assertIsNotNone(err, f'{reserved} should be refused')

    # ── defects real boards found that validation could not ────────────────

    def test_jira_asks_for_FIELDS_explicitly(self):
        """`/rest/api/3/search/jql` returns ONLY `id` when `fields` is absent —
        no key, no summary, no status — so every mapped field resolves to null
        and the board lists rows that look like empty tickets. It also keeps
        handing back a nextPageToken, so the walk never terminates.

        Nothing about that response is an error, which is exactly why no amount
        of schema validation catches it. Found against a real Jira site.
        """
        query = btemplates.JIRA_CLOUD['list']['request']['query']
        self.assertIn('fields', query,
                      'without this every Jira item maps to nulls')
        for field in ('summary', 'status', 'priority', 'assignee', 'updated'):
            self.assertIn(field, query['fields'], field)

    def test_every_jira_mapped_field_is_actually_requested(self):
        """The map and the `fields` list have to agree, or a field is mapped
        from a path the vendor was never asked to send."""
        cfg = btemplates.JIRA_CLOUD
        requested = set(cfg['list']['request']['query']['fields'].split(','))

        def paths(spec):
            if isinstance(spec, str):
                return [spec]
            if isinstance(spec, dict):
                out = []
                for key, val in spec.items():
                    if key in ('template', 'sep'):
                        continue
                    out += paths(val) if not isinstance(val, list) else [
                        p for v in val for p in paths(v)]
                return out
            return []

        for name, spec in cfg['map'].items():
            for path in paths(spec):
                if not path.startswith('fields.'):
                    continue
                top = path.split('.')[1]
                self.assertIn(top, requested,
                              f'map.{name} reads fields.{top}, which the list '
                              f'request never asks for')

    def test_jira_posts_comments_as_ADF_not_a_plain_string(self):
        """Jira REST v3 requires ADF (Atlassian Document Format) — a structured
        document. A plain string is refused with HTTP 400 `"Comment body is not
        valid!"`, an ordinary-looking failure no schema check can anticipate.
        Found approving a real staged comment against a live Jira site."""
        body = btemplates.JIRA_CLOUD['actions']['comment']['steps'][0]['body']
        doc = body['body']
        self.assertIsInstance(doc, dict, 'a plain string is refused by Jira v3')
        self.assertEqual(doc['type'], 'doc')
        self.assertEqual(doc['version'], 1)
        texts = [n['text'] for p in doc['content'] for n in p['content']]
        self.assertIn('${params.body}', texts)
        # The marker gets its OWN paragraph: ADF text nodes do not render "\n"
        # as a break, so concatenating would put bookkeeping on the same visual
        # line as the sentence a customer reads.
        self.assertIn('${marker}', texts)
        self.assertNotIn('${params.body}\n\n${marker}', texts)

    def test_github_covers_REOPENED(self):
        """`state_reason` is joined onto `state`, so every combination the
        vendor can emit needs covering. `open+reopened` is an ordinary state —
        any issue anyone has ever reopened — and leaving it out normalized a
        perfectly normal ticket to nothing. Found against a real repo where two
        of six issues were reopened."""
        statuses = btemplates.GITHUB_ISSUES['enums']['status']
        self.assertIn('open+reopened', statuses['OPEN'])
        self.assertIn('closed+completed', statuses['CLOSED'])
        self.assertIn('closed+not_planned', statuses['CLOSED'])

    def test_zendesk_maps_pending_to_ON_HOLD_not_IN_PROGRESS(self):
        """Zendesk's `pending` means "waiting on the requester". An agent must
        not treat a ticket blocked on the customer as work in flight."""
        z = btemplates.TEMPLATES['zendesk']
        self.assertIn('pending', z['enums']['status']['ON_HOLD'])
        self.assertNotIn('pending', z['enums']['status']['IN_PROGRESS'])

    def test_no_template_lists_one_vendor_value_under_two_buckets(self):
        """`_normalize_enum` returns the FIRST bucket that matches, so a value
        listed twice does not mean "either" — the later bucket is unreachable.

        Zendesk had `open` under both OPEN and IN_PROGRESS, which read as
        covering both and in fact meant IN_PROGRESS could never be produced for
        any ticket on the board. A dead branch is worse than a missing one: an
        absent mapping shows up as `(unmapped)` in the raw passthrough, while
        this one silently answers with the wrong bucket.
        """
        for tid, cfg in btemplates.TEMPLATES.items():
            for field, buckets in (cfg.get('enums') or {}).items():
                seen = {}
                for bucket, raws in buckets.items():
                    for raw in raws:
                        key = str(raw).lower()
                        self.assertNotIn(
                            key, seen,
                            f'{tid}.{field}: {raw!r} is under both '
                            f'{seen.get(key)} and {bucket}; only '
                            f'{seen.get(key)} can ever be produced')
                        seen[key] = bucket

    def test_zendesk_separates_new_from_open(self):
        """Zendesk has no distinct in-progress status: `new` is untriaged and
        `open` is assigned and being worked. Mapping both to OPEN throws away
        the only signal the vendor gives about whether anyone has picked the
        ticket up — which is precisely what a selection strategy filters on."""
        statuses = btemplates.TEMPLATES['zendesk']['enums']['status']
        self.assertEqual(statuses['OPEN'], ['new'])
        self.assertEqual(statuses['IN_PROGRESS'], ['open'])

    def test_every_interpolated_param_is_declared_required(self):
        """A request body interpolates STRICTLY — `${params.x}` with no `x`
        raises rather than rendering nothing. So an optional parameter that the
        body references is not optional at all; it is a landmine that goes off
        at approve time, in front of the reviewer, reported as an
        `unresolved interpolation token` they can do nothing about.

        Zendesk's `public` was declared optional and referenced in the comment
        body. Either the parameter is required or the body must not mention it.
        """
        for tid, cfg in btemplates.TEMPLATES.items():
            for name, action in (cfg.get('actions') or {}).items():
                declared = action.get('params') or {}
                blob = json.dumps(action.get('steps') or [])
                for pname, spec in declared.items():
                    if f'${{params.{pname}}}' not in blob:
                        continue
                    self.assertTrue(
                        spec.get('required'),
                        f'{tid}.{name}: body interpolates ${{params.{pname}}} '
                        f'but the parameter is optional — omitting it raises '
                        f'at approve time, not at stage time')

    def test_zendesk_authenticates_with_a_bearer_not_an_api_token(self):
        """Zendesk has withdrawn API-token creation from the admin UI — a new
        account has no "Add API token" button — and retires existing tokens on
        2027-04-30. The Basic `email/token:APITOKEN` scheme that every Zendesk
        integration guide still shows is therefore unobtainable for anyone
        setting up today and expiring for everyone else.

        Found by trying to get credentials for the live leg and discovering the
        button does not exist. A template nobody can authenticate is not a
        starting point, so this pins the scheme.
        """
        z = btemplates.TEMPLATES['zendesk']
        self.assertEqual(z['auth'].get('kind'), 'bearer')
        blob = json.dumps(z)
        self.assertNotIn('/token', blob,
                         'the retired Basic username suffix must not return')
        self.assertNotIn('Basic ', blob)
        self.assertEqual(z['credential_ref'], '@board-creds/ZENDESK_OAUTH_TOKEN')
        # The setup steps have to say how to mint one, because it is a browser
        # flow rather than a settings page anyone would find by looking.
        needs = ' '.join(btemplates.NEEDS['zendesk'])
        self.assertIn('OAuth', needs)
        self.assertIn('ACCESS TOKEN', needs)

    def test_the_zendesk_setup_steps_describe_PKCE_not_the_implicit_grant(self):
        """The instructions have to be runnable by the person reading them.

        The first version pointed at `response_type=token`, which is what every
        guide shows and what a Public client — the only kind the Zendesk admin
        UI creates — is not permitted to use. It refuses in a way that reads
        like a misconfigured client, so a new user's most likely next move is
        to go re-check settings that were right all along. Instructions that
        cannot work are the same defect as a template that cannot authenticate;
        this is the guard for it.
        """
        needs = ' '.join(btemplates.NEEDS['zendesk'])
        self.assertIn('PKCE', needs)
        self.assertIn('zendesk_oauth.py', needs)
        # The implicit grant may be NAMED — warning someone off the thing every
        # other guide tells them to do is worth a line — but never handed over
        # as the instruction.
        for step in btemplates.NEEDS['zendesk']:
            if 'response_type=token' in step:
                self.assertIn('NOT', step,
                              'the implicit grant is offered as a step')
        # And the helper the steps name has to actually be there, with both
        # halves of the exchange — a broken command in setup instructions is
        # indistinguishable to the reader from a broken product.
        path = os.path.join(os.path.dirname(HERE), 'boards', 'zendesk_oauth.py')
        self.assertTrue(os.path.exists(path), 'the steps name a script that '
                                              'does not ship')
        with open(path, encoding='utf-8') as f:
            src = f.read()
        self.assertIn("'start'", src)
        self.assertIn("'finish'", src)
        self.assertIn('S256', src)

    def test_zendesk_declares_a_per_ticket_write_cap(self):
        """30 per 10 minutes per user per ticket is the documented cap, and the
        reason `per_action` exists rather than one global limit."""
        limits = btemplates.TEMPLATES['zendesk']['limits']
        self.assertEqual(limits['per_action']['comment']['max_events'], 30)
        self.assertEqual(limits['per_action']['comment']['window_seconds'], 600)

    # ── connecting a board from a template ─────────────────────────────────
    #
    # The templates existed but nothing could fill them in: the connect form
    # needs to know WHICH strings are blanks, and prose in `needs` is not
    # something a form can read. These pin the machine-readable half.

    def test_every_declared_placeholder_actually_occurs_in_its_template(self):
        """A declared token that is not in the connector renders a form field
        that changes nothing — the operator answers it, the answer is
        discarded, and the board fetches from OWNER/REPO."""
        for tid, spec in btemplates.PLACEHOLDERS.items():
            blob = json.dumps(btemplates.TEMPLATES[tid])
            for p in spec:
                self.assertIn(p['token'], blob,
                              f'{tid}: declares {p["token"]!r} but no such '
                              f'text is in the connector')

    def test_no_template_carries_an_UNDECLARED_placeholder(self):
        """The other direction, and the one that ships a broken board.

        An undeclared blank is never asked about, so it survives `fill` intact
        and the connector points at somebody else's repository — or, for Jira,
        at the literal project key `PROJ`. The failure is a 404 at the first
        fetch with nothing in it that says "you were never asked for this".
        """
        vocabulary = {p['token'] for spec in btemplates.PLACEHOLDERS.values()
                      for p in spec}
        for tid, cfg in btemplates.TEMPLATES.items():
            declared = {p['token'] for p in btemplates.PLACEHOLDERS.get(tid, [])}
            blob = json.dumps(cfg)
            for token in vocabulary - declared:
                self.assertNotIn(token, blob,
                                 f'{tid}: contains {token!r} but never asks '
                                 f'for it')

    def test_fill_leaves_no_placeholder_behind(self):
        for tid, spec in btemplates.PLACEHOLDERS.items():
            values = {p['token']: p['example'] for p in spec}
            cfg, err = btemplates.fill(tid, values)
            self.assertIsNone(err, f'{tid}: {err}')
            blob = json.dumps(cfg)
            for p in spec:
                self.assertNotIn(p['token'], blob)

    def test_a_filled_template_is_a_connector_the_product_would_save(self):
        """Filling in blanks must not produce something `create` then rejects.
        Running the real validator is the only way to know that."""
        for tid, spec in btemplates.PLACEHOLDERS.items():
            cfg, err = btemplates.fill(
                tid, {p['token']: p['example'] for p in spec})
            self.assertIsNone(err)
            ok, errors = bschema.validate_connector(cfg)
            self.assertTrue(ok, f'{tid}: {errors}')

    def test_a_value_containing_another_token_is_not_rewritten(self):
        """One substitution pass, not one per token.

        `fill` replaces OWNER and REPO in the same sweep. Two sequential
        `replace` calls would substitute OWNER first and then go looking for
        REPO inside the text it had just inserted — so the owner `REPOhub`
        would come out as `billing-apihub`. Contrived-looking and completely
        silent, which is the combination worth a test.
        """
        cfg, err = btemplates.fill(
            'github-issues', {'OWNER': 'REPOhub', 'REPO': 'billing-api'})
        self.assertIsNone(err)
        self.assertIn('/repos/REPOhub/billing-api/issues',
                      cfg['list']['request']['url'])

    def test_a_value_that_could_rewrite_the_query_is_REFUSED(self):
        """Placeholders sit inside URL paths and inside Jira's JQL string, so a
        value carrying a space or a slash does not fail — it succeeds against a
        different board. `PROJ` = `X ORDER BY created` is a valid JQL query
        over somebody else's project, and a slash in `REPO` walks the path.

        There is no way to escape a value for a URL segment and JQL at once, so
        restricting what a value may contain is the whole defence.
        """
        for bad in ('X ORDER BY created', 'a/b', 'x"y', "o'r", 'a\nb', ''):
            cfg, err = btemplates.fill('jira-cloud',
                                       {'YOURSITE': 'acme', 'PROJ': bad})
            self.assertIsNone(cfg, f'{bad!r} was accepted')
            self.assertTrue(err)

    def test_fill_names_the_board_after_the_answers(self):
        """Three boards all called "GitHub Issues" is a rail nobody can use."""
        cfg, _ = btemplates.fill('github-issues',
                                 {'OWNER': 'acme', 'REPO': 'billing-api'})
        self.assertIn('acme', cfg['display_name'])
        self.assertIn('billing-api', cfg['display_name'])
        self.assertEqual(cfg['id'], 'github-billing-api')
        # An explicit choice still wins.
        cfg, _ = btemplates.fill('github-issues',
                                 {'OWNER': 'acme', 'REPO': 'billing-api'},
                                 board_id='support', display_name='Support')
        self.assertEqual(cfg['id'], 'support')
        self.assertEqual(cfg['display_name'], 'Support')

    def test_the_credential_spec_agrees_with_the_connector(self):
        """The form asks for what the connector will send. If they disagree the
        operator pastes a perfectly good secret in the wrong shape and gets a
        401 at the first fetch, with nothing anywhere saying which half is
        wrong."""
        for tid, cfg in btemplates.TEMPLATES.items():
            spec = btemplates.CREDENTIAL.get(tid)
            ref = cfg.get('credential_ref') or ''
            if not ref.startswith('@board-creds/'):
                # Brokered (@workspace-github) — there is nothing to paste, and
                # asking for something would be worse than asking for nothing.
                self.assertIsNone(spec, f'{tid}: asks for a credential the '
                                        f'connector does not name')
                continue
            self.assertIsNotNone(spec, f'{tid}: names {ref} and never asks '
                                       f'anyone for it')
            self.assertEqual(spec['name'], ref.split('/', 1)[1])
            composes_basic = 'Basic ' in ((cfg.get('auth') or {})
                                          .get('template') or '')
            self.assertEqual(spec['format'], 'basic' if composes_basic
                             else 'token', f'{tid}: form and auth disagree')
            if spec['format'] == 'basic':
                self.assertTrue(spec['username_label'],
                                'Basic composes username:secret — a form that '
                                'does not ask for the username cannot work')

    def test_the_listing_carries_what_a_form_needs(self):
        for row in btemplates.listing():
            self.assertIn('placeholders', row)
            self.assertIn('credential', row)
            self.assertFalse(row['verified'])

    def test_fill_reports_an_unknown_template_rather_than_guessing(self):
        cfg, err = btemplates.fill('trello', {})
        self.assertIsNone(cfg)
        self.assertIn('no template', err)

    def test_production_does_not_import_the_test_fixtures(self):
        """`board_fixtures` lives under tests/; a production import of it would
        break any install that ships without the test tree."""
        path = os.path.join(os.path.dirname(HERE), 'boards', 'templates.py')
        with open(path, encoding='utf-8') as f:
            src = f.read()
        code = '\n'.join(ln for ln in src.splitlines()
                         if not ln.lstrip().startswith('#'))
        self.assertNotIn('import board_fixtures', code)
        self.assertNotIn('from tests', code)


# ── strategies ─────────────────────────────────────────────────────────────

class StrategyTests(_Base):
    def test_a_new_board_starts_with_the_builtins(self):
        """An empty Strategies tab greeting a new board is a worse default than
        three obvious ones."""
        self._board()
        status, body = self._req('GET', '/api/boards/acme-jira/strategies')
        self.assertEqual(status, 200, body)
        self.assertEqual(sorted(body['strategies']),
                         ['Oldest first', 'Unassigned', 'Urgent only'])

    def test_the_offered_orders_are_exactly_what_the_validator_accepts(self):
        """A dropdown listing an order the validator rejects is a form that
        fails on submit for no discoverable reason."""
        self._board()
        _s, body = self._req('GET', '/api/boards/acme-jira/strategies')
        for order in body['orders']:
            _c, errors = server.boards.runs.validate_select({'order': order})
            self.assertEqual(errors, [], order)

    def test_saving_stores_the_CLEANED_select(self):
        self._board()
        status, body = self._req(
            'POST', '/api/boards/acme-jira/strategies',
            {'name': 'Billing', 'select': {'tags': ['billing'], 'limit': 5}})
        self.assertEqual(status, 200, body)
        saved = body['strategies']['Billing']
        self.assertEqual(saved['tags'], ['billing'])
        self.assertEqual(saved['limit'], 5)
        self.assertIn('order', saved)      # defaulted by the validator

    def test_an_invalid_select_cannot_be_saved(self):
        """A saved strategy must never be one the run route would reject."""
        self._board()
        status, body = self._req(
            'POST', '/api/boards/acme-jira/strategies',
            {'name': 'Broken', 'select': {'stattus': ['open']}})
        self.assertEqual(status, 400, body)
        self.assertIn('unknown field', body['error'])

    def test_a_strategy_cannot_carry_ignore_processed(self):
        """That flag belongs to the send-back round trip alone. A saved
        strategy carrying it would re-work every item the board has ever
        finished, every time it ran."""
        self._board()
        _s, body = self._req(
            'POST', '/api/boards/acme-jira/strategies',
            {'name': 'Everything', 'select': {'ignore_processed': True}})
        self.assertNotIn('ignore_processed', body['strategies']['Everything'])

    def test_a_bad_name_is_refused(self):
        self._board()
        for name in ('', '   ', 'x' * 60, '../escape'):
            status, _b = self._req('POST', '/api/boards/acme-jira/strategies',
                                   {'name': name, 'select': {}})
            self.assertEqual(status, 400, name)

    def test_deleting_a_strategy(self):
        self._board()
        status, body = self._req(
            'DELETE', '/api/boards/acme-jira/strategies/Oldest%20first')
        self.assertEqual(status, 200, body)
        self.assertNotIn('Oldest first', body['strategies'])

    def test_deleting_a_strategy_does_not_delete_the_BOARD(self):
        """The strategy route has to be matched before /api/boards/<id>."""
        self._board()
        self._req('DELETE', '/api/boards/acme-jira/strategies/Unassigned')
        self.assertIsNotNone(BM.get('acme-jira'))

    def test_deleting_an_unknown_strategy_is_404(self):
        self._board()
        status, _b = self._req('DELETE', '/api/boards/acme-jira/strategies/Nope')
        self.assertEqual(status, 404)

    def test_there_is_a_ceiling_on_stored_strategies(self):
        self._board()
        for i in range(SM.MAX_PER_BOARD + 4):
            status, body = self._req('POST', '/api/boards/acme-jira/strategies',
                                     {'name': f'S{i}', 'select': {'limit': 5}})
        self.assertEqual(status, 400, body)
        self.assertIn('at most', body['error'])

    # ── preview ────────────────────────────────────────────────────────────

    def _listing(self, issues):
        self.responses.append(J({'issues': issues}))

    def test_preview_counts_what_a_run_WOULD_work(self):
        """The alternative to knowing is spending twenty agents to find out."""
        self._board()
        self._listing([issue('1'), issue('2'), issue('3')])
        status, body = self._req('POST', '/api/boards/acme-jira/strategies/preview',
                                 {'select': {'limit': 2}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['matched'], 3)
        self.assertEqual(body['would_work'], 2)
        self.assertEqual(len(body['sample']), 2)

    def test_preview_reports_items_already_processed_separately(self):
        """"Would work 7, skipping 12 already processed" is the sentence that
        makes a re-run understandable."""
        self._board()
        self._listing([issue('1'), issue('2')])
        first = server.boards.engine.content_hash(
            {'id': '1', 'title': 'Refund not received',
             'body': 'Dana says so.',
             'status': {'raw': 'To Do'}, 'priority': {'raw': 'High'}})
        RM._processed('acme-jira').record('1', first, run_id='run-x',
                                          disposition='completed')
        self._listing([issue('1'), issue('2')])
        _s, body = self._req('POST', '/api/boards/acme-jira/strategies/preview',
                             {'select': {}})
        self.assertEqual(body['skipped_already_processed'], 1)
        self.assertEqual(body['would_work'], 1)

    def test_preview_uses_the_SAME_arithmetic_as_a_real_run(self):
        """A second implementation here could disagree with the run and would
        eventually be believed over it."""
        self._board()
        select = {'priority': ['HIGH'], 'limit': 10}
        issues = [issue('1', priority='High'), issue('2', priority='Low'),
                  issue('3', priority='High')]
        self._listing(issues)
        _s, preview = self._req('POST',
                                '/api/boards/acme-jira/strategies/preview',
                                {'select': select})
        self._listing(issues)
        run, err = RM.create(BM.get('acme-jira'), {'select': select,
                                                   'concurrency': 1})
        self.assertIsNone(err, err)
        self.assertEqual(preview['would_work'], len(run['items']))

    def test_preview_carries_an_incomplete_listing_forward(self):
        """"We would work every open ticket" and "every one we could see" are
        different claims."""
        self._board()
        self.responses.append(J({'issues': [issue(str(i)) for i in range(50)],
                                 'nextPageToken': None}))
        _s, body = self._req('POST', '/api/boards/acme-jira/strategies/preview',
                             {'select': {'limit': 100}})
        self.assertIs(body['listing_complete'], False)
        self.assertTrue(body['truncation_reason'])

    def test_preview_counts_items_another_run_already_holds(self):
        """A leased item cannot be claimed, so counting it as selectable would
        overstate what this run would actually do."""
        cfg = self._board()
        self._listing([issue('1'), issue('2')])
        run, _e = RM.create(cfg, {'concurrency': 1, 'select': {'limit': 1}})
        RM._dispatch(run['id'])
        self._listing([issue('1'), issue('2')])
        _s, body = self._req('POST', '/api/boards/acme-jira/strategies/preview',
                             {'select': {'ignore_processed': True}})
        self.assertEqual(body['held_by_another_run'], 1)

    def test_an_invalid_preview_select_is_400(self):
        self._board()
        status, body = self._req('POST',
                                 '/api/boards/acme-jira/strategies/preview',
                                 {'select': {'order': 'sideways'}})
        self.assertEqual(status, 400, body)


# ── metrics ────────────────────────────────────────────────────────────────

class MetricsTests(_Base):
    def _decide(self, state='rejected', item='46'):
        VM.ledger('acme-jira').append({
            't': 1, 'item_id': item, 'state': state, 'disposition': 'needs_review',
            'actions': 1, 'edited': False, 'ok': True, 'run_id': 'run-x'})

    def _report(self, disposition, item='46'):
        VM.ledger('acme-jira').append({
            't': 1, 'item_id': item, 'state': 'reported',
            'disposition': disposition, 'actions': 0, 'edited': False,
            'ok': None, 'run_id': 'run-x'})

    def test_approval_rate_is_ABSENT_not_zero_when_nothing_was_decided(self):
        """A board nobody has reviewed and a board where everything was
        rejected are different facts. A zero renders them identically."""
        self._board()
        self.assertIsNone(MM.for_board('acme-jira')['approval_rate'])

    def test_approval_rate(self):
        self._board()
        for _ in range(3):
            self._decide('approved')
        self._decide('rejected')
        stats = MM.for_board('acme-jira')
        self.assertEqual(stats['decided'], 4)
        self.assertEqual(stats['approved'], 3)
        self.assertAlmostEqual(stats['approval_rate'], 0.75)

    def test_a_partial_counts_as_approved_but_is_reported_separately(self):
        """`partial` means some of an approved item's writes failed. It is an
        approval — a human said yes — but not the same outcome."""
        self._board()
        self._decide('approved')
        self._decide('partial')
        stats = MM.for_board('acme-jira')
        self.assertEqual(stats['approval_rate'], 1.0)
        self.assertEqual(stats['decisions']['partial'], 1)

    def test_a_REPORTED_disposition_does_not_count_as_an_approval(self):
        """An item that settled with nothing staged had no human involved.
        Counting it would inflate the rate with work nobody reviewed."""
        self._board()
        for _ in range(5):
            self._report('completed')
        stats = MM.for_board('acme-jira')
        self.assertIsNone(stats['approval_rate'])
        self.assertEqual(stats['dispositions']['completed'], 5)

    def test_the_disposition_distribution_is_what_inflation_is_watched_in(self):
        self._board()
        self._report('needs_rescoping')
        self._report('needs_rescoping')
        self._report('completed')
        stats = MM.for_board('acme-jira')
        self.assertEqual(stats['dispositions'],
                         {'needs_rescoping': 2, 'completed': 1})

    def test_approval_rate_survives_the_queue_being_overwritten(self):
        """The load-bearing test. `_ensure` replaces a decided record when the
        item is staged again, and the round trip makes that routine — so the
        rate cannot be computed from the queue."""
        self._board()
        self._decide('approved')
        self._decide('rejected')
        # Whatever the queue currently holds, the counts stand.
        shutil.rmtree(VM.staged_dir('acme-jira'), ignore_errors=True)
        stats = MM.for_board('acme-jira')
        self.assertEqual(stats['decided'], 2)
        self.assertAlmostEqual(stats['approval_rate'], 0.5)

    def test_the_route_works(self):
        self._board()
        self._decide('approved')
        status, body = self._req('GET', '/api/boards/acme-jira/metrics')
        self.assertEqual(status, 200, body)
        self.assertEqual(body['approved'], 1)

    def test_a_board_with_a_ledger_but_no_connector_is_still_counted(self):
        """History outlives a deleted connector, and pretending otherwise would
        quietly reset an approval rate whenever someone recreated a board."""
        self._board()
        self._decide('approved')
        BM.delete('acme-jira')
        self.assertIn('acme-jira', MM.board_ids())


class PrometheusBoardSectionTests(_Base):
    def _render(self):
        return server.PrometheusMetricsCollector.render()

    def test_the_section_is_registered_and_reports_up(self):
        text = self._render()
        self.assertIn('kubecoder_metrics_collector_up{section="boards"} 1', text)

    def test_it_emits_the_four_families(self):
        self._board()
        VM.ledger('acme-jira').append({
            't': 1, 'item_id': '46', 'state': 'approved',
            'disposition': 'needs_review', 'actions': 1, 'edited': False,
            'ok': True, 'run_id': 'r'})
        text = self._render()
        for family in ('board_dispositions', 'board_decisions',
                       'board_approval_rate', 'board_review_open'):
            self.assertIn(f'kubecoder_{family}', text)

    def test_approval_rate_is_not_emitted_for_an_undecided_board(self):
        """Absent, not zero — the whole point of the None above."""
        self._board()
        text = self._render()
        rate_lines = [ln for ln in text.splitlines()
                      if ln.startswith('kubecoder_board_approval_rate{')]
        self.assertEqual(rate_lines, [])

    def test_the_board_label_is_CAPPED(self):
        """`board` is the first label here drawn from operator data rather than
        a fixed vocabulary, and an unbounded label is the standard way to take
        a Prometheus down."""
        with mock.patch.object(server.PrometheusMetricsCollector,
                               'MAX_BOARDS', 3):
            for i in range(8):
                VM.ledger(f'b{i}').append({
                    't': 1, 'item_id': '1', 'state': 'approved',
                    'disposition': 'needs_review', 'actions': 1,
                    'edited': False, 'ok': True, 'run_id': 'r'})
            text = self._render()
        labels = {ln.split('board="')[1].split('"')[0]
                  for ln in text.splitlines()
                  if ln.startswith('kubecoder_board_decisions{')}
        self.assertLessEqual(len(labels), 4)          # 3 kept + "other"
        self.assertIn('other', labels)

    def test_a_broken_board_section_does_not_take_the_whole_scrape_down(self):
        with mock.patch.object(MM, 'board_ids',
                               classmethod(lambda cls: (_ for _ in ()).throw(
                                   RuntimeError('disk gone')))):
            text = self._render()
        self.assertIn('kubecoder_metrics_collector_up{section="boards"} 0', text)
        self.assertIn('kubecoder_metrics_collector_up{section="tasks"} 1', text)


if __name__ == '__main__':
    unittest.main()
