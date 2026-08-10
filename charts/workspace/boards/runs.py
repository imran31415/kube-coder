"""Board runs — work N items concurrently, and never work one twice.

Phase 4 of #588. Everything here exists to make one sentence true:

    Run the same board twice and the second pass does nothing.

That is harder than it sounds, because "twice" has three different meanings and
each needs its own guard:

1. **Twice inside one run.** Two workers pick the same item off the list.
   Guarded by the LEASE: claiming is a compare-and-set against a per-board
   record, so exactly one claimant wins (`boards.store.JsonRecord.update`).
2. **Twice across overlapping runs.** Leases are per BOARD, not per run,
   precisely so a second run started while the first is live cannot claim what
   the first already holds.
3. **Twice across separate runs, hours apart.** Guarded by the PROCESSED LOG,
   keyed on `item.id + engine.content_hash(item)`. An item nobody touched keeps
   its hash and stays ineligible; an item somebody EDITED gets a new hash and
   becomes eligible again — which is the behaviour you actually want, and is
   why the key is a content hash rather than a bare id.

A fourth guard already exists from Phase 1 and is not re-implemented here: the
vendor-side idempotency marker (`engine.action_hash` + the probe), which catches
the case where a write landed but our record of it did not.

**Leases do not expire on a timer.** A TTL is a guess about how long work takes,
and guessing wrong either releases an item that is still being worked or strands
one forever. Instead `reclaim_orphans` sweeps at STARTUP, following
`hypervisor_session.reconcile_stale_running_threads`: at boot no worker of any
previous process can still be alive, so a lease held by a run that is not live
is *definitively* orphaned. Its run is marked `interrupted` rather than left
sitting at `running`, because a run that stalls silently is issue #462 all over
again.

Purity: the state-machine helpers at the top are pure functions over plain
dicts. `LeaseBook` and `ProcessedLog` are the impure half — they are handed a
`boards.store.JsonRecord` rather than a path, so a test can inject one and the
module never decides where anything lives.
"""

import re
import time

# The ONE content-hash implementation, re-exported so callers of this module
# never grow a second one. A processed marker written against a different hash
# than the write path computes would silently stop skipping anything.
from .engine import content_hash  # noqa: F401

# A run's own lifecycle. `interrupted` is distinct from `stopped` on purpose:
# stopped means a human ended it, interrupted means the process died under it.
# Collapsing them would hide crashes behind an ordinary-looking status.
RUN_STATUSES = ('running', 'done', 'stopped', 'interrupted')
LIVE_RUN_STATUSES = ('running',)

# Per-item progress WITHIN a run.
ITEM_STATES = ('pending', 'claimed', 'working', 'done', 'failed', 'skipped')
TERMINAL_ITEM_STATES = ('done', 'failed', 'skipped')

# `propose` stages every write for review; `autonomous` writes directly. The
# default is propose — an agent that can silently write to a customer's ticket
# on its first outing is not a feature anyone asked for.
MODES = ('propose', 'autonomous')
DEFAULT_MODE = 'propose'

# What caused a run to exist. `send_back` is the re-scoping round trip (#588
# Phase 6): a reviewer returned an item with a question, and the item is
# re-dispatched as a one-item run so it keeps a lease — and therefore keeps its
# mode enforcement. Working a sent-back item OUTSIDE a run would make
# `staging_run_for` return None and let the agent write straight to the board,
# at exactly the moment a human said "not like that".
ORIGINS = ('manual', 'send_back')

# Ceiling on a single run regardless of pod capacity. Board work is dominated by
# outbound vendor calls, and a vendor's rate limit is account-wide: past a
# handful of workers the extra parallelism buys nothing and spends the budget
# faster.
BOARD_MAX_CONCURRENCY = 8

# Anchored and character-restricted because a run id becomes a FILENAME. No
# dots, no slashes, no second dash run — so `..` and `../x` cannot be spelled.
_RUN_ID_RE = re.compile(r'^run-[0-9]{1,20}-[a-f0-9]{4,32}$')
#: Selection orderings. Public (re-exported as ORDERS) because the strategy UI
#: has to offer exactly these — a dropdown listing an order the validator would
#: reject is a form that fails on submit for no discoverable reason.
_ORDERS = ('updated_at asc', 'updated_at desc',
           'created_at asc', 'created_at desc',
           'priority', 'key')
ORDERS = _ORDERS

_PRIORITY_RANK = {'URGENT': 0, 'HIGH': 1, 'NORMAL': 2, 'LOW': 3}

