"""Three-tier board rate limiting.

The tests worth reading are the GitHub ones: its secondary limits arrive as
status 200 or 403 rather than 429, so a hardcoded 429 check would spend a run's
entire budget without ever noticing it was being throttled — while naively
treating every 403 as a limit would retry a permission denial forever.

Run:  python3 -m unittest tests.boards_limits_test   (from charts/workspace/)
"""

import os
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from boards.limits import BoardLimiter, LimitExceeded, RateLimiter  # noqa: E402


class RateLimiterTests(unittest.TestCase):
    def test_allows_up_to_max_then_refuses(self):
        rl = RateLimiter(max_events=3, window_seconds=60)
        self.assertTrue(all(rl.allow('k') for _ in range(3)))
        self.assertFalse(rl.allow('k'))

    def test_keys_are_independent(self):
        rl = RateLimiter(max_events=1, window_seconds=60)
        self.assertTrue(rl.allow('a'))
        self.assertTrue(rl.allow('b'))
        self.assertFalse(rl.allow('a'))

    def test_window_slides(self):
        rl = RateLimiter(max_events=1, window_seconds=60)
        with mock.patch.object(time, 'time', return_value=1000.0):
            self.assertTrue(rl.allow('k'))
            self.assertFalse(rl.allow('k'))
        with mock.patch.object(time, 'time', return_value=1061.0):
            self.assertTrue(rl.allow('k'))

    def test_retry_after_reports_time_until_free(self):
        rl = RateLimiter(max_events=1, window_seconds=60)
        with mock.patch.object(time, 'time', return_value=1000.0):
            rl.allow('k')
            self.assertAlmostEqual(rl.retry_after('k'), 60.0, places=1)
        with mock.patch.object(time, 'time', return_value=1030.0):
            self.assertAlmostEqual(rl.retry_after('k'), 30.0, places=1)


class PerItemWriteBudgetTests(unittest.TestCase):
    """comment + status + assign burns 3 of Zendesk's 30-per-10-min-per-ticket."""

    def setUp(self):
        self.lim = BoardLimiter({'per_item_writes': 3,
                                 'global': {'max_events': 999,
                                            'window_seconds': 60}})

    def test_three_writes_then_refused(self):
        for action in ('comment', 'set_status', 'assign'):
            self.lim.check('b1', 'T-1', action)
        self.assertEqual(self.lim.writes_used('b1', 'T-1'), 3)
        with self.assertRaises(LimitExceeded) as cm:
            self.lim.check('b1', 'T-1', 'comment')
        self.assertEqual(cm.exception.tier, 'per-item')

    def test_budget_is_per_item_not_per_board(self):
        for action in ('a', 'b', 'c'):
            self.lim.check('b1', 'T-1', action)
        self.lim.check('b1', 'T-2', 'comment')      # different ticket, fine

    def test_multi_unit_cost_is_respected(self):
        self.lim.check('b1', 'T-9', 'bulk', cost=3)
        with self.assertRaises(LimitExceeded):
            self.lim.check('b1', 'T-9', 'comment')

    def test_a_write_that_would_overflow_is_refused_before_being_counted(self):
        self.lim.check('b1', 'T-3', 'comment', cost=2)
        with self.assertRaises(LimitExceeded):
            self.lim.check('b1', 'T-3', 'bulk', cost=2)
        self.assertEqual(self.lim.writes_used('b1', 'T-3'), 2)

    def test_the_budget_is_a_WINDOW_not_a_lifetime_cap(self):
        """Zendesk's is 30 per 10 minutes per ticket. A lifetime total would
        silently retire a busy ticket forever — a worse failure than being
        throttled, because nothing ever clears it."""
        lim = BoardLimiter({'per_item_writes': 2,
                            'per_item_writes_window_seconds': 60})
        with mock.patch('boards.limits.time.time', return_value=1000.0):
            lim.check('b1', 'T-1', 'comment')
            lim.check('b1', 'T-1', 'comment')
            with self.assertRaises(LimitExceeded):
                lim.check('b1', 'T-1', 'comment')
        with mock.patch('boards.limits.time.time', return_value=1061.0):
            self.assertEqual(lim.writes_used('b1', 'T-1'), 0)
            lim.check('b1', 'T-1', 'comment')

    def test_the_refusal_names_the_window_so_the_wait_is_knowable(self):
        lim = BoardLimiter({'per_item_writes': 1,
                            'per_item_writes_window_seconds': 300})
        lim.check('b1', 'T-1', 'comment')
        with self.assertRaises(LimitExceeded) as cm:
            lim.check('b1', 'T-1', 'comment')
        self.assertIn('300s', cm.exception.detail)


