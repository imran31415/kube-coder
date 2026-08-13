"""Board Processor end to end (#588) — one scenario, real HTTP, real locks.

WHY THIS EXISTS. Every other board suite stubs `safe_http.fetch`, which is the
right call for testing pagination edge cases without a socket. But real-board
testing found **thirteen** defects in code that had a passing suite, and three
of those tests were passing *because of* a bug. What the unit suites could not
see was the seams: a run dispatching a build, an agent staging through the MCP
routes, a human approving, the vendor actually receiving one comment and not
two.

So this runs the whole loop against a **stub vendor over real sockets**:

* The vendor is a real `ThreadingHTTPServer` speaking a small GitHub-shaped
  issues API — `Link` pagination, a comment collection that records what was
  genuinely posted, and `PATCH` so a test can move a ticket underneath a
  staged action.
* Outbound traffic goes through the **real** `safe_http`, not a stub. The
  connector sets `allow_internal`, which is the supported escape hatch for a
  board on an internal address — so the SSRF guard is exercised rather than
  bypassed.
* Records are written through the real `boards.store` under a real `flock`
  (mutual-exclusion assertions `skipUnless(real_flock())`, so they skip loudly
  on Windows and run for real on Linux/CI).
* The AGENT is not faked. `create_task` is patched to record the launch, and
  the test then drives the same routes an agent's MCP tools call —
  `/items/<id>/actions` and `/items/<id>/disposition`. What is under test is
  the server-side contract, which is where all thirteen defects lived.

What this deliberately CANNOT prove is that a resumed agent actually uses its
prior context. That needs a real model and is the real-board leg's job; here we
prove only that the note was delivered and the right tier fired.

Run:  python3 -m unittest tests.boards_e2e_test   (from charts/workspace/)
"""

import copy
import http.server
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
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

import server                                   # noqa: E402
from boards import store as bstore              # noqa: E402
from boards import templates as btemplates      # noqa: E402

BM = server.BoardsManager
BCM = server.BoardCredentialsManager
RM = server.BoardRunsManager
VM = server.BoardReviewManager
CTM = server.ClaudeTaskManager

APPROVAL = 'e2e-aaaa-bbbb-cccc-dddddddddddd'
OTHER_APPROVAL = 'e2e-1111-2222-3333-444444444444'


# ── the stub vendor ────────────────────────────────────────────────────────

class Vendor:
    """Mutable board state, so a test can move a ticket under a staged write."""

    def __init__(self):
        self.reset()

    def reset(self):
        # Six issues: two open, one closed+completed, one closed+not_planned
        # (that pair proves the state_reason round-trip), one with null
        # optionals, one labelled.
        self.issues = [
            self._issue(1, 'Refund not received', state='open'),
            self._issue(2, 'Cannot log in', state='open'),
            self._issue(3, 'Add dark mode', state='closed',
                        reason='completed'),
            self._issue(4, 'Rewrite in Rust', state='closed',
                        reason='not_planned'),
            self._issue(5, None, state='open', bare=True),
            self._issue(6, 'Crash on export', state='open',
                        labels=['bug', 'urgent']),
        ]
        self.comments = {i['number']: [] for i in self.issues}
        self.requests = []

    @staticmethod
    def _issue(n, title, *, state='open', reason=None, labels=None, bare=False):
        return {
            'id': 1000 + n,                 # global id — markers key on this
            'number': n,                    # per-repo number
            'title': title or f'Issue {n}',
            'body': None if bare else f'Body of issue {n}.',
            'state': state,
            'state_reason': reason,
            'labels': [{'id': 900 + i, 'name': name}
                       for i, name in enumerate(labels or [])],
            'assignee': None if bare else {'id': 7, 'login': 'sam'},
            'user': {'id': 8, 'login': 'dana'},
            'repository_url': 'https://vendor/repos/acme/support',
            'html_url': f'https://vendor/acme/support/issues/{n}',
            'created_at': '2026-01-01T00:00:00Z',
            'updated_at': f'2026-02-0{n}T00:00:00Z',
        }

    def by_number(self, n):
        return next((i for i in self.issues if i['number'] == n), None)


VENDOR = Vendor()