#: Ceiling on `select.limit`, and the value a caller passes to mean "do not
#: truncate" — the strategy preview counts over the whole matched set so its
#: "already processed" figure is not itself truncated by the limit.
MAX_SELECT_LIMIT = 500


# ── identity ───────────────────────────────────────────────────────────────

def make_run_id(now, token):
    """`run-<epoch>-<token>`. The timestamp is first so a plain lexical sort of
    run ids is chronological, matching ClaudeTaskManager's task ids."""
    return f'run-{int(now)}-{token}'


def valid_run_id(run_id):
    return bool(run_id) and bool(_RUN_ID_RE.match(run_id))


def marker_key(item_id, content_hash):
    """The processed-log key: identity AND content.

    Keying on the id alone would mean an item edited after we worked it can
    never be picked up again. Keying on the hash alone would collide across
    items whose titles and bodies happen to match — which on a support board
    ("Refund not received") is not hypothetical.
    """
    return f'{item_id}@{content_hash}'


def item_source(board_id, run_id, item_id):
    """The `source` stamped on the Build created for an item.

    A run polls its workers by matching this string rather than arming a
    watcher per item: `WatcherManager` is thread-scoped and capped at 8 per
    thread, so a 20-item run would blow it.
    """
    return f'board:{board_id}:run:{run_id}:item:{item_id}'


def parse_item_source(source):
    """`(board_id, run_id, item_id)` or None. Item ids may contain colons
    (GraphQL global ids do), so only the first three fields are split off."""
    parts = (source or '').split(':', 5)
    if len(parts) < 6 or parts[0] != 'board' or parts[2] != 'run' or parts[4] != 'item':
        return None
    return parts[1], parts[3], parts[5]


# ── concurrency ────────────────────────────────────────────────────────────

def clamp_concurrency(requested, *, live_tasks, max_tasks,
                      board_max=BOARD_MAX_CONCURRENCY):
    """`(effective, reason)` — how many workers this run may really have.

    `ClaudeTaskManager.create_task` returns `{'status': 'rejected'}` WITHOUT
    creating anything once `at_capacity()`, so a run that ignores the pod's
    ceiling does not fail loudly — it quietly produces a row of rejections that
    look like items nobody worked. Clamping up front and SAYING SO is the
    difference between "we ran 4 of your 20 in parallel" and a silent hole in
    the results.

    `reason` is empty when nothing was clamped, and is persisted on the run so
    the UI can show it rather than the user inferring it from a count.
    """
    try:
        requested = int(requested)
    except (TypeError, ValueError):
        requested = 1
    requested = max(1, requested)

    headroom = max(0, int(max_tasks) - int(live_tasks))
    effective = min(requested, board_max, headroom)

    if effective >= requested:
        return requested, ''

    limits = []
    if headroom < requested:
        # KC_MAX_TASKS counts kube-coder-* tmux sessions; KC_MAX_SUBAGENTS
        # counts claude-* and is a different budget, so board workers press
        # against this one only.
        limits.append(f'KC_MAX_TASKS={max_tasks} with {live_tasks} already live')
    if board_max < requested:
        limits.append(f'board ceiling {board_max}')
    if effective == 0:
        return 0, (f'cannot start: the workspace is at its task limit '
                   f'({live_tasks}/{max_tasks}) — wait for a task to finish '
                   f'or raise KC_MAX_TASKS')
    return effective, (f'clamped to {effective} from {requested}: '
                       + ', '.join(limits))


# ── the run record ─────────────────────────────────────────────────────────

def new_run(run_id, board_id, *, mode=DEFAULT_MODE, select=None, concurrency=1,
            requested_concurrency=None, clamp_reason='', stop_on=None,
            origin='manual', now=None):
    now = time.time() if now is None else now
    return {
        'id': run_id,
        'board_id': board_id,
        'mode': mode if mode in MODES else DEFAULT_MODE,
        # Who asked for this run. `send_back` is a one-item run created when a
        # reviewer returns an item with a question (#588 Phase 6) — it needs to
        # be distinguishable in the Runs list, because a stream of one-item runs
        # is otherwise indistinguishable from someone clicking Start repeatedly.
        'origin': origin if origin in ORIGINS else 'manual',
        'select': dict(select or {}),
        'concurrency': int(concurrency),
        'requested_concurrency': int(
            requested_concurrency if requested_concurrency is not None
            else concurrency),
        'clamp_reason': clamp_reason,
        'stop_on': dict(stop_on or {}),
        'status': 'running',
        'stop_requested': False,
        'created_at': now,
        'updated_at': now,
        'finished_at': None,
        'error': '',
        'listing_complete': True,
        'truncation_reason': '',
        'consecutive_failures': 0,
        'items': {},
    }


