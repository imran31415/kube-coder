"""Atomic JSON records on the PVC — the one impure module in this package.

`schema` and `engine` are pure by design. This is not, and says so: it exists
because Phase 4 needs two things that only a real file lock can give.

1. **Compare-and-set.** Claiming an item for a run has to be a decision and a
   write inside ONE lock. Read-then-write across two syscalls is a race, and
   the failure it produces is the one this whole phase exists to prevent: two
   workers both believing they own item 46, both commenting on the customer's
   ticket.

2. **Read-modify-write across processes.** Per-item write budgets have to
   survive a pod restart, so they live on disk, and several run workers update
   them concurrently.

Phase 7 adds a third, with the opposite shape: an **append-only** ledger
(`JsonlLog`) for decisions, because the review book overwrites and approval rate
has to be counted over time. See that class for why it is not another
`JsonRecord`.

The implementation is `ClaudeTaskManager._atomic_update_meta`
(server.py:3208) generalised: a `.lock` sidecar held with `fcntl.flock(LOCK_EX)`,
a FRESH read taken inside the lock, and a tmp+rename to publish. The one
addition is that `update()` distinguishes "wrote" from "aborted", because a
lease claim needs to know which happened.

**The lock is real or it is nothing.** On a platform without a working `fcntl`
(a Windows dev box running the suite through a shim) `flock` is a no-op, and a
test that proves mutual exclusion would pass while proving nothing. `real_flock()`
reports that honestly so the locking tests can skip loudly rather than lie.
"""

import errno
import fcntl
import json
import os
import tempfile
import time


def real_flock():
    """Whether `fcntl.flock` on this interpreter actually locks.

    A shimmed fcntl (see tests) exposes the same names and does nothing. Any
    test that asserts mutual exclusion must skip when this is False — a green
    double-claim test on a no-op lock is worse than a skipped one, because it
    reports safety that was never checked.
    """
    if getattr(fcntl, '_kube_coder_shim', False):
        return False
    return hasattr(fcntl, 'flock') and getattr(fcntl.flock, '__module__', '') == 'fcntl'


class Aborted(Exception):
    """Raised by nothing — a sentinel type for callers that prefer exceptions.
    `update()` itself reports an abort by returning `(record, False)`."""


class JsonRecord:
    """One JSON document plus a `.lock` sidecar.

    Not a cache: every read hits the disk. These records are small, written
    rarely and read by more than one process, so staleness would cost far more
    than the syscall does.
    """

    def __init__(self, path, *, default=None):
        self.path = path
        self._default = default if default is not None else {}

    # ── plumbing ───────────────────────────────────────────────────────────

    @property
    def lock_path(self):
        return self.path + '.lock'

    def exists(self):
        return os.path.isfile(self.path)

    def _ensure_dir(self):
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, mode=0o700, exist_ok=True)

    def _read_unlocked(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return json.loads(json.dumps(self._default))
        if not isinstance(data, type(self._default)):
            return json.loads(json.dumps(self._default))
        return data

    def _write_unlocked(self, data):
        # tempfile in the SAME directory: os.replace is only atomic within a
        # filesystem, and /tmp is frequently a different one.
        self._ensure_dir()
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or '.',
                                   prefix='.tmp-', suffix='.json')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f, indent=2)
            os.chmod(tmp, 0o600)
            self._replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _replace(tmp, path):
        """`os.replace` with a short retry — a Windows accommodation only.

        On POSIX, renaming over a file another thread is READING is fine: the
        reader keeps its descriptor on the old inode. Windows refuses with
        `PermissionError` while any handle is open, so a run whose UI is
        polling its record can lose a write for no reason at all. Retrying for
        a few hundred milliseconds costs nothing on Linux (where the first call
        always succeeds) and makes the same code usable on a dev box.

        This is NOT a substitute for the lock. It handles a reader colliding
        with a writer; two *writers* still need `flock`, which `real_flock()`
        reports honestly on platforms that do not have it.
        """
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.02)

    # ── public ─────────────────────────────────────────────────────────────

    def read(self):
        """The current contents, or the default. Never raises."""
        return self._read_unlocked()

    def write(self, data):
        """Unconditional replace. Use `update` for anything that depends on the
        current value."""
        self._ensure_dir()
        with open(self.lock_path, 'a') as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            try:
                self._write_unlocked(data)
            finally:
                fcntl.flock(lockf, fcntl.LOCK_UN)
        return data

    def update(self, mutate_fn):
        """Read-modify-write under an exclusive lock. Returns `(record, wrote)`.

        `mutate_fn` receives the FRESH contents — read inside the lock, never a
        value the caller was holding beforehand — and may mutate it in place.
        Returning `False` aborts the write and yields `(record, False)`: that is
        the compare-and-set, and it is how a lease claim reports "someone else
        got here first" without a second round trip.
        """
        self._ensure_dir()
        with open(self.lock_path, 'a') as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            try:
                data = self._read_unlocked()
                if mutate_fn(data) is False:
                    return data, False
                self._write_unlocked(data)
                return data, True
            finally:
                fcntl.flock(lockf, fcntl.LOCK_UN)

    def delete(self):
        """Remove the record and its lock sidecar. Returns whether the record
        was there."""
        had = os.path.isfile(self.path)
        for p in (self.path, self.lock_path):
            try:
                os.unlink(p)
            except OSError as e:
                if e.errno not in (errno.ENOENT,):
                    raise
        return had


