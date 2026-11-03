"""`boards.outcome` — the Done rule and the one-line result (#709 Part 3).

Pure functions, so no server and no PVC. The rule mirrors the phone's
`mobile/src/util/boardTabs.ts`; `test_is_done_matches_the_phone_rule` walks the
same state and conclusion matrix the mobile vitest walks.

Run:  python3 -m unittest tests.boards_outcome_test   (from charts/workspace/)
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from boards import outcome  # noqa: E402

ROW_STATES = ('pending', 'claimed', 'working', 'done', 'failed', 'skipped')
DISPOSITIONS = (None, 'completed', 'needs_review', 'needs_rescoping',
                'blocked', 'rejected', 'failed')
REVIEW_STATES = (None, 'pending', 'partial', 'approved', 'rejected',
                 'sent_back')


def phone_tab(state, disposition, review):
    """The phone's classification, restated as the spec table reads."""
    if review in ('pending', 'partial'):
        return 'needs_you'
    if state in ('claimed', 'working'):
        return 'working'
    if review in ('approved', 'rejected', 'sent_back'):
        return 'done'
    if state == 'failed':
        return 'done'
    if state == 'done':
        if disposition in ('needs_review', 'needs_rescoping', 'blocked'):
            return 'needs_you'
        return 'done'
    return None


class IsDoneTests(unittest.TestCase):
    def test_is_done_matches_the_phone_rule(self):
        for state in ROW_STATES:
            for disposition in DISPOSITIONS:
                for review in REVIEW_STATES:
                    row = {'state': state, 'disposition': disposition}
                    record = {'state': review} if review else None
                    with self.subTest(state=state, disposition=disposition,
                                      review=review):
                        self.assertEqual(
                            outcome.is_done(row, record),
                            phone_tab(state, disposition, review) == 'done')

    def test_a_decision_with_no_run_row_is_done(self):
        self.assertTrue(outcome.is_done(None, {'state': 'approved'}))
        self.assertFalse(outcome.is_done(None, None))


class ResultLineTests(unittest.TestCase):
    def _approved(self, *actions):
        return {'state': 'approved', 'item_key': '#412',
                'actions': [{'action': a, 'state': s} for a, s in actions]}

    def test_a_posted_comment_is_a_reply(self):
        self.assertEqual(
            outcome.result_line({}, self._approved(('comment', 'done'))),
            'Reply posted on #412')

    def test_other_writes_are_named(self):
        self.assertEqual(
            outcome.result_line({}, self._approved(('comment', 'done'),
                                                   ('set_status', 'done'))),
            'Written to #412: comment, set status')

    def test_discarded_or_superseded_proposals_are_not_claimed_as_written(self):
        self.assertEqual(
            outcome.result_line({}, self._approved(('comment', 'superseded'))),
            'Completed on #412')

    def test_reject_send_back_and_failure(self):
        self.assertEqual(
            outcome.result_line({}, {'state': 'rejected',
                                     'result': {'detail': 'rejected'}}),
            'Rejected, nothing was written')
        self.assertEqual(
            outcome.result_line({}, {'state': 'rejected',
                                     'result': {'detail': 'wrong customer'}}),
            'Rejected: wrong customer')
        self.assertEqual(outcome.result_line({}, {'state': 'sent_back'}),
                         'Sent back to the agent')
        self.assertEqual(
            outcome.result_line({'state': 'failed',
                                 'error': 'GitHub returned 403'}, None),
            'Failed: GitHub returned 403')

    def test_agent_conclusions_without_a_review(self):
        row = {'state': 'done', 'key': 'SUP-9', 'disposition': 'completed'}
        self.assertEqual(outcome.result_line(row, None), 'Completed on SUP-9')
        row.update(disposition='failed', reason='API down')
        self.assertEqual(outcome.result_line(row, None), 'Failed: API down')
        row.update(disposition='rejected', reason='duplicate of SUP-3')
        self.assertEqual(outcome.result_line(row, None),
                         'Not actioned: duplicate of SUP-3')

    def test_the_line_is_one_line_and_bounded(self):
        line = outcome.result_line(
            {'state': 'failed', 'error': 'boom\n' * 100}, None)
        self.assertNotIn('\n', line)
        self.assertLessEqual(len(line), outcome.MAX_LINE)
        self.assertTrue(line.endswith('…'))


class FeedKeyTests(unittest.TestCase):
    def test_it_is_the_review_rows_key(self):
        # The SAME key `_notify_if_waiting` uses, so the result replaces the
        # waiting row in place. If these drift, the badge never drops.
        self.assertEqual(outcome.feed_key('gh', 'I_kw:1'), 'board:gh:I_kw:1')


if __name__ == '__main__':
    unittest.main()
