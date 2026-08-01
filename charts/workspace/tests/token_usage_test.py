"""Unit tests for token_usage.py — per-class token accounting (#574).

The load-bearing test here is DoubleIngestTest: ingesting the same transcript
twice must not change the total. Everything else guards the degradation path —
Claude Code's JSONL shape is not a contract, so a missing/empty/malformed/
schema-drifted transcript must produce zero-with-a-warning, never a crash and
never a guess.

Run with:    python3 -m unittest tests.token_usage_test
(from charts/workspace/)
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import token_usage as tu  # noqa: E402


def rec(msg_id, model='claude-opus-5', inp=1, read=0, write=0, out=0,
        req=None, uuid_=None, type_='assistant', usage=True):
    """One transcript record in Claude Code's shape (only the fields we read)."""
    o = {
        'type': type_,
        'uuid': uuid_ or f'u-{msg_id}',
        'sessionId': 's-1',
        'timestamp': '2026-07-30T00:00:00.000Z',
        'cwd': '/home/dev',
        'message': {'id': msg_id, 'role': 'assistant', 'model': model},
    }
    if req is not None:
        o['requestId'] = req
    if usage:
        o['message']['usage'] = {
            'input_tokens': inp,
            'cache_read_input_tokens': read,
            'cache_creation_input_tokens': write,
            'output_tokens': out,
            # Noise Claude Code really emits — must be ignored, not summed.
            'server_tool_use': {'web_search_requests': 0},
            'service_tier': 'standard',
            'cache_creation': {'ephemeral_5m_input_tokens': 0},
        }
    return o


def write_jsonl(path, records):
    with open(path, 'w') as f:
        for r in records:
            f.write(json.dumps(r) + '\n')


def append_jsonl(path, records):
    with open(path, 'a') as f:
        for r in records:
            f.write(json.dumps(r) + '\n')


# ── large-transcript fixture ─────────────────────────────────────────────────
# A ledger's per-file dedupe ring only retains the last few message keys, so a
# fixture of two or three records is re-caught by the ring alone — which masks
# the resume offset, the in-pass dedupe set and the group carry-over all at once.
# Every idempotency assertion at scale must therefore use a transcript with
# comfortably MORE usage-bearing records than the ring holds. Derived from
# _KEY_RING, so resizing the ring keeps these tests honest instead of quietly
# defanging them.
GROUPS = max(40, tu._KEY_RING * 5)
LINES_PER_GROUP = 3
MODELS = ('claude-opus-5', 'claude-fable-5', 'claude-haiku-4-5')


def group_records(gid, lines=LINES_PER_GROUP):
    """One API response, written the way Claude Code really writes it: `lines`
    records sharing a `message.id` and each repeating the same `usage`.

    Per-group values vary with gid so a partial count can't accidentally match
    the expected total.
    """
    inp, read, write, out = 1 + gid, 100 + gid, 10 + gid, 2 + gid
    return [rec(f'msg_{gid:04d}', model=MODELS[gid % len(MODELS)],
                inp=inp, read=read, write=write, out=out,
                req=f'req_{gid:04d}', uuid_=f'u-{gid:04d}-{i}')
            for i in range(lines)]


def group_total(gid):
    return (1 + gid) + (100 + gid) + (10 + gid) + (2 + gid)


def big_records(groups=GROUPS, start=0):
    """`groups` API responses with realistic non-usage lines interleaved."""
    out = []
    for gid in range(start, start + groups):
        out.append({'type': 'user', 'uuid': f'usr-{gid}',
                    'message': {'role': 'user', 'content': '<scrubbed>'}})
        out.extend(group_records(gid))
    return out


def big_total(groups=GROUPS, start=0):
    return sum(group_total(g) for g in range(start, start + groups))


class TmpDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, name='t.jsonl'):
        return os.path.join(self.tmp, name)


# ── the one that matters most ────────────────────────────────────────────────