class JsonlLog:
    """An append-only JSON-lines log with one generation of rotation.

    Why this exists rather than another `JsonRecord`: the review book keeps
    CURRENT state, one file per item, and `BoardReviewManager._ensure` REPLACES
    a decided record when the same item is staged again. That is right for the
    queue — a reviewer wants the live card, not a pile of history — but it means
    every decision is erased the moment the item comes back around, and the
    re-scoping round trip makes items come back around by design. Approval rate
    cannot be computed from a store that overwrites, so the counts live here
    instead, where nothing is ever rewritten.

    **Entries are counters, not records.** Item id, disposition, decision,
    timestamps — no reasons, no ticket bodies, no proposed text. The ledger
    exists to be summed, and keeping customer words out of it means it never
    becomes a second copy of the staged book with a different retention story.

    Rotation is by BYTES rather than lines because the check has to run on every
    append and `os.path.getsize` is one stat, where counting lines is a full
    read. Exceeding the cap rolls the file to `.1` and starts fresh; `.1` is
    then read back alongside the live file, so one rotation costs nothing and
    the second one drops the oldest generation. That is a deliberate ceiling: an
    uncapped log on a PVC is a slow-motion disk-full incident, and these figures
    are gauges, so an old decision ageing out lowers a total rather than
    corrupting one.
    """

    #: ~7k entries per generation at typical entry size, two generations kept.
    MAX_BYTES = 1_000_000

    def __init__(self, path, *, max_bytes=None):
        self.path = path
        self.max_bytes = self.MAX_BYTES if max_bytes is None else max_bytes

    @property
    def lock_path(self):
        return self.path + '.lock'

    @property
    def rotated_path(self):
        return self.path + '.1'

    def _ensure_dir(self):
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, mode=0o700, exist_ok=True)

    def append(self, entry):
        """Add one entry. Never raises on a malformed entry — a ledger write
        must not be able to fail a decision that already happened."""
        try:
            line = json.dumps(entry, sort_keys=True)
        except (TypeError, ValueError):
            return False
        self._ensure_dir()
        with open(self.lock_path, 'a') as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            try:
                self._rotate_if_needed()
                # 'a' is O_APPEND: concurrent writers cannot interleave a line
                # even without the lock. The lock is held anyway so rotation
                # and the write are one operation.
                with open(self.path, 'a', encoding='utf-8') as f:
                    f.write(line + '\n')
                    os.chmod(self.path, 0o600)
            finally:
                fcntl.flock(lockf, fcntl.LOCK_UN)
        return True

    def _rotate_if_needed(self):
        """Caller holds the lock."""
        try:
            if os.path.getsize(self.path) < self.max_bytes:
                return
        except OSError:
            return
        try:
            os.replace(self.path, self.rotated_path)
        except OSError:
            # A failed rotation must not lose the append that triggered it;
            # the file simply grows past the cap until the next attempt.
            pass

    def read(self, limit=None):
        """Every entry, oldest first, across both generations.

        A line that will not parse is skipped rather than raising: a truncated
        final line from a killed process is expected, and one bad line must not
        cost the whole ledger its other few thousand.
        """
        out = []
        for path in (self.rotated_path, self.path):
            try:
                with open(path, encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(entry, dict):
                            out.append(entry)
            except OSError:
                continue
        return out[-limit:] if limit else out

    def delete(self):
        """Remove both generations and the lock sidecar."""
        had = os.path.isfile(self.path)
        for p in (self.path, self.rotated_path, self.lock_path):
            try:
                os.unlink(p)
            except OSError as e:
                if e.errno not in (errno.ENOENT,):
                    raise
        return had