class _VendorHandler(http.server.BaseHTTPRequestHandler):
    """A small GitHub-shaped issues API. Only the parts the connector uses."""

    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):        # keep the test output readable
        pass

    # -- helpers --
    def _send(self, obj, status=200, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(n) or b'{}')

    # -- routes --
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        VENDOR.requests.append(('GET', parsed.path))

        m = re.match(r'^/repos/acme/support/issues/(\d+)/comments$', parsed.path)
        if m:
            return self._send(VENDOR.comments.get(int(m.group(1)), []))

        if parsed.path == '/repos/acme/support/issues':
            per_page = int((qs.get('per_page') or ['50'])[0])
            page = int((qs.get('page') or ['1'])[0])
            start = (page - 1) * per_page
            chunk = VENDOR.issues[start:start + per_page]
            links = []
            # GitHub keeps sending rel="prev"/"first" on the LAST page, so a
            # Link header with no `next` is a positive terminator rather than
            # missing metadata. Reproduced exactly, because reading those as
            # the same thing is defect #3 from the real-board round.
            if start + per_page < len(VENDOR.issues):
                links.append(f'<{self._url(per_page, page + 1)}>; rel="next"')
            if page > 1:
                links.append(f'<{self._url(per_page, page - 1)}>; rel="prev"')
                links.append(f'<{self._url(per_page, 1)}>; rel="first"')
            headers = {'Link': ', '.join(links)} if links else {}
            return self._send(chunk, headers=headers)

        self._send({'message': 'Not Found'}, 404)

    def _url(self, per_page, page):
        return (f'http://127.0.0.1:{self.server.server_address[1]}'
                f'/repos/acme/support/issues?state=all&per_page={per_page}'
                f'&page={page}')

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        VENDOR.requests.append(('POST', parsed.path))
        m = re.match(r'^/repos/acme/support/issues/(\d+)/comments$', parsed.path)
        if m:
            body = self._body()
            entry = {'id': 5000 + len(VENDOR.comments[int(m.group(1))]),
                     'body': body.get('body', '')}
            VENDOR.comments[int(m.group(1))].append(entry)
            return self._send(entry, 201)
        self._send({'message': 'Not Found'}, 404)

    def do_PATCH(self):
        parsed = urllib.parse.urlparse(self.path)
        VENDOR.requests.append(('PATCH', parsed.path))
        m = re.match(r'^/repos/acme/support/issues/(\d+)$', parsed.path)
        if m:
            issue = VENDOR.by_number(int(m.group(1)))
            issue.update(self._body())
            return self._send(issue)
        self._send({'message': 'Not Found'}, 404)


# ── the harness ────────────────────────────────────────────────────────────

