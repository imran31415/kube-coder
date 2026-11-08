"""One vocabulary for Build (task) statuses.

Every status string, every grouping of them and the table of legal moves live
here, so the dashboard server, the sub-agent orchestrator (a separate process),
the worktree sweep and the Hypervisor's watchers cannot disagree. The strings
are wire format: the dashboard, the mobile app and hook receivers match on
them. The web and mobile helpers (`web/src/util/taskStatus.ts`,
`mobile/src/util/taskStatus.ts`) mirror these groups, and
`tests/task_status_contract_test.py` fails if they drift.

Standard library only, and no sibling imports: anything in this directory can
import it, however that process was launched.
"""

import json
import os

RUNNING = 'running'
WAITING = 'waiting-for-input'
COMPLETED = 'completed'
ERROR = 'error'
KILLED = 'killed'
TERMINATED = 'terminated'

#: Every status a Build can hold. Ordered: this is the metrics label order.
ALL_STATUSES = (RUNNING, WAITING, COMPLETED, ERROR, KILLED, TERMINATED)

#: The Build's session still exists and it may yet do more work.
LIVE_STATUSES = frozenset((RUNNING, WAITING))

#: The Build is over and its status can never change again.
TERMINAL_STATUSES = frozenset((COMPLETED, ERROR, KILLED, TERMINATED))

#: Terminal, without a result to trust. `terminated` belongs here: a Build the
#: pod restart wiped never reached the end of its work.
FAILED_STATUSES = frozenset((ERROR, KILLED, TERMINATED))

#: The Build has stopped working and nothing more happens without someone
#: acting on it. Wider than TERMINAL_STATUSES on purpose: an interactive Build
#: keeps its REPL alive after finishing, so parking on `waiting-for-input` IS
#: its done signal, and whoever is waiting on it would otherwise wait forever.
QUIESCENT_STATUSES = TERMINAL_STATUSES | {WAITING}

#: status -> statuses it may become. A terminal status has no entry, and
#: neither does a same-status "move", so neither can ever transition.
TRANSITIONS = {
    RUNNING: TERMINAL_STATUSES | {WAITING},
    WAITING: TERMINAL_STATUSES | {RUNNING},
}

#: status -> the state a Mission Control card shows.
MISSION_STATE = {
    RUNNING: 'running',
    WAITING: 'waiting',
    **{status: 'done' for status in TERMINAL_STATUSES},
}


def can_transition(current, to):
    """Whether a Build in status `current` may move to status `to`."""
    return to in TRANSITIONS.get(current, ())


def atomic_update_meta(task_dir, mutate_fn):
    """Atomically read-modify-write task.json under an exclusive flock.

    The one implementation of this lock: every process that writes an existing
    task.json takes it here, so concurrent writers cannot lose each other's
    updates or bring a finished Build back to life.

    mutate_fn(meta) may modify meta in place. Returning False skips the write
    (used when the mutator decides the update is no longer needed after seeing
    fresh state). Returns the post-mutation meta dict, or None if the task
    directory is gone.
    """
    # Imported here so that importing this module needs no POSIX-only feature;
    # worktrees.py deliberately tolerates a platform without fcntl.
    import fcntl

    meta_path = os.path.join(task_dir, 'task.json')
    if not os.path.isfile(meta_path):
        return None
    lock_path = os.path.join(task_dir, '.meta.lock')
    with open(lock_path, 'a') as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            with open(meta_path, 'r') as f:
                meta = json.load(f)
            should_write = mutate_fn(meta)
            if should_write is False:
                return meta
            tmp_path = meta_path + '.tmp'
            with open(tmp_path, 'w') as f:
                json.dump(meta, f, indent=2)
            os.rename(tmp_path, meta_path)
            return meta
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)
