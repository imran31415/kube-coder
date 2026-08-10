"""`boards.runs` — the state machine, the lease book and the processed log.

The headline is `ProcessedLogTests`: run the same board twice and the second
pass does nothing, EXCEPT for an item somebody edited, which comes back. Those
two sentences are the entire point of Phase 4, and they are one design decision
— keying the marker on `item.id + content_hash` rather than on the id alone.

Mutual exclusion is tested where it can be tested honestly: the single-threaded
"second claim loses" case runs everywhere, and the genuinely concurrent case
lives in `tests/boards_store_test.py`, which skips loudly when `flock` is
shimmed rather than passing against a no-op lock.

Run:  python3 -m unittest tests.boards_runs_test   (from charts/workspace/)
"""

import os
import shutil
import sys
import tempfile
import unittest

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

from boards import engine, runs, store  # noqa: E402


def mk_item(item_id='46', **over):
    item = {
        'id': item_id, 'key': f'SUP-{item_id}', 'ref': {},
        'title': 'Refund not received', 'body': 'Dana says it never arrived.',
        'status': {'normalized': 'OPEN', 'raw': 'To Do'},
        'priority': {'normalized': 'HIGH', 'raw': 'P2'},
        'assignee': {}, 'contact': {}, 'collection': {},
        'tags': ['billing'], 'url': 'https://x/browse/SUP-1',
        'created_at': '2026-01-01', 'updated_at': '2026-02-01', 'raw': {},
    }
    item.update(over)
    return item


