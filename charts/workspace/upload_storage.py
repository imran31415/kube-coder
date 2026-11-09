"""Bounds on what uploads may occupy on the PVC (issue #556).

`/api/files/upload` writes straight to the volume, and agents upload too, so
nothing used to stop uploads filling it — which then fails in confusing ways
(builds die, the memory DB can't write, git breaks) with nothing pointing at
the cause. Two independent guards, and BOTH REJECT:

  quota     — ceiling on the bytes held by the *managed* upload dirs:
              uploads/<task-id>/ and .claude-tasks/<task-id>/attachments/, the
              ones the dashboard, mobile app and agents write into without
              anyone picking a location. A file deliberately uploaded into a
              project directory is a project file and is not charged.
  min_free  — floor on the free space an upload must leave on the volume,
              wherever it lands. This is the guard that stops a full PVC.

We deliberately do not evict. An uploaded file's absolute path is handed to an
agent as prompt text, so deleting it behind the agent's back turns a full disk
into a mysteriously-missing file, which is harder to debug than a refusal.

## Why a ledger rather than a size check

Checking the directory size and then writing is a race: N uploads can each see
room for themselves and together blow through the cap. So admission happens
under one lock, against what is on disk *plus* what every in-flight upload has
been promised, and the promise is recorded before the lock is dropped.

In-flight bytes sit in staging entries (see `STAGING_NAME`). The walk skips the
ones the ledger knows are live, because their reservation already charges them.
A staging entry the ledger does NOT know is an orphan from a crashed process:
it is charged, so the bound stays honest, and swept the first time this process
admits an upload into that home — the ledger is empty then, so nothing on disk
can be live. Those are half-written bytes nobody was ever told the path of, so
removing them does not break the never-evict rule above. The dashboard is a
single process on a `Recreate` deployment, so no other writer can own them.
Orphans outside the managed dirs are not swept: finding them would mean walking
the whole volume.

Committing moves staged bytes to their final name and drops the charge inside
the same lock, so a concurrent admission never sees them counted twice.

The free-space floor is deliberately a little conservative: `statvfs` already
reflects the bytes an in-flight upload has written so far, and its reservation
charges them again. That can only refuse early, by at most what is in flight.
"""

import errno
import os
import re
import shutil
import sys
import threading
import uuid

#: The name of a staging entry. Exact, so that sweeping an orphan can never
#: match a file somebody chose the name of.
STAGING_NAME = re.compile(r'^\.kc-upload-[0-9a-f]{32}$')

DEFAULT_QUOTA_BYTES = 2 * 1024 ** 3
DEFAULT_MIN_FREE_BYTES = 1024 ** 3

_SIZE_MULTIPLIERS = {
    '': 1, 'k': 10 ** 3, 'm': 10 ** 6, 'g': 10 ** 9, 't': 10 ** 12,
    'ki': 1024, 'mi': 1024 ** 2, 'gi': 1024 ** 3, 'ti': 1024 ** 4,
}

_SIZE_RE = re.compile(r'^(\d+(?:\.\d+)?)\s*([a-z]*?)b?$')


def parse_size_bytes(value, default, name='value'):
    """A k8s-style size ("2Gi", "500M", "1073741824") → bytes. Empty/None →
    `default`; "0" → 0, which callers read as "guard disabled". Anything
    unparseable falls back to `default` and says so on stderr: a typo in a
    chart value must not take the dashboard down, nor silently unbound it."""
    raw = str(value if value is not None else '').strip()
    if not raw:
        return default
    match = _SIZE_RE.match(raw.lower())
    multiplier = _SIZE_MULTIPLIERS.get(match.group(2)) if match else None
    if multiplier is None:
        print(f'[uploads] ignoring unparseable size {name}={raw!r}; '
              f'using {default} bytes', file=sys.stderr)
        return default
    return int(float(match.group(1)) * multiplier)


def human_bytes(n):
    """Bytes → a short string for an error a user has to act on. Binary units,
    because that is what the chart values and `df -h` speak."""
    n = float(n)
    for unit in ('B', 'KiB', 'MiB'):
        if abs(n) < 1024:
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} GiB'