def new_run_item(item, *, content_hash, resume=None, now=None):
    """One row of a run's `items` map. `content_hash` is stamped at SELECTION
    time, not at write time — it is what the processed log will key on and what
    Phase 5's staleness guard compares against.

    `resume` is set only by the send-back round trip and carries what the
    dispatcher needs to continue earlier work: the reviewer's note, the prior
    task and Claude session, and the agent's own earlier reason.
    """
    now = time.time() if now is None else now
    return {
        'id': str(item.get('id', '')),
        'key': str(item.get('key', '') or ''),
        'title': (item.get('title') or '')[:200],
        'url': item.get('url') or '',
        'content_hash': content_hash,
        'state': 'pending',
        'lease_owner': '',
        'lease_at': 0,
        'task_id': '',
        'disposition': None,
        'reason': '',
        'writes_used': 0,
        'error': '',
        'resume': dict(resume) if resume else None,
        'created_at': now,
        'updated_at': now,
    }


def counts(run):
    """Item states tallied — what the progress bar reads."""
    out = {state: 0 for state in ITEM_STATES}
    for row in (run.get('items') or {}).values():
        state = row.get('state')
        if state in out:
            out[state] += 1
    return out


def is_finished(run):
    """Every item terminal. Note this is about ITEMS: a run whose items are all
    done is finished even if nothing has updated `status` yet."""
    rows = (run.get('items') or {}).values()
    return bool(rows) and all(r.get('state') in TERMINAL_ITEM_STATES
                              for r in rows)


def summary(run):
    """The list-view shape — everything except the per-item detail, which for a
    200-item run is far more than a list needs."""
    if not isinstance(run, dict):
        return {}
    c = counts(run)
    return {
        'id': run.get('id'),
        'board_id': run.get('board_id'),
        'mode': run.get('mode'),
        'origin': run.get('origin', 'manual'),
        'status': run.get('status'),
        'concurrency': run.get('concurrency'),
        'requested_concurrency': run.get('requested_concurrency'),
        'clamp_reason': run.get('clamp_reason', ''),
        'created_at': run.get('created_at'),
        'updated_at': run.get('updated_at'),
        'finished_at': run.get('finished_at'),
        'error': run.get('error', ''),
        'listing_complete': run.get('listing_complete', True),
        'truncation_reason': run.get('truncation_reason', ''),
        'total': len(run.get('items') or {}),
        'counts': c,
        'done': c['done'], 'failed': c['failed'], 'skipped': c['skipped'],
    }


def should_stop(run):
    """`(stop, reason)` from the run's own `stop_on` policy.

    Only `consecutive_failures` for now, and consecutive rather than total on
    purpose: three failures scattered through a 200-item board are noise, three
    in a row mean the credential expired or the vendor is down, and continuing
    just burns the rate-limit budget proving it.
    """
    if run.get('stop_requested'):
        return True, 'stopped by request'
    policy = run.get('stop_on') or {}
    threshold = policy.get('consecutive_failures')
    if isinstance(threshold, int) and threshold > 0:
        if run.get('consecutive_failures', 0) >= threshold:
            return True, (f'{run["consecutive_failures"]} consecutive failures '
                          f'(stop_on.consecutive_failures={threshold})')
    return False, ''


# ── selection ──────────────────────────────────────────────────────────────

