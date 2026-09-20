"""Per-trigger run history: the ledger, its write points, and its API (#91).

A fire used to leave nothing durable behind — `EventBroker.publish` is
in-memory, `FeedManager.emit_trigger` records only that *something* fired, and
the spawned task ages out. Worse, the branches people actually need to debug
returned early and emitted neither: a bad HMAC, a replayed body, a cron firing
with a rotated token, a fire refused at the task cap.

So the tests that matter here are the REJECTION ones. "An accepted webhook is
recorded" is the easy half; "a 404'd webhook POST is recorded, with
signature_verified false" is the half the feature exists for, and the half a
future refactor of the receive path can silently drop.

The endpoints run against a real `ThreadingHTTPServer` on a loopback port (the
precedent is tests/boards_runs_api_test.py) rather than calling handlers
directly, because the route table is part of what is being asserted: a ledger
nobody can reach is not a feature.

Run:  python3 -m unittest tests.trigger_runs_test   (from charts/workspace/)
"""

import hashlib
import hmac
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

# server.py imports fcntl at module load, which does not exist on Windows.
# Same shim as tests/page_watch_api_test.py so this suite runs on a dev laptop.
try:
    import fcntl  # noqa: F401
except ImportError:  # pragma: no cover - platform shim
    import types
    _shim = types.ModuleType('fcntl')
    _shim._kube_coder_shim = True
    _shim.flock = lambda *a, **k: None
    _shim.lockf = lambda *a, **k: None
    _shim.LOCK_EX = 2
    _shim.LOCK_UN = 8
    _shim.LOCK_NB = 4
    sys.modules['fcntl'] = _shim

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import safe_http  # noqa: E402
import server  # noqa: E402
from live_state import (  # noqa: E402
    isolate_feed_and_push,
    isolate_trigger_runs,
    silence_prompt_delivery,
)

TRM = server.TriggerRunsManager


def _kubectl_ok(*args, **kwargs):
    """subprocess.run stub: kubectl (and tmux) always succeed, quietly."""
    return mock.Mock(returncode=0, stdout='', stderr='')


def _page(body='<p>seed</p>', status=200):
    """A (status, headers, body) triple shaped like safe_http.fetch returns."""
    return status, {'Content-Type': 'text/html'}, body.encode('utf-8')


class FakeTasks:
    """Stands in for ClaudeTaskManager.create_task.

    Every outcome the fire paths branch on is a status string, so the fake is a
    dial rather than a mock: set `status` and assert what the ledger wrote.
    """

    def __init__(self):
        self.n = 0
        self.status = 'running'
        self.error = None

    def create_task(self, prompt, **kw):
        self.n += 1
        task_id = f'tk-{self.n}'
        out = {'task_id': task_id, 'status': self.status, 'prompt': prompt}
        out.update({k: v for k, v in kw.items() if k == 'source'})
        if self.error:
            out['error'] = self.error
        return out


# ══ the ledger itself ════════════════════════════════════════════════════════

