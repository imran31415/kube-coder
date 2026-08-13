"""The /metrics/prometheus endpoint (#105).

Three things this file has to establish, in order of how expensive they are to
get wrong:

1. **The JSON /metrics is untouched.** The dashboard SPA's Metrics page reads
   it, and #363's product block rides along inside it. `JsonMetricsGoldenTests`
   does not assert that the response "looks like" the old one — it replays a
   golden captured from the code as it stood BEFORE this change
   (`tests/fixtures/metrics_json_golden.json`, produced by driving the
   pre-change handler with a fixed collector) and requires byte equality plus
   the same status and headers.

2. **The exposition parses.** Everything goes through `tests/promparse.py`, a
   parser written from the format spec rather than from our writer, so a pass
   is independent evidence. `prometheus_exposition_test.py` proves that parser
   rejects malformed documents, which is what stops these assertions being
   vacuous.

3. **The labels stay bounded and the types stay honest.** `CardinalityTests`
   fails if a per-request identifier ever becomes a label — one series per
   task id is the standard way to take a Prometheus down. `MetricTypeTests`
   fails if a figure recomputed from disk is typed as a counter: those fall
   when a task is pruned, and Prometheus reads a fall as a reset and credits
   the whole remaining value as fresh increase.

No test here touches live workspace state: task metas are fixtures, the
hypervisor and memory subsystems are stubbed, and the one test that exercises
the real embedding-queue query builds its own SQLite database in a tempdir.

Run with:  python3 -m unittest tests.prometheus_metrics_test
(from charts/workspace/)
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import server  # noqa: E402
import token_usage as tu  # noqa: E402
import promparse  # noqa: E402

GOLDEN = os.path.join(HERE, 'fixtures', 'metrics_json_golden.json')
PREFIX = server.PrometheusMetricsCollector.PREFIX


# ── fixtures ─────────────────────────────────────────────────────────────────

def _classes(inp=0, cache_read=0, cache_write=0, output=0):
    return {'input': inp, 'cache_read': cache_read,
            'cache_write': cache_write, 'output': output}


def _usage(by_model=None, legacy=0, coverage=tu.COVERAGE_MEASURED):
    """A v2 ledger with the given per-model class counts."""
    u = tu.empty_usage(source=tu.SOURCE_TRANSCRIPT, coverage=coverage)
    for model, cls in (by_model or {}).items():
        tu.add_usage(u, {**cls, 'records': 1,
                         'by_model': {model: {**cls, 'records': 1}}})
    if legacy:
        u['legacy_input_combined'] = legacy
    return u


#: Seven Builds covering every shape the collector has to fold: two measured
#: with a per-model breakdown, one uninstrumented assistant, one pre-#574
#: ledger whose input classes cannot be split, one status outside the known
#: set, one dead-lettered hook and one delivered.
TASK_METAS = [
    {'task_id': 't1', 'status': 'completed', 'assistant': 'claude',
     'usage': _usage({'model-a': _classes(10, 100, 5, 2)})},
    {'task_id': 't2', 'status': 'running', 'assistant': 'claude',
     'usage': _usage({'model-a': _classes(1, 2, 3, 4),
                      'model-b': _classes(5, 6, 7, 8)})},
    {'task_id': 't3', 'status': 'running', 'assistant': 'codex'},
    {'task_id': 't4', 'status': 'error', 'assistant': 'claude',
     'usage': _usage({}, legacy=7) | {'output': 3}},
    {'task_id': 't5', 'status': 'no-such-status', 'assistant': 'claude'},
    {'task_id': 't6', 'status': 'killed', 'assistant': 'claude',
     'hook_delivery': {'state': 'failed', 'attempts': 4}},
    {'task_id': 't7', 'status': 'completed', 'assistant': 'claude',
     'hook_delivery': {'state': 'delivered', 'attempts': 1}},
]

#: Threads: two models, no legacy residue, three uninstrumented sessions.
THREAD_TOKENS = _usage({'model-a': _classes(1, 1, 1, 1),
                        'model-c': _classes(2, 2, 2, 2)})


class _FakeHypervisor:
    @staticmethod
    def product_totals():
        threads = tu.public_block(THREAD_TOKENS, sessions=2)
        return {
            'chats': {'total': 5, 'active': 1},
            'tokens': {'total': threads['total'], 'input': 0, 'output': 0,
                       'per_session_avg': 0, 'schema': tu.SCHEMA_VERSION,
                       'threads': threads,
                       'coverage': tu.coverage_summary(measured=2,
                                                       not_instrumented=3)},
            'skills': {'invocations_by_name': {'kc-preflight': 4}},
        }


class _FakeMemory:
    pending = 12

    @classmethod
    def pending_embeddings(cls):
        return cls.pending

    @staticmethod
    def recall_counts(limit=10):
        return []


class _FakeWorker:
    running = True

    @classmethod
    def status(cls):
        return {'running': cls.running}


@contextlib.contextmanager
def stubbed(metas=None, hypervisor=_FakeHypervisor, memory=_FakeMemory,
            worker=_FakeWorker):
    """Point the collector at fixtures instead of the live workspace."""
    metas = TASK_METAS if metas is None else metas
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(
            server.ProjectsManager, '_scan_task_metas',
            staticmethod(lambda: list(metas))))
        stack.enter_context(mock.patch.object(server, 'HypervisorSession', hypervisor))
        stack.enter_context(mock.patch.object(server, 'MemoryManager', memory))
        stack.enter_context(mock.patch.object(server, 'EmbeddingWorker', worker))
        yield


def _render():
    return server.PrometheusMetricsCollector.render()


def _parsed(**kwargs):
    with stubbed(**kwargs):
        return promparse.assert_valid(_render(), prefix=PREFIX)


# ── 1. the JSON endpoint is byte-for-byte what it was ────────────────────────

def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _ServerTestCase(unittest.TestCase):
    """A real HTTP server on a loopback port, auth on, tasks dir in a tempdir."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix='kc-prom-')
        cls._auth_mode = server.AUTH_MODE
        cls._tasks_dir = server.ClaudeTaskManager.TASKS_DIR
        cls._token_file = server.ClaudeTaskManager.TOKEN_FILE
        # oauth2 is the mode where server.py is the enforcer; the default
        # 'basic' short-circuits check_claude_auth at the edge.
        server.AUTH_MODE = 'oauth2'
        server.ClaudeTaskManager.TASKS_DIR = cls.tmpdir
        server.ClaudeTaskManager.TOKEN_FILE = os.path.join(cls.tmpdir, '.api-token')
        cls.token = server.ClaudeTaskManager.get_or_create_token()
        cls.port = _free_port()
        cls.httpd = http.server.ThreadingHTTPServer(
            ('127.0.0.1', cls.port), server.BrowserHandler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.AUTH_MODE = cls._auth_mode
        server.ClaudeTaskManager.TASKS_DIR = cls._tasks_dir
        server.ClaudeTaskManager.TOKEN_FILE = cls._token_file
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def get(self, path, token=None):
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}')
        if token is not False:
            req.add_header('Authorization', f'Bearer {token or self.token}')
        return urllib.request.urlopen(req, timeout=10)