class DurableWriteBudgetTests(unittest.TestCase):
    """The budget has to survive a restart.

    Held only in memory, a pod restart mid-run resets every ticket's counter to
    zero — so a connector declaring "at most 3 writes per ticket" quietly
    spends six. `write_log` seeds a fresh limiter from what was persisted;
    `on_write` is how new writes get there.
    """

    def test_a_seeded_log_is_already_spent(self):
        now = time.time()
        lim = BoardLimiter(
            {'per_item_writes': 3, 'per_item_writes_window_seconds': 600},
            write_log={'b1:T-1': [[now - 10, 1], [now - 5, 2]]})
        self.assertEqual(lim.writes_used('b1', 'T-1'), 3)
        with self.assertRaises(LimitExceeded):
            lim.check('b1', 'T-1', 'comment')

    def test_a_seeded_log_older_than_the_window_does_not_count(self):
        now = time.time()
        lim = BoardLimiter(
            {'per_item_writes': 1, 'per_item_writes_window_seconds': 60},
            write_log={'b1:T-1': [[now - 3600, 1]]})
        self.assertEqual(lim.writes_used('b1', 'T-1'), 0)
        lim.check('b1', 'T-1', 'comment')

    def test_on_write_is_called_once_per_permitted_write_and_never_on_refusal(self):
        seen = []
        lim = BoardLimiter({'per_item_writes': 2},
                           on_write=lambda k, ts, c: seen.append((k, c)))
        lim.check('b1', 'T-1', 'comment')
        lim.check('b1', 'T-1', 'assign')
        with self.assertRaises(LimitExceeded):
            lim.check('b1', 'T-1', 'comment')
        self.assertEqual(seen, [('b1:T-1', 1), ('b1:T-1', 1)])

    def test_the_snapshot_round_trips_into_a_fresh_limiter(self):
        first = BoardLimiter({'per_item_writes': 3})
        first.check('b1', 'T-1', 'comment')
        first.check('b1', 'T-1', 'assign')
        second = BoardLimiter({'per_item_writes': 3},
                              write_log=first.write_log_snapshot())
        self.assertEqual(second.writes_used('b1', 'T-1'), 2)
        second.check('b1', 'T-1', 'set_status')
        with self.assertRaises(LimitExceeded):
            second.check('b1', 'T-1', 'comment')

    def test_the_snapshot_drops_entries_that_fell_out_of_the_window(self):
        lim = BoardLimiter({'per_item_writes': 5,
                            'per_item_writes_window_seconds': 60},
                           write_log={'b1:T-1': [[time.time() - 999, 1]]})
        self.assertEqual(lim.write_log_snapshot(), {})


class TierPrecedenceTests(unittest.TestCase):
    def test_per_action_override_is_tighter_than_global_and_wins(self):
        lim = BoardLimiter({
            'global': {'max_events': 100, 'window_seconds': 60},
            'per_action': {'comment': {'max_events': 2, 'window_seconds': 600}},
        })
        lim.check('b1', 'T-1', 'comment')
        lim.check('b1', 'T-2', 'comment')
        with self.assertRaises(LimitExceeded) as cm:
            lim.check('b1', 'T-3', 'comment')
        self.assertEqual(cm.exception.tier, 'per-action')
        # A different action is unaffected by comment's tighter bucket.
        lim.check('b1', 'T-4', 'set_status')

    def test_global_limit_throttles_across_items_and_actions(self):
        lim = BoardLimiter({'global': {'max_events': 2, 'window_seconds': 60}})
        lim.check('b1', 'T-1', 'comment')
        lim.check('b1', 'T-2', 'set_status')
        with self.assertRaises(LimitExceeded) as cm:
            lim.check('b1', 'T-3', 'assign')
        self.assertEqual(cm.exception.tier, 'global')

    def test_global_limit_is_per_board(self):
        lim = BoardLimiter({'global': {'max_events': 1, 'window_seconds': 60}})
        lim.check('b1', 'T-1', 'comment')
        lim.check('b2', 'T-1', 'comment')

    def test_per_item_checked_before_global_so_a_doomed_write_wastes_nothing(self):
        lim = BoardLimiter({'per_item_writes': 1,
                            'global': {'max_events': 5, 'window_seconds': 60}})
        lim.check('b1', 'T-1', 'comment')
        with self.assertRaises(LimitExceeded) as cm:
            lim.check('b1', 'T-1', 'comment')
        self.assertEqual(cm.exception.tier, 'per-item')
        # The refused write must not have consumed a global slot.
        for i in range(4):
            lim.check('b1', f'T-other-{i}', 'comment')