class _Disk(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='kc-runs-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def record(self, name):
        return store.JsonRecord(os.path.join(self.tmp, name))


# ── identity ───────────────────────────────────────────────────────────────

class IdentityTests(unittest.TestCase):
    def test_run_ids_sort_chronologically(self):
        early = runs.make_run_id(1000, 'aaaaaa')
        late = runs.make_run_id(2000, 'aaaaaa')
        self.assertLess(early, late)
        self.assertTrue(runs.valid_run_id(early))

    def test_a_forged_run_id_is_refused(self):
        for bad in ('', 'run', '../../etc/passwd', 'run-x-y', 'RUN-1-aa'):
            with self.subTest(run_id=bad):
                self.assertFalse(runs.valid_run_id(bad))

    def test_item_source_round_trips_an_id_containing_colons(self):
        """Linear and Monday hand out `gid://…` global ids. Splitting on every
        colon would mangle them into a different item."""
        source = runs.item_source('acme', 'run-1-abcdef', 'gid://issue/46:7')
        self.assertEqual(runs.parse_item_source(source),
                         ('acme', 'run-1-abcdef', 'gid://issue/46:7'))

    def test_a_non_board_source_parses_as_None(self):
        for src in ('', 'webhook:42', 'cron:nightly', 'board:a:run:b'):
            self.assertIsNone(runs.parse_item_source(src))

    def test_the_marker_key_carries_both_identity_and_content(self):
        a = runs.marker_key('46', 'hash1')
        self.assertIn('46', a)
        self.assertIn('hash1', a)
        self.assertNotEqual(a, runs.marker_key('46', 'hash2'))
        self.assertNotEqual(a, runs.marker_key('47', 'hash1'))

    def test_the_content_hash_is_the_engines_one(self):
        """A second implementation that drifted would silently stop skipping
        anything — every item would look edited on every run."""
        self.assertIs(runs.content_hash, engine.content_hash)


# ── concurrency clamp ──────────────────────────────────────────────────────

class ClampTests(unittest.TestCase):
    """`create_task` returns `rejected` WITHOUT creating anything at capacity,
    so an unclamped run produces a row of rejections that look like items
    nobody worked. Clamping and SAYING SO is the difference."""

    def test_nothing_to_clamp_reports_no_reason(self):
        n, reason = runs.clamp_concurrency(4, live_tasks=0, max_tasks=12)
        self.assertEqual((n, reason), (4, ''))

    def test_pod_headroom_clamps_and_names_KC_MAX_TASKS(self):
        n, reason = runs.clamp_concurrency(20, live_tasks=8, max_tasks=12)
        self.assertEqual(n, 4)
        self.assertIn('KC_MAX_TASKS=12', reason)
        self.assertIn('8 already live', reason)

    def test_the_board_ceiling_clamps_independently_of_the_pod(self):
        n, reason = runs.clamp_concurrency(50, live_tasks=0, max_tasks=999,
                                           board_max=8)
        self.assertEqual(n, 8)
        self.assertIn('board ceiling 8', reason)

    def test_a_full_pod_refuses_rather_than_starting_a_run_of_zero(self):
        n, reason = runs.clamp_concurrency(4, live_tasks=12, max_tasks=12)
        self.assertEqual(n, 0)
        self.assertIn('at its task limit', reason)

    def test_garbage_concurrency_becomes_one_rather_than_raising(self):
        for value in (None, 'lots', -5, 0):
            with self.subTest(value=value):
                n, _r = runs.clamp_concurrency(value, live_tasks=0, max_tasks=12)
                self.assertEqual(n, 1)


# ── the run record ─────────────────────────────────────────────────────────

class RunRecordTests(unittest.TestCase):
    def test_a_new_run_defaults_to_propose_not_autonomous(self):
        """An agent that can silently write to a customer's ticket on its first
        outing is not a default anyone asked for."""
        run = runs.new_run('run-1-aa', 'b1')
        self.assertEqual(run['mode'], 'propose')
        run = runs.new_run('run-1-aa', 'b1', mode='nonsense')
        self.assertEqual(run['mode'], 'propose')

    def test_counts_and_is_finished(self):
        run = runs.new_run('run-1-aa', 'b1')
        run['items'] = {
            '1': runs.new_run_item(mk_item('1'), content_hash='h1'),
            '2': runs.new_run_item(mk_item('2'), content_hash='h2'),
        }
        self.assertFalse(runs.is_finished(run))
        self.assertEqual(runs.counts(run)['pending'], 2)
        run['items']['1']['state'] = 'done'
        run['items']['2']['state'] = 'failed'
        self.assertTrue(runs.is_finished(run))
        self.assertEqual(runs.counts(run)['done'], 1)

    def test_an_empty_run_is_not_reported_as_finished_by_is_finished(self):
        # all() of nothing is True; a run with no items is handled explicitly
        # at creation instead, so this must not silently claim completion.
        self.assertFalse(runs.is_finished(runs.new_run('run-1-aa', 'b1')))

    def test_summary_omits_the_per_item_detail(self):
        run = runs.new_run('run-1-aa', 'b1', concurrency=4,
                           requested_concurrency=20, clamp_reason='clamped')
        run['items'] = {'1': runs.new_run_item(mk_item('1'), content_hash='h')}
        s = runs.summary(run)
        self.assertNotIn('items', s)
        self.assertEqual(s['total'], 1)
        self.assertEqual(s['requested_concurrency'], 20)
        self.assertEqual(s['clamp_reason'], 'clamped')

    def test_stop_on_consecutive_failures(self):
        run = runs.new_run('run-1-aa', 'b1', stop_on={'consecutive_failures': 3})
        run['consecutive_failures'] = 2
        self.assertEqual(runs.should_stop(run), (False, ''))
        run['consecutive_failures'] = 3
        stop, reason = runs.should_stop(run)
        self.assertTrue(stop)
        self.assertIn('consecutive failures', reason)

    def test_an_explicit_stop_request_wins_over_any_policy(self):
        run = runs.new_run('run-1-aa', 'b1')
        run['stop_requested'] = True
        self.assertTrue(runs.should_stop(run)[0])


# ── selection ──────────────────────────────────────────────────────────────

class SelectValidationTests(unittest.TestCase):
    def test_an_unknown_filter_key_is_an_ERROR_not_a_no_op(self):
        """A typo'd filter that silently matches everything would work the
        whole board — the most expensive possible failure mode."""
        _clean, errors = runs.validate_select({'statuss': ['OPEN']})
        self.assertTrue(any('unknown field' in e for e in errors))

    def test_defaults_are_applied(self):
        clean, errors = runs.validate_select({})
        self.assertEqual(errors, [])
        self.assertEqual(clean['limit'], 20)
        self.assertEqual(clean['order'], 'updated_at asc')

    def test_limit_is_bounded(self):
        for bad in (0, 501, 'ten', 1.5):
            with self.subTest(limit=bad):
                _c, errors = runs.validate_select({'limit': bad})
                self.assertTrue(errors)

    def test_an_unknown_order_is_refused(self):
        _c, errors = runs.validate_select({'order': 'random'})
        self.assertTrue(errors)

    def test_non_dict_select_is_refused(self):
        _c, errors = runs.validate_select(['OPEN'])
        self.assertEqual(errors, ['select must be an object'])


class SelectItemsTests(unittest.TestCase):
    def setUp(self):
        self.items = [
            mk_item('1', updated_at='2026-01-03', tags=['billing'],
                    status={'normalized': 'OPEN', 'raw': 'To Do'}),
            mk_item('2', updated_at='2026-01-01', tags=['auth'],
                    status={'normalized': 'CLOSED', 'raw': 'Done'}),
            mk_item('3', updated_at='2026-01-02', tags=[],
                    assignee={'name': 'Sam'},
                    status={'normalized': 'OPEN', 'raw': 'To Do'}),
        ]

    def choose(self, select):
        clean, errors = runs.validate_select(select)
        self.assertEqual(errors, [])
        return runs.select_items(self.items, clean)[0]

    def test_default_order_is_oldest_first(self):
        self.assertEqual([i['id'] for i in self.choose({})], ['2', '3', '1'])

    def test_order_desc(self):
        chosen = self.choose({'order': 'updated_at desc'})
        self.assertEqual([i['id'] for i in chosen], ['1', '3', '2'])

    def test_status_filter_matches_normalized_or_raw(self):
        self.assertEqual([i['id'] for i in self.choose({'status': ['OPEN']})],
                         ['3', '1'])
        self.assertEqual([i['id'] for i in self.choose({'status': ['Done']})],
                         ['2'])

    def test_tag_filter_is_case_insensitive_and_matches_any(self):
        self.assertEqual([i['id'] for i in self.choose({'tags': ['BILLING']})],
                         ['1'])

    def test_unassigned_filter(self):
        self.assertEqual([i['id'] for i in self.choose({'unassigned': True})],
                         ['2', '1'])

    def test_query_searches_key_title_and_body(self):
        self.assertEqual(len(self.choose({'query': 'refund'})), 3)
        self.assertEqual([i['id'] for i in self.choose({'query': 'SUP-2'})], ['2'])

    def test_item_ids_pins_an_explicit_set(self):
        self.assertEqual([i['id'] for i in self.choose({'item_ids': ['3']})],
                         ['3'])

    def test_updated_since(self):
        chosen = self.choose({'updated_since': '2026-01-02'})
        self.assertEqual([i['id'] for i in chosen], ['3', '1'])

    def test_priority_order_puts_urgent_first(self):
        self.items[1]['priority'] = {'normalized': 'URGENT', 'raw': 'P1'}
        self.assertEqual(self.choose({'order': 'priority'})[0]['id'], '2')

    def test_limit_counts_CHOSEN_items_not_scanned_ones(self):
        """A limit of 2 with 1 already-processed item must still yield 2 —
        otherwise a re-run silently shrinks as the processed log grows."""
        clean, _e = runs.validate_select({'limit': 2})
        chosen, skipped = runs.select_items(
            self.items, clean, is_processed=lambda i: i['id'] == '2')
        self.assertEqual([i['id'] for i in chosen], ['3', '1'])
        self.assertEqual([i['id'] for i in skipped], ['2'])


# ── leases ─────────────────────────────────────────────────────────────────

class LeaseBookTests(_Disk):
    def setUp(self):
        super().setUp()
        self.book = runs.LeaseBook(self.record('leases.json'))

    def test_a_second_claim_on_the_same_item_loses(self):
        self.assertTrue(self.book.claim('46', 'run-1-aa', 'w1'))
        self.assertFalse(self.book.claim('46', 'run-2-bb', 'w2'))
        self.assertEqual(self.book.owner('46')['run_id'], 'run-1-aa')

    def test_leases_are_per_BOARD_so_overlapping_runs_cannot_both_claim(self):
        """This is why the book is keyed per board rather than per run. A
        per-run lease would let a second run started while the first is live
        claim everything the first already holds."""
        self.assertTrue(self.book.claim('46', 'run-1-aa'))
        second = runs.LeaseBook(self.record('leases.json'))   # a different run
        self.assertFalse(second.claim('46', 'run-2-bb'))

    def test_re_claiming_your_own_lease_writes_nothing(self):
        self.assertTrue(self.book.claim('46', 'run-1-aa', 'w1'))
        self.assertFalse(self.book.claim('46', 'run-1-aa', 'w1'))

    def test_different_items_do_not_contend(self):
        self.assertTrue(self.book.claim('46', 'run-1-aa'))
        self.assertTrue(self.book.claim('47', 'run-2-bb'))

    def test_a_run_cannot_release_another_runs_lease(self):
        """Otherwise a stopping run could free an item a live run is mid-write
        on, and re-open the double-claim leases exist to prevent."""
        self.book.claim('46', 'run-1-aa')
        self.assertFalse(self.book.release('46', 'run-2-bb'))
        self.assertIsNotNone(self.book.owner('46'))
        self.assertTrue(self.book.release('46', 'run-1-aa'))
        self.assertIsNone(self.book.owner('46'))

    def test_release_run_frees_only_that_runs_items(self):
        self.book.claim('1', 'run-1-aa')
        self.book.claim('2', 'run-1-aa')
        self.book.claim('3', 'run-2-bb')
        self.assertEqual(sorted(self.book.release_run('run-1-aa')), ['1', '2'])
        self.assertEqual(list(self.book.all()), ['3'])

    def test_the_boot_sweep_reclaims_leases_of_runs_that_are_not_live(self):
        self.book.claim('1', 'run-1-aa')
        self.book.claim('2', 'run-2-bb')
        orphans = self.book.reclaim_orphans(['run-2-bb'])
        self.assertEqual(orphans, {'1': 'run-1-aa'})
        self.assertEqual(list(self.book.all()), ['2'])

    def test_at_boot_nothing_is_live_so_every_lease_is_reclaimed(self):
        self.book.claim('1', 'run-1-aa')
        self.book.claim('2', 'run-2-bb')
        self.assertEqual(len(self.book.reclaim_orphans([])), 2)
        self.assertEqual(self.book.all(), {})

    def test_leases_do_NOT_expire_on_a_timer(self):
        """A TTL has to guess how long work takes: too short frees an item mid
        write, too long strands it. There is deliberately no such knob."""
        self.assertFalse(hasattr(self.book, 'ttl'))
        self.book.claim('46', 'run-1-aa', now=0)      # claimed in 1970
        self.assertFalse(self.book.claim('46', 'run-2-bb'))


# ── processed markers: the headline ────────────────────────────────────────

class ProcessedLogTests(_Disk):
    def setUp(self):
        super().setUp()
        self.log = runs.ProcessedLog(self.record('processed.json'))

    def test_running_the_same_board_twice_does_nothing_the_second_time(self):
        """THE Phase 4 assertion."""
        items = [mk_item('1'), mk_item('2'), mk_item('3')]
        clean, _e = runs.validate_select({})

        def seen(item):
            return self.log.seen(item['id'], engine.content_hash(item)) is not None

        first, skipped = runs.select_items(items, clean, is_processed=seen)
        self.assertEqual(len(first), 3)
        self.assertEqual(skipped, [])
        for item in first:
            self.log.record(item['id'], engine.content_hash(item),
                            run_id='run-1-aa', disposition='completed')

        second, skipped = runs.select_items(items, clean, is_processed=seen)
        self.assertEqual(second, [])
        self.assertEqual(len(skipped), 3)

    def test_an_EDITED_item_becomes_eligible_again(self):
        """The reason the key is a content hash and not a bare id."""
        item = mk_item('1')
        self.log.record('1', engine.content_hash(item), run_id='run-1-aa')
        self.assertIsNotNone(self.log.seen('1', engine.content_hash(item)))

        edited = mk_item('1', title='Refund not received — URGENT',
                         updated_at='2026-03-01')
        self.assertIsNone(self.log.seen('1', engine.content_hash(edited)))

    def test_two_items_with_identical_TEXT_do_not_collide(self):
        """On a support board 'Refund not received' is not a rare title, so a
        hash-only key would mark one ticket processed by working another."""
        a, b = mk_item('1'), mk_item('2')
        self.log.record('1', engine.content_hash(a))
        self.assertIsNone(self.log.seen('2', engine.content_hash(b)))

    def test_the_recorded_outcome_explains_why_an_item_was_skipped(self):
        item = mk_item('1')
        self.log.record('1', engine.content_hash(item), run_id='run-9-ff',
                        disposition='needs_review')
        entry = self.log.seen('1', engine.content_hash(item))
        self.assertEqual(entry['run_id'], 'run-9-ff')
        self.assertEqual(entry['disposition'], 'needs_review')

    def test_forget_makes_one_item_workable_again_without_editing_the_vendor(self):
        item = mk_item('1')
        h = engine.content_hash(item)
        self.log.record('1', h)
        self.assertTrue(self.log.forget('1', h))
        self.assertIsNone(self.log.seen('1', h))
        self.assertFalse(self.log.forget('1', h))

    def test_forget_item_drops_every_recorded_version_of_it(self):
        self.log.record('1', 'hash-a')
        self.log.record('1', 'hash-b')
        self.log.record('2', 'hash-c')
        self.assertEqual(len(self.log.forget_item('1')), 2)
        self.assertEqual(len(self.log.all()), 1)

    def test_pruning_is_by_COUNT_not_age(self):
        """An item nobody has touched in a year must still be skipped, so age
        is the wrong axis — but unbounded growth on a busy board is real."""
        log = runs.ProcessedLog(self.record('p2.json'), max_entries=3)
        for i in range(5):
            log.record(str(i), 'h', now=1000 + i)
        stored = log.all()
        self.assertEqual(len(stored), 3)
        self.assertIsNone(log.seen('0', 'h'))       # oldest went first
        self.assertIsNotNone(log.seen('4', 'h'))


if __name__ == '__main__':
    unittest.main()