def validate_select(select):
    """`(cleaned, errors)`. An allowlist, like the connector schema: a typo'd
    filter key must be an error, never a filter that silently matches
    everything and works the whole board."""
    errors = []
    if select is None:
        select = {}
    if not isinstance(select, dict):
        return None, ['select must be an object']

    known = ('status', 'priority', 'tags', 'unassigned', 'updated_since',
             'query', 'limit', 'order', 'item_ids', 'ignore_processed')
    for field in select:
        if field not in known:
            errors.append(f'select: unknown field {field!r} '
                          f'(allowed: {", ".join(sorted(known))})')

    cleaned = {}
    for field in ('status', 'priority', 'tags', 'item_ids'):
        val = select.get(field)
        if val is None:
            continue
        if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
            errors.append(f'select.{field} must be a list of strings')
        else:
            cleaned[field] = list(val)

    if select.get('unassigned') is not None:
        if not isinstance(select['unassigned'], bool):
            errors.append('select.unassigned must be a boolean')
        else:
            cleaned['unassigned'] = select['unassigned']

    # Re-work an item the processed log has already seen. The ONE legitimate
    # caller is the send-back round trip (#588 Phase 6): the reviewer looked at
    # the result and asked for it again, which is exactly the case the marker
    # is meant to suppress. Deliberately not exposed in the run form — a board
    # run that ignores the processed log is a board run that re-comments on
    # every ticket it already answered.
    if select.get('ignore_processed') is not None:
        if not isinstance(select['ignore_processed'], bool):
            errors.append('select.ignore_processed must be a boolean')
        else:
            cleaned['ignore_processed'] = select['ignore_processed']

    for field in ('updated_since',):
        val = select.get(field)
        if val is None:
            continue
        if not isinstance(val, str):
            errors.append(f'select.{field} must be an ISO-8601 string')
        else:
            cleaned[field] = val

    if select.get('query') is not None:
        if not isinstance(select['query'], str):
            errors.append('select.query must be a string')
        else:
            cleaned['query'] = select['query']

    limit = select.get('limit', 20)
    if not isinstance(limit, int) or not (1 <= limit <= MAX_SELECT_LIMIT):
        errors.append(f'select.limit must be an integer between 1 and '
                      f'{MAX_SELECT_LIMIT}')
    else:
        cleaned['limit'] = limit

    order = select.get('order', 'updated_at asc')
    if order not in _ORDERS:
        errors.append(f'select.order must be one of {_ORDERS}')
    else:
        cleaned['order'] = order

    return (None, errors) if errors else (cleaned, [])


def _matches(item, select):
    want_status = select.get('status')
    if want_status:
        status = (item.get('status') or {})
        if status.get('normalized') not in want_status and \
                status.get('raw') not in want_status:
            return False

    want_priority = select.get('priority')
    if want_priority:
        priority = (item.get('priority') or {})
        if priority.get('normalized') not in want_priority and \
                priority.get('raw') not in want_priority:
            return False

    want_tags = select.get('tags')
    if want_tags:
        tags = {str(t).lower() for t in (item.get('tags') or [])}
        if not tags & {t.lower() for t in want_tags}:
            return False

    if select.get('unassigned'):
        assignee = item.get('assignee') or {}
        if any(assignee.get(k) for k in ('id', 'name', 'email')):
            return False

    since = select.get('updated_since')
    if since:
        # String compare on ISO-8601 is correct and needs no parser, but only
        # for values that ARE ISO-8601; anything else is passed rather than
        # silently dropped, because dropping items is the failure that matters.
        updated = item.get('updated_at') or ''
        if updated and updated < since:
            return False

    query = (select.get('query') or '').strip().lower()
    if query:
        haystack = ' '.join(str(item.get(f) or '')
                            for f in ('key', 'title', 'body')).lower()
        if query not in haystack:
            return False

    ids = select.get('item_ids')
    if ids and str(item.get('id')) not in {str(i) for i in ids}:
        return False
    return True


def _sort_key(order):
    if order == 'priority':
        return lambda i: (_PRIORITY_RANK.get(
            (i.get('priority') or {}).get('normalized'), 99),
            i.get('updated_at') or '')
    if order == 'key':
        return lambda i: str(i.get('key') or '')
    field = order.split()[0]
    return lambda i: (i.get(field) or '')


def select_items(items, select, *, is_processed=None):
    """`(chosen, skipped)` — which items this run will work, and which were
    filtered out as already-processed.

    `is_processed(item)` is injected rather than a `ProcessedLog` being passed
    in, so this stays a pure function and the "an edited item becomes eligible
    again" rule is testable without touching a disk.
    """
    order = select.get('order', 'updated_at asc')
    matched = [i for i in items if _matches(i, select)]
    matched.sort(key=_sort_key(order), reverse=order.endswith(' desc'))

    chosen, skipped = [], []
    limit = select.get('limit', 20)
    honour_markers = not select.get('ignore_processed')
    for item in matched:
        if honour_markers and is_processed is not None and is_processed(item):
            skipped.append(item)
            continue
        if len(chosen) >= limit:
            break
        chosen.append(item)
    return chosen, skipped


# ── leases (impure: one JsonRecord per board) ──────────────────────────────