class LimitDetectionTests(unittest.TestCase):
    def test_default_detects_429_only(self):
        lim = BoardLimiter({})
        self.assertTrue(lim.is_limit_response(429, b''))
        self.assertFalse(lim.is_limit_response(403, b'Forbidden'))

    def test_github_secondary_limit_at_403_with_phrase(self):
        lim = BoardLimiter({'limit_detect': {
            'statuses': [403, 429],
            'body_contains': ['secondary rate limit', 'api rate limit exceeded']}})
        self.assertTrue(lim.is_limit_response(
            403, b'{"message":"You have exceeded a secondary rate limit"}'))

    def test_github_secondary_limit_at_200_with_phrase(self):
        lim = BoardLimiter({'limit_detect': {
            'statuses': [200, 403], 'body_contains': ['secondary rate limit']}})
        self.assertTrue(lim.is_limit_response(200, b'secondary rate limit hit'))

    def test_plain_403_is_a_permission_error_not_a_limit(self):
        """Retrying a permission denial forever is its own outage."""
        lim = BoardLimiter({'limit_detect': {
            'statuses': [403, 429], 'body_contains': ['secondary rate limit']}})
        self.assertFalse(lim.is_limit_response(
            403, b'{"message":"Resource not accessible by integration"}'))

    def test_phrase_match_is_case_insensitive(self):
        lim = BoardLimiter({'limit_detect': {
            'statuses': [403], 'body_contains': ['Secondary Rate Limit']}})
        self.assertTrue(lim.is_limit_response(403, b'SECONDARY RATE LIMIT'))


class GlobalBackoffTests(unittest.TestCase):
    def test_retry_after_delta_seconds_pauses_the_whole_run(self):
        lim = BoardLimiter({})
        waited = lim.note_limit_response({'Retry-After': '45'})
        self.assertAlmostEqual(waited, 45.0, places=1)
        with self.assertRaises(LimitExceeded) as cm:
            lim.check('b1', 'T-1', 'comment')
        self.assertEqual(cm.exception.tier, 'global-backoff')

    def test_backoff_is_global_not_per_worker(self):
        """The budget is account-wide, so one worker hitting a limit must pause
        every other item too."""
        lim = BoardLimiter({})
        lim.note_limit_response({'Retry-After': '30'})
        for item in ('T-1', 'T-2', 'T-3'):
            with self.assertRaises(LimitExceeded):
                lim.check('b1', item, 'comment')

    def test_absolute_epoch_reset_header_honoured(self):
        lim = BoardLimiter({'retry_after_headers': ['ratelimit-reset']})
        future = time.time() + 60
        waited = lim.note_limit_response({'ratelimit-reset': str(int(future))})
        self.assertGreater(waited, 50)
        self.assertLess(waited, 70)

    def test_header_name_is_matched_case_insensitively(self):
        lim = BoardLimiter({})
        self.assertAlmostEqual(
            lim.note_limit_response({'retry-after': '20'}), 20.0, places=1)

    def test_missing_header_falls_back_to_the_default(self):
        lim = BoardLimiter({})
        self.assertAlmostEqual(
            lim.note_limit_response({}, default_backoff=12.0), 12.0, places=1)

    def test_garbage_header_falls_back_rather_than_crashing(self):
        lim = BoardLimiter({})
        self.assertAlmostEqual(
            lim.note_limit_response({'Retry-After': 'Wed, 21 Oct 2026 07:28:00 GMT'},
                                    default_backoff=15.0), 15.0, places=1)

    def test_backoff_is_capped_so_a_run_never_parks_indefinitely(self):
        lim = BoardLimiter({})
        self.assertLessEqual(lim.note_limit_response({'Retry-After': '99999'}), 900.0)

    def test_pause_expires(self):
        lim = BoardLimiter({})
        with mock.patch.object(time, 'time', return_value=1000.0):
            lim.note_limit_response({'Retry-After': '30'})
        with mock.patch.object(time, 'time', return_value=1031.0):
            lim.check('b1', 'T-1', 'comment')     # no raise


if __name__ == '__main__':
    unittest.main()
