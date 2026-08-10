"""`boards.store.JsonRecord` — the compare-and-set primitive Phase 4 rests on.

Two classes of test, and they are not equally trustworthy on every platform:

- The plain read/write/abort behaviour is platform-independent and always runs.
- **Mutual exclusion is only meaningful where `flock` really locks.** This dev
  box shims `fcntl` so the Linux-targeted suite imports at all, and a shimmed
  `flock` is a no-op. A concurrency test running against a no-op lock would go
  green while proving nothing — worse than not running, because it reports
  safety nobody checked. Those tests therefore `skipUnless(real_flock())` and
  skip LOUDLY on Windows; they run for real in CI on Linux.

Run:  python3 -m unittest tests.boards_store_test   (from charts/workspace/)
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
    _shim.flock = lambda *a, **k: None
    _shim.lockf = lambda *a, **k: None
    _shim.LOCK_EX = _shim.LOCK_UN = _shim.LOCK_SH = _shim.LOCK_NB = 0
    _shim._kube_coder_shim = True
    sys.modules['fcntl'] = _shim

from boards import store  # noqa: E402

REAL_LOCK = store.real_flock()


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='kc-bstore-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def rec(self, name='r.json', default=None):
        return store.JsonRecord(os.path.join(self.tmp, name), default=default)


class BasicsTests(_Base):
    def test_absent_record_reads_as_the_default(self):
        self.assertEqual(self.rec().read(), {})
        self.assertEqual(self.rec('l.json', default=[]).read(), [])

    def test_write_then_read(self):
        r = self.rec()
        r.write({'a': 1})
        self.assertEqual(self.rec().read(), {'a': 1})

    def test_update_mutates_in_place_and_reports_that_it_wrote(self):
        r = self.rec()
        data, wrote = r.update(lambda d: d.__setitem__('n', 1))
        self.assertTrue(wrote)
        self.assertEqual(data['n'], 1)
        self.assertEqual(r.read(), {'n': 1})

    def test_returning_False_aborts_the_write(self):
        """The compare-and-set. A lease claim that loses the race says so by
        returning False, and nothing is written."""
        r = self.rec()
        r.write({'owner': 'a'})

        def claim(d):
            if d.get('owner'):
                return False        # someone already owns it
            d['owner'] = 'b'

        data, wrote = r.update(claim)
        self.assertFalse(wrote)
        self.assertEqual(data['owner'], 'a')
        self.assertEqual(r.read()['owner'], 'a')

    def test_the_mutator_sees_state_written_after_the_caller_last_read(self):
        """The read happens INSIDE the lock, so a mutator never decides on a
        value the caller was holding beforehand."""
        r = self.rec()
        r.write({'v': 1})
        stale = r.read()
        store.JsonRecord(r.path).write({'v': 2})
        seen = []
        r.update(lambda d: seen.append(d['v']))
        self.assertEqual(stale['v'], 1)
        self.assertEqual(seen, [2])

    def test_a_corrupt_record_reads_as_the_default_rather_than_raising(self):
        r = self.rec()
        os.makedirs(self.tmp, exist_ok=True)
        with open(r.path, 'w') as f:
            f.write('{ not json')
        self.assertEqual(r.read(), {})
        _d, wrote = r.update(lambda d: d.__setitem__('n', 1))
        self.assertTrue(wrote)
        self.assertEqual(r.read(), {'n': 1})

    def test_a_record_of_the_wrong_TYPE_reads_as_the_default(self):
        r = self.rec('l.json', default=[])
        with open(r.path, 'w') as f:
            json.dump({'not': 'a list'}, f)
        self.assertEqual(r.read(), [])

    def test_the_default_is_not_shared_between_reads(self):
        r = self.rec()
        first = r.read()
        first['scribbled'] = True
        self.assertEqual(r.read(), {})

    def test_parent_directories_are_created(self):
        r = self.rec(os.path.join('deep', 'deeper', 'r.json'))
        r.update(lambda d: d.__setitem__('n', 1))
        self.assertTrue(os.path.isfile(r.path))

    def test_delete_removes_the_record_and_its_lock(self):
        r = self.rec()
        r.write({'a': 1})
        self.assertTrue(r.delete())
        self.assertFalse(os.path.exists(r.path))
        self.assertFalse(os.path.exists(r.lock_path))
        self.assertFalse(r.delete())

    def test_a_failed_mutator_leaves_the_record_untouched(self):
        r = self.rec()
        r.write({'a': 1})

        def boom(_d):
            raise ValueError('nope')

        with self.assertRaises(ValueError):
            r.update(boom)
        self.assertEqual(r.read(), {'a': 1})

    def test_no_temp_files_are_left_behind(self):
        r = self.rec()
        for i in range(5):
            r.update(lambda d, i=i: d.__setitem__('n', i))
        leftovers = [n for n in os.listdir(self.tmp) if n.startswith('.tmp-')]
        self.assertEqual(leftovers, [])

    @unittest.skipIf(sys.platform == 'win32', 'POSIX file modes')
    def test_the_record_is_0600(self):
        r = self.rec()
        r.write({'a': 1})
        self.assertEqual(os.stat(r.path).st_mode & 0o777, 0o600)


@unittest.skipUnless(
    REAL_LOCK,
    'fcntl.flock is shimmed on this platform — a mutual-exclusion test here '
    'would pass without locking anything. Runs for real on Linux/CI.')
class MutualExclusionTests(_Base):
    """The property Phase 4 actually needs: exactly one claimant."""

    def test_exactly_one_of_twenty_threads_claims_the_item(self):
        r = self.rec()
        r.write({'owner': None})
        winners = []
        barrier = threading.Barrier(20)

        def claim(worker):
            barrier.wait()

            def mutate(d):
                if d.get('owner') is not None:
                    return False
                d['owner'] = worker

            _data, wrote = store.JsonRecord(r.path).update(mutate)
            if wrote:
                winners.append(worker)

        threads = [threading.Thread(target=claim, args=(f'w{i}',))
                   for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(winners), 1, f'expected one winner, got {winners}')
        self.assertEqual(r.read()['owner'], winners[0])

    def test_concurrent_increments_do_not_lose_updates(self):
        """Read-modify-write across threads. Without the lock this loses
        increments — that is exactly how a write budget silently overspends."""
        r = self.rec()
        r.write({'n': 0})

        def bump():
            for _ in range(20):
                store.JsonRecord(r.path).update(
                    lambda d: d.__setitem__('n', d['n'] + 1))

        threads = [threading.Thread(target=bump) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(r.read()['n'], 100)


class RealFlockDetectionTests(unittest.TestCase):
    def test_the_shim_is_reported_as_not_a_real_lock(self):
        """If this ever returns True under the shim, every skipUnless above
        turns into a test that lies."""
        if sys.platform == 'win32':
            self.assertFalse(store.real_flock())
        else:
            self.assertTrue(store.real_flock())


if __name__ == '__main__':
    unittest.main()