class LeaseBook:
    """Who currently owns each item on ONE board.

    Per board rather than per run, which is the whole point: two overlapping
    runs share this record, so they cannot both claim item 46. A claim is a
    single `JsonRecord.update` whose mutator returns False when the item is
    already held — decision and write inside one lock, no read-then-write race.
    """

    def __init__(self, record):
        self._record = record

    def all(self):
        return self._record.read()

    def owner(self, item_id):
        entry = self._record.read().get(str(item_id))
        return entry if isinstance(entry, dict) else None

    def claim(self, item_id, run_id, worker='', now=None):
        """True if THIS caller now owns the item. False means someone else got
        there first — not an error, just a lost race, which is exactly what a
        worker loop wants to hear."""
        now = time.time() if now is None else now
        item_id = str(item_id)

        def mutate(data):
            held = data.get(item_id)
            if isinstance(held, dict) and held.get('run_id'):
                if held.get('run_id') == run_id and held.get('worker') == worker:
                    return False        # already ours; nothing to write
                return False
            data[item_id] = {'run_id': run_id, 'worker': worker, 'at': now}

        _data, wrote = self._record.update(mutate)
        return wrote

    def release(self, item_id, run_id):
        """Release only what this run holds. A run must never be able to free
        another run's lease — that would re-open the double-claim it exists to
        prevent."""
        item_id = str(item_id)

        def mutate(data):
            held = data.get(item_id)
            if not isinstance(held, dict) or held.get('run_id') != run_id:
                return False
            del data[item_id]

        _data, wrote = self._record.update(mutate)
        return wrote

    def release_run(self, run_id):
        """Drop every lease held by one run. Returns the item ids freed."""
        freed = []

        def mutate(data):
            for item_id in list(data):
                held = data.get(item_id)
                if isinstance(held, dict) and held.get('run_id') == run_id:
                    del data[item_id]
                    freed.append(item_id)
            return bool(freed)

        self._record.update(mutate)
        return freed

    def reclaim_orphans(self, live_run_ids):
        """The BOOT SWEEP. Returns `{item_id: run_id}` for every lease whose
        owning run is not live.

        Deliberately not a TTL. A timeout is a guess about how long work takes,
        and either guess is wrong somewhere: too short frees an item that is
        still being worked (the double-write this module exists to prevent),
        too long strands it. At startup there is no guessing — nothing from a
        previous process can still be running.
        """
        orphans = {}
        live = set(live_run_ids or ())

        def mutate(data):
            for item_id in list(data):
                held = data.get(item_id)
                run_id = held.get('run_id') if isinstance(held, dict) else None
                if run_id not in live:
                    orphans[item_id] = run_id
                    del data[item_id]
            return bool(orphans)

        self._record.update(mutate)
        return orphans


# ── processed markers (impure: one JsonRecord per board) ───────────────────

class ProcessedLog:
    """What this board has already had worked, durably.

    The key is `item.id @ engine.content_hash(item)`. That single choice is
    what makes both halves of the requirement true at once: re-running a board
    skips everything untouched, and an item somebody edited comes back into
    scope because its hash changed.

    Entries are pruned by count rather than age. Age is the wrong axis — an
    item nobody has touched in a year must still be skipped — while unbounded
    growth on a busy board is a real problem.
    """

    MAX_ENTRIES = 5000

    def __init__(self, record, max_entries=None):
        self._record = record
        self._max = max_entries or self.MAX_ENTRIES

    def all(self):
        return self._record.read()

    def seen(self, item_id, content_hash):
        """The recorded outcome, or None. Presence is what makes an item
        ineligible; the value is for explaining WHY in the UI."""
        entry = self._record.read().get(marker_key(item_id, content_hash))
        return entry if isinstance(entry, dict) else None

    def record(self, item_id, content_hash, *, run_id='', disposition='',
               now=None):
        now = time.time() if now is None else now
        key = marker_key(item_id, content_hash)

        def mutate(data):
            data[key] = {'item_id': str(item_id), 'run_id': run_id,
                         'disposition': disposition, 'at': now}
            if len(data) > self._max:
                # Oldest first. `at` is always present on entries we wrote; a
                # malformed one sorts first and is the first to go, which is
                # the right instinct for data we cannot interpret.
                doomed = sorted(data, key=lambda k: (data[k] or {}).get('at', 0))
                for k in doomed[:len(data) - self._max]:
                    del data[k]

        self._record.update(mutate)
        return key

    def forget(self, item_id, content_hash):
        """Make one item eligible again without editing it on the vendor — the
        'work this again' escape hatch."""
        key = marker_key(item_id, content_hash)

        def mutate(data):
            if key not in data:
                return False
            del data[key]

        _data, wrote = self._record.update(mutate)
        return wrote

    def forget_item(self, item_id):
        """Every recorded hash for one item, whatever its content is now."""
        item_id = str(item_id)
        removed = []

        def mutate(data):
            for key in list(data):
                entry = data.get(key)
                if isinstance(entry, dict) and entry.get('item_id') == item_id:
                    del data[key]
                    removed.append(key)
            return bool(removed)

        self._record.update(mutate)
        return removed