class DoubleIngestTest(TmpDirTest):
    """Re-reading a transcript must not re-add its messages."""

    def setUp(self):
        super().setUp()
        self.p = self.path()
        write_jsonl(self.p, [
            rec('msg_a', inp=2, read=100, write=50, out=10),
            rec('msg_b', inp=3, read=200, write=60, out=20),
        ])

    def test_second_ingest_of_unchanged_file_changes_nothing(self):
        first, state = tu.ingest([self.p])
        second, state2 = tu.ingest([self.p], state)
        self.assertEqual(tu.classes_total(first), 445)
        for c in tu.CLASSES:
            self.assertEqual(second[c], first[c], c)
        self.assertEqual(second['records'], first['records'])
        self.assertEqual(second['by_model'], first['by_model'])
        # And a third time, for good measure.
        third, _ = tu.ingest([self.p], state2)
        self.assertEqual(tu.classes_total(third), tu.classes_total(first))

    def test_ingest_is_a_pure_function_of_the_files(self):
        """Even losing the resume state entirely must not double the total —
        per-file ledgers are recomputed, not accumulated."""
        once, _ = tu.ingest([self.p])
        cold, _ = tu.ingest([self.p])            # fresh state, same file
        self.assertEqual(tu.classes_total(cold), tu.classes_total(once))

    def test_one_api_response_split_across_lines_counts_once(self):
        """Claude Code writes one line per content block, each repeating the SAME
        `usage`. Counting lines would double/triple the real spend — verified on
        this pod: 6,672 request ids spanned >1 line, always with identical
        usage."""
        p = self.path('split.jsonl')
        write_jsonl(p, [
            rec('msg_x', inp=2, read=20628, write=18426, out=319, uuid_='u1'),
            rec('msg_x', inp=2, read=20628, write=18426, out=319, uuid_='u2'),
            rec('msg_x', inp=2, read=20628, write=18426, out=319, uuid_='u3'),
        ])
        usage, _ = tu.ingest([p])
        self.assertEqual(usage['records'], 1)
        self.assertEqual(usage['output'], 319)
        self.assertEqual(usage['cache_read'], 20628)
        self.assertEqual(usage['cache_write'], 18426)

    def test_group_straddling_two_polls_counts_once(self):
        """The dedupe ring must survive the file growing between polls mid-group."""
        p = self.path('straddle.jsonl')
        write_jsonl(p, [rec('msg_g', inp=5, out=7, uuid_='g1')])
        first, state = tu.ingest([p])
        append_jsonl(p, [rec('msg_g', inp=5, out=7, uuid_='g2'),
                         rec('msg_g', inp=5, out=7, uuid_='g3')])
        second, state = tu.ingest([p], state)
        self.assertEqual(second['records'], 1)
        self.assertEqual(second['output'], 7)
        # A genuinely new response after the straddle still lands.
        append_jsonl(p, [rec('msg_h', inp=1, out=2)])
        third, _ = tu.ingest([p], state)
        self.assertEqual(third['records'], 2)
        self.assertEqual(third['output'], 9)

    def test_appends_accumulate(self):
        first, state = tu.ingest([self.p])
        append_jsonl(self.p, [rec('msg_c', inp=1, read=5, write=2, out=3)])
        second, _ = tu.ingest([self.p], state)
        self.assertEqual(second['records'], 3)
        self.assertEqual(tu.classes_total(second), 445 + 11)

    def test_rewritten_file_recounts_instead_of_doubling(self):
        """A new inode (or a shrunk file) means the offset is meaningless. The
        file's ledger is rebuilt from scratch — never added on top."""
        first, state = tu.ingest([self.p])
        os.remove(self.p)
        write_jsonl(self.p, [rec('msg_z', inp=7, out=8)])
        second, _ = tu.ingest([self.p], state)
        self.assertEqual(tu.classes_total(second), 15)
        self.assertTrue(any('transcript_rewritten' in w
                            for w in second.get('warnings', [])))

    def test_a_partial_trailing_line_is_read_whole_next_poll(self):
        p = self.path('partial.jsonl')
        write_jsonl(p, [rec('msg_a', inp=10, out=1)])
        with open(p, 'a') as f:
            f.write(json.dumps(rec('msg_b', inp=99, out=99))[:40])  # truncated
        first, state = tu.ingest([p])
        self.assertEqual(first['records'], 1)
        with open(p, 'w') as f:  # rewrite whole, both lines complete
            f.write(json.dumps(rec('msg_a', inp=10, out=1)) + '\n')
            f.write(json.dumps(rec('msg_b', inp=99, out=99)) + '\n')
        second, _ = tu.ingest([p], state)
        self.assertEqual(second['records'], 2)
        self.assertEqual(second['input'], 109)