class _E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = os.path.realpath(tempfile.mkdtemp(prefix='kc-e2e-'))
        cls._saved_home, BM.HOME_ROOT = BM.HOME_ROOT, cls.tmpdir
        cls._saved_cred, BCM.HOME_ROOT = BCM.HOME_ROOT, cls.tmpdir
        cls._auth = server.BrowserHandler.check_claude_auth
        server.BrowserHandler.check_claude_auth = lambda self: True

        cls.vendor = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0), _VendorHandler)
        cls.vendor_port = cls.vendor.server_address[1]
        cls.vendor_thread = threading.Thread(target=cls.vendor.serve_forever,
                                             daemon=True)
        cls.vendor_thread.start()

        cls.httpd = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0), server.BrowserHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.vendor.shutdown()
        cls.vendor.server_close()
        BM.HOME_ROOT = cls._saved_home
        BCM.HOME_ROOT = cls._saved_cred
        server.BrowserHandler.check_claude_auth = cls._auth
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        VENDOR.reset()
        shutil.rmtree(BM.boards_dir(), ignore_errors=True)
        try:
            os.remove(BCM.creds_file())
        except OSError:
            pass
        BCM.set('VENDOR_TOKEN', 'secret-token')

        self.launched = []

        def create_task(prompt, **kw):
            self.launched.append({'prompt': prompt, **kw})
            return {'status': 'running',
                    'task_id': f'task-{len(self.launched)}'}

        for name, fn in (('create_task', create_task),
                         ('task_status', lambda t: 'running'),
                         ('count_live_tasks', lambda: 0),
                         ('send_followup',
                          lambda t, p, submit=True: (None, 'Session is no longer running'))):
            p = mock.patch.object(CTM, name, fn)
            p.start()
            self.addCleanup(p.stop)
        # The driver thread is stepped by hand so assertions are deterministic.
        p = mock.patch.object(RM, '_spawn_driver',
                              classmethod(lambda cls, r: None))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(server.FeedManager, 'emit', lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    # -- plumbing --
    def req(self, method, path, body=None):
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

    def board(self, per_page='50'):
        """The GitHub TEMPLATE, pointed at the stub vendor.

        Starting from the shipped template rather than a bespoke fixture means
        this scenario also proves the template is usable — the one thing a
        template can be wrong about that validation cannot catch.
        """
        cfg = copy.deepcopy(btemplates.GITHUB_ISSUES)
        cfg['id'] = 'e2e'
        cfg['base_url'] = f'http://127.0.0.1:{self.vendor_port}'
        cfg['credential_ref'] = '@board-creds/VENDOR_TOKEN'
        # The board is on loopback, so it needs the SUPPORTED escape hatch.
        # Stubbing safe_http instead would leave the guard untested on the one
        # path that reaches the network for real.
        cfg['allow_internal'] = True
        cfg['list']['request']['url'] = '${base_url}/repos/acme/support/issues'
        cfg['list']['request']['query'] = {'state': 'all', 'per_page': per_page}
        cfg['list']['page_size'] = int(per_page)
        cfg['map']['ref'] = {'owner': {'template': 'acme'},
                             'repo': {'template': 'support'},
                             'number': 'number'}
        saved, err = BM.create_or_update(cfg)
        self.assertIsNone(err, err)
        return saved

    # -- acting as the agent --
    def agent_stages(self, item_id, body, action='comment'):
        return self.req('POST', f'/api/boards/e2e/items/{item_id}/actions',
                        {'action': action, 'params': {'body': body},
                         'preview': body})

    def agent_reports(self, item_id, disposition, reason='', evidence=None):
        return self.req('POST', f'/api/boards/e2e/items/{item_id}/disposition',
                        {'disposition': disposition, 'reason': reason,
                         'evidence': evidence or {'tool_calls': 3}})

    def comments_on(self, number):
        return VENDOR.comments[number]


# ── 1. reading ─────────────────────────────────────────────────────────────

class ReadPathTests(_E2E):
    def test_the_shipped_github_template_reads_a_real_endpoint(self):
        self.board()
        status, body = self.req('POST', '/api/boards/e2e/test-fetch', {})
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body['items']), 6)
        self.assertTrue(body['complete'])

    def test_pagination_past_page_one_reports_complete_HONESTLY(self):
        """6 issues at per_page=2 is 3 pages, and the last one is FULL — the
        exact shape that made a correct walk report complete:false before the
        link-header terminator was fixed."""
        self.board(per_page='2')
        status, body = self.req('POST', '/api/boards/e2e/test-fetch', {})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['complete'], body.get('truncation_reason'))
        self.assertEqual(body['pages_fetched'], 3)
        self.assertEqual(len(body['items']), 6)

    def test_state_reason_round_trips(self):
        """closed+completed and closed+not_planned both normalize to CLOSED and
        differ only in raw. For a board processor that difference matters."""
        self.board()
        _s, body = self.req('POST', '/api/boards/e2e/test-fetch', {})
        by_key = {str(i['key']): i for i in body['items']}
        self.assertEqual(by_key['3']['status']['normalized'], 'CLOSED')
        self.assertEqual(by_key['4']['status']['normalized'], 'CLOSED')
        self.assertEqual(by_key['3']['status']['raw'], 'closed+completed')
        self.assertEqual(by_key['4']['status']['raw'], 'closed+not_planned')

    def test_labels_map_to_readable_tags_not_python_reprs(self):
        """Stringifying the label OBJECT put `{'id': 900, 'name': 'bug'}` in
        the UI and in agent prompts as if it were a tag name."""
        self.board()
        _s, body = self.req('POST', '/api/boards/e2e/test-fetch', {})
        tags = next(i for i in body['items'] if str(i['key']) == '6')['tags']
        self.assertEqual(sorted(tags), ['bug', 'urgent'])

    def test_null_optionals_survive(self):
        """An issue with no body and no assignee must map without exploding and
        without inventing content. The engine normalizes a null scalar to '' —
        the property that matters is that the ITEM survives and the absent
        fields are empty rather than the string "None"."""
        self.board()
        _s, body = self.req('POST', '/api/boards/e2e/test-fetch', {})
        bare = next(i for i in body['items'] if str(i['key']) == '5')
        self.assertEqual(bare['body'], '')
        self.assertEqual(bare['assignee'], {})
        self.assertEqual(bare['tags'], [])
        # The raw vendor object is always retained, nulls and all.
        self.assertIsNone(bare['raw']['body'])

    def test_a_board_on_loopback_WITHOUT_allow_internal_is_refused(self):
        """The guard is real here, not stubbed — so this proves it."""
        cfg = copy.deepcopy(btemplates.GITHUB_ISSUES)
        cfg['id'] = 'unsafe'
        cfg['base_url'] = f'http://127.0.0.1:{self.vendor_port}'
        cfg['credential_ref'] = '@board-creds/VENDOR_TOKEN'
        _saved, err = BM.create_or_update(cfg)
        self.assertIsNotNone(err)
        self.assertIn('allow_internal', ' '.join(err) if isinstance(err, list)
                      else str(err))


