"""What a board is DOING right now, in one vocabulary (#712).

Three surfaces have to answer "what is the overall state of this board?" — the
dashboard's standing strip, the mobile Board screen, and the run form deciding
whether a new run may start. Before this module the dashboard derived its own
sentence client-side and mobile said nothing at all, which is how the Board
screen on iOS came to look empty: with nothing staged for review there was
literally nothing on it.

So the vocabulary lives here, server-side, computed from LOCAL state only —
run records and staged records on the PVC. No vendor call, so any surface may
ask as often as it polls. (The dashboard also folds in the item count from its
own listing; that costs an outbound fetch and stays client-side.)

Purity: one pure function over plain dicts. `charts/workspace/web/src/store/
boards.ts` mirrors the same five states for the surfaces it already computes
locally — if a state is added here, add it there.
"""

from .runs import is_live

# The five states a board can be in, most urgent first. A board can be in more
# than one at once (a run in flight WITH items already awaiting a decision);
# the first match wins and `detail` carries the rest, because a state badge
# that says two things says nothing.
STATES = ('needs_credential', 'running', 'awaiting_human', 'never_run', 'idle')

LABELS = {
    'needs_credential': 'Needs a credential',
    'running': 'Runs in progress',
    'awaiting_human': 'Waiting on you',
    'never_run': 'Not run yet',
    'idle': 'Idle',
}


def _plural(count, one, many):
    return one if count == 1 else many


def standing(*, board=None, runs=(), awaiting=0, awaiting_breakdown=None):
    """The board's overall state.

    `board` is a connector's public view (only `credential_set` is read),
    `runs` the run SUMMARIES for that board (newest first is not required),
    `awaiting` the number of staged records still open, and
    `awaiting_breakdown` an optional `{disposition: count}` for the detail
    line.
    """
    board = board or {}
    summaries = [r for r in runs if isinstance(r, dict)]
    # `is_live`, not `status == 'running'`: a run whose items have all settled
    # keeps that status until something finalises it, and one whose process
    # died keeps it until the boot sweep. Neither is in flight, and calling
    # either one "running" would lock the board out of its next run.
    live = next((r for r in summaries if is_live(r)), None)
    counts = (live or {}).get('counts') or {}
    working = int(counts.get('working') or 0) + int(counts.get('claimed') or 0)
    queued = int(counts.get('pending') or 0)
    run_total = int((live or {}).get('total') or 0)
    settled = (int((live or {}).get('done') or 0)
               + int((live or {}).get('failed') or 0)
               + int((live or {}).get('skipped') or 0)) if live else 0
    awaiting = max(0, int(awaiting or 0))

    if board.get('credential_set') is False:
        state = 'needs_credential'
    elif live:
        state = 'running'
    elif awaiting > 0:
        state = 'awaiting_human'
    elif not summaries:
        state = 'never_run'
    else:
        state = 'idle'

    # The detail line is the state's supporting evidence, not a second state:
    # it is what a tooltip shows and what the phone prints under the badge.
    if state == 'needs_credential':
        detail = ('No credential is stored for this board, so nothing can '
                  'authenticate against it.')
    elif state == 'running':
        parts = [f'{settled}/{run_total} worked']
        if working:
            parts.append(f'{working} working')
        if queued:
            parts.append(f'{queued} queued')
        if awaiting:
            parts.append(f'{awaiting} awaiting you')
        detail = ' · '.join(parts)
    elif state == 'awaiting_human':
        detail = (f'{awaiting} {_plural(awaiting, "item is", "items are")} '
                  f'waiting on your decision.')
        if awaiting_breakdown:
            named = ' · '.join(
                f'{n} {d.replace("_", " ")}'
                for d, n in sorted(awaiting_breakdown.items(),
                                   key=lambda kv: (-kv[1], kv[0]))
                if n)
            if named:
                detail = f'{detail} {named}'
    elif state == 'never_run':
        detail = 'Nothing has been worked here yet.'
    else:
        detail = 'Everything worked so far has been decided.'

    # Starting a second run while one is live is not refused for tidiness: item
    # leases are per BOARD, so the second run can only mark as `skipped`
    # whatever the first already holds. What looks like a parallel run is a run
    # that does nothing, reported as twenty skips (#712).
    if state == 'needs_credential':
        blocked = ('This board has no credential, so a run could not '
                   'authenticate.')
    elif live:
        blocked = (f'A run is already in flight on this board '
                   f'({live.get("id") or "run"}). Stop it, or wait for it to '
                   f'finish — a second run would only skip the items this one '
                   f'holds.')
    else:
        blocked = ''

    return {
        'state': state,
        'label': LABELS[state],
        'detail': detail,
        'live': bool(live),
        'run_id': (live or {}).get('id') or '',
        'mode': (live or {}).get('mode') or '',
        'working': working,
        'queued': queued,
        'settled': settled,
        'run_total': run_total,
        'awaiting': awaiting,
        'runs': len(summaries),
        'can_start_run': not blocked,
        'blocked_reason': blocked,
    }