class LargeTranscriptIdempotencyTest(TmpDirTest):
    """Idempotency at a scale the dedupe ring cannot cover on its own.

    The small fixtures above pass even with the resume offset broken, because a
    ring of _KEY_RING keys re-catches every record in a 2-record file. With
    GROUPS (>> _KEY_RING) responses, only a correct offset keeps a re-ingest from
    re-adding the records that have fallen out of the ring — which is the real
    failure this issue named as its main risk (observed at 1.98x on a 10 MB
    transcript with the offset disabled)."""

    def setUp(self):
        super().setUp()
        # Guard the guard: if someone raises _KEY_RING past the fixture size,
        # these tests silently lose their teeth. Fail loudly instead.
        self.assertGreater(GROUPS, tu._KEY_RING * 2,
                           'fixture must comfortably outrun the dedupe ring')
        self.p = self.path('big.jsonl')
        write_jsonl(self.p, big_records())

    def test_first_pass_counts_each_api_response_once(self):
        u, _ = tu.ingest([self.p])
        self.assertEqual(u['records'], GROUPS)          # not GROUPS * LINES_PER_GROUP
        self.assertEqual(tu.classes_total(u), big_total())

    def test_re_ingest_through_persisted_state_changes_nothing(self):
        """The polling path: state is persisted (task.json) and handed back."""
        first, state = tu.ingest([self.p])
        self.assertEqual(tu.classes_total(first), big_total())
        for i in range(4):
            # Round-trip the state exactly as task.json persists it, so a shape
            # that can't survive json.dump/load is caught here too.
            state = json.loads(json.dumps(state))
            again, state = tu.ingest([self.p], state)
            self.assertEqual(tu.classes_total(again), big_total(),
                             f're-ingest {i + 1} changed the total')
            self.assertEqual(again['records'], GROUPS, f're-ingest {i + 1}')
            self.assertEqual(again['by_model'], first['by_model'],
                             f're-ingest {i + 1}')

    def test_cold_rescan_matches(self):
        warm, _ = tu.ingest([self.p])
        cold, _ = tu.ingest([self.p])
        self.assertEqual(tu.classes_total(cold), tu.classes_total(warm))

    def test_incremental_polling_equals_a_cold_scan_of_the_final_file(self):
        """Ingest, append, re-ingest — repeatedly, including an append that
        splits a message group across two polls — and land on exactly the number
        a single cold scan of the finished file produces."""
        p = self.path('grow.jsonl')
        chunk = GROUPS // 4
        write_jsonl(p, big_records(chunk))
        usage, state = tu.ingest([p])
        gid = chunk
        while gid < GROUPS:
            batch = min(chunk, GROUPS - gid)
            recs = big_records(batch, start=gid)
            # Split the batch mid-group so one API response's lines straddle two
            # polls — the case the group carry-over and the ring both guard.
            cut = len(recs) - 2
            append_jsonl(p, recs[:cut])
            state = json.loads(json.dumps(state))
            usage, state = tu.ingest([p], state)
            append_jsonl(p, recs[cut:])
            state = json.loads(json.dumps(state))
            usage, state = tu.ingest([p], state)
            gid += batch
        cold, _ = tu.ingest([p])
        self.assertEqual(cold['records'], GROUPS)
        self.assertEqual(tu.classes_total(cold), big_total())
        self.assertEqual(tu.classes_total(usage), tu.classes_total(cold))
        self.assertEqual(usage['records'], cold['records'])
        self.assertEqual(usage['by_model'], cold['by_model'])

    def test_repeated_message_id_out_of_group_counts_once(self):
        """`seen` is what makes dedupe identity-based rather than merely
        positional: a repeat of a message id that is NOT adjacent to its original
        is still counted once.

        No real transcript on this pod does this (0 non-contiguous groups across
        358 files), so this pins the "the JSONL shape is not a contract" guard
        rather than a live path. It is bounded by the ring — only a repeat within
        the last _KEY_RING responses is caught — which is the guarantee the
        implementation actually offers, so the repeat sits inside that window.
        """
        # At least one OTHER response must sit between the original and its
        # repeat, or they'd be adjacent and the contiguous-group skip would
        # handle it instead — leaving this test asserting nothing about `seen`.
        n = max(2, tu._KEY_RING - 1)
        p = self.path('repeat.jsonl')
        recs = big_records(n)
        recs.extend(group_records(0))   # re-emit the FIRST, still in the window
        write_jsonl(p, recs)
        u, _ = tu.ingest([p])
        self.assertEqual(u['records'], n)
        self.assertEqual(tu.classes_total(u), big_total(n))