class JsonMetricsGoldenTests(_ServerTestCase):
    """/metrics must still return EXACTLY what it returned before #105.

    The golden was captured by driving the pre-change handler with the fixed
    collector output stored alongside it, so this is a replay of the old
    behaviour rather than a description of the new one.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with open(GOLDEN) as f:
            cls.golden = json.load(f)

    def _fetch(self):
        with mock.patch.object(server.MetricsCollector, 'get_all_metrics',
                               staticmethod(lambda: self.golden['fixture'])):
            with self.get('/metrics') as r:
                return r.status, dict(r.headers), r.read()

    def test_body_is_byte_identical(self):
        _, _, body = self._fetch()
        self.assertEqual(body,
                         self.golden['response']['body_latin1'].encode('latin-1'))

    def test_status_and_headers_are_identical(self):
        status, headers, body = self._fetch()
        want = self.golden['response']
        self.assertEqual(status, want['status'])
        self.assertEqual(headers.get('Content-type'), want['content_type'])
        self.assertEqual(headers.get('Cache-Control'), want['cache_control'])
        self.assertEqual(len(body), want['body_len'])

    def test_the_golden_is_the_json_the_collector_produced(self):
        # Guards the fixture itself: if someone regenerates the golden from a
        # different serialisation, this catches it independently of the server.
        self.assertEqual(
            self.golden['response']['body_latin1'].encode('latin-1'),
            json.dumps(self.golden['fixture']).encode())

    def test_product_block_still_rides_along(self):
        # #363's product section is nested inside this response; the SPA reads
        # it from here. A separate Prometheus path must not have moved it.
        _, _, body = self._fetch()
        self.assertIn('product', json.loads(body))


class JsonMetricsShapeTests(unittest.TestCase):
    """The golden above pins how the payload is serialised; this pins what
    goes into it.

    Necessary because the golden test hands the handler a fixed dict — so it
    would not notice `get_all_metrics` quietly dropping a section. Between the
    two, both halves of "the JSON endpoint is unchanged" are covered.
    """

    def _metrics(self):
        cheap = {'usage_percent': 0.0, 'cores': 1}
        with stubbed(), mock.patch.object(server.MetricsCollector, 'get_cpu_usage',
                                          staticmethod(lambda: cheap)):
            return server.MetricsCollector.get_all_metrics()

    def test_top_level_sections_are_unchanged(self):
        self.assertEqual(set(self._metrics()),
                         {'cpu', 'memory', 'disk', 'alerts', 'timestamp', 'product'})

    def test_product_sections_are_unchanged(self):
        self.assertEqual(set(self._metrics()['product']),
                         {'chats', 'tokens', 'skills', 'memory'})

    def test_the_574_token_shape_is_unchanged(self):
        # The pre-#574 keys keep their thread-only meaning and the
        # class-separated figures sit beside them. Both are on the wire the SPA
        # reads, so both are part of the compatibility surface.
        self.assertEqual(
            set(self._metrics()['product']['tokens']),
            {'total', 'input', 'output', 'per_session_avg', 'schema',
             'threads', 'builds', 'all', 'coverage'})


class RoutingAndAuthTests(_ServerTestCase):
    def test_unauthenticated_scrape_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get('/metrics/prometheus', token=False)
        self.assertEqual(ctx.exception.code, 401)

    def test_bad_token_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get('/metrics/prometheus', token='not-the-token')
        self.assertEqual(ctx.exception.code, 401)

    def test_scrape_returns_the_exposition_not_the_spa(self):
        # 'metrics' is not in SPA_TOP_LEVEL today; if it ever is, the SPA
        # catch-all runs first and this endpoint silently starts serving HTML.
        with stubbed():
            with self.get('/metrics/prometheus') as r:
                body = r.read().decode('utf-8')
                self.assertEqual(r.status, 200)
                self.assertEqual(r.headers.get('Content-type'),
                                 'text/plain; version=0.0.4; charset=utf-8')
                self.assertEqual(r.headers.get('Content-Length'),
                                 str(len(body.encode('utf-8'))))
        self.assertNotIn('<html', body.lower())
        promparse.assert_valid(body, prefix=PREFIX)

    def test_a_query_string_still_reaches_the_endpoint(self):
        with stubbed():
            with self.get('/metrics/prometheus?collect[]=x') as r:
                self.assertEqual(r.status, 200)
                promparse.assert_valid(r.read().decode('utf-8'), prefix=PREFIX)

    def test_json_metrics_and_prometheus_metrics_are_different_paths(self):
        with stubbed():
            with self.get('/metrics') as j, self.get('/metrics/prometheus') as p:
                self.assertEqual(j.headers.get('Content-type'), 'application/json')
                self.assertTrue(
                    p.headers.get('Content-type').startswith('text/plain'))


# ── 2. what the exposition says ──────────────────────────────────────────────

class TokenMetricTests(unittest.TestCase):
    """The figures #581 is blocked on: per-class, per-model, with coverage."""

    def setUp(self):
        self.p = _parsed()

    def _tok(self, scope, model, cls):
        return self.p.value(PREFIX + 'agent_tokens',
                            scope=scope, model=model, **{'class': cls})

    def test_the_four_classes_are_kept_apart_per_model(self):
        # t1 + t2 both spent on model-a: 10+1 fresh input, 100+2 cache read.
        self.assertEqual(self._tok('builds', 'model-a', 'input'), 11)
        self.assertEqual(self._tok('builds', 'model-a', 'cache_read'), 102)
        self.assertEqual(self._tok('builds', 'model-a', 'cache_write'), 8)
        self.assertEqual(self._tok('builds', 'model-a', 'output'), 6)
        self.assertEqual(self._tok('builds', 'model-b', 'input'), 5)

    def test_thread_and_build_scopes_are_separate_series(self):
        self.assertEqual(self._tok('threads', 'model-a', 'input'), 1)
        self.assertEqual(self._tok('builds', 'model-a', 'input'), 11)

    def test_no_all_scope_that_would_double_count_a_sum(self):
        self.assertEqual(self.p.label_values(PREFIX + 'agent_tokens', 'scope'),
                         {'threads', 'builds'})

    def test_summing_over_model_equals_the_ledgers_own_class_total(self):
        # THE INVARIANT: a partial by_model breakdown must not silently
        # under-report. t4's ledger has 3 output tokens and no model, so they
        # surface as model="unattributed" rather than disappearing.
        by_class = {}
        for labels, value in self.p.series(PREFIX + 'agent_tokens'):
            if labels['scope'] == 'builds':
                by_class[labels['class']] = by_class.get(labels['class'], 0) + value
        self.assertEqual(by_class['input'], 16)        # 10+1+5
        self.assertEqual(by_class['cache_read'], 108)  # 100+2+6
        self.assertEqual(by_class['cache_write'], 15)  # 5+3+7
        self.assertEqual(by_class['output'], 17)       # 2+4+8 + t4's orphaned 3
        self.assertEqual(self._tok('builds', 'unattributed', 'output'), 3)

    def test_unattributed_is_emitted_even_when_zero(self):
        # So the series exists from the first scrape and a dashboard does not
        # have to cope with it appearing later.
        self.assertEqual(self._tok('threads', 'unattributed', 'input'), 0)

    def test_pre_574_residue_is_a_separate_metric_never_summed_as_priceable(self):
        # t4's 7 input-side tokens were combined before they were recorded and
        # cannot be split; pricing them as fresh input would overstate ~10x.
        self.assertEqual(
            self.p.value(PREFIX + 'agent_tokens_unclassified', scope='builds'), 7)
        self.assertEqual(
            self.p.value(PREFIX + 'agent_tokens_unclassified', scope='threads'), 0)
        for labels, _ in self.p.series(PREFIX + 'agent_tokens'):
            self.assertIn(labels['class'], tu.CLASSES)

    def test_coverage_marks_the_zeros_that_are_not_measurements(self):
        # t3 runs an uninstrumented assistant: its 0 means "unknown", and this
        # is the only thing that says so.
        self.assertEqual(self.p.value(PREFIX + 'agent_runs', scope='builds',
                                      coverage='not_instrumented'), 1)
        self.assertEqual(self.p.value(PREFIX + 'agent_runs', scope='builds',
                                      coverage='measured'), 6)
        self.assertEqual(self.p.value(PREFIX + 'agent_runs', scope='threads',
                                      coverage='not_instrumented'), 3)
        self.assertEqual(self.p.value(PREFIX + 'agent_runs', scope='threads',
                                      coverage='measured'), 2)

    def test_every_coverage_state_is_present_even_at_zero(self):
        self.assertEqual(
            self.p.label_values(PREFIX + 'agent_runs', 'coverage'),
            {tu.COVERAGE_MEASURED, tu.COVERAGE_NOT_INSTRUMENTED,
             tu.COVERAGE_NO_SESSION})

    def test_an_empty_workspace_still_exposes_the_families(self):
        empty = _parsed(metas=[], hypervisor=None)
        self.assertEqual(empty.value(PREFIX + 'agent_tokens', scope='builds',
                                     model='unattributed', **{'class': 'input'}), 0)
        self.assertEqual(empty.value(PREFIX + 'metrics_collector_up',
                                     section='tokens'), 1)