# ── 2. propose mode ────────────────────────────────────────────────────────

class ProposeModeTests(_E2E):
    def _run(self, mode='propose', limit=6, concurrency=3):
        cfg = self.board()
        status, run = self.req('POST', '/api/boards/e2e/runs',
                               {'mode': mode, 'concurrency': concurrency,
                                'select': {'limit': limit}})
        self.assertEqual(status, 201, run)
        RM._dispatch(run['id'])
        return cfg, RM.get(run['id'])

    def test_a_run_dispatches_one_build_per_item_up_to_its_concurrency(self):
        _cfg, run = self._run(concurrency=3)
        self.assertEqual(len(self.launched), 3)
        working = [r for r in run['items'].values() if r['state'] == 'working']
        self.assertEqual(len(working), 3)

    def test_board_builds_are_launched_UNATTENDED(self):
        """A propose run stopped dead at the CLI's approval menu for
        get_board_item — a READ — with nobody present to answer. The mode
        governs whether WRITES are staged, not whether the terminal prompts."""
        self._run()
        for launch in self.launched:
            self.assertTrue(launch['auto_approve'])
            self.assertTrue(launch['source'].startswith('board:'))

    def test_propose_mode_STAGES_the_write_and_the_vendor_sees_NOTHING(self):
        """The headline. Six items worked, and the board is untouched."""
        _cfg, run = self._run()
        item = next(iter(run['items'].values()))
        status, body = self.agent_stages(item['id'], 'Hi Dana — refunded.')
        self.assertEqual(status, 202, body)
        self.assertTrue(body['staged'])
        for number in range(1, 7):
            self.assertEqual(self.comments_on(number), [],
                             f'issue {number} must have no comments')

    def test_the_agent_cannot_opt_out_of_staging(self):
        """The decision reads the run LEASE — server state no agent can reach."""
        _cfg, run = self._run()
        item = next(iter(run['items'].values()))
        status, body = self.req(
            'POST', f'/api/boards/e2e/items/{item["id"]}/actions',
            {'action': 'comment', 'params': {'body': 'x'},
             'mode': 'autonomous', 'stage': False, 'confirm': True})
        self.assertEqual(status, 202, body)
        self.assertTrue(body['staged'])

    def test_an_autonomous_run_writes_straight_through(self):
        _cfg, run = self._run(mode='autonomous', limit=1, concurrency=1)
        item = next(iter(run['items'].values()))
        status, body = self.agent_stages(item['id'], 'Posted directly.')
        self.assertEqual(status, 200, body)
        posted = self.comments_on(int(item['key']))
        self.assertEqual(len(posted), 1)
        self.assertIn('Posted directly.', posted[0]['body'])

    def test_a_disposition_with_no_reason_is_refused(self):
        """A disposition with no reason is indistinguishable from progress in
        every list it appears in."""
        _cfg, run = self._run()
        item = next(iter(run['items'].values()))
        status, body = self.agent_reports(item['id'], 'needs_rescoping')
        self.assertEqual(status, 400, body)