# ── class separation + model attribution ────────────────────────────────────

class ClassSplitTest(TmpDirTest):
    def test_four_classes_stay_apart(self):
        p = self.path()
        write_jsonl(p, [rec('m1', inp=1, read=10, write=100, out=1000)])
        u, _ = tu.ingest([p])
        self.assertEqual(u['input'], 1)
        self.assertEqual(u['cache_read'], 10)
        self.assertEqual(u['cache_write'], 100)
        self.assertEqual(u['output'], 1000)
        self.assertEqual(u['priceable_total'] if 'priceable_total' in u
                         else tu.priceable_total(u), 1111)

    def test_model_recorded_per_message(self):
        p = self.path()
        write_jsonl(p, [
            rec('m1', model='claude-opus-5', inp=1, out=2),
            rec('m2', model='claude-haiku-4-5-20251001', inp=3, out=4),
            rec('m3', model='claude-opus-5', inp=5, out=6),
        ])
        u, _ = tu.ingest([p])
        self.assertEqual(set(u['by_model']), {'claude-opus-5',
                                              'claude-haiku-4-5-20251001'})
        self.assertEqual(u['by_model']['claude-opus-5'],
                         {'input': 6, 'cache_read': 0, 'cache_write': 0,
                          'output': 8, 'records': 2})
        self.assertEqual(u['by_model']['claude-haiku-4-5-20251001']['records'], 1)

    def test_synthetic_model_records_are_not_counted(self):
        """`<synthetic>` is Claude Code's local API-error notice — not a model
        anyone can price. Every one on this pod carried all-zero usage."""
        p = self.path()
        write_jsonl(p, [
            rec('m1', model='<synthetic>', inp=0, out=0),
            rec('m2', model='claude-opus-5', inp=4, out=5),
        ])
        u, _ = tu.ingest([p])
        self.assertEqual(u['records'], 1)
        self.assertNotIn('<synthetic>', u['by_model'])


class SubagentTranscriptTest(TmpDirTest):
    def test_finds_subagent_logs_for_a_session(self):
        proj, sid = self.tmp, 'ses-1'
        subs = os.path.join(proj, sid, 'subagents')
        os.makedirs(subs)
        for n in ('agent-b.jsonl', 'agent-a.jsonl', 'notes.txt'):
            open(os.path.join(subs, n), 'w').close()
        found = tu.subagent_transcripts(proj, sid)
        self.assertEqual([os.path.basename(p) for p in found],
                         ['agent-a.jsonl', 'agent-b.jsonl'])

    def test_no_subagent_dir_is_empty_not_an_error(self):
        self.assertEqual(tu.subagent_transcripts(self.tmp, 'nope'), [])
        self.assertEqual(tu.subagent_transcripts('', 'x'), [])
        self.assertEqual(tu.subagent_transcripts(self.tmp, ''), [])

    def test_subagent_spend_is_added_to_the_session_total(self):
        main = self.path('main.jsonl')
        sub = self.path('sub.jsonl')
        write_jsonl(main, [rec('m1', inp=10, out=1)])
        write_jsonl(sub, [rec('s1', model='claude-haiku-4-5', inp=20, out=2)])
        u, _ = tu.ingest([main, sub])
        self.assertEqual(tu.classes_total(u), 33)
        self.assertEqual(u['files'], 2)
        self.assertEqual(set(u['by_model']),
                         {'claude-opus-5', 'claude-haiku-4-5'})


