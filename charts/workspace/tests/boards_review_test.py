"""`boards.review` — dispositions and the three approval guards.

The tests that matter are in `ApprovalGuardTests`. Each one names a distinct
thing that goes wrong if the guard is missing:

- **stale** — the ticket changed on the vendor after the action was staged, so
  approving would write over a colleague's reply. This is the most damaging
  thing the Board Processor could do.
- **hash_mismatch** — the reviewer's UI is showing an older card than the one
  on disk, so they would be approving something they have not read.
- **replay** — a phone on a flaky connection retries, and the second attempt
  must return the first attempt's result rather than posting a second comment.

Run:  python3 -m unittest tests.boards_review_test   (from charts/workspace/)
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

from boards import review, store  # noqa: E402

ITEM = {'id': '46', 'key': 'SUP-5', 'title': 'Refund not received',
        'url': 'https://acme.atlassian.net/browse/SUP-5'}
APPROVAL = 'a1b2c3d4-e5f6-4711-8899-aabbccddeeff'


def rec(**over):
    r = review.new_record('acme-jira', ITEM, content_hash='hash-1',
                          run_id='run-1-aaaa')
    r.update(over)
    return r


class RecordTests(unittest.TestCase):
    def test_a_new_record_copies_the_item_text_the_agent_saw(self):
        """The reviewer must see what the agent saw. Re-fetching for the card
        would show them a ticket that may already have moved on — and would
        make the staleness check compare a value against itself."""
        r = rec()
        self.assertEqual(r['item_key'], 'SUP-5')
        self.assertEqual(r['item_title'], 'Refund not received')
        self.assertEqual(r['content_hash'], 'hash-1')
        self.assertEqual(r['state'], 'pending')

    def test_staging_appends_an_action_with_a_preview(self):
        r = rec()
        entry = review.stage_action(
            r, action_id='a1', action='comment',
            params={'body': 'Hi Dana — the refund was issued.'},
            preview='Hi Dana — the refund was issued.')
        self.assertEqual(entry['state'], 'pending')
        self.assertEqual(len(r['actions']), 1)
        self.assertIn('Dana', entry['preview'])

    def test_a_duplicate_action_id_is_refused(self):
        r = rec()
        review.stage_action(r, action_id='a1', action='comment', params={})
        with self.assertRaises(review.ReviewError) as cm:
            review.stage_action(r, action_id='a1', action='comment', params={})
        self.assertEqual(cm.exception.code, 'duplicate_action_id')

    def test_staging_against_a_decided_record_is_refused(self):
        r = rec()
        review.consume(r, APPROVAL, state='rejected')
        with self.assertRaises(review.ReviewError) as cm:
            review.stage_action(r, action_id='a1', action='comment', params={})
        self.assertEqual(cm.exception.code, 'already_decided')

    def test_the_staged_action_count_is_bounded(self):
        r = rec()
        for i in range(review.MAX_STAGED_ACTIONS):
            review.stage_action(r, action_id=f'a{i}', action='comment',
                                params={})
        with self.assertRaises(review.ReviewError) as cm:
            review.stage_action(r, action_id='zz', action='comment', params={})
        self.assertEqual(cm.exception.code, 'too_many_actions')


class DispositionTests(unittest.TestCase):
    def test_completed_needs_no_reason(self):
        r = rec()
        review.set_disposition(r, 'completed')
        self.assertEqual(r['disposition'], 'completed')

    def test_every_other_disposition_REQUIRES_a_reason(self):
        """Enforced here rather than trusted to the preamble: a disposition
        with no reason is indistinguishable from progress in every list it
        appears in."""
        for d in ('needs_review', 'needs_rescoping', 'blocked', 'rejected',
                  'failed'):
            with self.subTest(disposition=d):
                with self.assertRaises(review.ReviewError) as cm:
                    review.set_disposition(rec(), d, reason='   ')
                self.assertEqual(cm.exception.code, 'reason_required')

    def test_an_unknown_disposition_is_refused(self):
        with self.assertRaises(review.ReviewError) as cm:
            review.set_disposition(rec(), 'kind_of_done', reason='x')
        self.assertEqual(cm.exception.code, 'bad_disposition')

    def test_evidence_rides_along_with_the_reason(self):
        r = rec()
        review.set_disposition(r, 'blocked', reason='no refund record',
                               evidence={'tool_calls': 3, 'searched': 'Stripe'})
        self.assertEqual(r['evidence']['tool_calls'], 3)

    def test_grouping_puts_needs_review_first(self):
        """Mission Control puts `waiting` first for the same reason: the
        column that needs a human is the one you opened the page for."""
        records = [rec(disposition='completed'), rec(disposition='blocked'),
                   rec(disposition='needs_review')]
        groups = review.group_by_disposition(records)
        self.assertEqual([g['disposition'] for g in groups],
                         ['needs_review', 'blocked', 'completed'])

    def test_an_unreported_item_is_grouped_rather_than_dropped(self):
        groups = review.group_by_disposition([rec()])
        self.assertEqual(groups[0]['disposition'], 'unreported')


class ApprovalGuardTests(unittest.TestCase):
    def approvable(self, r, *, echoed='hash-1', fresh='hash-1',
                   approval_id=APPROVAL):
        return review.check_approvable(r, echoed_hash=echoed, fresh_hash=fresh,
                                       approval_id=approval_id)

    def test_a_clean_approval_proceeds(self):
        self.assertEqual(self.approvable(rec()), ('proceed', None))

    def test_a_ticket_edited_after_staging_is_STALE(self):
        """The guard that matters. Someone may have replied to the ticket, and
        writing over a colleague's reply is the worst outcome available."""
        with self.assertRaises(review.ReviewError) as cm:
            self.approvable(rec(), fresh='hash-2')
        self.assertEqual(cm.exception.code, 'stale')
        self.assertIn('someone may have replied', cm.exception.detail)

    def test_a_stale_CARD_is_refused_separately_from_a_stale_ticket(self):
        """The reviewer is looking at an older card than the record on disk, so
        they would be approving something they have not read."""
        with self.assertRaises(review.ReviewError) as cm:
            self.approvable(rec(), echoed='hash-0')
        self.assertEqual(cm.exception.code, 'hash_mismatch')

    def test_a_retry_of_the_SAME_approval_replays_the_stored_result(self):
        """A phone on a dropped connection retries. The second attempt must not
        post a second comment. There is no idempotency-key convention anywhere
        in the mobile app, so this had to be designed rather than inherited."""
        r = rec()
        review.consume(r, APPROVAL, state='approved',
                       result={'ok': True, 'actions': ['comment']})
        verdict, stored = self.approvable(r)
        self.assertEqual(verdict, 'replay')
        self.assertEqual(stored['ok'], True)

    def test_a_replay_is_answered_even_when_the_ticket_has_since_CHANGED(self):
        """It changed BECAUSE of the write we already made. Reporting `stale`
        to a retry would make a successful approval look like a conflict."""
        r = rec()
        review.consume(r, APPROVAL, state='approved', result={'ok': True})
        verdict, _stored = self.approvable(r, fresh='hash-after-our-comment')
        self.assertEqual(verdict, 'replay')

    def test_a_DIFFERENT_approval_on_a_decided_record_is_a_conflict(self):
        """Two reviewers, one item. The second is told, not silently applied."""
        r = rec()
        review.consume(r, APPROVAL, state='approved', result={'ok': True})
        with self.assertRaises(review.ReviewError) as cm:
            self.approvable(r, approval_id='ffffffff-0000-0000-0000-000000000000')
        self.assertEqual(cm.exception.code, 'already_decided')

    def test_a_missing_or_malformed_approval_id_is_refused(self):
        for bad in ('', 'short', None, 'has spaces in it here', 'x' * 200):
            with self.subTest(approval_id=bad):
                with self.assertRaises(review.ReviewError) as cm:
                    self.approvable(rec(), approval_id=bad)
                self.assertEqual(cm.exception.code, 'bad_approval_id')

    def test_a_partial_record_is_still_open_for_approval(self):
        """A run whose first action succeeded and second failed must remain
        actionable rather than showing a green 'approved' over half a change."""
        r = rec(state='partial')
        self.assertEqual(self.approvable(r)[0], 'proceed')


class StagedBookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='kc-review-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.book = review.StagedBook(
            lambda item_id: store.JsonRecord(
                os.path.join(self.tmp, f'{item_id}.json')))

    def test_round_trip(self):
        self.assertIsNone(self.book.get('46'))
        self.book.put(rec())
        self.assertEqual(self.book.get('46')['item_key'], 'SUP-5')

    def test_records_are_per_ITEM_so_two_reviews_do_not_serialise(self):
        """Approving 46 and rejecting 47 are independent decisions a review
        queue makes concurrently; one board-wide file would put them behind a
        single lock for no reason."""
        a = rec()
        b = rec()
        b['item_id'] = '47'
        self.book.put(a)
        self.book.put(b)
        self.assertNotEqual(
            os.path.join(self.tmp, '46.json'), os.path.join(self.tmp, '47.json'))
        self.assertEqual(self.book.get('47')['item_id'], '47')

    def test_update_aborts_cleanly(self):
        self.book.put(rec())
        _r, wrote = self.book.update('46', lambda d: False)
        self.assertFalse(wrote)

    def test_public_view_marks_open_records_and_lists_pending_actions(self):
        r = rec()
        review.stage_action(r, action_id='a1', action='comment', params={})
        view = review.public_view(r)
        self.assertTrue(view['open'])
        self.assertEqual(len(view['pending_actions']), 1)
        review.consume(r, APPROVAL, state='approved')
        self.assertFalse(review.public_view(r)['open'])


if __name__ == '__main__':
    unittest.main()