class ModelCardinalityTests(unittest.TestCase):
    """`model` is the one label whose values come from data, not from code."""

    def _many_models(self, n):
        counts = {f'model-{i:03d}': _classes(inp=n - i) for i in range(n)}
        return [{'task_id': 'big', 'status': 'completed', 'assistant': 'claude',
                 'usage': _usage(counts)}]

    def test_model_count_is_capped(self):
        cap = server.PrometheusMetricsCollector.MAX_MODELS
        p = _parsed(metas=self._many_models(cap + 10), hypervisor=None)
        models = p.label_values(PREFIX + 'agent_tokens', 'model')
        # cap kept + 'other' + 'unattributed'
        self.assertEqual(len(models), cap + 2)
        self.assertIn('other', models)

    def test_the_capped_tail_is_folded_in_not_dropped(self):
        cap = server.PrometheusMetricsCollector.MAX_MODELS
        n = cap + 10
        p = _parsed(metas=self._many_models(n), hypervisor=None)
        total = sum(v for labels, v in p.series(PREFIX + 'agent_tokens')
                    if labels['scope'] == 'builds' and labels['class'] == 'input')
        self.assertEqual(total, sum(n - i for i in range(n)))

    def test_the_cap_sheds_the_smallest_spenders(self):
        cap = server.PrometheusMetricsCollector.MAX_MODELS
        p = _parsed(metas=self._many_models(cap + 10), hypervisor=None)
        models = p.label_values(PREFIX + 'agent_tokens', 'model')
        self.assertIn('model-000', models)          # the biggest
        self.assertNotIn(f'model-{cap + 9:03d}', models)  # the smallest

    def test_a_nameless_model_becomes_unknown_not_an_empty_label(self):
        metas = [{'task_id': 'x', 'status': 'completed', 'assistant': 'claude',
                  'usage': _usage({'': _classes(inp=5)})}]
        p = _parsed(metas=metas, hypervisor=None)
        self.assertEqual(p.value(PREFIX + 'agent_tokens', scope='builds',
                                 model='unknown', **{'class': 'input'}), 5)

    def test_a_model_named_like_a_sentinel_is_merged_not_overwritten(self):
        # 'unattributed' and 'other' are our own label values. A ledger is free
        # to contain a model with either name; replacing it with the sentinel
        # would break sum-over-model-equals-the-total.
        metas = [{'task_id': 'x', 'status': 'completed', 'assistant': 'claude',
                  'usage': _usage({'unattributed': _classes(inp=4),
                                   'other': _classes(inp=6)})}]
        p = _parsed(metas=metas, hypervisor=None)
        total = sum(v for labels, v in p.series(PREFIX + 'agent_tokens')
                    if labels['scope'] == 'builds' and labels['class'] == 'input')
        self.assertEqual(total, 10)

    def test_a_hostile_model_name_cannot_break_the_scrape(self):
        metas = [{'task_id': 'x', 'status': 'completed', 'assistant': 'claude',
                  'usage': _usage({'ev"il\\model\nname': _classes(inp=5)})}]
        p = _parsed(metas=metas, hypervisor=None)   # assert_valid: parses
        self.assertEqual(p.value(PREFIX + 'agent_tokens', scope='builds',
                                 model='ev"il\\model\nname',
                                 **{'class': 'input'}), 5)