class LedgerTests(unittest.TestCase):
    """TriggerRunsManager in isolation: shape, order, paging, and the two
    refusals that keep it from becoming a filesystem primitive."""

    def setUp(self):
        self.root = isolate_trigger_runs(self)

    def test_an_entry_carries_the_fields_the_ui_renders(self):
        TRM.record('webhook', 'gh', 'spawned', task_id='tk-1',
                   source_ip='10.0.0.7', signature_verified=True,
                   provider='github', ts=1700000000)
        (run,) = TRM.list_runs('webhook', 'gh')['runs']
        self.assertEqual(run['ts'], 1700000000)
        self.assertEqual(run['type'], 'webhook')
        self.assertEqual(run['trigger_id'], 'gh')
        self.assertEqual(run['outcome'], 'spawned')
        self.assertEqual(run['task_id'], 'tk-1')
        self.assertEqual(run['source_ip'], '10.0.0.7')
        self.assertTrue(run['signature_verified'])
        self.assertEqual(run['provider'], 'github')

    def test_unset_fields_are_omitted_rather_than_null(self):
        """Every byte counts against MAX_BYTES, and `"provider": null` on each
        of a cron's entries is pure rotation pressure."""
        TRM.record('cron', 'nightly', 'spawned', task_id='tk-1')
        (run,) = TRM.list_runs('cron', 'nightly')['runs']
        for absent in ('provider', 'reason', 'error', 'source_ip',
                       'signature_verified', 'manual', 'forwarded_for'):
            self.assertNotIn(absent, run)

    def test_history_reads_newest_first(self):
        for i in range(5):
            TRM.record('cron', 'nightly', 'spawned', task_id=f'tk-{i}', ts=1700 + i)
        got = [r['task_id'] for r in TRM.list_runs('cron', 'nightly')['runs']]
        self.assertEqual(got, ['tk-4', 'tk-3', 'tk-2', 'tk-1', 'tk-0'])

    def test_paging_walks_backwards_through_history(self):
        for i in range(10):
            TRM.record('cron', 'nightly', 'spawned', task_id=f'tk-{i}', ts=1700 + i)
        first = TRM.list_runs('cron', 'nightly', limit=3)
        second = TRM.list_runs('cron', 'nightly', limit=3, offset=3)
        self.assertEqual([r['task_id'] for r in first['runs']],
                         ['tk-9', 'tk-8', 'tk-7'])
        self.assertEqual([r['task_id'] for r in second['runs']],
                         ['tk-6', 'tk-5', 'tk-4'])
        self.assertEqual(first['total'], 10)
        self.assertEqual(second['offset'], 3)

    def test_a_limit_past_the_end_is_an_empty_page_not_an_error(self):
        TRM.record('cron', 'nightly', 'spawned')
        page = TRM.list_runs('cron', 'nightly', limit=10, offset=99)
        self.assertEqual(page['runs'], [])
        self.assertEqual(page['total'], 1)

    def test_limit_is_clamped_to_MAX_LIMIT(self):
        """An unbounded ?limit= turns one GET into a full-ledger read."""
        self.assertEqual(TRM.list_runs('cron', 'n', limit=10 ** 6)['limit'],
                         TRM.MAX_LIMIT)
        self.assertEqual(TRM.list_runs('cron', 'n', limit=0)['limit'], 1)

    def test_a_garbage_limit_falls_back_to_the_default(self):
        page = TRM.list_runs('cron', 'n', limit='eleventy', offset='soon')
        self.assertEqual(page['limit'], TRM.DEFAULT_LIMIT)
        self.assertEqual(page['offset'], 0)

    def test_reading_a_trigger_that_never_fired_is_empty_not_an_error(self):
        page = TRM.list_runs('webhook', 'never-used')
        self.assertEqual(page, {'runs': [], 'total': 0,
                                'limit': TRM.DEFAULT_LIMIT, 'offset': 0})

    def test_an_unknown_kind_is_refused(self):
        """KINDS doubles as the directory allowlist."""
        self.assertFalse(TRM.record('sasquatch', 'x', 'spawned'))
        self.assertEqual(TRM.list_runs('sasquatch', 'x')['runs'], [])

    def test_a_traversing_id_cannot_write_outside_the_runs_dir(self):
        for bad in ('../../etc/cron', 'a/b', '', 'x' * 65):
            with self.subTest(bad=bad):
                self.assertFalse(TRM.record('cron', bad, 'spawned'))
        # Nothing was created anywhere — not even an empty kind directory.
        self.assertEqual(
            sorted(os.listdir(self.root)) if os.path.isdir(self.root) else [], [])

    def test_an_unknown_outcome_lands_as_error_rather_than_a_new_word(self):
        """The UI keys presentation off `outcome`; an unrecognised value would
        render as an unstyled blank rather than announce itself."""
        TRM.record('cron', 'nightly', 'probably-fine')
        (run,) = TRM.list_runs('cron', 'nightly')['runs']
        self.assertEqual(run['outcome'], 'error')

    def test_a_long_vendor_error_is_truncated(self):
        TRM.record('cron', 'nightly', 'error', error='x' * 5000)
        (run,) = TRM.list_runs('cron', 'nightly')['runs']
        self.assertEqual(len(run['error']), TRM.MAX_ERROR_CHARS)

    def test_the_ledger_is_bounded(self):
        """AC: 'Ledger size is bounded'. Two generations of MAX_BYTES, so the
        oldest entries age out instead of filling the PVC."""
        with mock.patch.object(TRM, 'MAX_BYTES', 2048):
            for i in range(400):
                TRM.record('cron', 'nightly', 'spawned', task_id=f'tk-{i}',
                           error='p' * 100, ts=1700 + i)
            path = os.path.join(self.root, 'cron', 'nightly.jsonl')
            total = sum(os.path.getsize(p) for p in (path, path + '.1')
                        if os.path.isfile(p))
            self.assertLess(total, 2048 * 2 + 4096,
                            'rotation must keep at most two generations')
            runs = TRM.list_runs('cron', 'nightly', limit=TRM.MAX_LIMIT)['runs']
            self.assertLess(len(runs), 400, 'old entries must age out')
            self.assertEqual(runs[0]['task_id'], 'tk-399',
                             'the newest entry always survives')

    def test_an_unwritable_ledger_costs_the_entry_not_the_fire(self):
        """A ledger write must never be able to fail a fire that has already
        happened, so `record` swallows what the filesystem throws at it."""
        with mock.patch.object(server, '_JsonlLog') as fake:
            fake.return_value.append.side_effect = OSError('no space left')
            self.assertFalse(TRM.record('cron', 'nightly', 'spawned'))

    def test_deleting_a_trigger_drops_its_history(self):
        TRM.record('cron', 'nightly', 'spawned')
        self.assertTrue(TRM.delete('cron', 'nightly'))
        self.assertEqual(TRM.list_runs('cron', 'nightly')['total'], 0)