def is_managed_upload_dir(home, path):
    """True when `path` sits inside a directory the quota governs. Matched on
    the path's shape rather than a listing, so a destination that does not
    exist yet (the first attachment of a new task) is still recognised."""
    rel = os.path.relpath(path, home)
    if rel == '.' or rel.startswith('..'):
        return False
    parts = rel.split(os.sep)
    if parts[0] == 'uploads':
        return True
    return len(parts) >= 3 and parts[0] == '.claude-tasks' and parts[2] == 'attachments'


def managed_upload_dirs(home):
    """The managed directories that exist right now. For a task, only its
    `attachments` — the rest of `.claude-tasks/<id>` is transcript and state,
    which is not upload storage and must not be charged as if it were."""
    dirs = []
    top = os.path.join(home, 'uploads')
    if os.path.isdir(top):
        dirs.append(top)
    try:
        with os.scandir(os.path.join(home, '.claude-tasks')) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    attachments = os.path.join(entry.path, 'attachments')
                    if os.path.isdir(attachments):
                        dirs.append(attachments)
    except OSError:
        pass  # no tasks dir yet, or unreadable: nothing to charge
    return sorted(dirs)


def _tree_size_bytes(path, skip):
    """Bytes in a tree, minus entries named in `skip`. Symlinks are never
    followed and count as the link itself: an `uploads/big` link to `/` must
    neither walk the volume nor bill the user for the target."""
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            if name in skip:
                continue
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass  # raced with a delete: it is 0 bytes to us now
    return total


def _remove_entry(path):
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.unlink(path)
    except OSError:
        pass  # already gone, which is the outcome we wanted


class UploadCapacityError(Exception):
    """A storage bound refused this write. `payload` is the JSON error body,
    ready to answer with: the HTTP layer should send it as a 507."""

    def __init__(self, payload):
        super().__init__(payload['error'])
        self.payload = payload


def disk_full_payload():
    """The refusal for a real ENOSPC/EDQUOT: the guards above passed (or are
    disabled) and the volume filled anyway, typically from outside uploads."""
    return {
        'error': ('The workspace volume is full, so this upload could not be '
                  'saved. Free up space on /home/dev and try again.'),
        'code': 'insufficient_disk_space',
    }


def is_disk_full(exc):
    return isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT)


_lock = threading.Lock()
_live = set()
_swept_homes = set()


def _live_staging_names():
    return {name for r in _live for name in r.staging_names}


def _sweep_orphans_locked(home):
    """Remove staging entries a previous process left behind. Only called
    before this process has admitted anything into `home`: see the module
    docstring for why nothing on disk can be live at that point."""
    if home in _swept_homes:
        return
    _swept_homes.add(home)
    for top in managed_upload_dirs(home):
        for root, dirs, files in os.walk(top, followlinks=False):
            for name in [n for n in files + dirs if STAGING_NAME.match(n)]:
                _remove_entry(os.path.join(root, name))
            dirs[:] = [d for d in dirs if not STAGING_NAME.match(d)]


def _free_bytes(home):
    try:
        st = os.statvfs(home)
    except OSError:
        return None
    return st.f_bavail * st.f_frsize