# ── 3. review ──────────────────────────────────────────────────────────────

class ReviewTests(_E2E):
    def _staged(self, key='1', text='Hi Dana — the refund went out Monday.'):
        cfg = self.board()
        _s, run = self.req('POST', '/api/boards/e2e/runs',
                           {'mode': 'propose', 'concurrency': 1,
                            'select': {'limit': 1, 'order': 'key'}})
        RM._dispatch(run['id'])
        item = next(r for r in RM.get(run['id'])['items'].values()
                    if r['key'] == key)
        self.agent_stages(item['id'], text)
        self.agent_reports(item['id'], 'needs_review',
                           reason='drafted a reply; please check the tone')
        return cfg, run, item

    def test_the_queue_groups_by_disposition_with_needs_review_first(self):
        self._staged()
        status, body = self.req('GET', '/api/boards/e2e/review')
        self.assertEqual(status, 200, body)
        self.assertEqual(body['groups'][0]['disposition'], 'needs_review')
        self.assertEqual(body['open'], 1)

    def test_approve_posts_EXACTLY_one_comment(self):
        _cfg, _run, item = self._staged()
        record = VM.get('e2e', item['id'])
        status, body = self.req(
            'POST', f'/api/boards/e2e/staged/{item["id"]}/approve',
            {'content_hash': record['content_hash'], 'approval_id': APPROVAL})
        self.assertEqual(status, 200, body)
        posted = self.comments_on(1)
        self.assertEqual(len(posted), 1)
        self.assertIn('refund went out Monday', posted[0]['body'])

    def test_a_REPLAY_returns_the_stored_result_and_posts_nothing_more(self):
        """What makes approving from a phone on a flaky connection safe."""
        _cfg, _run, item = self._staged()
        record = VM.get('e2e', item['id'])
        payload = {'content_hash': record['content_hash'],
                   'approval_id': APPROVAL}
        self.req('POST', f'/api/boards/e2e/staged/{item["id"]}/approve', payload)
        status, body = self.req(
            'POST', f'/api/boards/e2e/staged/{item["id"]}/approve', payload)
        self.assertEqual(status, 200, body)
        self.assertTrue(body['replayed'])
        self.assertEqual(len(self.comments_on(1)), 1)

    def test_a_DIFFERENT_approval_id_on_a_decided_record_is_409(self):
        _cfg, _run, item = self._staged()
        record = VM.get('e2e', item['id'])
        self.req('POST', f'/api/boards/e2e/staged/{item["id"]}/approve',
                 {'content_hash': record['content_hash'],
                  'approval_id': APPROVAL})
        status, body = self.req(
            'POST', f'/api/boards/e2e/staged/{item["id"]}/approve',
            {'content_hash': record['content_hash'],
             'approval_id': OTHER_APPROVAL})
        self.assertEqual(status, 409, body)
        self.assertEqual(body['code'], 'already_decided')
        self.assertEqual(len(self.comments_on(1)), 1)

    def test_the_EDITED_text_is_what_the_vendor_receives(self):
        _cfg, _run, item = self._staged()
        record = VM.get('e2e', item['id'])
        self.req('POST', f'/api/boards/e2e/staged/{item["id"]}/edit',
                 {'action_id': 'a1',
                  'params': {'body': 'Rewritten, warmer.'}})
        self.req('POST', f'/api/boards/e2e/staged/{item["id"]}/approve',
                 {'content_hash': record['content_hash'],
                  'approval_id': APPROVAL})
        posted = self.comments_on(1)
        self.assertEqual(len(posted), 1)
        self.assertIn('Rewritten, warmer.', posted[0]['body'])

    def test_STALE_the_ticket_moved_under_the_staged_write(self):
        """The guard that matters most. Writing over a colleague's reply is the
        most damaging thing this feature could do."""
        _cfg, _run, item = self._staged()
        record = VM.get('e2e', item['id'])

        # Someone edits the ticket on the board, for real.
        req = urllib.request.Request(
            f'http://127.0.0.1:{self.vendor_port}/repos/acme/support/issues/1',
            data=json.dumps({'title': 'Refund not received — URGENT'}).encode(),
            method='PATCH', headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10).read()

        status, body = self.req(
            'POST', f'/api/boards/e2e/staged/{item["id"]}/approve',
            {'content_hash': record['content_hash'], 'approval_id': APPROVAL})
        self.assertEqual(status, 409, body)
        self.assertEqual(body['code'], 'stale')
        self.assertIn('Nothing was written', body['error'])
        self.assertEqual(self.comments_on(1), [])

    def test_rejecting_writes_nothing(self):
        _cfg, _run, item = self._staged()
        status, _b = self.req(
            'POST', f'/api/boards/e2e/staged/{item["id"]}/reject',
            {'approval_id': APPROVAL, 'reason': 'wrong tone'})
        self.assertEqual(status, 200)
        self.assertEqual(self.comments_on(1), [])