# ══ the HTTP surface: write points + read endpoints ══════════════════════════

class _ApiBase(unittest.TestCase):
    """One real HTTP server for the suite; per-test temp dirs for every store
    the trigger handlers touch."""

    AUTH_OK = True

    @classmethod
    def setUpClass(cls):
        cls._auth_save = server.BrowserHandler.check_claude_auth
        server.BrowserHandler.check_claude_auth = lambda self, *a, **k: cls.AUTH_OK
        # Pinned, not inherited: READONLY_MODE is read from the environment at
        # import, and a suite that asserts on POST behaviour must not depend on
        # how the machine running it is configured.
        cls._ro_save, server.READONLY_MODE = server.READONLY_MODE, False
        cls.httpd = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0), server.BrowserHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.BrowserHandler.check_claude_auth = cls._auth_save
        server.READONLY_MODE = cls._ro_save

    def setUp(self):
        isolate_feed_and_push(self)       # a fire emits a Feed item (#685)
        isolate_trigger_runs(self)        # ... and now a ledger entry (#91)
        silence_prompt_delivery(self)
        self.tmpdir = tempfile.mkdtemp(prefix='kctest-trigruns-')
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        for target, attr, sub in (
            (server.WebhookManager, 'WEBHOOKS_DIR', 'webhooks'),
            (server.CronManager, 'CRONS_DIR', 'crons'),
            (server.PageWatchManager, 'PAGE_WATCHES_DIR', 'page-watches'),
            (server.ClaudeTaskManager, 'TASKS_DIR', 'tasks'),
        ):
            path = os.path.join(self.tmpdir, sub)
            os.makedirs(path)
            p = mock.patch.object(target, attr, path)
            p.start()
            self.addCleanup(p.stop)
        for attr, value in (('detect_user', 'octo'), ('detect_namespace', 'coder')):
            p = mock.patch.object(server.CronManager, attr,
                                  staticmethod(lambda v=value: v))
            p.start()
            self.addCleanup(p.stop)
        # The replay cache is module-level, so one test's body would be a
        # "replay" in the next. A fresh cache per test, except where a test
        # deliberately sends the same body twice.
        p = mock.patch.object(server.WebhookManager, 'REPLAY_CACHE',
                              server._ReplayCache(capacity=64, ttl_seconds=300))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch('server.subprocess.run', side_effect=_kubectl_ok)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(safe_http, 'is_safe_url', lambda url, **kw: True)
        p.start()
        self.addCleanup(p.stop)
        self.tasks = FakeTasks()
        p = mock.patch.object(server.ClaudeTaskManager, 'create_task',
                              self.tasks.create_task)
        p.start()
        self.addCleanup(p.stop)

    # ── request helpers ──────────────────────────────────────────────────────

    def _req(self, method, path, body=None, headers=None, raw=None):
        data = raw if raw is not None else (
            json.dumps(body).encode() if body is not None else None)
        hdrs = {'Content-Type': 'application/json'} if data is not None else {}
        hdrs.update(headers or {})
        r = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}',
                                   data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                payload = resp.read()
                return resp.status, (json.loads(payload) if payload else {})
        except urllib.error.HTTPError as e:
            payload = e.read()
            try:
                return e.code, json.loads(payload)
            except ValueError:
                return e.code, payload

    # ── fixtures ─────────────────────────────────────────────────────────────

    SECRET = 'top-secret-hmac'

    def _webhook(self, webhook_id='gh-pr', **over):
        data = {'id': webhook_id, 'prompt_template': 'Review it',
                'hmac_secret': self.SECRET}
        data.update(over)
        cfg, err = server.WebhookManager.create_or_update(data)
        self.assertIsNone(err, err)
        return cfg

    def _signed(self, webhook_id, payload=None, secret=None):
        """POST a body with a valid generic HMAC in X-Hub-Signature-256."""
        raw = json.dumps(payload if payload is not None else {'n': 1}).encode()
        mac = hmac.new((secret or self.SECRET).encode(), raw,
                       hashlib.sha256).hexdigest()
        return self._req('POST', f'/api/webhooks/{webhook_id}', raw=raw,
                         headers={'X-Hub-Signature-256': f'sha256={mac}'})

    def _cron(self, cron_id='nightly', **over):
        data = {'id': cron_id, 'schedule': '0 * * * *',
                'prompt_template': 'Summarise the day'}
        data.update(over)
        cfg, err = server.CronManager.create_or_update(data)
        self.assertIsNone(err, err)
        return server.CronManager.get_cron(cron_id, include_secrets=True)

    def _fire_cron(self, cron_id, token):
        # The k8s CronJob's curl pod calls this route, not /api/crons/<id>/...
        return self._req('POST', f'/api/triggers/cron-fire/{cron_id}',
                         body={}, headers={'Authorization': f'Bearer {token}'})

    def _watch(self, watch_id='ci', **over):
        data = {'id': watch_id, 'url': 'https://example.test/build',
                'schedule': '*/5 * * * *', 'prompt_template': 'CI moved'}
        data.update(over)
        cfg, err = server.PageWatchManager.create_or_update(
            data, fetch=lambda url, **kw: _page())
        self.assertIsNone(err, err)
        return server.PageWatchManager.get_page_watch(watch_id, include_secrets=True)

    def _check_watch(self, watch_id, token, body='<p>seed</p>'):
        with mock.patch.object(safe_http, 'fetch',
                               lambda url, **kw: _page(body)):
            # The scheduled receiver, which is the one that carries a token.
            return self._req('POST', f'/api/triggers/page-watch-check/{watch_id}',
                             body={}, headers={'Authorization': f'Bearer {token}'})

    def _runs(self, kind_path, trigger_id, query=''):
        status, payload = self._req(
            'GET', f'/api/{kind_path}/{trigger_id}/runs{query}')
        self.assertEqual(status, 200, payload)
        return payload

    def _only(self, kind_path, trigger_id):
        runs = self._runs(kind_path, trigger_id)['runs']
        self.assertEqual(len(runs), 1, runs)
        return runs[0]


