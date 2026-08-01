"""Build token accounting in server.py (#574).

Builds run an interactive CLI in a tmux pane with no structured stream, so no
Build had ever reported a token. These tests cover the recovery path end to end
at the unit level: the pinned Claude session id, reading the transcript it names,
idempotency across polls, and the coverage marker that keeps a 0 from an
uninstrumented assistant out of the measured column.

Run with:    python3 -m unittest tests.build_usage_test
(from charts/workspace/)
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import uuid as uuidlib
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import server  # noqa: E402
import token_usage as tu  # noqa: E402
# Shared large-transcript fixture builders — deliberately reused so the Build
# path and the reader are held to the same scale (see token_usage_test).
from token_usage_test import (  # noqa: E402
    GROUPS, big_records, big_total, write_jsonl, append_jsonl)

CTM = server.ClaudeTaskManager
FIXTURE = os.path.join(HERE, 'fixtures', 'claude_usage_transcript.jsonl')

# The fixture's two API responses, summed.
FIXTURE_INPUT = 2 + 2
FIXTURE_CACHE_WRITE = 15733 + 18426
FIXTURE_CACHE_READ = 15273 + 20628
FIXTURE_OUTPUT = 4 + 319
FIXTURE_TOTAL = FIXTURE_INPUT + FIXTURE_CACHE_WRITE + FIXTURE_CACHE_READ + FIXTURE_OUTPUT


class ValidUuidTest(unittest.TestCase):
    def test_accepts_a_real_uuid(self):
        self.assertTrue(server._valid_uuid(str(uuidlib.uuid4())))
        self.assertTrue(server._valid_uuid(' ' + str(uuidlib.uuid4()) + ' '))

    def test_rejects_anything_else(self):
        for bad in ('', None, 42, 'not-a-uuid', '../../etc/passwd',
                    'a; rm -rf /', str(uuidlib.uuid4())[:-1],
                    str(uuidlib.uuid4()) + 'x', '/home/dev/x.jsonl'):
            self.assertFalse(server._valid_uuid(bad), repr(bad))


class SessionIdPinnedTest(unittest.TestCase):
    """The Claude CLI is launched with --session-id so the Build's transcript
    lands at a path we KNOW — the alternative (newest .jsonl in the project dir)
    silently attributes another session's spend when two share a workdir.

    The `claude --help` probe is stubbed in both directions: CI has no `claude`
    binary, and a cached probe result must not leak between tests."""

    def setUp(self):
        self._probe = mock.patch.object(CTM, '_CLAUDE_SESSION_ID_SUPPORTED', True)
        self._probe.start()

    def tearDown(self):
        self._probe.stop()

    def test_claude_command_pins_the_session_id(self):
        sid = str(uuidlib.uuid4())
        cmd = CTM.assistant_command('claude', session_id=sid)
        self.assertIn(f'--session-id {sid}', cmd)

    def test_malformed_session_id_is_dropped_not_passed(self):
        for bad in ('', 'nope', '$(id)', None):
            cmd = CTM.assistant_command('claude', session_id=bad)
            self.assertNotIn('--session-id', cmd)

    def test_other_assistants_are_unchanged(self):
        sid = str(uuidlib.uuid4())
        for a in ('ante', 'codex', 'librefang', 'opencode-deepseek',
                  'kc-harness'):
            self.assertNotIn('--session-id',
                             CTM.assistant_command(a, session_id=sid), a)

    def test_flags_compose(self):
        sid = str(uuidlib.uuid4())
        cmd = CTM.assistant_command('claude', auto_approve=True,
                                    model='claude-opus-5', session_id=sid)
        self.assertIn('--dangerously-skip-permissions', cmd)
        self.assertIn('--model claude-opus-5', cmd)
        self.assertIn(f'--session-id {sid}', cmd)

    def test_a_cli_without_the_flag_launches_unchanged(self):
        """Measurement must never be able to stop a Build from starting: an
        unrecognised flag would make the CLI refuse to launch."""
        sid = str(uuidlib.uuid4())
        with mock.patch.object(CTM, '_CLAUDE_SESSION_ID_SUPPORTED', False):
            self.assertEqual(CTM.assistant_command('claude', session_id=sid),
                             'claude')


class SessionIdProbeTest(unittest.TestCase):
    def setUp(self):
        self._reset()

    def tearDown(self):
        self._reset()

    def _reset(self):
        CTM._CLAUDE_SESSION_ID_SUPPORTED = None

    def test_detects_support_from_help_and_caches_it(self):
        with mock.patch.object(server.subprocess, 'run',
                               return_value=mock.Mock(
                                   stdout='  --session-id <uuid>  ...')) as run:
            self.assertTrue(CTM._claude_supports_session_id())
            self.assertTrue(CTM._claude_supports_session_id())
        self.assertEqual(run.call_count, 1)

    def test_absent_flag_reads_as_unsupported(self):
        with mock.patch.object(server.subprocess, 'run',
                               return_value=mock.Mock(stdout='usage: claude')):
            self.assertFalse(CTM._claude_supports_session_id())

    def test_a_missing_binary_reads_as_unsupported(self):
        with mock.patch.object(server.subprocess, 'run',
                               side_effect=FileNotFoundError('no claude')):
            self.assertFalse(CTM._claude_supports_session_id())


class CreateTaskRecordsSessionIdTest(unittest.TestCase):
    """create_task stamps the id it pinned — and only when it really pinned it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._patches = [
            mock.patch.object(CTM, 'TASKS_DIR', self.tmp),
            mock.patch.object(CTM, 'at_capacity', return_value=(False, 0, 12)),
            mock.patch.object(CTM, '_ensure_claude_trust'),
            mock.patch.object(server.ProjectsManager, 'project_for_workdir',
                              return_value=''),
            mock.patch.object(server.ProjectsManager, 'defaults_for',
                              return_value=('', '', '')),
            mock.patch.object(server.ProviderKeysManager, 'env_overlay',
                              return_value={}),
            mock.patch.object(server, 'threading'),
            mock.patch.object(server.EventBroker, 'publish'),
            mock.patch.object(server.subprocess, 'run',
                              return_value=mock.Mock(returncode=0, stdout='',
                                                     stderr='')),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _created(self, **kw):
        meta = CTM.create_task('hi', workdir='/home/dev/proj', **kw)
        with open(os.path.join(self.tmp, meta['task_id'], 'task.json')) as f:
            return json.load(f)

    def test_claude_build_records_the_pinned_session_id(self):
        with mock.patch.object(CTM, '_CLAUDE_SESSION_ID_SUPPORTED', True):
            on_disk = self._created(assistant='claude')
        self.assertEqual(on_disk['claude_session_id'], on_disk['session_id'])
        self.assertTrue(server._valid_uuid(on_disk['claude_session_id']))

    def test_no_id_is_claimed_when_the_cli_cannot_be_pinned(self):
        with mock.patch.object(CTM, '_CLAUDE_SESSION_ID_SUPPORTED', False):
            on_disk = self._created(assistant='claude')
        self.assertEqual(on_disk['claude_session_id'], '')

    def test_other_assistants_record_no_claude_session_id(self):
        with mock.patch.object(CTM, '_CLAUDE_SESSION_ID_SUPPORTED', True):
            on_disk = self._created(assistant='ante')
        self.assertEqual(on_disk['claude_session_id'], '')


class TaskUsageTest(unittest.TestCase):
    """ingest_usage against a task dir + a fake ~/.claude/projects tree."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tasks = os.path.join(self.tmp, 'tasks')
        os.makedirs(self.tasks)
        self.projects = os.path.join(self.tmp, 'projects')
        os.makedirs(self.projects)
        self._tasks_patch = mock.patch.object(CTM, 'TASKS_DIR', self.tasks)
        self._tasks_patch.start()
        # Route both session-log resolvers at our fake project tree, keeping the
        # real slug + exact-<session_id>.jsonl semantics.
        self._proj_patch = mock.patch.object(
            server, 'hv_claude_project_dir', lambda wd: self._proj_dir(wd))
        self._proj_patch.start()
        self._loc_patch = mock.patch.object(
            server, 'hv_locate_session_log', self._locate)
        self._loc_patch.start()

    def tearDown(self):
        self._loc_patch.stop()
        self._proj_patch.stop()
        self._tasks_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _proj_dir(self, workdir):
        import re
        return os.path.join(self.projects,
                            re.sub(r'[^A-Za-z0-9]', '-', workdir or ''))

    def _locate(self, workdir, session_id=None):
        p = os.path.join(self._proj_dir(workdir), f'{session_id}.jsonl')
        return p if os.path.isfile(p) else None

    def _task(self, **over):
        task_id = over.pop('task_id', f'{int(time.time())}-abcd1234')
        meta = {
            'task_id': task_id,
            'session_id': str(uuidlib.uuid4()),
            'assistant': 'claude',
            'workdir': '/home/dev/proj',
            'status': 'completed',
        }
        meta['claude_session_id'] = meta['session_id']
        meta.update(over)
        d = os.path.join(self.tasks, task_id)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'task.json'), 'w') as f:
            json.dump(meta, f)
        return meta, d

    def _put_transcript(self, meta, src=FIXTURE):
        """Place a transcript where the resolver will find it. `src=None` returns
        the path without writing, for callers that build their own."""
        d = self._proj_dir(meta['workdir'])
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, f'{meta["claude_session_id"]}.jsonl')
        if src is not None:
            shutil.copyfile(src, dst)
        return dst

    # ── the headline: a Build reports tokens where it never could before ──

    def test_build_usage_is_recorded_from_the_transcript(self):
        meta, d = self._task()
        self._put_transcript(meta)
        u = CTM.ingest_usage(meta, d)
        self.assertEqual(u['input'], FIXTURE_INPUT)
        self.assertEqual(u['cache_read'], FIXTURE_CACHE_READ)
        self.assertEqual(u['cache_write'], FIXTURE_CACHE_WRITE)
        self.assertEqual(u['output'], FIXTURE_OUTPUT)
        self.assertEqual(u['records'], 2)
        self.assertEqual(u['coverage'], tu.COVERAGE_MEASURED)
        self.assertEqual(u['source'], tu.SOURCE_TRANSCRIPT)
        self.assertTrue(u['transcript_found'])
        self.assertEqual(set(u['by_model']),
                         {'claude-opus-5', 'claude-opus-4-8'})
        # Persisted, with the resume state kept separate.
        with open(os.path.join(d, 'task.json')) as f:
            on_disk = json.load(f)
        self.assertEqual(tu.classes_total(on_disk['usage']), FIXTURE_TOTAL)
        self.assertIn('usage_ingest', on_disk)

    def test_ingesting_twice_does_not_double_the_total(self):
        meta, d = self._task(status='running')
        self._put_transcript(meta)
        first = CTM.ingest_usage(meta, d)
        with mock.patch.object(CTM, 'USAGE_SCAN_INTERVAL', 0):
            second = CTM.ingest_usage(meta, d)
            third = CTM.ingest_usage(meta, d)
        self.assertEqual(tu.classes_total(first), FIXTURE_TOTAL)
        self.assertEqual(tu.classes_total(second), FIXTURE_TOTAL)
        self.assertEqual(tu.classes_total(third), FIXTURE_TOTAL)

    def test_a_long_build_is_not_double_counted_across_polls(self):
        """The production polling path at a scale the dedupe ring cannot cover.

        The small-transcript cases above stay green even with the resume offset
        broken, because a ring of _KEY_RING keys re-catches every record in a
        3-record file. Here the transcript carries GROUPS (>> _KEY_RING) API
        responses, the resume state round-trips through task.json on disk between
        every poll, and the total must not move."""
        self.assertGreater(GROUPS, tu._KEY_RING * 2,
                           'fixture must comfortably outrun the dedupe ring')
        meta, d = self._task(status='running')
        path = self._put_transcript(meta, src=None)
        write_jsonl(path, big_records())
        first = CTM.ingest_usage(meta, d)
        self.assertEqual(first['records'], GROUPS)
        self.assertEqual(tu.classes_total(first), big_total())
        with mock.patch.object(CTM, 'USAGE_SCAN_INTERVAL', 0):
            for i in range(4):
                # Re-read the meta from disk each poll, so the resume state is
                # exactly what was persisted — not a live in-memory dict.
                with open(os.path.join(d, 'task.json')) as f:
                    fresh = json.load(f)
                again = CTM.ingest_usage(fresh, d)
                self.assertEqual(tu.classes_total(again), big_total(),
                                 f'poll {i + 1} changed the total')
                self.assertEqual(again['records'], GROUPS, f'poll {i + 1}')

    def test_a_growing_build_lands_on_the_cold_scan_total(self):
        """Append-and-poll, group-splitting appends included, must end at exactly
        the figure one cold scan of the finished transcript gives."""
        meta, d = self._task(status='running')
        path = self._put_transcript(meta, src=None)
        chunk = GROUPS // 4
        write_jsonl(path, big_records(chunk))
        CTM.ingest_usage(meta, d)
        gid = chunk
        with mock.patch.object(CTM, 'USAGE_SCAN_INTERVAL', 0):
            while gid < GROUPS:
                batch = min(chunk, GROUPS - gid)
                recs = big_records(batch, start=gid)
                cut = len(recs) - 2      # split one group across two polls
                for part in (recs[:cut], recs[cut:]):
                    append_jsonl(path, part)
                    with open(os.path.join(d, 'task.json')) as f:
                        fresh = json.load(f)
                    usage = CTM.ingest_usage(fresh, d)
                gid += batch
        cold, _ = tu.ingest([path])
        self.assertEqual(cold['records'], GROUPS)
        self.assertEqual(tu.classes_total(cold), big_total())
        self.assertEqual(tu.classes_total(usage), tu.classes_total(cold))
        self.assertEqual(usage['by_model'], cold['by_model'])

    def test_growing_transcript_accumulates_without_recounting(self):
        meta, d = self._task(status='running')
        path = self._put_transcript(meta)
        CTM.ingest_usage(meta, d)
        with open(path, 'a') as f:
            f.write(json.dumps({
                'type': 'assistant', 'uuid': 'new-1',
                'message': {'id': 'msg_new', 'model': 'claude-opus-5',
                            'usage': {'input_tokens': 7, 'output_tokens': 3,
                                      'cache_read_input_tokens': 0,
                                      'cache_creation_input_tokens': 0}}}) + '\n')
        with mock.patch.object(CTM, 'USAGE_SCAN_INTERVAL', 0):
            u = CTM.ingest_usage(meta, d)
        self.assertEqual(tu.classes_total(u), FIXTURE_TOTAL + 10)
        self.assertEqual(u['records'], 3)

    def test_subagent_transcripts_are_counted_too(self):
        meta, d = self._task()
        self._put_transcript(meta)
        subs = os.path.join(self._proj_dir(meta['workdir']),
                            meta['claude_session_id'], 'subagents')
        os.makedirs(subs)
        with open(os.path.join(subs, 'agent-a1.jsonl'), 'w') as f:
            f.write(json.dumps({
                'type': 'assistant', 'uuid': 's1', 'isSidechain': True,
                'message': {'id': 'msg_sub', 'model': 'claude-haiku-4-5',
                            'usage': {'input_tokens': 100, 'output_tokens': 20,
                                      'cache_read_input_tokens': 0,
                                      'cache_creation_input_tokens': 0}}}) + '\n')
        u = CTM.ingest_usage(meta, d)
        self.assertEqual(tu.classes_total(u), FIXTURE_TOTAL + 120)
        self.assertIn('claude-haiku-4-5', u['by_model'])
        self.assertEqual(u['files'], 2)

    # ── coverage: a 0 must say why ──

    def test_uninstrumented_assistant_is_marked_not_measured(self):
        meta, d = self._task(assistant='codex')
        u = CTM.ingest_usage(meta, d)
        self.assertEqual(u['coverage'], tu.COVERAGE_NOT_INSTRUMENTED)
        self.assertEqual(tu.classes_total(u), 0)
        # Stamped once, then it stops doing work.
        with mock.patch.object(server.tu, 'empty_usage',
                               side_effect=AssertionError('re-stamped')):
            self.assertEqual(CTM.ingest_usage(meta, d)['coverage'],
                             tu.COVERAGE_NOT_INSTRUMENTED)

    def test_terminal_task_row_is_marked_not_instrumented(self):
        meta, d = self._task(assistant=None, kind='terminal')
        self.assertEqual(CTM.ingest_usage(meta, d)['coverage'],
                         tu.COVERAGE_NOT_INSTRUMENTED)

    def test_legacy_build_without_a_session_id_is_not_guessed(self):
        """No most-recently-modified fallback: an honest unknown beats silently
        attributing another session's spend."""
        meta, d = self._task(claude_session_id='')
        # A transcript IS present in the project dir — it must still not be used.
        other = os.path.join(self._proj_dir(meta['workdir']))
        os.makedirs(other, exist_ok=True)
        shutil.copyfile(FIXTURE, os.path.join(other, f'{uuidlib.uuid4()}.jsonl'))
        u = CTM.ingest_usage(meta, d)
        self.assertEqual(u['coverage'], tu.COVERAGE_NO_SESSION)
        self.assertEqual(tu.classes_total(u), 0)

    def test_malformed_session_id_is_treated_as_absent(self):
        meta, d = self._task(claude_session_id='../../etc/passwd')
        self.assertEqual(CTM.ingest_usage(meta, d)['coverage'],
                         tu.COVERAGE_NO_SESSION)

    # ── degradation ──

    def test_missing_transcript_is_zero_with_a_warning(self):
        meta, d = self._task()
        u = CTM.ingest_usage(meta, d)
        self.assertEqual(tu.classes_total(u), 0)
        self.assertFalse(u['transcript_found'])
        self.assertIn('transcript_absent', u['warnings'])

    def test_a_reader_explosion_never_reaches_the_caller(self):
        meta, d = self._task()
        self._put_transcript(meta)
        with mock.patch.object(server.tu, 'ingest',
                               side_effect=RuntimeError('boom')):
            self.assertIsNone(CTM.ingest_usage(meta, d))
        # And the task is untouched.
        with open(os.path.join(d, 'task.json')) as f:
            self.assertNotIn('usage', json.load(f))

    def test_missing_task_dir_is_survived(self):
        meta, d = self._task()
        shutil.rmtree(d)
        CTM.ingest_usage(meta, d)  # must not raise

    def test_no_work_once_terminal_and_settled(self):
        meta, d = self._task()
        self._put_transcript(meta)
        CTM.ingest_usage(meta, d)
        with mock.patch.object(server.tu, 'ingest',
                               side_effect=AssertionError('rescanned')):
            u = CTM.ingest_usage(meta, d)
        self.assertEqual(tu.classes_total(u), FIXTURE_TOTAL)

    def test_live_task_is_throttled(self):
        meta, d = self._task(status='running')
        self._put_transcript(meta)
        CTM.ingest_usage(meta, d)
        with mock.patch.object(server.tu, 'ingest',
                               side_effect=AssertionError('rescanned')):
            CTM.ingest_usage(meta, d)     # within USAGE_SCAN_INTERVAL

    # ── API projection ──

    def test_usage_view_hides_the_resume_state(self):
        meta, d = self._task()
        self._put_transcript(meta)
        CTM.ingest_usage(meta, d)
        view = CTM.usage_view(meta)
        self.assertEqual(view['total'], FIXTURE_TOTAL)
        self.assertEqual(view['priceable_total'], FIXTURE_TOTAL)
        self.assertEqual(view['coverage'], tu.COVERAGE_MEASURED)
        self.assertNotIn('files', view)
        self.assertNotIn('keys', view)
        for k in ('input', 'cache_read', 'cache_write', 'output', 'by_model'):
            self.assertIn(k, view)

    def test_usage_view_on_a_task_that_was_never_ingested(self):
        meta, _ = self._task(assistant='ante')
        view = CTM.usage_view(meta)
        self.assertEqual(view['total'], 0)
        self.assertEqual(view['coverage'], tu.COVERAGE_NOT_INSTRUMENTED)

    def test_get_task_does_not_leak_ingest_state(self):
        meta, d = self._task()
        self._put_transcript(meta)
        CTM.ingest_usage(meta, d)
        with mock.patch.object(server.subprocess, 'run',
                               return_value=mock.Mock(returncode=1, stdout='',
                                                      stderr='')):
            got = CTM.get_task(meta['task_id'])
        self.assertNotIn('usage_ingest', got)
        self.assertEqual(got['usage']['total'], FIXTURE_TOTAL)

    def test_list_tasks_carries_usage(self):
        meta, d = self._task()
        self._put_transcript(meta)
        CTM.ingest_usage(meta, d)
        with mock.patch.object(server.subprocess, 'run',
                               return_value=mock.Mock(returncode=1, stdout='',
                                                      stderr='')):
            rows = CTM.list_tasks()
        row = next(r for r in rows if r['task_id'] == meta['task_id'])
        self.assertEqual(row['usage']['total'], FIXTURE_TOTAL)
        self.assertEqual(row['usage']['coverage'], tu.COVERAGE_MEASURED)


class BuildTokenTotalsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._patch = mock.patch.object(CTM, 'TASKS_DIR', self.tmp)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _task(self, tid, meta):
        d = os.path.join(self.tmp, tid)
        os.makedirs(d)
        with open(os.path.join(d, 'task.json'), 'w') as f:
            json.dump(meta, f)

    def test_aggregates_classes_models_and_coverage(self):
        self._task('t1', {'task_id': 't1', 'assistant': 'claude', 'usage': {
            'schema': 2, 'input': 1, 'cache_read': 10, 'cache_write': 100,
            'output': 1000, 'records': 2, 'coverage': tu.COVERAGE_MEASURED,
            'by_model': {'claude-opus-5': {'input': 1, 'cache_read': 10,
                                           'cache_write': 100, 'output': 1000,
                                           'records': 2}}}})
        self._task('t2', {'task_id': 't2', 'assistant': 'codex', 'usage': {
            'schema': 2, 'input': 0, 'output': 0,
            'coverage': tu.COVERAGE_NOT_INSTRUMENTED}})
        self._task('t3', {'task_id': 't3', 'assistant': 'claude', 'usage': {
            'schema': 2, 'input': 0, 'output': 0,
            'coverage': tu.COVERAGE_NO_SESSION}})
        self._task('t4', {'task_id': 't4', 'assistant': 'claude'})  # never scanned
        usage, cov = CTM.build_token_totals()
        self.assertEqual(usage['total'], 1111)
        self.assertEqual(usage['tasks'], 1)
        self.assertEqual(usage['by_model']['claude-opus-5']['output'], 1000)
        self.assertEqual(cov['measured'], 2)         # t1 + t4
        self.assertEqual(cov['not_instrumented'], 1)
        self.assertEqual(cov['no_session_id'], 1)

    def test_empty_and_corrupt_dirs_are_zero(self):
        self._task('t1', {'task_id': 't1'})
        os.makedirs(os.path.join(self.tmp, 'nometa'))
        with open(os.path.join(self.tmp, 't1', 'task.json'), 'w') as f:
            f.write('{ not json')
        usage, cov = CTM.build_token_totals()
        self.assertEqual(usage['total'], 0)
        self.assertEqual(usage['tasks'], 0)


class ProductMetricsShapeTest(unittest.TestCase):
    """The /metrics product block: pre-#574 keys keep their exact meaning, the
    class-separated figures land beside them."""

    THREADS = {
        'total': 200, 'input': 150, 'output': 50, 'per_session_avg': 100,
        'schema': 2,
        'threads': {'input': 0, 'cache_read': 0, 'cache_write': 0, 'output': 50,
                    'legacy_input_combined': 150, 'priceable_total': 50,
                    'total': 200, 'records': 2, 'by_model': {}, 'sessions': 1},
        'coverage': {'measured_assistants': ['claude'], 'measured': 1,
                     'not_instrumented': 0, 'no_session_id': 0},
    }
    BUILDS = ({'input': 5, 'cache_read': 6, 'cache_write': 7, 'output': 8,
               'legacy_input_combined': 0, 'priceable_total': 26, 'total': 26,
               'records': 3, 'by_model': {'claude-opus-5': {
                   'input': 5, 'cache_read': 6, 'cache_write': 7, 'output': 8,
                   'records': 3}}, 'tasks': 1},
              {'measured_assistants': ['claude'], 'measured': 1,
               'not_instrumented': 2, 'no_session_id': 0})

    def test_legacy_keys_are_untouched_and_all_sums_both_paths(self):
        with mock.patch.object(CTM, 'build_token_totals',
                               return_value=self.BUILDS):
            out = server.ProductMetricsCollector._with_build_tokens(
                dict(self.THREADS))
        self.assertEqual(out['total'], 200)          # threads only, unchanged
        self.assertEqual(out['input'], 150)
        self.assertEqual(out['output'], 50)
        self.assertEqual(out['per_session_avg'], 100)
        self.assertEqual(out['builds']['total'], 26)
        self.assertEqual(out['all']['total'], 226)
        self.assertEqual(out['all']['priceable_total'], 76)
        self.assertEqual(out['all']['legacy_input_combined'], 150)
        self.assertEqual(out['all']['by_model']['claude-opus-5']['output'], 8)
        self.assertEqual(out['coverage']['builds']['not_instrumented'], 2)
        self.assertEqual(out['coverage']['measured_assistants'], ['claude'])

    def test_shape_holds_without_a_hypervisor(self):
        with mock.patch.object(CTM, 'build_token_totals',
                               return_value=self.BUILDS):
            out = server.ProductMetricsCollector._with_build_tokens(
                {'total': 0, 'input': 0, 'output': 0, 'per_session_avg': 0})
        self.assertEqual(out['threads']['total'], 0)
        self.assertEqual(out['all']['total'], 26)

    def test_get_product_metrics_includes_the_new_block(self):
        with mock.patch.object(CTM, 'build_token_totals',
                               return_value=self.BUILDS), \
             mock.patch.object(server.HypervisorSession, 'product_totals',
                               return_value={'chats': {'total': 1, 'active': 0},
                                             'tokens': dict(self.THREADS),
                                             'skills': {'invocations_by_name': {}}}):
            p = server.ProductMetricsCollector.get_product_metrics()
        self.assertEqual(p['tokens']['all']['total'], 226)
        self.assertIn('coverage', p['tokens'])

    def test_a_build_scan_error_does_not_500_the_endpoint(self):
        with mock.patch.object(CTM, 'build_token_totals',
                               side_effect=OSError('disk gone')):
            p = server.ProductMetricsCollector.get_product_metrics()
        self.assertIn('error', p['tokens'])


if __name__ == '__main__':
    unittest.main()