# ── degradation: the shape is not a contract ─────────────────────────────────

class DegradationTest(TmpDirTest):
    def test_absent_transcript_is_zero_with_a_warning(self):
        u, state = tu.ingest([])
        self.assertEqual(tu.classes_total(u), 0)
        self.assertIn('transcript_absent', u['warnings'])
        self.assertEqual(state['files'], {})

    def test_missing_file_is_zero_with_a_warning(self):
        u, _ = tu.ingest([self.path('nope.jsonl')])
        self.assertEqual(tu.classes_total(u), 0)
        self.assertTrue(any(w.startswith('transcript_unreadable')
                            for w in u['warnings']))

    def test_records_without_usage_contribute_nothing(self):
        p = self.path()
        write_jsonl(p, [
            {'type': 'user', 'message': {'role': 'user', 'content': 'hi'}},
            rec('m1', usage=False),
            {'type': 'queue-operation'},
            {'type': 'ai-title', 'message': None},
            rec('m2', inp=9, out=1),
        ])
        u, _ = tu.ingest([p])
        self.assertEqual(u['records'], 1)
        self.assertEqual(tu.classes_total(u), 10)

    def test_malformed_lines_are_survived(self):
        p = self.path()
        with open(p, 'w') as f:
            f.write('not json at all\n')
            f.write('[1, 2, 3]\n')                       # valid JSON, wrong type
            f.write('{"type": "assistant"\n')             # truncated object
            f.write(json.dumps(rec('m1', inp=4, out=1)) + '\n')
            f.write('\n')
        u, _ = tu.ingest([p])
        self.assertEqual(tu.classes_total(u), 5)
        self.assertTrue(any(w.startswith('transcript_bad_line')
                            for w in u['warnings']))

    def test_unexpected_schema_degrades_to_zero(self):
        """Every field optional, every value untrusted: a plausible-looking
        future/renamed shape must yield 0, not a wrong number."""
        p = self.path()
        write_jsonl(p, [
            # usage renamed / moved
            {'type': 'assistant', 'message': {'id': 'a', 'tokenUsage':
                                              {'in': 5, 'out': 6}}},
            # usage present but not an object
            {'type': 'assistant', 'message': {'id': 'b', 'usage': 'lots'}},
            {'type': 'assistant', 'message': {'id': 'c', 'usage': [1, 2]}},
            # message not an object
            {'type': 'assistant', 'message': 'hello'},
            # nulls and wrong types inside usage
            {'type': 'assistant', 'message': {'id': 'd', 'usage': {
                'input_tokens': None, 'output_tokens': {}, 'model': 1,
                'cache_read_input_tokens': [], 'cache_creation_input_tokens': True}}},
        ])
        u, _ = tu.ingest([p])
        self.assertEqual(tu.classes_total(u), 0)
        self.assertEqual(u['records'], 0)

    def test_tolerant_coercion_of_odd_values(self):
        p = self.path()
        write_jsonl(p, [{'type': 'assistant', 'message': {
            'id': 'e', 'model': 'claude-opus-5', 'usage': {
                'input_tokens': '12', 'output_tokens': 3.9,
                'cache_read_input_tokens': -5,      # negative is nonsense → 0
                'cache_creation_input_tokens': None}}}])
        u, _ = tu.ingest([p])
        self.assertEqual(u['input'], 12)
        self.assertEqual(u['output'], 3)
        self.assertEqual(u['cache_read'], 0)

    def test_keyless_record_still_deduped(self):
        """No message.id / requestId / uuid: fall back to the line's byte offset,
        which is stable inside an append-only file."""
        p = self.path()
        with open(p, 'w') as f:
            f.write(json.dumps({'type': 'assistant', 'message': {
                'model': 'claude-opus-5',
                'usage': {'input_tokens': 6, 'output_tokens': 1}}}) + '\n')
        first, state = tu.ingest([p])
        self.assertEqual(tu.classes_total(first), 7)
        second, _ = tu.ingest([p], state)
        self.assertEqual(tu.classes_total(second), 7)

    def test_directory_instead_of_file_is_a_warning(self):
        d = os.path.join(self.tmp, 'adir.jsonl')
        os.makedirs(d)
        u, _ = tu.ingest([d])
        self.assertEqual(tu.classes_total(u), 0)
        self.assertTrue(u['warnings'])

    def test_warnings_are_bounded(self):
        u = tu.empty_usage()
        for i in range(50):
            tu._warn(u, 'code', str(i))
        self.assertLessEqual(len(u['warnings']), tu.MAX_WARNINGS)

    def test_too_many_transcripts_is_capped_and_reported(self):
        paths = [self.path(f'f{i}.jsonl') for i in range(tu.MAX_TRACKED_FILES + 3)]
        for p in paths:
            write_jsonl(p, [rec(f'm{os.path.basename(p)}', inp=1)])
        u, _ = tu.ingest(paths)
        self.assertEqual(u['files'], tu.MAX_TRACKED_FILES)
        self.assertTrue(any(w.startswith('too_many_transcripts')
                            for w in u['warnings']))