class Reservation:
    """One in-flight upload's claim on storage. Always `release()` it."""

    def __init__(self, home, dest_dir, quota, min_free):
        self.home = home
        self.quota = max(0, int(quota or 0))
        self.min_free = max(0, int(min_free or 0))
        self.managed = self.quota > 0 and is_managed_upload_dir(home, dest_dir)
        self.quota_bytes = 0
        self.disk_bytes = 0
        self.staging_paths = []
        self.staging_names = set()

    def _check_fits_locked(self, quota_bytes, disk_bytes, what):
        """Raise UploadCapacityError if this claim does not fit, naming
        whichever bound is tighter: that is the one the user has to act on."""
        others = [r for r in _live if r is not self]
        refusals = []
        if self.managed:
            used = sum(_tree_size_bytes(d, _live_staging_names())
                       for d in managed_upload_dirs(self.home))
            used += sum(r.quota_bytes for r in others if r.managed)
            room = self.quota - used
            if quota_bytes > room:
                refusals.append((room, {
                    'error': (
                        f'Upload storage is full: attachments and session '
                        f'uploads use {human_bytes(used)} of the '
                        f'{human_bytes(self.quota)} limit and {what} '
                        f'{human_bytes(quota_bytes)}. Delete what you no longer '
                        f'need under ~/uploads or ~/.claude-tasks/*/attachments '
                        f'from a terminal, or raise files.uploadQuota in the '
                        f'chart values.'),
                    'code': 'upload_quota_exceeded',
                    'used_bytes': used,
                    'quota_bytes': self.quota,
                    'needed_bytes': quota_bytes,
                }))
        free = _free_bytes(self.home) if self.min_free else None
        if free is not None:
            free -= sum(r.disk_bytes for r in others)
            room = free - self.min_free
            if disk_bytes > room:
                refusals.append((room, {
                    'error': (
                        f'Not enough free disk space: /home/dev has '
                        f'{human_bytes(max(0, free))} free, {what} '
                        f'{human_bytes(disk_bytes)}, and '
                        f'{human_bytes(self.min_free)} is kept in reserve so '
                        f'builds, git and the memory database keep working. '
                        f'Free up space on the volume, or lower '
                        f'files.minFreeSpace in the chart values.'),
                    'code': 'insufficient_disk_space',
                    'free_bytes': max(0, free),
                    'min_free_bytes': self.min_free,
                    'needed_bytes': disk_bytes,
                }))
        if refusals:
            raise UploadCapacityError(min(refusals, key=lambda r: r[0])[1])

    def staging_path(self, dest_dir):
        """A fresh, dot-hidden path in `dest_dir` (same filesystem, so the
        final rename is atomic) that the size walk will not double-charge."""
        name = f'.kc-upload-{uuid.uuid4().hex}'
        path = os.path.join(dest_dir, name)
        with _lock:
            self.staging_names.add(name)
            self.staging_paths.append(path)
        return path

    def charge_expansion(self, expanded_bytes):
        """Re-base this claim on what a zip will really occupy. Content-Length
        says nothing about that, so a 10 MiB archive would otherwise expand
        straight through a cap it was admitted under.

        The claim becomes the expansion alone on both bounds: the archive is
        deleted once unpacked so the quota should not charge it, and it is
        already written, so `statvfs` has already taken it off the free space.
        """
        with _lock:
            self._check_fits_locked(expanded_bytes, expanded_bytes,
                                    'this archive expands to')
            self.quota_bytes = expanded_bytes if self.managed else 0
            self.disk_bytes = expanded_bytes

    def commit(self, promote):
        """Run `promote()` (the rename into place) and drop the charge inside
        one critical section, so the bytes are never counted twice."""
        with _lock:
            promote()
            self.quota_bytes = 0
            self.disk_bytes = 0

    def release(self):
        """Delete whatever staging is left and forget this claim. Idempotent.
        The entry stays live while its staging is removed, so a concurrent
        walk can't charge bytes that are on their way out."""
        for path in self.staging_paths:
            _remove_entry(path)
        with _lock:
            _live.discard(self)


def reserve(home, dest_dir, size, quota, min_free):
    """Admit `size` bytes bound for `dest_dir`, or raise UploadCapacityError.
    The returned Reservation holds that room until `release()`."""
    reservation = Reservation(home, dest_dir, quota, min_free)
    with _lock:
        if not _live:
            _sweep_orphans_locked(home)
        reservation._check_fits_locked(size, size, 'this upload needs')
        reservation.quota_bytes = size if reservation.managed else 0
        reservation.disk_bytes = size
        _live.add(reservation)
    return reservation


def promote_tree(staging, dest):
    """Move an extracted tree into `dest`, merging with what is there — an
    upload into an existing directory has always overwritten same-named files
    and left the rest alone."""
    for root, _dirs, files in os.walk(staging):
        rel = os.path.relpath(root, staging)
        target = dest if rel == '.' else os.path.join(dest, rel)
        os.makedirs(target, exist_ok=True)
        for name in files:
            os.replace(os.path.join(root, name), os.path.join(target, name))


def usage(home, quota):
    """Managed upload storage against its cap, for the dashboard and scrape.
    `quota` of 0 means unbounded: `percent` is then 0 and `available_bytes`
    None, which is how a client knows there is no wall to warn about."""
    with _lock:
        skip = _live_staging_names()
    used = sum(_tree_size_bytes(d, skip) for d in managed_upload_dirs(home))
    quota = max(0, int(quota or 0))
    return {
        'used_bytes': used,
        'quota_bytes': quota,
        'available_bytes': max(0, quota - used) if quota else None,
        'percent': round(used / quota * 100, 1) if quota else 0,
    }
