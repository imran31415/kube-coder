"""Dispositions and staged actions — the 14-of-20 case (#588 Phase 5).

Phase 4 makes a run work twenty items without working any of them twice. It
does not make the *outcome* useful, because most items do not end in "done":
they end in "I need a human to look at this before it goes to the customer".

So two things live here.

**A disposition** is what the agent says happened. `completed` is a checkable
claim — the vendor API returned success — and every other disposition carries a
`reason` AND `evidence`. "Needs rescoping" with no reason looks like progress
and isn't.

**A staged action** is a write the agent WANTS to make, held until a human
approves it. Three properties matter, and each has a distinct failure it
prevents:

1. **Staleness.** Between staging and approving, someone may have replied to
   the ticket. Writing over a colleague's reply is the most damaging thing this
   feature could do, so approval re-fetches the item and compares
   `engine.content_hash`. A mismatch is a 409 with both versions, never a
   blind write. The precedent is `DevcontainerManager.apply` — compare-and-swap
   on the thing the user was actually shown.

2. **Replay.** A phone on a flaky connection retries. `approval_id` is minted
   by the client, stored on the record when the decision is consumed, and the
   stored RESULT is what a replay gets back — the `hook_fired_at` idiom, decide
   and mark inside one lock. There is no idempotency-key convention anywhere
   in the mobile app today, so this had to be designed rather than inherited.

3. **Echo.** The approver must send back the `content_hash` they were shown. A
   UI holding a stale card cannot approve the newer thing that replaced it.

Purity: everything in this module is a pure function over plain dicts, except
`StagedBook`, which is handed a `boards.store.JsonRecord`. No HTTP, no
filesystem paths, no server import.
"""

import re
import time

# What the agent reports. `completed` is the only one that asserts the board
# changed, and it is the only one a vendor response can verify — which is why
# it is checkable and the rest are not.
DISPOSITIONS = ('completed', 'needs_review', 'needs_rescoping', 'blocked',
                'rejected', 'failed')

# Dispositions whose staged writes are held for a human. `completed` in propose
# mode still stages: the agent claiming success does not make the write happen.
NEEDS_HUMAN = ('needs_review', 'needs_rescoping', 'blocked')

# A staged record's own lifecycle.
STAGED_STATES = ('pending', 'approved', 'rejected', 'sent_back', 'partial')
OPEN_STATES = ('pending', 'partial')

# One ACTION's lifecycle, which is not the record's. `discarded` is what a
# reject/send-back does to the proposals it refuses; `superseded` is what a
# later run does to proposals an earlier run left pending on the same item —
# kept visible rather than deleted, because a reviewer should be able to see
# that an earlier proposal existed and was replaced.
ACTION_STATES = ('pending', 'done', 'failed', 'discarded', 'superseded')

_ACTION_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
# A client-minted uuid or similar. Bounded and character-restricted because it
# is stored and compared, never interpolated anywhere.
_APPROVAL_ID_RE = re.compile(r'^[A-Za-z0-9_.:-]{8,128}$')

MAX_STAGED_ACTIONS = 12
MAX_REASON = 4000
MAX_PREVIEW = 8000


class ReviewError(Exception):
    """A rejected review operation. `code` is what the route maps to a status:
    `stale` and `hash_mismatch` are 409, everything else 400."""

    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def valid_disposition(value):
    return value in DISPOSITIONS


def valid_approval_id(value):
    return bool(value) and bool(_APPROVAL_ID_RE.match(str(value)))


def new_record(board_id, item, *, content_hash, run_id='', task_id='',
               now=None):
    """One item's review record. Item text is COPIED in at staging time
    deliberately: the reviewer must see what the agent saw, and re-fetching for
    the card would show them a ticket that may already have moved on."""
    now = time.time() if now is None else now
    return {
        'board_id': board_id,
        'item_id': str(item.get('id', '')),
        'item_key': str(item.get('key') or ''),
        'item_title': (item.get('title') or '')[:300],
        'item_url': item.get('url') or '',
        'content_hash': content_hash,
        'run_id': run_id,
        'task_id': task_id,
        'state': 'pending',
        'disposition': None,
        'reason': '',
        'evidence': {},
        'actions': [],
        'approval_id': '',
        'approved_at': 0,
        'decided_by': '',
        'result': None,
        'created_at': now,
        'updated_at': now,
    }


def stage_action(record, *, action_id, action, params, preview='', writes=1,
                 now=None):
    """Append one proposed write. Mutates `record`; raises ReviewError when the
    record is no longer open."""
    if record.get('state') not in OPEN_STATES:
        raise ReviewError(
            'already_decided',
            f'this item was already {record.get("state")}; nothing more can be '
            f'staged against it')
    if not _ACTION_ID_RE.match(action_id or ''):
        raise ReviewError('bad_action_id',
                          'action_id must be 1-64 chars of [a-z0-9_-]')
    if any(a['id'] == action_id for a in record['actions']):
        raise ReviewError('duplicate_action_id',
                          f'action_id {action_id!r} is already staged')
    if len(record['actions']) >= MAX_STAGED_ACTIONS:
        raise ReviewError('too_many_actions',
                          f'at most {MAX_STAGED_ACTIONS} actions may be staged '
                          f'against one item')
    record['actions'].append({
        'id': action_id,
        'action': action,
        'params': dict(params or {}),
        # What a human will read. The engine renders the real request at
        # approval time from `action` + `params`; this is never executed, so a
        # mismatch is a display bug and cannot become a different write.
        'preview': (preview or '')[:MAX_PREVIEW],
        'writes': int(writes),
        'state': 'pending',
        'result': None,
        'staged_at': time.time() if now is None else now,
    })
    record['updated_at'] = time.time() if now is None else now
    return record['actions'][-1]