# ── the stream path (Hypervisor threads) ─────────────────────────────────────

class StreamResultTest(unittest.TestCase):
    def test_model_usage_is_preferred_and_split(self):
        """Verified against a live `claude -p --output-format stream-json` run:
        `modelUsage` is per-model AND more complete than the top-level `usage`,
        which covered only the primary model and omitted a 521-token haiku
        side-call."""
        u = tu.usage_from_stream_result({
            'type': 'result',
            'usage': {'input_tokens': 2, 'cache_creation_input_tokens': 15733,
                      'cache_read_input_tokens': 15273, 'output_tokens': 4},
            'modelUsage': {
                'claude-opus-5[1m]': {'inputTokens': 2, 'outputTokens': 4,
                                      'cacheReadInputTokens': 15273,
                                      'cacheCreationInputTokens': 15733,
                                      'costUSD': 0.165},
                'claude-haiku-4-5-20251001': {'inputTokens': 521,
                                              'outputTokens': 12,
                                              'cacheReadInputTokens': 0,
                                              'cacheCreationInputTokens': 0},
            },
        })
        self.assertEqual(u['input'], 523)
        self.assertEqual(u['output'], 16)
        self.assertEqual(u['cache_read'], 15273)
        self.assertEqual(u['cache_write'], 15733)
        self.assertEqual(u['records'], 1)          # one turn, two models
        self.assertEqual(set(u['by_model']),
                         {'claude-opus-5[1m]', 'claude-haiku-4-5-20251001'})
        # Pricing is Phase 2 — no dollar figure is carried here.
        self.assertNotIn('costUSD', u)
        self.assertNotIn('cost_usd', u)

    def test_falls_back_to_top_level_usage_with_the_turn_model(self):
        u = tu.usage_from_stream_result({
            'type': 'result', 'modelUsage': {},
            'usage': {'input_tokens': 10, 'output_tokens': 7,
                      'cache_read_input_tokens': 5,
                      'cache_creation_input_tokens': 2},
        }, fallback_model='claude-fable-5')
        self.assertEqual([u['input'], u['cache_read'], u['cache_write'], u['output']],
                         [10, 5, 2, 7])
        self.assertEqual(u['by_model']['claude-fable-5']['output'], 7)

    def test_no_usage_at_all_returns_none(self):
        self.assertIsNone(tu.usage_from_stream_result({'type': 'result'}))
        self.assertIsNone(tu.usage_from_stream_result(None))
        self.assertIsNone(tu.usage_from_stream_result('nope'))

    def test_model_usage_entries_that_are_junk_are_skipped(self):
        u = tu.usage_from_stream_result({
            'usage': {'input_tokens': 1, 'output_tokens': 1},
            'modelUsage': {'m1': 'not a dict', 'm2': {'inputTokens': 0,
                                                      'outputTokens': 0}},
        }, fallback_model='claude-opus-5')
        # Nothing usable in modelUsage → top-level fallback.
        self.assertEqual(u['input'], 1)
        self.assertEqual(list(u['by_model']), ['claude-opus-5'])