# ── AC 1 + 2: every webhook POST is recorded, accepted and rejected ──────────

class WebhookLedgerTests(_ApiBase):
    def test_an_accepted_post_records_the_task_it_spawned(self):
        self._webhook()
        status, body = self._signed('gh-pr')
        self.assertEqual(status, 202, body)
        run = self._only('webhooks', 'gh-pr')
        self.assertEqual(run['outcome'], 'spawned')
        self.assertEqual(run['task_id'], body['task_id'])
        self.assertTrue(run['signature_verified'])
        self.assertEqual(run['provider'], 'generic')
        self.assertEqual(run['source_ip'], '127.0.0.1')

    def test_a_bad_signature_is_visible_with_signature_verified_false(self):
        """The headline acceptance criterion. This branch answers a deliberately
        vague 404 and used to emit nothing at all, which is precisely why
        'my GitHub webhook did not work' was guesswork."""
        self._webhook()
        status, _ = self._req('POST', '/api/webhooks/gh-pr', body={'n': 1},
                              headers={'X-Hub-Signature-256': 'sha256=deadbeef'})
        self.assertEqual(status, 404)
        run = self._only('webhooks', 'gh-pr')
        self.assertEqual(run['outcome'], 'rejected')
        self.assertEqual(run['reason'], 'bad_signature')
        self.assertIs(run['signature_verified'], False)

    def test_a_missing_signature_header_is_recorded_too(self):
        self._webhook()
        status, _ = self._req('POST', '/api/webhooks/gh-pr', body={'n': 1})
        self.assertEqual(status, 404)
        self.assertEqual(self._only('webhooks', 'gh-pr')['reason'],
                         'bad_signature')

    def test_a_replay_is_recorded_as_a_replay_not_a_bad_signature(self):
        """Same 'rejected', different fix: the signature checked out, and the
        body was refused for having been seen already."""
        self._webhook()
        self.assertEqual(self._signed('gh-pr', {'same': 'body'})[0], 202)
        self.assertEqual(self._signed('gh-pr', {'same': 'body'})[0], 409)
        runs = self._runs('webhooks', 'gh-pr')['runs']
        self.assertEqual([r['reason'] for r in runs if 'reason' in r], ['replay'])
        replay = runs[0]
        self.assertEqual(replay['outcome'], 'rejected')
        self.assertIs(replay['signature_verified'], True,
                      'a replayed body was correctly signed')

    def test_an_unparseable_body_is_recorded(self):
        self._webhook()
        raw = b'{not json'
        mac = hmac.new(self.SECRET.encode(), raw, hashlib.sha256).hexdigest()
        status, _ = self._req('POST', '/api/webhooks/gh-pr', raw=raw,
                              headers={'X-Hub-Signature-256': f'sha256={mac}'})
        self.assertEqual(status, 400)
        run = self._only('webhooks', 'gh-pr')
        self.assertEqual(run['reason'], 'invalid_payload')

    def test_an_oversized_body_is_recorded_without_claiming_a_verdict(self):
        """Verification has not run at the size check, so the entry carries no
        signature_verified: a cross there would read as 'the HMAC was wrong'."""
        self._webhook()
        raw = b'x' * (1024 * 1024 + 1)
        status, _ = self._req('POST', '/api/webhooks/gh-pr', raw=raw)
        self.assertEqual(status, 413)
        run = self._only('webhooks', 'gh-pr')
        self.assertEqual(run['reason'], 'payload_too_large')
        self.assertNotIn('signature_verified', run)

    def test_a_full_pod_is_recorded_as_rejected_at_capacity(self):
        self._webhook()
        self.tasks.status = 'rejected'
        self.tasks.error = 'too many running tasks'
        status, _ = self._signed('gh-pr')
        self.assertEqual(status, 429)
        run = self._only('webhooks', 'gh-pr')
        self.assertEqual((run['outcome'], run['reason']),
                         ('rejected', 'at_capacity'))
        self.assertIn('too many running tasks', run['error'])

    def test_a_failed_spawn_is_recorded_as_an_error(self):
        self._webhook()
        self.tasks.status = 'error'
        self.tasks.error = 'tmux unreachable'
        status, _ = self._signed('gh-pr')
        self.assertEqual(status, 502)
        run = self._only('webhooks', 'gh-pr')
        self.assertEqual((run['outcome'], run['reason']),
                         ('error', 'spawn_failed'))

    def test_a_post_to_an_unknown_id_writes_nothing_at_all(self):
        """Recording an id we do not have would let an anonymous caller create
        one ledger file per guessed id — a disk-fill primitive wearing an audit
        log's clothes."""
        status, _ = self._req('POST', '/api/webhooks/never-existed',
                              body={'n': 1})
        self.assertEqual(status, 404)
        root = server.TriggerRunsManager.RUNS_DIR
        self.assertFalse(os.path.isdir(os.path.join(root, 'webhook')))

    def test_an_unsigned_webhook_is_recorded_as_unverified_even_when_allowed(self):
        """KC_ALLOW_UNSIGNED_WEBHOOKS lets the POST through with nothing
        checked. The ledger reports what was verified, not what was allowed."""
        # create_or_update auto-mints a secret, so an open webhook can only be
        # a hand-written or migrated config. Write one directly, which is also
        # exactly how the configs this branch exists for got there.
        path = os.path.join(server.WebhookManager.WEBHOOKS_DIR, 'open-hook.json')
        with open(path, 'w') as f:
            json.dump({'id': 'open-hook', 'prompt_template': 'Do it',
                       'workdir': '/home/dev', 'interpolate_mode': 'attach',
                       'provider': 'generic'}, f)
        with mock.patch.dict(os.environ, {'KC_ALLOW_UNSIGNED_WEBHOOKS': '1'}):
            status, _ = self._req('POST', '/api/webhooks/open-hook',
                                  body={'n': 1})
        self.assertEqual(status, 202)
        run = self._only('webhooks', 'open-hook')
        self.assertEqual(run['outcome'], 'spawned')
        self.assertIs(run['signature_verified'], False,
                      'nothing was verified, so nothing may be claimed')

    def test_the_dashboard_test_button_is_marked_manual(self):
        """It authenticates as the workspace owner and never looks at an HMAC,
        so neither a tick nor a cross belongs in the verified column."""
        self._webhook()
        status, _ = self._req('POST', '/api/webhooks/gh-pr/test',
                              body={'payload': {'n': 1}})
        self.assertEqual(status, 202)
        run = self._only('webhooks', 'gh-pr')
        self.assertEqual(run['outcome'], 'spawned')
        self.assertTrue(run['manual'])
        self.assertNotIn('signature_verified', run)

    def test_a_forwarded_for_hop_is_kept_separate_from_the_socket_peer(self):
        self._webhook()
        raw = json.dumps({'n': 1}).encode()
        mac = hmac.new(self.SECRET.encode(), raw, hashlib.sha256).hexdigest()
        self._req('POST', '/api/webhooks/gh-pr', raw=raw, headers={
            'X-Hub-Signature-256': f'sha256={mac}',
            'X-Forwarded-For': '203.0.113.9, 10.0.0.1',
        })
        run = self._only('webhooks', 'gh-pr')
        self.assertEqual(run['source_ip'], '127.0.0.1')
        self.assertEqual(run['forwarded_for'], '203.0.113.9')

    def test_deleting_the_webhook_deletes_its_history(self):
        self._webhook()
        self._signed('gh-pr')
        self.assertEqual(self._runs('webhooks', 'gh-pr')['total'], 1)
        self.assertEqual(self._req('DELETE', '/api/webhooks/gh-pr')[0], 200)
        self._webhook()   # same id, recreated
        self.assertEqual(self._runs('webhooks', 'gh-pr')['total'], 0,
                         'a recreated id must not inherit a stranger\'s history')


