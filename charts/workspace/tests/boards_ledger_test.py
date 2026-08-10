"""The decision ledger (#588 Phase 7) — boards.store.JsonlLog.

Approval rate is the metric Phase 7 exists to produce, and it cannot be
computed from the staged book: `BoardReviewManager._ensure` REPLACES a decided
record when the same item is staged again, and the re-scoping round trip makes
that the normal case. So the ledger is append-only, and these tests are mostly
about the two properties that follow from that — nothing is ever rewritten, and
it cannot grow without bound.

Run:  python3 -m unittest tests.boards_ledger_test   (from charts/workspace/)
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

try:
    import fcntl  # noqa: F401
except ImportError:  # pragma: no cover - platform shim
    import types
    _shim = types.ModuleType('fcntl')
    _shim._kube_coder_shim = True
    _shim.flock = lambda *a, **k: None
    _shim.LOCK_EX = _shim.LOCK_UN = _shim.LOCK_SH = _shim.LOCK_NB = 0
    sys.modules['fcntl'] = _shim

from boards import store  # noqa: E402


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kc-ledger-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, 'nested', 'board.jsonl')

    def log(self, **kw):
        return store.JsonlLog(self.path, **kw)

    # ── the basic contract ─────────────────────────────────────────────────

    def test_entries_come_back_in_the_order_they_were_appended(self):
        log = self.log()
        for i in range(5):
            log.append({'item_id': str(i)})
        self.assertEqual([e['item_id'] for e in log.read()],
                         ['0', '1', '2', '3', '4'])

    def test_it_creates_its_own_directory(self):
        # The board dir exists, but `decisions/` will not on first use.
        self.log().append({'item_id': '1'})
        self.assertTrue(os.path.isfile(self.path))

    def test_reading_a_ledger_that_was_never_written_is_empty_not_an_error(self):
        self.assertEqual(self.log().read(), [])

    def test_limit_returns_the_NEWEST_entries(self):
        log = self.log()
        for i in range(10):
            log.append({'item_id': str(i)})
        self.assertEqual([e['item_id'] for e in log.read(limit=3)],
                         ['7', '8', '9'])

    # ── the properties that matter ─────────────────────────────────────────

    def test_an_entry_is_never_rewritten(self):
        """The whole reason this is not a JsonRecord. Appending a second
        decision for the same item must leave the first one intact — that is
        what the staged book cannot do."""
        log = self.log()
        log.append({'item_id': '46', 'state': 'sent_back'})
        log.append({'item_id': '46', 'state': 'approved'})
        states = [e['state'] for e in log.read() if e['item_id'] == '46']
        self.assertEqual(states, ['sent_back', 'approved'])

    def test_a_truncated_final_line_does_not_cost_the_rest_of_the_ledger(self):
        """A process killed mid-append leaves a partial line. One bad line must
        not make the other few thousand unreadable."""
        log = self.log()
        log.append({'item_id': '1'})
        log.append({'item_id': '2'})
        with open(self.path, 'a', encoding='utf-8') as f:
            f.write('{"item_id": "3", "sta')       # killed mid-write
        self.assertEqual([e['item_id'] for e in log.read()], ['1', '2'])

    def test_an_unserialisable_entry_is_refused_not_raised(self):
        """A ledger append happens AFTER the decision is consumed and the
        writes have fired. Raising here would turn a completed approval into an
        error the caller would reasonably retry."""
        log = self.log()
        self.assertIs(log.append({'bad': object()}), False)
        self.assertEqual(log.read(), [])

    # ── rotation ───────────────────────────────────────────────────────────

    def test_it_rotates_at_the_byte_cap_and_still_reads_both_generations(self):
        log = self.log(max_bytes=400)
        for i in range(40):
            log.append({'item_id': str(i), 'pad': 'x' * 20})
        self.assertTrue(os.path.isfile(log.rotated_path),
                        'the log should have rolled to .1')
        ids = [e['item_id'] for e in log.read()]
        # Everything still present across the two generations, in order.
        self.assertEqual(ids, sorted(ids, key=int))
        self.assertIn('39', ids)

    def test_a_second_rotation_drops_the_OLDEST_generation(self):
        """The ceiling is deliberate: these are gauges, so an old decision
        ageing out lowers a total rather than corrupting one."""
        log = self.log(max_bytes=300)
        for i in range(120):
            log.append({'item_id': str(i), 'pad': 'x' * 20})
        ids = [e['item_id'] for e in log.read()]
        self.assertNotIn('0', ids, 'the oldest generation should be gone')
        self.assertIn('119', ids, 'the newest entry must always survive')
        # Bounded: two generations, each under the cap plus one entry.
        self.assertLess(os.path.getsize(log.path), 400)

    def test_delete_removes_both_generations(self):
        log = self.log(max_bytes=300)
        for i in range(60):
            log.append({'item_id': str(i), 'pad': 'x' * 20})
        self.assertTrue(os.path.isfile(log.rotated_path))
        log.delete()
        self.assertEqual(log.read(), [])
        self.assertFalse(os.path.isfile(log.rotated_path))
        self.assertFalse(os.path.isfile(log.lock_path))

    # ── concurrency ────────────────────────────────────────────────────────

    @unittest.skipUnless(store.real_flock(),
                         'fcntl.flock is shimmed on this platform — a green '
                         'concurrency test against a no-op lock proves nothing')
    def test_concurrent_appends_do_not_interleave_or_lose_lines(self):
        """Two reviewers deciding two items at the same moment is the ordinary
        case, not the exotic one."""
        log = self.log()
        errors = []

        def writer(worker):
            try:
                for i in range(50):
                    log.append({'item_id': f'{worker}-{i}', 'pad': 'y' * 50})
            except Exception as e:      # pragma: no cover - surfaced below
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(w,)) for w in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        entries = log.read()
        self.assertEqual(len(entries), 200, 'every append must survive')
        self.assertEqual(len({e['item_id'] for e in entries}), 200)
        # And every line is intact JSON — no half-written record from a
        # writer that got scheduled out mid-line.
        with open(self.path, encoding='utf-8') as f:
            for line in f:
                json.loads(line)


if __name__ == '__main__':
    unittest.main()