# ── migration of the persisted shape ─────────────────────────────────────────

class MigrationTest(unittest.TestCase):
    def test_v1_collapsed_input_moves_to_the_legacy_bucket(self):
        """v1's `input` mixed fresh + cache-read + cache-write. Re-labelling it
        as fresh input would overstate cost by ~10x once priced, so it moves to
        `legacy_input_combined`: still counted, never priced."""
        v2 = tu.migrate({'input': 150, 'output': 50})
        self.assertEqual(v2['schema'], tu.SCHEMA_VERSION)
        self.assertEqual(v2['input'], 0)
        self.assertEqual(v2['cache_read'], 0)
        self.assertEqual(v2['cache_write'], 0)
        self.assertEqual(v2['output'], 50)
        self.assertEqual(v2['legacy_input_combined'], 150)
        self.assertEqual(v2['migrated_from_schema'], 1)

    def test_migration_preserves_the_token_total(self):
        """The one number the dashboard already shows must not move."""
        self.assertEqual(tu.classes_total(tu.migrate({'input': 150, 'output': 50})),
                         200)
        self.assertEqual(tu.priceable_total(tu.migrate({'input': 150, 'output': 50})),
                         50)

    def test_v2_is_left_alone(self):
        v2 = tu.empty_usage(source=tu.SOURCE_STREAM, coverage=tu.COVERAGE_MEASURED)
        tu.add_usage(v2, {'input': 1, 'cache_read': 2, 'cache_write': 3,
                          'output': 4, 'records': 1,
                          'by_model': {'m': {'input': 1, 'output': 4,
                                             'records': 1}}})
        again = tu.migrate(v2)
        self.assertEqual([again[c] for c in tu.CLASSES], [1, 2, 3, 4])
        self.assertEqual(again['source'], tu.SOURCE_STREAM)
        self.assertEqual(again['by_model']['m']['input'], 1)
        self.assertNotIn('migrated_from_schema', again)

    def test_migrate_tolerates_garbage(self):
        for bad in (None, 'x', 42, [], {'input': 'lots', 'output': None},
                    {'schema': 2, 'by_model': 'nope'}):
            v2 = tu.migrate(bad)
            self.assertEqual(v2['schema'], tu.SCHEMA_VERSION)
            self.assertEqual(tu.classes_total(v2), 0)

    def test_v1_turns_carry_over_as_records(self):
        self.assertEqual(tu.migrate({'input': 1, 'output': 1, 'turns': 3})['records'], 3)


class AddUsageTest(unittest.TestCase):
    def test_accumulates_classes_and_models(self):
        a = tu.empty_usage()
        for _ in range(3):
            tu.add_usage(a, {'input': 1, 'cache_read': 2, 'cache_write': 3,
                             'output': 4, 'records': 1,
                             'by_model': {'m': {'input': 1, 'cache_read': 2,
                                                'cache_write': 3, 'output': 4,
                                                'records': 1}}})
        self.assertEqual([a[c] for c in tu.CLASSES], [3, 6, 9, 12])
        self.assertEqual(a['records'], 3)
        self.assertEqual(a['by_model']['m']['output'], 12)

    def test_public_block_output_can_be_re_added(self):
        """product metrics sum `threads` + `builds`, both public blocks."""
        a = tu.empty_usage()
        tu.add_usage(a, {'input': 5, 'output': 5, 'records': 1,
                         'legacy_input_combined': 7})
        b = tu.public_block(a)
        c = tu.empty_usage()
        tu.add_usage(c, b)
        tu.add_usage(c, b)
        self.assertEqual(tu.classes_total(c), 2 * tu.classes_total(a))

    def test_add_usage_ignores_junk(self):
        a = tu.empty_usage()
        tu.add_usage(a, None)
        tu.add_usage(a, 'nope')
        tu.add_usage(a, {'by_model': {'m': 'not a dict'}})
        self.assertEqual(tu.classes_total(a), 0)
        self.assertEqual(a['by_model'], {})