# ── AC 1 + 2: cron fires, including the ones that are turned away ────────────

class CronLedgerTests(_ApiBase):
    def test_a_scheduled_fire_records_its_task(self):
        cfg = self._cron()
        status, body = self._fire_cron('nightly', cfg['fire_token'])
        self.assertEqual(status, 202, body)
        run = self._only('crons', 'nightly')
        self.assertEqual(run['outcome'], 'spawned')
        self.assertEqual(run['task_id'], body['task_id'])
        self.assertIs(run['signature_verified'], True)

    def test_a_stale_fire_token_is_visible_as_a_rejection(self):
        """What a rotated fire_token looks like from the receiver's side: the
        CronJob keeps firing, every call 404s, and until now nothing said so."""
        self._cron()
        status, _ = self._fire_cron('nightly', 'not-the-token')
        self.assertEqual(status, 404)
        run = self._only('crons', 'nightly')
        self.assertEqual((run['outcome'], run['reason']),
                         ('rejected', 'bad_token'))
        self.assertIs(run['signature_verified'], False)

    def test_a_fire_token_never_reaches_the_ledger(self):
        cfg = self._cron()
        self._fire_cron('nightly', cfg['fire_token'])
        blob = json.dumps(self._runs('crons', 'nightly'))
        self.assertNotIn(cfg['fire_token'], blob)

    def test_an_unknown_cron_id_writes_nothing(self):
        status, _ = self._fire_cron('no-such-cron', 'whatever')
        self.assertEqual(status, 404)
        root = server.TriggerRunsManager.RUNS_DIR
        self.assertFalse(os.path.isdir(os.path.join(root, 'cron')))

    def test_a_paused_cron_that_fires_anyway_is_recorded(self):
        cfg = self._cron()
        server.CronManager.set_suspended('nightly', True)
        status, _ = self._fire_cron('nightly', cfg['fire_token'])
        self.assertEqual(status, 409)
        self.assertEqual(self._only('crons', 'nightly')['reason'], 'suspended')

    def test_a_full_pod_is_recorded(self):
        cfg = self._cron()
        self.tasks.status = 'rejected'
        self.tasks.error = 'too many running tasks'
        self.assertEqual(self._fire_cron('nightly', cfg['fire_token'])[0], 429)
        self.assertEqual(self._only('crons', 'nightly')['reason'], 'at_capacity')

    def test_a_failed_spawn_is_recorded_as_error_even_though_the_http_code_is_202(self):
        """The handler answers 202 on a spawn error (changing that is a separate
        question); the ledger must agree with reality, not with the status code."""
        cfg = self._cron()
        self.tasks.status = 'error'
        self.tasks.error = 'tmux unreachable'
        self.assertEqual(self._fire_cron('nightly', cfg['fire_token'])[0], 202)
        run = self._only('crons', 'nightly')
        self.assertEqual((run['outcome'], run['reason']), ('error', 'spawn_failed'))

    def test_deleting_the_cron_deletes_its_history(self):
        cfg = self._cron()
        self._fire_cron('nightly', cfg['fire_token'])
        self.assertEqual(self._req('DELETE', '/api/crons/nightly')[0], 200)
        self._cron()
        self.assertEqual(self._runs('crons', 'nightly')['total'], 0)