# ── 4. the round trip ──────────────────────────────────────────────────────

class RoundTripTests(ReviewTests):
    def test_send_back_re_dispatches_the_item_with_the_note(self):
        _cfg, run, item = self._staged()
        RM._finish(run['id'], 'done')          # the reaper settles the build
        before = len(self.launched)

        status, body = self.req(
            'POST', f'/api/boards/e2e/staged/{item["id"]}/send-back',
            {'approval_id': APPROVAL,
             'note': 'Which refund — the January one or the March one?'})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['resume']['dispatched'], body['resume'])

        resume_run = RM.get(body['resume']['run_id'])
        self.assertEqual(resume_run['origin'], 'send_back')
        self.assertEqual(resume_run['mode'], 'propose')
        RM._dispatch(resume_run['id'])

        self.assertEqual(len(self.launched), before + 1)
        self.assertIn('January one or the March one',
                      self.launched[-1]['prompt'])
        self.assertEqual(self.comments_on(1), [],
                         'sending back must write nothing')

    def test_send_back_without_a_note_is_refused(self):
        _cfg, run, item = self._staged()
        RM._finish(run['id'], 'done')
        status, body = self.req(
            'POST', f'/api/boards/e2e/staged/{item["id"]}/send-back',
            {'approval_id': APPROVAL})
        self.assertEqual(status, 400, body)
        self.assertEqual(body['code'], 'note_required')

    def test_the_resumed_item_is_still_governed_by_propose_mode(self):
        """The safety property. A build outside a run would write STRAIGHT to
        the board, at exactly the moment a human said 'not like that'."""
        _cfg, run, item = self._staged()
        RM._finish(run['id'], 'done')
        _s, body = self.req(
            'POST', f'/api/boards/e2e/staged/{item["id"]}/send-back',
            {'approval_id': APPROVAL, 'note': 'which refund?'})
        RM._dispatch(body['resume']['run_id'])

        status, staged = self.agent_stages(item['id'], 'Second attempt.')
        self.assertEqual(status, 202, staged)
        self.assertEqual(self.comments_on(1), [])

    def test_the_resumed_run_reports_which_TIER_actually_fired(self):
        _cfg, run, item = self._staged()
        RM._finish(run['id'], 'done')
        _s, body = self.req(
            'POST', f'/api/boards/e2e/staged/{item["id"]}/send-back',
            {'approval_id': APPROVAL, 'note': 'which refund?'})
        resume_run_id = body['resume']['run_id']
        RM._dispatch(resume_run_id)
        row = next(iter(RM.get(resume_run_id)['items'].values()))
        # send_followup is stubbed to fail and there is no claude_session_id,
        # so tier 3 is the honest answer here.
        self.assertEqual(row['resume_tier'], 'fresh')


# ── 5. idempotency across runs ─────────────────────────────────────────────