class CoverageTest(unittest.TestCase):
    def test_only_claude_is_instrumented(self):
        self.assertTrue(tu.is_instrumented('claude'))
        for a in ('codex', 'ante', 'antigravity', 'librefang',
                  'opencode-openrouter', 'opencode-deepseek', 'opencode-zen',
                  'kc-harness', '', None):
            self.assertFalse(tu.is_instrumented(a), a)
            self.assertEqual(tu.assistant_coverage(a),
                             tu.COVERAGE_NOT_INSTRUMENTED, a)
        self.assertEqual(tu.assistant_coverage('claude'), tu.COVERAGE_MEASURED)

    def test_summary_names_what_is_measurable(self):
        s = tu.coverage_summary(measured=2, not_instrumented=3, no_session_id=1)
        self.assertEqual(s['measured_assistants'], ['claude'])
        self.assertEqual((s['measured'], s['not_instrumented'],
                          s['no_session_id']), (2, 3, 1))


class PublicBlockTest(unittest.TestCase):
    def test_shape_and_totals(self):
        u = tu.empty_usage()
        tu.add_usage(u, {'input': 1, 'cache_read': 2, 'cache_write': 3,
                         'output': 4, 'records': 2, 'legacy_input_combined': 10})
        b = tu.public_block(u, sessions=5)
        self.assertEqual(b['priceable_total'], 10)
        self.assertEqual(b['total'], 20)
        self.assertEqual(b['sessions'], 5)
        self.assertEqual(b['legacy_input_combined'], 10)

    def test_none_is_all_zero(self):
        b = tu.public_block(None)
        self.assertEqual(b['total'], 0)
        self.assertEqual(b['by_model'], {})


class RealTranscriptTest(unittest.TestCase):
    """Against a fixture captured verbatim from real Claude Code sessions on this
    pod (prose scrubbed, every structural field intact): two API responses — one
    written as a single line, one split across three content-block lines that all
    repeat the same usage — plus a `<synthetic>` API-error notice, a `user` line
    and two non-conversational record types."""

    FIXTURE = os.path.join(HERE, 'fixtures', 'claude_usage_transcript.jsonl')

    def test_reads_the_real_shape(self):
        u, _ = tu.ingest([self.FIXTURE])
        # 2 API responses, NOT the 5 usage-bearing lines in the file.
        self.assertEqual(u['records'], 2)
        self.assertEqual(u['input'], 2 + 2)
        self.assertEqual(u['cache_write'], 15733 + 18426)
        self.assertEqual(u['cache_read'], 15273 + 20628)
        self.assertEqual(u['output'], 4 + 319)
        self.assertEqual(set(u['by_model']), {'claude-opus-5', 'claude-opus-4-8'})
        self.assertEqual(u['by_model']['claude-opus-4-8']['records'], 1)
        self.assertEqual(u['warnings'] if 'warnings' in u else [], [])

    def test_is_idempotent_on_the_real_shape(self):
        u, state = tu.ingest([self.FIXTURE])
        again, _ = tu.ingest([self.FIXTURE], state)
        self.assertEqual(tu.classes_total(again), tu.classes_total(u))
        cold, _ = tu.ingest([self.FIXTURE])
        self.assertEqual(tu.classes_total(cold), tu.classes_total(u))


if __name__ == '__main__':
    unittest.main()
