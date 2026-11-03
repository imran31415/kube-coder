"""What happened to one board item, in one line (#709 Part 3).

The phone's Board screen has three tabs — Needs you, Working, Done — and the
Done tab is a list of results: *"Reply posted on #412"*, *"Failed: the build
ended as error"*. Two surfaces need that same sentence: the Feed item posted
when an item finishes (which carries the unread flag the Done row's dot reads)
and the read endpoint the phone builds Working and Done from. Computing it
twice would let them disagree, so it is computed here, once.

Which TAB a row is on is the phone's call (`mobile/src/util/boardTabs.ts`), and
this module only needs the one half of it the server acts on: "is this item
done?". The two must agree, so the rule is spelled the same way in both:

1. A review that is still open needs you, whatever the run says.
2. A row still claimed or working is working.
3. A decided review, a failed row, or a row done with a conclusion that does
   not need a human is done.

Purity: plain functions over the dicts `boards.runs` and `boards.review` already
produce. No filesystem, no server import.
"""

from .review import NEEDS_HUMAN, OPEN_STATES

#: A staged record a human has ruled on. `partial` is deliberately absent: an
#: approval whose writes half-failed is left OPEN so the reviewer can see it.
DECIDED_STATES = ('approved', 'rejected', 'sent_back')

#: Run-item states the phone shows under Working.
WORKING_STATES = ('claimed', 'working')

#: The longest result line. It is one line on a phone, and a vendor error body
#: pasted in whole would be a paragraph.
MAX_LINE = 140

#: Run-item states the read endpoint returns. `pending` (queued, not yet
#: claimed) and `skipped` (the run stopped first) are on no tab.
SHOWN_STATES = WORKING_STATES + ('done', 'failed')


def feed_key(board_id, item_id):
    """The Feed `dedupe_key` for one item — the SAME key the "needs your
    review" row uses, so the finished row replaces the waiting one in place
    and the waiting badge drops when the item is decided."""
    return f'board:{board_id}:{item_id}'


def is_done(row, record):
    """True when the item belongs on the Done tab. `row` is the item's newest
    run row, `record` its staged review record; either may be None."""
    row = row or {}
    record = record or {}
    if record.get('state') in OPEN_STATES:
        return False
    if row.get('state') in WORKING_STATES:
        return False
    if record.get('state') in DECIDED_STATES:
        return True
    if row.get('state') == 'failed':
        return True
    return row.get('state') == 'done' and \
        row.get('disposition') not in NEEDS_HUMAN


def _clip(text):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= MAX_LINE else text[:MAX_LINE - 1] + '…'


def _is_reply(action_name):
    name = (action_name or '').lower()
    return 'comment' in name or 'reply' in name


def _written(record):
    """Names of the staged actions that actually landed."""
    return [a.get('action') or '' for a in record.get('actions') or []
            if a.get('state') == 'done']


def result_line(row, record, *, key=''):
    """One sentence saying how the item ended. `key` is the ticket's display
    key (`#412`, `SUP-9`); it is woven in where the sentence names the ticket.
    """
    row = row or {}
    record = record or {}
    key = key or record.get('item_key') or row.get('key') or \
        record.get('item_id') or row.get('id') or 'the item'
    state = record.get('state')
    if state == 'approved':
        written = _written(record)
        if written and all(_is_reply(a) for a in written):
            return _clip(f'Reply posted on {key}')
        if written:
            names = ', '.join(a.replace('_', ' ') for a in written)
            return _clip(f'Written to {key}: {names}')
        # Nothing staged: either nothing needed writing, or an autonomous
        # run wrote it directly. Both are "completed"; neither is a reply.
        return _clip(f'Completed on {key}')
    if state == 'rejected':
        detail = ((record.get('result') or {}).get('detail') or '').strip()
        if detail and detail != 'rejected':
            return _clip(f'Rejected: {detail}')
        return _clip('Rejected, nothing was written')
    if state == 'sent_back':
        return _clip('Sent back to the agent')
    if row.get('state') == 'failed':
        return _clip(f'Failed: {row.get("error") or "no reason recorded"}')
    disposition = row.get('disposition')
    reason = row.get('reason') or record.get('reason') or ''
    if disposition == 'failed':
        return _clip(f'Failed: {reason or "the agent reported a failure"}')
    if disposition == 'rejected':
        return _clip(f'Not actioned: {reason}' if reason else 'Not actioned')
    return _clip(f'Completed on {key}')


def recent_rows(runs, records, feed=None, *, limit=200):
    """The rows Working and Done are built from.

    `runs` are FULL run records (items included), newest first. `records` maps
    item id to that item's staged record. `feed` maps a `feed_key` to
    `{'id', 'read', 'waiting'}` for the Feed items that exist.

    One row per item: the newest run that touched it wins, so an item sent back
    and being worked again shows as working, not as its earlier result.
    """
    feed = feed or {}
    out = []
    seen = set()
    for run in runs:
        board_id = run.get('board_id') or ''
        items = (run.get('items') or {}).values()
        for row in sorted(items, key=lambda r: r.get('updated_at') or 0,
                          reverse=True):
            item_id = str(row.get('id') or '')
            if not item_id or item_id in seen:
                continue
            if row.get('state') not in SHOWN_STATES:
                continue
            seen.add(item_id)
            record = records.get(item_id) or {}
            key = row.get('key') or record.get('item_key') or item_id
            done = is_done(row, record)
            entry = feed.get(feed_key(board_id, item_id)) or {}
            # A row's dot is the Feed's read flag, and only for a finished
            # row. While the Feed item is still the WAITING one, the unread
            # it carries belongs to the Needs-you badge, not to this row.
            has_dot = done and bool(entry) and not entry.get('waiting')
            out.append({
                'board_id': board_id,
                'item_id': item_id,
                'item_key': key,
                'item_title': row.get('title') or record.get('item_title') or '',
                'item_url': row.get('url') or record.get('item_url') or '',
                'run_id': run.get('id') or '',
                'run_origin': run.get('origin') or 'manual',
                'task_id': row.get('task_id') or '',
                'state': row.get('state'),
                'disposition': row.get('disposition'),
                'review_state': record.get('state') or None,
                'reason': row.get('reason') or record.get('reason') or '',
                'error': row.get('error') or '',
                'result': result_line(row, record, key=key) if done else '',
                'updated_at': max(row.get('updated_at') or 0,
                                  record.get('updated_at') or 0),
                'feed_id': entry.get('id') if has_dot else None,
                'unread': has_dot and not entry.get('read'),
            })
            if len(out) >= limit:
                return out
    return out