# ── page-watches: a check that finds nothing IS the answer ───────────────────

class PageWatchLedgerTests(_ApiBase):
    def test_a_check_that_found_no_change_is_still_recorded(self):
        """For a webhook, 'it arrived' and 'it fired' are one event. For a watch
        they are not, and 'it checked on time and the page had not moved' is the
        most common answer to 'why didn't my watch fire?'."""
        cfg = self._watch()
        self._check_watch('ci', cfg['fire_token'])          # baseline
        self._check_watch('ci', cfg['fire_token'])          # unchanged
        reasons = [r['reason'] for r in self._runs('page-watches', 'ci')['runs']]
        self.assertEqual(reasons, ['unchanged', 'baseline'])

    def test_a_change_records_the_task_it_spawned(self):
        cfg = self._watch()
        self._check_watch('ci', cfg['fire_token'])
        status, body = self._check_watch('ci', cfg['fire_token'],
                                         body='<p>now passing</p>')
        self.assertEqual(status, 202, body)
        run = self._runs('page-watches', 'ci')['runs'][0]
        self.assertEqual(run['outcome'], 'spawned')
        self.assertEqual(run['task_id'], body['task_id'])

    def test_a_page_that_could_not_be_read_is_recorded_as_an_error(self):
        cfg = self._watch()
        with mock.patch.object(safe_http, 'fetch',
                               side_effect=OSError('connection refused')):
            status, _ = self._req(
                'POST', f"/api/triggers/page-watch-check/ci", body={},
                headers={'Authorization': f"Bearer {cfg['fire_token']}"})
        self.assertEqual(status, 200)
        run = self._runs('page-watches', 'ci')['runs'][0]
        self.assertEqual((run['outcome'], run['reason']), ('error', 'fetch_failed'))

    def test_a_paused_watch_whose_timer_fires_anyway_is_recorded(self):
        cfg = self._watch()
        server.PageWatchManager.set_suspended('ci', True)
        status, _ = self._check_watch('ci', cfg['fire_token'])
        self.assertEqual(status, 409)
        self.assertEqual(self._runs('page-watches', 'ci')['runs'][0]['reason'],
                         'suspended')

    def test_check_now_is_marked_manual(self):
        self._watch()
        with mock.patch.object(safe_http, 'fetch', lambda url, **kw: _page()):
            status, _ = self._req('POST', '/api/page-watches/ci/check',
                                  body={})
        self.assertEqual(status, 200)
        run = self._runs('page-watches', 'ci')['runs'][0]
        self.assertTrue(run['manual'])
        self.assertNotIn('signature_verified', run)