def set_disposition(record, disposition, *, reason='', evidence=None,
                    now=None):
    """Record what the agent concluded.

    A non-`completed` disposition REQUIRES a reason. That is enforced here
    rather than trusted to the preamble, because a disposition with no reason
    is indistinguishable from progress in every list it appears in.
    """
    if not valid_disposition(disposition):
        raise ReviewError('bad_disposition',
                          f'disposition must be one of {", ".join(DISPOSITIONS)}')
    reason = (reason or '').strip()
    if disposition != 'completed' and not reason:
        raise ReviewError(
            'reason_required',
            f'{disposition!r} requires a reason — what was tried, what was '
            f'found, and the specific question that needs answering')
    record['disposition'] = disposition
    record['reason'] = reason[:MAX_REASON]
    record['evidence'] = dict(evidence or {})
    record['updated_at'] = time.time() if now is None else now
    return record


def check_approvable(record, *, echoed_hash, fresh_hash, approval_id):
    """The three guards, in the order a failure is most likely.

    Returns `('proceed', None)` or `('replay', stored_result)`. Raises
    ReviewError otherwise. Callers MUST run this inside the same lock that
    consumes the record — checking and then writing in two steps is exactly the
    race the `hook_fired_at` idiom exists to close.
    """
    if not valid_approval_id(approval_id):
        raise ReviewError('bad_approval_id',
                          'approval_id must be a client-generated id of 8-128 '
                          'chars of [A-Za-z0-9_.:-]')

    # Replay first: a retry of an already-consumed approval is a SUCCESS, not a
    # conflict, and must be answered before any staleness check — the ticket
    # has almost certainly changed as a result of the write we already made.
    if record.get('approval_id') and record['approval_id'] == approval_id:
        return 'replay', record.get('result')

    if record.get('state') not in OPEN_STATES:
        raise ReviewError(
            'already_decided',
            f'this item was already {record.get("state")}'
            + (f' by another approval' if record.get('approval_id') else ''))

    if echoed_hash != record.get('content_hash'):
        # The approver's UI is showing an older card than the one on disk.
        raise ReviewError(
            'hash_mismatch',
            'this item changed since the card you are looking at was drawn — '
            'reload the review queue and read it again')

    if fresh_hash != record.get('content_hash'):
        # The TICKET moved on the vendor. This is the guard that matters.
        raise ReviewError(
            'stale',
            'the ticket changed on the board after this action was staged — '
            'someone may have replied. Nothing was written; re-read the item '
            'and re-stage if it still applies')
    return 'proceed', None


def consume(record, approval_id, *, state, result=None, decided_by='',
            now=None):
    """Mark the record decided. One caller, inside one lock, once."""
    now = time.time() if now is None else now
    record['state'] = state
    record['approval_id'] = approval_id
    record['approved_at'] = now
    record['decided_by'] = decided_by
    record['result'] = result
    record['updated_at'] = now
    return record


def public_view(record):
    """What the review UI sees. Includes everything — a staged record holds no
    secret by construction, because a connector holds no secret and params are
    the agent's own words."""
    if not isinstance(record, dict):
        return {}
    out = dict(record)
    out['open'] = record.get('state') in OPEN_STATES
    out['pending_actions'] = [a for a in record.get('actions') or []
                              if a.get('state') == 'pending']
    return out


def group_by_disposition(records):
    """The review queue's shape. `needs_review` first, deliberately — Mission
    Control puts `waiting` first for the same reason: the column that needs a
    human is the one you opened the page for."""
    order = ['needs_review', 'needs_rescoping', 'blocked', 'failed',
             'completed', 'rejected', None]
    groups = {}
    for record in records:
        key = record.get('disposition')
        groups.setdefault(key if key in order else None, []).append(record)
    out = []
    for key in order:
        items = groups.get(key)
        if not items:
            continue
        items.sort(key=lambda r: r.get('created_at', 0))
        out.append({'disposition': key or 'unreported', 'items': items,
                    'count': len(items)})
    return out


class StagedBook:
    """Every review record for ONE board, one JSON per item.

    Per item rather than one file per board: approving item 46 and rejecting
    item 47 are independent decisions that a review queue makes concurrently,
    and a single board-wide file would serialise them behind one lock for no
    reason.
    """

    def __init__(self, record_for):
        # `record_for(item_id) -> boards.store.JsonRecord`. Injected so this
        # class never decides where anything lives.
        self._record_for = record_for

    def get(self, item_id):
        rec = self._record_for(str(item_id))
        return rec.read() if rec.exists() else None

    def put(self, record):
        self._record_for(record['item_id']).write(record)
        return record

    def update(self, item_id, mutate_fn):
        """Read-modify-write under the record's lock. `(record, wrote)`."""
        return self._record_for(str(item_id)).update(mutate_fn)

    def delete(self, item_id):
        return self._record_for(str(item_id)).delete()