class TaskAndHookMetricTests(unittest.TestCase):
    def setUp(self):
        self.p = _parsed()

    def test_tasks_are_counted_by_status(self):
        self.assertEqual(self.p.value(PREFIX + 'tasks', status='running'), 2)
        self.assertEqual(self.p.value(PREFIX + 'tasks', status='completed'), 2)
        self.assertEqual(self.p.value(PREFIX + 'tasks', status='error'), 1)
        self.assertEqual(self.p.value(PREFIX + 'tasks', status='killed'), 1)
        self.assertEqual(self.p.value(PREFIX + 'tasks',
                                      status='waiting-for-input'), 0)

    def test_an_unrecognised_status_folds_into_unknown(self):
        # t5's status is not one ClaudeTaskManager writes. Passing it through
        # as a label would let any future status mint a permanent series.
        self.assertEqual(self.p.value(PREFIX + 'tasks', status='unknown'), 1)
        self.assertNotIn('no-such-status',
                         self.p.label_values(PREFIX + 'tasks', 'status'))

    def test_dead_letters_are_counted_from_disk_so_they_survive_a_restart(self):
        self.assertEqual(self.p.value(PREFIX + 'hook_dead_letters'), 1)

    def test_hook_deliveries_are_counted_in_process(self):
        # Deltas, not absolutes: these counters are process-wide by design and
        # other suites in this run deliver hooks from daemon threads that can
        # outlive the test that started them. Asserting on a zero baseline here
        # is a flake waiting for a slow CI runner.
        before = _parsed()
        for outcome in ('delivered', 'failed', 'failed'):
            server.ProcessCounters.inc(
                server.PrometheusMetricsCollector.HOOK_COUNTER,
                {'outcome': outcome})
        after = _parsed()
        name = PREFIX + 'hook_deliveries_total'
        self.assertEqual(after.value(name, outcome='delivered')
                         - before.value(name, outcome='delivered'), 1)
        self.assertEqual(after.value(name, outcome='failed')
                         - before.value(name, outcome='failed'), 2)

    def test_counters_are_thread_safe(self):
        # A lost update is what this catches: `d[k] += 1` is several bytecodes
        # and hooks are delivered from daemon threads.
        name = 'test-thread-safety'

        def bump():
            for _ in range(500):
                server.ProcessCounters.inc(name, {'outcome': 'delivered'})

        threads = [threading.Thread(target=bump) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(
            server.ProcessCounters.value(name, {'outcome': 'delivered'}), 8 * 500)


class DeliveryInstrumentationTests(unittest.TestCase):
    """The counter is incremented by the delivery path itself, not inferred."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='kc-prom-hook-')
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmpdir,
                                                            ignore_errors=True))
        self._tasks_dir = server.ClaudeTaskManager.TASKS_DIR
        server.ClaudeTaskManager.TASKS_DIR = self.tmpdir
        self.addCleanup(setattr, server.ClaudeTaskManager, 'TASKS_DIR',
                        self._tasks_dir)
        self.before = {o: self._counter(o) for o in ('delivered', 'failed')}

    @staticmethod
    def _counter(outcome):
        return server.ProcessCounters.value(
            server.PrometheusMetricsCollector.HOOK_COUNTER, {'outcome': outcome})

    def _delta(self, outcome):
        """Change since setUp. Absolutes would race the delivery threads other
        suites leave running (see test_hook_deliveries_are_counted_in_process)."""
        return self._counter(outcome) - self.before[outcome]

    def test_a_successful_delivery_increments_delivered(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value = mock.Mock(status=200)
        with mock.patch.object(server.ClaudeTaskManager, '_hook_urlopen',
                               return_value=resp):
            server.ClaudeTaskManager._deliver_hook(
                'task-1', 'https://example.test/hook', b'{}', {})
        self.assertEqual(self._delta('delivered'), 1)

    def test_an_exhausted_delivery_increments_failed_once(self):
        with mock.patch.object(server.ClaudeTaskManager, '_hook_urlopen',
                               side_effect=OSError('nope')), \
                mock.patch.object(server.time, 'sleep', lambda *_: None):
            server.ClaudeTaskManager._deliver_hook(
                'task-2', 'https://example.test/hook', b'{}', {}, max_attempts=3)
        # Three attempts, ONE terminal outcome — the counter counts outcomes,
        # not retries, so a flaky endpoint doesn't inflate the failure rate.
        self.assertEqual(self._delta('failed'), 1)
        self.assertEqual(self._delta('delivered'), 0)


class MemoryMetricTests(unittest.TestCase):
    def test_queue_depth_and_worker_state_are_reported(self):
        p = _parsed()
        self.assertEqual(p.value(PREFIX + 'memory_embeddings_pending'), 12)
        self.assertEqual(p.value(PREFIX + 'memory_embeddings_worker_up'), 1)

    def test_a_stopped_worker_reads_zero(self):
        class Stopped(_FakeWorker):
            running = False
        p = _parsed(worker=Stopped)
        self.assertEqual(p.value(PREFIX + 'memory_embeddings_worker_up'), 0)


class RealQueueDepthTests(unittest.TestCase):
    """`pending_embeddings` against a real SQLite database — in a tempdir.

    Never the workspace's own store: /home/dev/.claude-memory/memory.db is live
    shared state that the dashboard, the MCP server and every running agent
    write concurrently.
    """

    def setUp(self):
        import memory.store as store_mod
        from memory.manager import MemoryManager
        from memory.store import MemoryStore
        self.MemoryManager = MemoryManager
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_store = MemoryManager._store
        self._orig_init = store_mod._INITIALIZED
        store_mod._INITIALIZED = False
        MemoryManager._store = MemoryStore(os.path.join(self._tmp.name, 'm.db'))

        def restore():
            MemoryManager._store = self._orig_store
            store_mod._INITIALIZED = self._orig_init
        self.addCleanup(restore)

    def test_depth_tracks_memories_awaiting_embedding(self):
        self.assertEqual(self.MemoryManager.pending_embeddings(), 0)
        for i in range(3):
            self.MemoryManager.upsert(namespace='user', key=f'k{i}', value='v')
        self.assertEqual(self.MemoryManager.pending_embeddings(), 3)

    def test_rewriting_a_memory_does_not_inflate_the_depth(self):
        # #597 bounds the queue at one row per memory; the gauge inherits that
        # bound, which is what makes it cheap to compute on every scrape.
        for i in range(20):
            self.MemoryManager.upsert(namespace='user', key='k', value=f'v{i}')
        self.assertEqual(self.MemoryManager.pending_embeddings(), 1)


# ── 3. cardinality, types, isolation, cost ───────────────────────────────────

class CardinalityTests(unittest.TestCase):
    """Every label must come from a small, fixed vocabulary."""

    #: Labels that identify one request, task, thread or path. Each distinct
    #: value is a permanent time series; this is how a Prometheus falls over.
    FORBIDDEN = {'task_id', 'session_id', 'thread_id', 'id', 'workdir', 'path',
                 'project', 'project_id', 'url', 'prompt', 'user', 'name',
                 'parent_task_id', 'key', 'namespace', 'error'}

    ALLOWED = {'scope', 'class', 'model', 'coverage', 'status', 'outcome',
               'section'}

    def setUp(self):
        self.p = _parsed()

    def test_no_unbounded_label_is_used_anywhere(self):
        used = {label for _, labels in self.p.samples for label, _ in labels}
        self.assertEqual(used & self.FORBIDDEN, set())
        self.assertLessEqual(used, self.ALLOWED)

    def test_series_count_stays_small_and_does_not_grow_with_task_count(self):
        before = len(self.p.samples)
        many = [dict(TASK_METAS[0], task_id=f't{i}') for i in range(500)]
        after = len(_parsed(metas=TASK_METAS + many).samples)
        self.assertEqual(before, after)
        self.assertLess(after, 100)

    def test_every_label_value_is_bounded_in_length(self):
        for _, labels in self.p.samples:
            for _, value in labels:
                self.assertLessEqual(len(value),
                                     server.prom.MAX_LABEL_VALUE_LEN)


class MetricTypeTests(unittest.TestCase):
    """Counter vs gauge, decided by whether the number can fall."""

    #: Recomputed from task.json / thread.json / SQLite on every scrape. All of
    #: these fall when the underlying record is deleted, and Prometheus reads a
    #: fall as a counter reset — crediting the post-fall value as fresh
    #: increase. They are gauges for that reason, not for tidiness.
    #: The board families (#588 Phase 7) are gauges for the same reason plus
    #: one of their own: the decision ledger ROTATES, so an old decision ageing
    #: out lowers a total. That is a deliberate ceiling on unbounded disk
    #: growth, and a counter that can fall is exactly what Prometheus
    #: misreads as a reset.
    DISK_DERIVED = ('agent_tokens', 'agent_tokens_unclassified', 'agent_runs',
                    'tasks', 'hook_dead_letters', 'memory_embeddings_pending',
                    'memory_embeddings_worker_up', 'metrics_collector_up',
                    'board_dispositions', 'board_decisions',
                    'board_approval_rate', 'board_review_open')

    #: Incremented in memory as the event happens, so monotonic for the life of
    #: the process. A restart resets it, which Prometheus models correctly.
    IN_PROCESS = ('hook_deliveries_total',)

    def setUp(self):
        self.p = _parsed()

    def test_disk_derived_metrics_are_gauges(self):
        for name in self.DISK_DERIVED:
            self.assertEqual(self.p.types[PREFIX + name], 'gauge', name)

    def test_in_process_counters_are_counters(self):
        for name in self.IN_PROCESS:
            self.assertEqual(self.p.types[PREFIX + name], 'counter', name)

    def test_every_family_is_accounted_for_by_this_file(self):
        # So a new metric cannot be added without a deliberate type decision.
        self.assertEqual(
            set(self.p.types),
            {PREFIX + n for n in self.DISK_DERIVED + self.IN_PROCESS})

    def test_a_disk_derived_total_falls_when_a_task_is_pruned(self):
        # The concrete reason DISK_DERIVED cannot be counters: delete the task
        # and the number goes down. Demonstrated, not asserted.
        full = _parsed().value(PREFIX + 'agent_tokens', scope='builds',
                               model='model-a', **{'class': 'input'})
        pruned = _parsed(metas=TASK_METAS[1:]).value(
            PREFIX + 'agent_tokens', scope='builds', model='model-a',
            **{'class': 'input'})
        self.assertLess(pruned, full)

    def test_every_family_carries_help_text(self):
        for name in self.p.types:
            self.assertTrue(self.p.helps.get(name, '').strip(), name)


class SectionIsolationTests(unittest.TestCase):
    """One broken source must cost its own section, not the scrape."""

    def test_a_failing_section_is_reported_not_hidden(self):
        class Broken(_FakeMemory):
            @classmethod
            def pending_embeddings(cls):
                raise RuntimeError('database is locked')

        p = _parsed(memory=Broken)
        self.assertEqual(p.value(PREFIX + 'metrics_collector_up',
                                 section='memory'), 0)
        self.assertEqual(p.value(PREFIX + 'metrics_collector_up',
                                 section='tasks'), 1)
        # …and the section's own metric is ABSENT rather than reported as 0,
        # so nobody reads a broken collector as a drained queue.
        self.assertNotIn(PREFIX + 'memory_embeddings_pending', p.names())

    def test_an_unreadable_tasks_directory_does_not_kill_the_document(self):
        with mock.patch.object(server.ProjectsManager, '_scan_task_metas',
                               staticmethod(mock.Mock(side_effect=OSError('nope')))), \
                mock.patch.object(server, 'HypervisorSession', None), \
                mock.patch.object(server, 'MemoryManager', _FakeMemory), \
                mock.patch.object(server, 'EmbeddingWorker', _FakeWorker):
            p = promparse.assert_valid(_render(), prefix=PREFIX)
        self.assertEqual(p.value(PREFIX + 'tasks', status='running'), 0)
        self.assertEqual(p.value(PREFIX + 'memory_embeddings_pending'), 12)

    def test_a_missing_memory_subsystem_marks_the_section_down(self):
        p = _parsed(memory=None, worker=None)
        self.assertEqual(p.value(PREFIX + 'metrics_collector_up',
                                 section='memory'), 0)

    def test_every_section_is_reported(self):
        p = _parsed()
        self.assertEqual(p.label_values(PREFIX + 'metrics_collector_up', 'section'),
                         set(server.PrometheusMetricsCollector.SECTIONS))


class ScrapeCostTests(unittest.TestCase):
    """Prometheus scrapes every 15-30s; the endpoint must stay cheap."""

    def test_the_tasks_directory_is_walked_exactly_once_per_scrape(self):
        # Token totals and the task/hook gauges both need every task.json.
        # Two independent scans would double the cost of the most expensive
        # thing on the endpoint.
        scan = mock.Mock(return_value=list(TASK_METAS))
        with mock.patch.object(server.ProjectsManager, '_scan_task_metas',
                               staticmethod(scan)), \
                mock.patch.object(server, 'HypervisorSession', _FakeHypervisor), \
                mock.patch.object(server, 'MemoryManager', _FakeMemory), \
                mock.patch.object(server, 'EmbeddingWorker', _FakeWorker):
            _render()
        self.assertEqual(scan.call_count, 1)

    def test_cpu_sampling_is_not_on_the_scrape_path(self):
        # get_cpu_usage sleeps 500ms to difference /proc/stat. Node exporter
        # and cAdvisor already report CPU; paying half a second per scrape for
        # a second copy is not a trade worth making.
        with mock.patch.object(server.MetricsCollector, 'get_cpu_usage',
                               staticmethod(mock.Mock(
                                   side_effect=AssertionError('sampled CPU')))):
            with stubbed():
                _render()

    def test_the_expensive_memory_stats_query_is_not_used(self):
        # stats() runs seven counts including full scans of memory_history and
        # memory_refs; queue depth is one count on a bounded table.
        class Watched(_FakeMemory):
            @staticmethod
            def stats():
                raise AssertionError('called stats() on the scrape path')

        _parsed(memory=Watched)


class BackwardCompatibilityTests(unittest.TestCase):
    """The `task_metas` parameter added for sharing the scan is optional."""

    def test_build_token_totals_still_scans_when_called_with_no_argument(self):
        scan = mock.Mock(return_value=list(TASK_METAS))
        with mock.patch.object(server.ProjectsManager, '_scan_task_metas',
                               staticmethod(scan)):
            usage, coverage = server.ClaudeTaskManager.build_token_totals()
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(usage['input'], 16)
        self.assertEqual(coverage['not_instrumented'], 1)

    def test_passing_metas_in_gives_the_same_answer_as_scanning(self):
        with mock.patch.object(server.ProjectsManager, '_scan_task_metas',
                               staticmethod(lambda: list(TASK_METAS))):
            scanned = server.ClaudeTaskManager.build_token_totals()
        passed = server.ClaudeTaskManager.build_token_totals(list(TASK_METAS))
        self.assertEqual(scanned, passed)

    def test_get_product_metrics_still_works_with_no_argument(self):
        with stubbed():
            product = server.ProductMetricsCollector.get_product_metrics()
        self.assertEqual(product['tokens']['builds']['input'], 16)
        self.assertEqual(product['chats']['total'], 5)


if __name__ == '__main__':
    unittest.main()