class IdempotencyTests(_E2E):
    def _work_everything(self):
        cfg = self.board()
        _s, run = self.req('POST', '/api/boards/e2e/runs',
                           {'mode': 'autonomous', 'concurrency': 6,
                            'select': {'limit': 6}})
        RM._dispatch(run['id'])
        for row in RM.get(run['id'])['items'].values():
            self.agent_reports(row['id'], 'completed')
            RM._settle(run['id'], RM.get(run['id'])['items'][row['id']],
                       RM._leases('e2e'), RM._processed('e2e'), 'done', '')
        return cfg, run

    def test_re_running_the_same_board_does_NOTHING(self):
        """The headline idempotency claim."""
        self._work_everything()
        status, second = self.req('POST', '/api/boards/e2e/runs',
                                  {'mode': 'autonomous', 'concurrency': 6,
                                   'select': {'limit': 6}})
        self.assertEqual(status, 201, second)
        self.assertEqual(len(second['items']), 0)
        self.assertEqual(second['skipped_already_processed'], 6)

    def test_an_EDITED_item_becomes_eligible_again_and_only_that_one(self):
        self._work_everything()
        req = urllib.request.Request(
            f'http://127.0.0.1:{self.vendor_port}/repos/acme/support/issues/2',
            data=json.dumps({'title': 'Cannot log in — still broken'}).encode(),
            method='PATCH', headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10).read()

        _s, second = self.req('POST', '/api/boards/e2e/runs',
                              {'mode': 'autonomous', 'concurrency': 6,
                               'select': {'limit': 6}})
        self.assertEqual(len(second['items']), 1)
        self.assertEqual(next(iter(second['items'].values()))['key'], '2')

    def test_OUR_OWN_write_does_not_re_open_the_item(self):
        """`content_hash` used to include `updated_at`, which every vendor
        bumps on any touch — including our own comment. A run therefore
        invalidated its own processed markers and re-selected exactly the items
        it had just worked."""
        cfg = self.board()
        _s, run = self.req('POST', '/api/boards/e2e/runs',
                           {'mode': 'autonomous', 'concurrency': 1,
                            'select': {'limit': 1, 'order': 'key'}})
        RM._dispatch(run['id'])
        row = next(iter(RM.get(run['id'])['items'].values()))
        before = row['content_hash']

        status, _b = self.agent_stages(row['id'], 'A real comment.')
        self.assertEqual(status, 200)
        self.assertEqual(len(self.comments_on(int(row['key']))), 1)

        _s, fetched = self.req('POST', '/api/boards/e2e/test-fetch', {})
        item = next(i for i in fetched['items'] if str(i['id']) == row['id'])
        after = server.boards.engine.content_hash(item)
        self.assertEqual(before, after,
                         'our own comment must not change the item identity')

    def test_the_vendor_marker_stops_a_duplicate_comment(self):
        """Setting a status twice is harmless; commenting twice is a visible
        mistake in front of a customer."""
        cfg = self.board()
        _s, run = self.req('POST', '/api/boards/e2e/runs',
                           {'mode': 'autonomous', 'concurrency': 1,
                            'select': {'limit': 1, 'order': 'key'}})
        RM._dispatch(run['id'])
        row = next(iter(RM.get(run['id'])['items'].values()))
        self.agent_stages(row['id'], 'Only once, please.')
        status, body = self.agent_stages(row['id'], 'Only once, please.')
        self.assertEqual(status, 200, body)
        self.assertEqual(len(self.comments_on(int(row['key']))), 1)


# ── 6. strategies and metrics over the same board ──────────────────────────