# ── AC 3: the read endpoints ────────────────────────────────────────────────

class RunsEndpointTests(_ApiBase):
    def test_runs_come_back_newest_first_and_paginated(self):
        cfg = self._cron()
        for _ in range(4):
            self._fire_cron('nightly', cfg['fire_token'])
        page = self._runs('crons', 'nightly', '?limit=2')
        self.assertEqual([r['task_id'] for r in page['runs']], ['tk-4', 'tk-3'])
        self.assertEqual((page['total'], page['limit'], page['offset']),
                         (4, 2, 0))
        second = self._runs('crons', 'nightly', '?limit=2&offset=2')
        self.assertEqual([r['task_id'] for r in second['runs']], ['tk-2', 'tk-1'])

    def test_a_trigger_that_never_fired_answers_an_empty_ledger(self):
        self._webhook()
        self.assertEqual(self._runs('webhooks', 'gh-pr'),
                         {'runs': [], 'total': 0, 'limit': 50, 'offset': 0})

    def test_an_unknown_trigger_is_404_not_an_empty_list(self):
        """An empty page for an id that does not exist would let the panel look
        healthy while pointing at nothing."""
        for path in ('/api/webhooks/nope/runs', '/api/crons/nope/runs',
                     '/api/page-watches/nope/runs'):
            with self.subTest(path=path):
                self.assertEqual(self._req('GET', path)[0], 404)

    def test_a_silly_limit_is_clamped_rather_than_honoured(self):
        self._cron()
        self.assertEqual(self._runs('crons', 'nightly', '?limit=99999')['limit'],
                         server.TriggerRunsManager.MAX_LIMIT)

    def test_the_three_kinds_keep_separate_ledgers(self):
        """Ids only have to be unique within a kind, so a webhook and a cron
        called the same thing must not share a history."""
        self._webhook('daily')
        cron = self._cron('daily')
        self._signed('daily')
        self._fire_cron('daily', cron['fire_token'])
        self.assertEqual(self._only('webhooks', 'daily')['type'], 'webhook')
        self.assertEqual(self._only('crons', 'daily')['type'], 'cron')


class RunsAuthTests(_ApiBase):
    AUTH_OK = False

    def test_reading_a_ledger_requires_auth(self):
        for path in ('/api/webhooks/x/runs', '/api/crons/x/runs',
                     '/api/page-watches/x/runs'):
            with self.subTest(path=path):
                status, body = self._req('GET', path)
                self.assertEqual(status, 401, body)


class RunsReadonlyTests(_ApiBase):
    """Read-only mode blocks mutations, and a ledger read is not one: the public
    demo already lists the triggers themselves."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(server, 'READONLY_MODE', True)
        p.start()
        self.addCleanup(p.stop)

    def test_readonly_mode_can_still_read_the_ledger(self):
        self._webhook()
        self.assertEqual(self._req('GET', '/api/webhooks/gh-pr/runs')[0], 200)


if __name__ == '__main__':
    unittest.main()