class StrategyAndMetricsTests(_E2E):
    def test_preview_predicts_exactly_what_the_run_then_claims(self):
        """A second implementation of the arithmetic would eventually be
        believed over the run's own."""
        self.board()
        select = {'status': ['OPEN'], 'limit': 10}
        _s, preview = self.req('POST', '/api/boards/e2e/strategies/preview',
                               {'select': select})
        _s, run = self.req('POST', '/api/boards/e2e/runs',
                           {'mode': 'propose', 'concurrency': 1,
                            'select': select})
        self.assertEqual(preview['would_work'], len(run['items']))
        self.assertEqual(preview['would_work'], 4)     # 4 open of 6

    def test_a_saved_strategy_can_start_a_run(self):
        self.board()
        _s, saved = self.req('POST', '/api/boards/e2e/strategies',
                             {'name': 'Bugs only',
                              'select': {'tags': ['bug'], 'limit': 5}})
        status, run = self.req('POST', '/api/boards/e2e/runs',
                               {'mode': 'propose', 'concurrency': 1,
                                'select': saved['strategies']['Bugs only']})
        self.assertEqual(status, 201, run)
        self.assertEqual(len(run['items']), 1)
        self.assertEqual(next(iter(run['items'].values()))['key'], '6')

    def test_the_ledger_and_the_metrics_agree_with_what_happened(self):
        cfg = self.board()
        _s, run = self.req('POST', '/api/boards/e2e/runs',
                           {'mode': 'propose', 'concurrency': 2,
                            'select': {'limit': 2, 'order': 'key'}})
        RM._dispatch(run['id'])
        rows = list(RM.get(run['id'])['items'].values())
        for row in rows:
            self.agent_stages(row['id'], f'Reply to {row["key"]}.')
            self.agent_reports(row['id'], 'needs_review', reason='check tone')

        # Approve one, reject the other.
        rec = VM.get('e2e', rows[0]['id'])
        self.req('POST', f'/api/boards/e2e/staged/{rows[0]["id"]}/approve',
                 {'content_hash': rec['content_hash'], 'approval_id': APPROVAL})
        self.req('POST', f'/api/boards/e2e/staged/{rows[1]["id"]}/reject',
                 {'approval_id': OTHER_APPROVAL, 'reason': 'no'})

        status, metrics = self.req('GET', '/api/boards/e2e/metrics')
        self.assertEqual(status, 200, metrics)
        self.assertEqual(metrics['decided'], 2)
        self.assertEqual(metrics['approved'], 1)
        self.assertAlmostEqual(metrics['approval_rate'], 0.5)
        self.assertEqual(metrics['dispositions']['needs_review'], 2)
        # And exactly one comment actually landed on the board.
        self.assertEqual(len(self.comments_on(int(rows[0]['key']))), 1)
        self.assertEqual(self.comments_on(int(rows[1]['key'])), [])

    def test_prometheus_reports_the_same_approval_rate(self):
        self.test_the_ledger_and_the_metrics_agree_with_what_happened()
        text = server.PrometheusMetricsCollector.render()
        self.assertIn('kubecoder_board_approval_rate{board="e2e"} 0.5', text)


# ── 7. locking, for real ───────────────────────────────────────────────────

class LeaseTests(_E2E):
    @unittest.skipUnless(bstore.real_flock(),
                         'fcntl.flock is shimmed on this platform — a green '
                         'double-claim test against a no-op lock is worse than '
                         'a skipped one')
    def test_two_overlapping_runs_never_both_claim_an_item(self):
        """C10 from the real-board plan, which the manual round never
        exercised. The lease is per BOARD, not per run."""
        self.board()
        _s, first = self.req('POST', '/api/boards/e2e/runs',
                             {'mode': 'propose', 'concurrency': 6,
                              'select': {'limit': 6}})
        _s, second = self.req('POST', '/api/boards/e2e/runs',
                              {'mode': 'propose', 'concurrency': 6,
                               'select': {'limit': 6, 'ignore_processed': True}})

        errors = []

        def drive(run_id):
            try:
                RM._dispatch(run_id)
            except Exception as e:            # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=drive, args=(r['id'],))
                   for r in (first, second)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        claimed = []
        for run_id in (first['id'], second['id']):
            claimed += [r['id'] for r in RM.get(run_id)['items'].values()
                        if r['state'] in ('claimed', 'working')]
        self.assertEqual(len(claimed), len(set(claimed)),
                         'an item was claimed by both runs')
        # And every build that started belongs to exactly one owner.
        self.assertEqual(len(self.launched), len(set(claimed)))

    @unittest.skipUnless(bstore.real_flock(), 'fcntl.flock is shimmed here')
    def test_the_boot_sweep_frees_leases_and_marks_the_run_interrupted(self):
        """C11's recovery half: a run left in flight by a dead process."""
        self.board()
        _s, run = self.req('POST', '/api/boards/e2e/runs',
                           {'mode': 'propose', 'concurrency': 2,
                            'select': {'limit': 2}})
        RM._dispatch(run['id'])
        self.assertTrue(RM._leases('e2e').all())

        RM.sweep_orphans()

        after = RM.get(run['id'])
        self.assertEqual(after['status'], 'interrupted')
        self.assertIn('restarted', after['error'])
        self.assertEqual(RM._leases('e2e').all(), {})


if __name__ == '__main__':
    unittest.main()
